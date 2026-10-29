"""Deterministic numeric-tolerance checks for authored CAD criteria.

Each ``criterion_tolerances`` entry ties one criterion to a measured quantity
from the battery rows; its pass/fail is shown to the judge and overrides that
criterion's verdict.
"""

from __future__ import annotations

import fnmatch
import json
import math
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from .probes import PROVENANCE_MESH, PROVENANCE_OCCT

Measure = Literal[
    "volume", "surface_area", "bbox_x", "bbox_y", "bbox_z", "bbox_diagonal"
]
MEASURE_UNITS: dict[str, str] = {
    "volume": "mm^3",
    "surface_area": "mm^2",
    "bbox_x": "mm",
    "bbox_y": "mm",
    "bbox_z": "mm",
    "bbox_diagonal": "mm",
}
BBOX_MEASURES = frozenset({"bbox_x", "bbox_y", "bbox_z", "bbox_diagonal"})
TOLERANCE_PROBE_ID = "tolerance_check"


class CriterionTolerance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion_id: str = Field(min_length=1)
    measure: Measure
    expected: float = Field(allow_inf_nan=False)
    tolerance: float = Field(ge=0, allow_inf_nan=False)
    tolerance_kind: Literal["abs", "pct"] = "abs"
    # Snapshot path or glob of the agent file measured; optional with one file.
    file: str | None = None


_TOLERANCES = TypeAdapter(list[CriterionTolerance])


def parse_tolerances(raw: object, criterion_ids: set[str]) -> list[CriterionTolerance]:
    """Validated entries; raises ValueError naming the first bad one."""
    if raw is None or raw == []:
        return []
    entries = _TOLERANCES.validate_python(raw)
    unknown = sorted({e.criterion_id for e in entries} - criterion_ids)
    if unknown:
        raise ValueError(
            f"criterion_tolerances names criterion ids {unknown} that are not in "
            "criterion_definitions"
        )
    if len({e.criterion_id for e in entries}) != len(entries):
        raise ValueError("criterion_tolerances has more than one entry per criterion")
    return entries


@dataclass(frozen=True)
class ToleranceCheck:
    entry: CriterionTolerance
    verdict: Literal["pass", "fail"] | None
    row: dict[str, Any]


def _payload(row: dict[str, Any]) -> dict[str, Any] | None:
    try:
        payload = json.loads(str(row.get("outcome")))
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def _brep_measures(payload: dict[str, Any]) -> dict[str, float]:
    # The battery box is loose on curved faces, so spans come from exact_bbox.
    parts = [p for p in payload.get("parts") or [] if isinstance(p, dict)]
    summary = payload.get("parts_summary")
    if isinstance(summary, dict):
        volume, area = summary.get("volume_total"), summary.get("surface_area_total")
    else:
        volume = sum(p["volume"] for p in parts) if parts else None
        area = sum(p["surface_area"] for p in parts) if parts else None
    return _measures(volume, area, None, None)


def _box_measures(payload: dict[str, Any]) -> dict[str, float]:
    return _measures(None, None, payload.get("bbox_min"), payload.get("bbox_max"))


def _mesh_measures(payload: dict[str, Any]) -> dict[str, float]:
    return _measures(
        payload.get("volume"),
        payload.get("surface_area"),
        payload.get("bbox_min"),
        payload.get("bbox_max"),
    )


def _measures(volume: Any, area: Any, lo: Any, hi: Any) -> dict[str, float]:
    out: dict[str, float] = {}
    if isinstance(volume, (int, float)):
        out["volume"] = float(volume)
    if isinstance(area, (int, float)):
        out["surface_area"] = float(area)
    if isinstance(lo, list) and isinstance(hi, list) and len(lo) == len(hi) == 3:
        spans = [float(hi[i]) - float(lo[i]) for i in range(3)]
        out.update(bbox_x=spans[0], bbox_y=spans[1], bbox_z=spans[2])
        out["bbox_diagonal"] = math.hypot(*spans)
    return out


def _measured(
    probe_rows: list[dict[str, Any]], path: str, measure: str
) -> tuple[float, str] | None:
    """``measure`` on the file from exact B-rep rows first, else its mesh battery."""
    for probe_id, reader, provenance in (
        ("exact_bbox", _box_measures, PROVENANCE_OCCT),
        ("brep_battery", _brep_measures, PROVENANCE_OCCT),
        ("mesh_battery", _mesh_measures, PROVENANCE_MESH),
    ):
        for row in probe_rows:
            if row.get("probe_id") != probe_id or row.get("target") != path:
                continue
            if not row.get("ok") or (payload := _payload(row)) is None:
                continue
            value = reader(payload).get(measure)
            if value is not None:
                return value, provenance
    return None


def _resolve_file(
    entry: CriterionTolerance, agent_paths: list[str]
) -> tuple[str | None, str]:
    if entry.file is None:
        if len(agent_paths) == 1:
            return agent_paths[0], ""
        return None, (
            f"{len(agent_paths)} agent CAD files are staged and the entry names no file"
        )
    matches = [p for p in agent_paths if fnmatch.fnmatchcase(p, entry.file)]
    if len(matches) == 1:
        return matches[0], ""
    return None, f"file {entry.file!r} matches {len(matches)} staged agent files"


def run_checks(
    entries: list[CriterionTolerance],
    agent_paths: list[str],
    probe_rows: list[dict[str, Any]],
    penalty_ids: frozenset[str] = frozenset(),
) -> list[ToleranceCheck]:
    """One check per entry; ``verdict`` is None when nothing was measured.

    A penalty criterion states a mistake, so in tolerance decides ``fail``.
    """
    checks: list[ToleranceCheck] = []
    for entry in entries:
        unit = MEASURE_UNITS[entry.measure]
        params = entry.model_dump(exclude_none=True)
        path, problem = _resolve_file(entry, agent_paths)
        measured = _measured(probe_rows, path, entry.measure) if path else None
        if path is None or measured is None:
            reason = problem or f"no battery row measured {entry.measure} on {path}"
            row = {
                "probe_id": TOLERANCE_PROBE_ID,
                "target": path or (entry.file or ""),
                "params": params,
                "outcome": f"not measured: {reason}",
                "provenance": f"failed: {reason}",
                "ok": False,
            }
            checks.append(ToleranceCheck(entry=entry, verdict=None, row=row))
            continue
        value, provenance = measured
        allowed = (
            entry.tolerance
            if entry.tolerance_kind == "abs"
            else abs(entry.expected) * entry.tolerance / 100.0
        )
        deviation = value - entry.expected
        within = abs(deviation) <= allowed
        verdict: Literal["pass", "fail"] = (
            "pass" if within != (entry.criterion_id in penalty_ids) else "fail"
        )
        outcome = {
            "criterion_id": entry.criterion_id,
            "measure": entry.measure,
            "measured": round(value, 6),
            "expected": entry.expected,
            "allowed_deviation": round(allowed, 6),
            "deviation": round(deviation, 6),
            "unit": unit,
            "within_tolerance": within,
            "verdict": verdict,
            "note": (
                f"Deterministic tolerance check: this verdict decides criterion "
                f"{entry.criterion_id}."
            ),
        }
        row = {
            "probe_id": TOLERANCE_PROBE_ID,
            "target": path,
            "params": params,
            "outcome": json.dumps(outcome, ensure_ascii=False),
            "provenance": provenance,
            "ok": True,
        }
        checks.append(ToleranceCheck(entry=entry, verdict=verdict, row=row))
    return checks

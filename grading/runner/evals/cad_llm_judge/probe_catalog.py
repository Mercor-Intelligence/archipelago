"""The CAD judge's fixed probe catalog and the code that runs an accepted probe.

Kept apart from ``planner`` so the geometry worker child can execute planned
probes without importing the LLM stack the planner needs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

from .probes import (
    GeometryStore,
    ProbeResult,
    ProbeRunner,
    brep_battery,
    distance_probe,
    failed,
    golden_compare_probe,
    interference_probe,
    mesh_battery,
    overhang_probe,
    symmetry_probe,
    wall_thickness_probe,
)
from .render import (
    _EXTRA_ISO_VIEWS,  # pyright: ignore[reportPrivateUsage]
    _VIEWS,  # pyright: ignore[reportPrivateUsage]
    PROVENANCE_RENDER,
    RenderBudget,
    RenderedView,
    render_parts,
    render_section,
)

_PLANES = ("xy", "yz", "zx")
_VIEW_NAMES = tuple(_VIEWS) + tuple(_EXTRA_ISO_VIEWS)  # pyright: ignore[reportPrivateUsage]

ParamKind = Literal["file", "reference", "part", "plane", "view", "number"]


@dataclass(frozen=True)
class ParamSpec:
    name: str
    kind: ParamKind
    required: bool
    description: str


@dataclass(frozen=True)
class ProbeSpec:
    probe_id: str
    description: str
    params: tuple[ParamSpec, ...]
    kind: Literal["geometry", "render"] = "geometry"


_FILE = ParamSpec("file", "file", True, "a staged CAD file path from the index")
_PLANE = ParamSpec("plane", "plane", True, f"one of {', '.join(_PLANES)}")

PROBE_CATALOG: dict[str, ProbeSpec] = {
    spec.probe_id: spec
    for spec in (
        ProbeSpec(
            "brep_battery",
            "re-run the full B-rep battery (volume, area, center of mass, bbox, "
            "validity, cylindrical-face census) on one file",
            (_FILE,),
        ),
        ProbeSpec(
            "mesh_battery",
            "mesh measurements (watertight, winding, non-manifold edges, volume, "
            "area, bbox) for one file's tessellation",
            (_FILE,),
        ),
        ProbeSpec(
            "distance",
            "minimum distance between two named parts",
            (
                ParamSpec("part_a", "part", True, "a part id from the index"),
                ParamSpec("part_b", "part", True, "a part id from the index"),
            ),
        ),
        ProbeSpec(
            "interference",
            "boolean-common interference volume and minimum clearance between two named parts",
            (
                ParamSpec("part_a", "part", True, "a part id from the index"),
                ParamSpec("part_b", "part", True, "a part id from the index"),
            ),
        ),
        ProbeSpec(
            "wall_thickness",
            "wall-thickness estimate by inward ray casting over sampled surface points",
            (_FILE,),
        ),
        ProbeSpec(
            "symmetry",
            "mirror the model across an axis-aligned plane through its centroid "
            "and measure the deviation",
            (_FILE, _PLANE),
        ),
        ProbeSpec(
            "overhang_census",
            "DFM overhang census for a +Z build direction",
            (
                _FILE,
                ParamSpec(
                    "max_angle_deg",
                    "number",
                    False,
                    "overhang threshold in degrees from vertical (default 45)",
                ),
            ),
        ),
        ProbeSpec(
            "golden_compare",
            "ICP-align the file against a staged ground-truth reference and "
            "report Hausdorff distance plus volume/area/center-of-mass deltas",
            (
                _FILE,
                ParamSpec(
                    "reference",
                    "reference",
                    False,
                    "a staged ground-truth reference path (defaults to the "
                    "first reference)",
                ),
            ),
        ),
        ProbeSpec(
            "section_render",
            "render a 2D cross-section of the file cut by an axis-aligned plane",
            (
                _FILE,
                _PLANE,
                ParamSpec(
                    "offset",
                    "number",
                    False,
                    "where to cut, as a 0..1 fraction across the bbox (default 0.5)",
                ),
            ),
            kind="render",
        ),
        ProbeSpec(
            "view_render",
            "render the file from a named view",
            (
                _FILE,
                ParamSpec("view", "view", True, f"one of {', '.join(_VIEW_NAMES)}"),
            ),
            kind="render",
        ),
    )
}


@dataclass(frozen=True)
class ValidatedProbe:
    probe_id: str
    raw_params: dict[str, Any]
    resolved: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def as_row(self) -> dict[str, Any]:
        row: dict[str, Any] = {"probe_id": self.probe_id, "params": self.raw_params}
        if self.error:
            row["rejected"] = self.error
        return row


def _target_label(store: GeometryStore, path: str) -> str:
    """Reference files must never read as agent work in a probe row."""
    if path in store.reference_paths:
        return f"{path} [ground-truth reference, not the agent's work]"
    return path


def execute_plan(
    validated: list[ValidatedProbe],
    store: GeometryStore,
    runner: ProbeRunner,
    render_budget: RenderBudget,
) -> tuple[list[ProbeResult], list[RenderedView]]:
    """Run every accepted probe; rejected ones become visible failures unrun.

    Sync and CPU-bound — the caller runs it off the event loop. Duplicate
    invocations reuse the first result (cached per id+target+params).
    """
    results: list[ProbeResult] = []
    views: list[RenderedView] = []
    cache: dict[str, ProbeResult] = {}
    for probe in validated:
        if probe.error:
            results.append(
                failed(
                    probe.probe_id, str(probe.raw_params), probe.error, probe.raw_params
                )
            )
            continue
        key = f"{probe.probe_id}|{json.dumps(probe.raw_params, sort_keys=True, default=str)}"
        if key in cache:
            results.append(cache[key])
            continue
        result = _execute_one(probe, store, runner, render_budget, views)
        cache[key] = result
        results.append(result)
    return results, views


def _execute_one(
    probe: ValidatedProbe,
    store: GeometryStore,
    runner: ProbeRunner,
    render_budget: RenderBudget,
    views: list[RenderedView],
) -> ProbeResult:
    spec = PROBE_CATALOG[probe.probe_id]
    r = probe.resolved

    if spec.kind == "render":
        path = r["file"]
        if not render_budget.take():
            return failed(
                probe.probe_id, path, "render image budget exhausted", probe.raw_params
            )
        mesh = store.mesh_for(path)
        if mesh is None:
            return failed(
                probe.probe_id,
                path,
                "no mesh could be built for this file",
                probe.raw_params,
            )
        try:
            shown = _target_label(store, path)
            if probe.probe_id == "section_render":
                png = render_section(mesh, r["plane"], float(r.get("offset", 0.5)))
                label = f"{shown} — section {r['plane']} @ {float(r.get('offset', 0.5)):.2f}"
            else:
                png = render_parts([(mesh, (176, 196, 222))], r["view"])
                label = f"{shown} — {r['view']} (planner-requested)"
        except Exception as e:
            return failed(
                probe.probe_id, path, f"{type(e).__name__}: {e}", probe.raw_params
            )
        if png is None:
            return failed(
                probe.probe_id,
                path,
                "the cut plane does not intersect the model",
                probe.raw_params,
            )
        views.append(RenderedView(label=label, png_bytes=png))
        return ProbeResult(
            probe_id=probe.probe_id,
            target=_target_label(store, path),
            params=probe.raw_params,
            outcome=f"rendered: {label} (see the attached image)",
            provenance=PROVENANCE_RENDER,
            ok=True,
        )

    return _execute_geometry(probe, store, runner)


def _execute_geometry(
    probe: ValidatedProbe, store: GeometryStore, runner: ProbeRunner
) -> ProbeResult:
    r = probe.resolved
    pid = probe.probe_id

    def _need_mesh(path: str) -> Any | None:
        return store.mesh_for(path)

    if pid == "brep_battery":
        path = r["file"]
        label = _target_label(store, path)
        entry = store.loaded(path)
        if not entry.solids:
            return failed(
                pid, label, entry.error or "no B-rep solids loaded", probe.raw_params
            )
        return runner.run(
            pid, label, lambda: brep_battery(label, entry.solids), probe.raw_params
        )
    if pid in {"mesh_battery", "wall_thickness"}:
        path = r["file"]
        label = _target_label(store, path)
        mesh = _need_mesh(path)
        if mesh is None:
            return failed(
                pid, label, "no mesh could be built for this file", probe.raw_params
            )
        fn = (
            (lambda: mesh_battery(label, mesh))
            if pid == "mesh_battery"
            else (lambda: wall_thickness_probe(label, mesh))
        )
        return runner.run(pid, label, fn, probe.raw_params)
    if pid == "symmetry":
        path = r["file"]
        label = _target_label(store, path)
        mesh = _need_mesh(path)
        if mesh is None:
            return failed(
                pid, label, "no mesh could be built for this file", probe.raw_params
            )
        return runner.run(
            pid,
            label,
            lambda: symmetry_probe(label, mesh, r["plane"]),
            probe.raw_params,
        )
    if pid == "overhang_census":
        path = r["file"]
        label = _target_label(store, path)
        mesh = _need_mesh(path)
        if mesh is None:
            return failed(
                pid, label, "no mesh could be built for this file", probe.raw_params
            )
        angle = float(r.get("max_angle_deg", 45.0))
        return runner.run(
            pid,
            label,
            lambda: overhang_probe(label, mesh, angle),
            probe.raw_params,
        )
    if pid == "golden_compare":
        path = r["file"]
        reference = r.get("reference") or next(
            iter(sorted(store.reference_paths)), None
        )
        if reference is None:
            return failed(
                pid, path, "no reference artifact is staged", probe.raw_params
            )
        mesh, reference_mesh = _need_mesh(path), _need_mesh(reference)
        if mesh is None or reference_mesh is None:
            return failed(
                pid, path, "candidate or reference has no mesh", probe.raw_params
            )
        return runner.run(
            pid,
            path,
            lambda: golden_compare_probe(path, mesh, reference, reference_mesh),
            probe.raw_params,
        )
    if pid in {"distance", "interference"}:
        resolved_a = store.resolve_part(str(r["part_a"]))
        resolved_b = store.resolve_part(str(r["part_b"]))
        if resolved_a is None or resolved_b is None:
            missing = r["part_a"] if resolved_a is None else r["part_b"]
            return failed(
                pid,
                f"{r['part_a']} vs {r['part_b']}",
                f"part {missing!r} no longer resolves in this worker",
                probe.raw_params,
            )
        path_a, name_a, shape_a = resolved_a
        path_b, name_b, shape_b = resolved_b
        id_a, id_b = f"{path_a}::{name_a}", f"{path_b}::{name_b}"
        fn = (
            (lambda: distance_probe(id_a, shape_a, id_b, shape_b))
            if pid == "distance"
            else (lambda: interference_probe(id_a, shape_a, id_b, shape_b))
        )
        return runner.run(pid, f"{id_a} vs {id_b}", fn, probe.raw_params)
    return failed(pid, str(probe.raw_params), "probe has no executor", probe.raw_params)

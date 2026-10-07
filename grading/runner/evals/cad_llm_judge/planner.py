"""Probe planner for the CAD LLM judge: plan → probe → judge, still non-agentic.

One LLM call (before the judge call) reads the criteria plus a compact index of
the staged evidence and returns probe invocations from the FIXED catalog below.
Nothing the model says is executed as code: every request is validated against
the catalog's parameter specs and the actually-staged file/part names, and an
invalid or unresolvable request becomes a visible ``failed: <reason>`` ledger
entry the judge sees — never silently dropped, never executed.
"""

from __future__ import annotations

import json
from dataclasses import replace
from typing import Any, Literal

from litellm import Choices
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from runner.utils.grading_log import logger
from runner.utils.llm import build_messages, call_llm

from .probe_catalog import (
    _PLANES,  # pyright: ignore[reportPrivateUsage]
    _VIEW_NAMES,  # pyright: ignore[reportPrivateUsage]
    PROBE_CATALOG,
    ParamSpec,
    ValidatedProbe,
)
from .probes import GeometryStore
from .prompts import UnverifiableCategory

LOG_PREFIX = "CAD_LLM_JUDGE"
PLANNER_TIMEOUT = 900
MAX_PLANNER_RETRIES = 3
DEFAULT_PROBE_BUDGET = 24
# Bounds the plan no matter what a world config asks for.
PROBE_BUDGET_CEILING = 64


# test_grading_cad_planner pins this Literal and the param fields below to the
# catalog, so a catalog change that forgets the wire model fails fast.
ProbeId = Literal[
    "brep_battery",
    "mesh_battery",
    "distance",
    "interference",
    "wall_thickness",
    "symmetry",
    "overhang_census",
    "golden_compare",
    "section_render",
    "view_render",
]

_PARAM_FIELD_NAMES = (
    "file",
    "part_a",
    "part_b",
    "plane",
    "view",
    "offset",
    "max_angle_deg",
    "reference",
)


class PlannedProbe(BaseModel):
    """One planner request: enum probe_id, each catalog param a flat optional
    field — an open params dict is unrepresentable under strict
    structured-output decoding."""

    model_config = ConfigDict(extra="forbid")

    probe_id: ProbeId
    file: str | None = None
    part_a: str | None = None
    part_b: str | None = None
    plane: str | None = None
    view: str | None = None
    offset: float | None = None
    max_angle_deg: float | None = None
    reference: str | None = None

    @property
    def params(self) -> dict[str, str | float]:
        return {
            name: value
            for name in _PARAM_FIELD_NAMES
            if (value := getattr(self, name)) is not None
        }


CoverageSource = Literal["battery", "digest", "renders"]


class CriterionCoverage(BaseModel):
    """The planner's answer to "how will the judge decide this criterion?" —
    flat closed fields only, so it survives strict structured-output decoding
    like PlannedProbe."""

    model_config = ConfigDict(extra="forbid")

    criterion_key: str
    probe_ids: list[ProbeId] = Field(default_factory=list)
    covered_by: CoverageSource | None = None
    expected_unverifiable: UnverifiableCategory | None = None


class CadProbePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    probes: list[PlannedProbe] = Field(default_factory=list)
    coverage: list[CriterionCoverage] = Field(default_factory=list)
    rationale: str = ""


def catalog_text() -> str:
    lines: list[str] = []
    for spec in PROBE_CATALOG.values():
        params = ", ".join(
            f"{p.name}{'' if p.required else '?'} ({p.kind}: {p.description})"
            for p in spec.params
        )
        lines.append(f"- {spec.probe_id}: {spec.description}. Params: {params}.")
    return "\n".join(lines)


def _validate_param(
    spec: ParamSpec, value: Any, store: GeometryStore
) -> tuple[Any, str | None]:
    if spec.kind == "file":
        resolved = store.resolve(str(value))
        if resolved is None:
            return None, f"no staged file matches {value!r}"
        return resolved, None
    if spec.kind == "reference":
        # An agent file here would let the deliverable be compared with itself
        # and read as a golden match.
        resolved = store.resolve(str(value))
        if resolved is None or resolved not in store.reference_paths:
            return None, f"{value!r} is not a staged ground-truth reference"
        return resolved, None
    if spec.kind == "part":
        # An id string, not a live shape: validation may run in the parent off
        # the worker-reported inventory, and the executing child re-resolves.
        part_id = store.resolve_part_id(str(value))
        if part_id is None:
            return None, f"no loaded part matches {value!r}"
        if store.part_file(part_id) in store.reference_paths:
            # A measurement over golden geometry would reach the judge
            # reading as the agent's work.
            return None, f"{value!r} is a ground-truth reference part, not agent work"
        return part_id, None
    if spec.kind == "plane":
        plane = str(value).lower()
        if plane not in _PLANES:
            return None, f"plane must be one of {_PLANES}, got {value!r}"
        return plane, None
    if spec.kind == "view":
        view = str(value).lower()
        if view not in _VIEW_NAMES:
            return None, f"view must be one of {_VIEW_NAMES}, got {value!r}"
        return view, None
    if spec.kind == "number":
        try:
            return float(value), None
        except (TypeError, ValueError):
            return None, f"{spec.name} must be a number, got {value!r}"
    return str(value), None


def validate_plan(
    plan: CadProbePlan, store: GeometryStore, probe_budget: int
) -> list[ValidatedProbe]:
    """Every planned invocation, each either resolved against the catalog and the
    staged evidence or carrying the reason it will not run."""
    validated: list[ValidatedProbe] = []
    accepted = 0
    for planned in plan.probes:
        raw = dict(planned.params)
        spec = PROBE_CATALOG.get(planned.probe_id)
        if spec is None:
            validated.append(
                ValidatedProbe(
                    planned.probe_id,
                    raw,
                    error=f"unknown probe id {planned.probe_id!r}",
                )
            )
            continue
        if accepted >= probe_budget:
            validated.append(
                ValidatedProbe(
                    planned.probe_id,
                    raw,
                    error=f"probe budget of {probe_budget} exhausted",
                )
            )
            continue
        unknown = set(raw) - {p.name for p in spec.params}
        if unknown:
            validated.append(
                ValidatedProbe(
                    planned.probe_id, raw, error=f"unknown params {sorted(unknown)}"
                )
            )
            continue
        resolved: dict[str, Any] = {}
        error: str | None = None
        for param in spec.params:
            if param.name not in raw:
                if param.required:
                    error = f"missing required param {param.name!r}"
                    break
                continue
            value, error = _validate_param(param, raw[param.name], store)
            if error:
                break
            resolved[param.name] = value
        if (
            error is None
            and planned.probe_id == "golden_compare"
            and resolved.get("file") in store.reference_paths
        ):
            # With the reference param defaulted, a reference-as-file request
            # compares a golden to itself and reports a near-perfect match.
            error = (
                f"{resolved['file']!r} is a ground-truth reference; "
                "golden_compare's file must be an agent deliverable"
            )
        if error:
            validated.append(ValidatedProbe(planned.probe_id, raw, error=error))
            continue
        accepted += 1
        validated.append(ValidatedProbe(planned.probe_id, raw, resolved=resolved))
    return validated


async def plan_probes(
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    extra_args: dict[str, Any] | None,
    task_id: str,
    attempts: int = MAX_PLANNER_RETRIES,
) -> tuple[CadProbePlan | None, str]:
    """Call the planner model; (plan, last_error) with plan None after retries.

    A retry after an unparseable response feeds the parse error back so the
    model can correct its output instead of repeating it.
    """
    last_error = "no response"
    feedback = ""
    for attempt in range(attempts):
        try:
            response = await call_llm(
                model=model,
                messages=build_messages(
                    system_prompt=system_prompt, user_prompt=user_prompt + feedback
                ),
                timeout=PLANNER_TIMEOUT,
                extra_args=extra_args,
                response_format=CadProbePlan,
            )
        except Exception as e:
            last_error = f"planner LLM call failed: {e}"
            logger.warning(
                f"[{LOG_PREFIX}] task={task_id} | planner attempt {attempt + 1}: "
                f"{last_error}"
            )
            continue
        choices = response.choices
        raw = ""
        if choices and isinstance(choices[0], Choices):
            raw = choices[0].message.content or ""
        if not raw:
            last_error = "planner returned empty content"
            continue
        try:
            return CadProbePlan.model_validate_json(raw), last_error
        except ValidationError as e:
            last_error = f"unparseable plan: {str(e)[:200]}"
            feedback = (
                "\n\nYour previous response was rejected — "
                f"{last_error}. Respond again with ONLY a JSON object matching "
                "the required schema."
            )
            logger.warning(
                f"[{LOG_PREFIX}] task={task_id} | planner attempt {attempt + 1}: "
                f"{last_error}"
            )
    return None, last_error


def rejection_feedback(validated: list[ValidatedProbe]) -> str | None:
    """A repair-pass prompt section listing every rejected invocation, or None
    when the whole plan validated."""
    rejected = [probe for probe in validated if probe.error]
    if not rejected:
        return None
    lines = "\n".join(
        f"- {probe.probe_id} "
        f"{json.dumps(probe.raw_params, sort_keys=True, default=str)}: {probe.error}"
        for probe in rejected
    )
    return (
        "<REJECTED_PROBES>\nThese requests from your previous plan were "
        f"rejected and will not run:\n{lines}\n</REJECTED_PROBES>\n"
        "Respond with corrected versions of ONLY the rejected requests — the "
        "accepted ones already run and must not be repeated. Drop any request "
        "you cannot express with the catalog's params and the staged names."
    )


def merge_repaired_plan(
    validated: list[ValidatedProbe],
    repaired: CadProbePlan,
    store: GeometryStore,
    probe_budget: int,
) -> list[ValidatedProbe]:
    """The first plan's accepted probes stay, its rejections stay visible
    (annotated), and the repair's entries fill the gaps.

    Repair entries duplicating an accepted invocation are dropped before
    validation so they cannot spend the remaining budget.
    """

    def _key(probe_id: str, params: dict[str, Any]) -> str:
        return f"{probe_id}|{json.dumps(params, sort_keys=True, default=str)}"

    accepted = [probe for probe in validated if probe.error is None]
    kept_rejected = [
        replace(probe, error=f"{probe.error} (repair attempted)")
        for probe in validated
        if probe.error
    ]
    seen = {_key(probe.probe_id, probe.raw_params) for probe in accepted}
    fresh = [
        probe
        for probe in repaired.probes
        if _key(probe.probe_id, probe.params) not in seen
    ]
    revalidated = validate_plan(
        CadProbePlan(probes=fresh, rationale=repaired.rationale),
        store,
        max(0, probe_budget - len(accepted)),
    )
    return [*accepted, *kept_rejected, *revalidated]


# Fallback keyword routing for criteria the planner left unmapped. Digest
# hints run first: "dimension entities" is a digest fact, not a bbox one.
_FALLBACK_DIGEST_HINTS = ("dxf", "layer", "dimension entit", "linetype")
_FALLBACK_THICKNESS_HINTS = ("thick", "wall")
_FALLBACK_BATTERY_HINTS = (
    "dimension", "width", "wide", "height", "length", "depth", "diameter",
    "radius", "span", "size", " mm", "inch", "hole", "bore", "spacing",
    "distance", "clearance",
)  # fmt: skip


def _coverage_row(
    key: str,
    text: str,
    source: Literal["planner", "fallback"],
    *,
    probe_ids: list[str] | None = None,
    covered_by: str | None = None,
    expected_unverifiable: str | None = None,
) -> dict[str, Any]:
    return {
        "criterion_key": key,
        "criterion": text,
        "source": source,
        "probe_ids": probe_ids or [],
        "covered_by": covered_by,
        "expected_unverifiable": expected_unverifiable,
    }


def _fallback_coverage(
    key: str, text: str, agent_paths: list[str]
) -> tuple[dict[str, Any], list[PlannedProbe]]:
    lowered = text.lower()
    if any(h in lowered for h in _FALLBACK_DIGEST_HINTS):
        return _coverage_row(key, text, "fallback", covered_by="digest"), []
    if any(h in lowered for h in _FALLBACK_THICKNESS_HINTS):
        probes = [
            PlannedProbe(probe_id="wall_thickness", file=path) for path in agent_paths
        ]
        # Battery bboxes bound outer spans, not wall thickness; only the
        # requested probe may be claimed as this criterion's coverage.
        return (
            _coverage_row(key, text, "fallback", probe_ids=["wall_thickness"]),
            probes,
        )
    if any(h in lowered for h in _FALLBACK_BATTERY_HINTS):
        return _coverage_row(key, text, "fallback", covered_by="battery"), []
    return _coverage_row(key, text, "fallback"), []


def resolve_coverage(
    plan: CadProbePlan | None,
    units: list[tuple[str, str]],
    agent_paths: list[str],
) -> tuple[list[dict[str, Any]], list[PlannedProbe]]:
    """One coverage row per criterion unit — the planner's mapping where it
    gave one, else the deterministic keyword fallback — plus the fallback's
    probe requests to validate alongside the plan."""
    by_key: dict[str, CriterionCoverage] = {}
    for entry in plan.coverage if plan is not None else []:
        by_key.setdefault(entry.criterion_key, entry)
    planned_ids = {p.probe_id for p in plan.probes} if plan is not None else set()
    rows: list[dict[str, Any]] = []
    extra: list[PlannedProbe] = []
    # Several unmapped criteria can request the same probe; a duplicate would
    # spend a validate_plan budget slot without adding a measurement.
    seen_probes: set[str] = set()
    for key, text in units:
        mapped = by_key.get(key)
        if mapped is not None:
            # A coverage claim only counts as a mapping when the plan really
            # requests the probes it names; a bare claim would suppress the
            # fallback and leave the criterion without its measurement.
            claimed = [pid for pid in mapped.probe_ids if pid in planned_ids]
            if claimed or mapped.covered_by or mapped.expected_unverifiable:
                rows.append(
                    _coverage_row(
                        key,
                        text,
                        "planner",
                        probe_ids=claimed,
                        covered_by=mapped.covered_by,
                        expected_unverifiable=mapped.expected_unverifiable,
                    )
                )
                continue
        row, probes = _fallback_coverage(key, text, agent_paths)
        rows.append(row)
        for probe in probes:
            probe_key = (
                f"{probe.probe_id}|"
                f"{json.dumps(probe.params, sort_keys=True, default=str)}"
            )
            if probe_key not in seen_probes:
                seen_probes.add(probe_key)
                extra.append(probe)
    return rows, extra

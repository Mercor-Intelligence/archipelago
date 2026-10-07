"""CAD LLM judge — plan → probe → judge over CAD files in the final snapshot.

Evidence has three tiers: pure-Python digests (the shared
``agentic_verifier/cad.py`` layer), measured geometry (OCCT/trimesh probes, a
subset of them planned by a first LLM call from a fixed catalog), and offscreen
renders judged multimodally. Recompute-class and simulation criteria stay
"unverifiable" by contract rather than guessed.
"""

from __future__ import annotations

import asyncio
import io
import json
import shutil
import tempfile
import time
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from litellm import Choices
from litellm.exceptions import ContextWindowExceededError
from pydantic import BaseModel, Field, ValidationError

from runner.evals.models import EvalImplInput
from runner.models import VerifierResult
from runner.utils.criterion_metadata import (
    CRITERION_DEFINITIONS_KEY,
    CriterionDefinition,
    parse_criterion_definitions,
)
from runner.utils.grading_log import logger
from runner.utils.llm import build_messages, call_llm
from runner.utils.token_utils import count_tokens_uncached, get_model_context_limit
from runner.utils.ungradeable import no_deliverable_result, ungradeable_result

from .geometry_worker import (
    WORKER_STARTUP_ALLOWANCE_S,
    WorkerOutcome,
    run_worker,
)
from .planner import (
    DEFAULT_PROBE_BUDGET,
    PROBE_BUDGET_CEILING,
    CadProbePlan,
    ValidatedProbe,
    catalog_text,
    merge_repaired_plan,
    plan_probes,
    rejection_feedback,
    resolve_coverage,
    validate_plan,
)
from .probes import (
    FEATURE_HISTORY_MARK,
    MAX_GOLDEN_COMPARISONS,
    MAX_INTERFERENCE_PAIRS,
    PER_PROBE_TIMEOUT_S,
    PROBE_TOTAL_BUDGET_S,
    GeometryStore,
    golden_compare_spent_budget,
)
from .probes import (
    failed as failed_probe,
)
from .prompts import (
    PLANNER_SYSTEM_PROMPT,
    UNVERIFIABLE_CATEGORIES,
    UnverifiableCategory,
    build_planner_user_prompt,
    build_system_prompt,
    build_user_prompt,
    criterion_units,
)
from .render import (
    RENDER_IMAGE_BUDGET,
    RENDER_MODES,
    RenderedView,
)
from .staging import (
    StagedCadFile,
    discover_cad_files,
    seed_identical_paths,
    stage_cad_files,
    stage_reference_files,
)

LOG_PREFIX = "CAD_LLM_JUDGE"
LLM_JUDGE_TIMEOUT = 3600
MAX_JSON_RETRIES = 5
# Sibling verifiers grade under one gather; each geometry child holds a full
# OCP/trimesh import, so unbounded parallel children could exhaust the grading
# container's memory. Waiting here spends the waiter's own probe budget.
MAX_CONCURRENT_GEOMETRY_WORKERS = 2
_worker_slots = asyncio.Semaphore(MAX_CONCURRENT_GEOMETRY_WORKERS)
DEFAULT_MAX_FILES_IN_PROMPT = 12
# Bounds the prompt no matter what a world config asks for.
MAX_FILES_IN_PROMPT_CEILING = 50
DEFAULT_RENDER_VIEWS = "default"

# Evidence-budget fitting: the assembled judge prompt is token-counted against
# the model's context limit and trimmed lowest-priority-first before the call;
# a ContextWindowExceededError still shrinks the budget and retries before the
# grade voids. call_llm never retries that error itself.
_JUDGE_OUTPUT_RESERVE_TOKENS = 24_000
_MIN_EVIDENCE_BUDGET_TOKENS = 8_000
_IMAGE_TOKEN_ESTIMATE = 1_600
MAX_EVIDENCE_SHRINKS = 2
_MIN_RENDERS_KEPT = 2
_TRIMMED_ROW_CHARS = 2_000
_MIN_TRIMMED_ROW_CHARS = 400


# Harness-assigned sentinel for a refusal that never named a valid category
# even after feedback retries: the row scores 0 and is never reported as an
# honest refusal. Not in UNVERIFIABLE_CATEGORIES, so the judge cannot use it.
UNCATEGORIZED = "uncategorized"


class CadCriterionVerdict(BaseModel):
    criterion: str
    criterion_id: str | None = None
    met: bool | None = None
    verdict: Literal["pass", "fail", "unverifiable"]
    evidence: str
    confidence: Literal["parsed", "visual", "inferred"]
    unverifiable_reason: str | None = None
    unverifiable_category: UnverifiableCategory | Literal["uncategorized"] | None = None


class CadVerdict(BaseModel):
    criteria: list[CadCriterionVerdict] = Field(default_factory=list)
    summary: str = ""
    rationale: str = ""


class CadCriterionVerdictWire(CadCriterionVerdict):
    """Judge-facing schema: the harness sentinel is not a legal output value."""

    unverifiable_category: UnverifiableCategory | None = None  # pyright: ignore[reportIncompatibleVariableOverride]


class CadVerdictWire(CadVerdict):
    criteria: list[CadCriterionVerdictWire] = Field(default_factory=list)  # pyright: ignore[reportIncompatibleVariableOverride]


def _uncategorized_refusal(row: CadCriterionVerdict) -> bool:
    return row.verdict == "unverifiable" and (
        row.unverifiable_category not in UNVERIFIABLE_CATEGORIES
        or not (row.unverifiable_reason or "").strip()
    )


def _retry_feedback(error: str) -> str:
    return (
        f"Your previous verdict was rejected — {error}. Respond again with "
        "the complete corrected JSON verdict (every criterion row), matching "
        "the schema exactly."
    )


@dataclass(frozen=True)
class _StagedEvidence:
    error: str | None = None
    staged: list[StagedCadFile] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    references: list[StagedCadFile] = field(default_factory=list)
    store: GeometryStore | None = None


@dataclass(frozen=True)
class _ProbePhase:
    probe_rows: list[dict[str, Any]] = field(default_factory=list)
    planned_rows: list[dict[str, Any]] = field(default_factory=list)
    views: list[RenderedView] = field(default_factory=list)
    coverage_rows: list[dict[str, Any]] = field(default_factory=list)


def _config_str(values: dict[str, Any], key: str) -> str:
    value = values.get(key)
    return value.strip() if isinstance(value, str) else ""


def _config_str_list(values: dict[str, Any], key: str) -> list[str]:
    value = values.get(key)
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _reference_names(values: dict[str, Any]) -> list[str]:
    """Artifact names from ``artifacts_to_reference`` (dict or bare-string specs)."""
    names: list[str] = []
    for spec in values.get("artifacts_to_reference") or []:
        name = spec.get("name") if isinstance(spec, dict) else spec
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


def _bounded_int_config(
    input: EvalImplInput,  # noqa: A002
    key: str,
    default: int,
    ceiling: int,
) -> int:
    raw = (input.eval_config.eval_config_values or {}).get(key)
    if not isinstance(raw, (int, float, str)) or isinstance(raw, bool):
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(1, min(value, ceiling))


def _max_files_in_prompt(input: EvalImplInput) -> int:  # noqa: A002
    return _bounded_int_config(
        input,
        "max_files_in_prompt",
        DEFAULT_MAX_FILES_IN_PROMPT,
        MAX_FILES_IN_PROMPT_CEILING,
    )


def _probe_budget(input: EvalImplInput) -> int:  # noqa: A002
    return _bounded_int_config(
        input,
        "probe_budget",
        DEFAULT_PROBE_BUDGET,
        PROBE_BUDGET_CEILING,
    )


def _planner_model(input: EvalImplInput) -> str:  # noqa: A002
    raw = (input.eval_config.eval_config_values or {}).get("planner_model")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return input.grading_settings.llm_judge_model


def _render_mode(values: dict[str, Any]) -> str:
    raw = values.get("render_views")
    if isinstance(raw, str) and raw.strip().lower() in RENDER_MODES:
        return raw.strip().lower()
    return DEFAULT_RENDER_VIEWS


def _open_zip(snapshot_bytes: Any) -> zipfile.ZipFile | None:
    if snapshot_bytes is None:
        return None
    try:
        snapshot_bytes.seek(0)
        return zipfile.ZipFile(snapshot_bytes, "r")
    except (zipfile.BadZipFile, OSError, ValueError):
        return None


def _independent_stream(snapshot: Any) -> Any | None:
    """A same-content stream with its own cursor.

    The runner hands every sibling verifier the same snapshot handle and runs
    them under one gather, so reading that handle from a worker thread would
    race their cursors. Named files reopen; BytesIO copies; anything else
    (prod snapshots are SpooledTemporaryFile) is spooled to a private temp
    file HERE on the loop — a bounded sequential copy, so the shared cursor
    is only ever touched from the loop thread while the CPU-heavy parse still
    runs in the worker.
    """
    if snapshot is None:
        return None
    name = getattr(snapshot, "name", None)
    if isinstance(name, str):
        try:
            return open(name, "rb")  # noqa: SIM115 — closed by _stage_evidence
        except OSError:
            pass
    if isinstance(snapshot, io.BytesIO):
        return io.BytesIO(snapshot.getvalue())
    try:
        copy = tempfile.TemporaryFile()
    except OSError:
        return None
    try:
        snapshot.seek(0)
        shutil.copyfileobj(snapshot, copy, length=8 * 1024 * 1024)
        snapshot.seek(0)
        copy.seek(0)
        return copy
    except (OSError, ValueError):
        copy.close()
        return None


def _stage_evidence(
    final_stream: Any,
    initial_stream: Any,
    golden_streams: list[Any],
    values: dict[str, Any],
    max_files: int,
    store_root: Path,
    *,
    close_streams: bool,
) -> _StagedEvidence:
    """Open the snapshots and digest CAD evidence. Sync and CPU-bound — run it
    off the event loop with independent streams, or inline with the shared ones.
    Raw bytes are staged into a :class:`GeometryStore` at ``store_root`` for the
    probe/render phase."""
    final_zip: zipfile.ZipFile | None = None
    initial_zip: zipfile.ZipFile | None = None
    golden_zips: list[zipfile.ZipFile] = []
    try:
        final_zip = _open_zip(final_stream)
        if final_zip is None:
            return _StagedEvidence(error="snapshot_unreadable")
        initial_zip = _open_zip(initial_stream)
        golden_zips = [
            z for z in (_open_zip(g) for g in golden_streams) if z is not None
        ]
        ordered = discover_cad_files(
            final_zip,
            expected_cad_files=_config_str_list(values, "expected_cad_files"),
            cad_base_path=_config_str(values, "cad_base_path"),
            initial_zip=initial_zip,
        )
        if not ordered:
            return _StagedEvidence(error="no_cad_file")
        store = GeometryStore(store_root)
        staged, skipped = stage_cad_files(
            final_zip,
            ordered,
            max_files,
            seed_identical=seed_identical_paths(final_zip, initial_zip, ordered),
            sink=store.add_file,
        )
        references = stage_reference_files(
            _reference_names(values),
            initial_zip,
            golden_zips,
            sink=lambda name, data: store.add_file(name, data, reference=True),
        )
        return _StagedEvidence(
            staged=staged, skipped=skipped, references=references, store=store
        )
    finally:
        for zf in (final_zip, initial_zip, *golden_zips):
            if zf is not None:
                zf.close()
        if close_streams:
            for stream in (final_stream, initial_stream, *golden_streams):
                if stream is not None:
                    stream.close()


async def _staged_evidence(
    input: EvalImplInput,  # noqa: A002
    values: dict[str, Any],
    max_files: int,
    store_root: Path,
) -> _StagedEvidence:
    final_stream = _independent_stream(input.final_snapshot_bytes)
    if final_stream is not None:
        initial_stream = _independent_stream(input.initial_snapshot_bytes)
        golden_streams = [
            s
            for s in (_independent_stream(g) for g in input.golden_snapshots)
            if s is not None
        ]
        return await asyncio.to_thread(
            _stage_evidence,
            final_stream,
            initial_stream,
            golden_streams,
            values,
            max_files,
            store_root,
            close_streams=True,
        )
    # No reopenable handle: stage inline on the loop like sibling evals do, so
    # the shared cursor is never touched from another thread.
    return _stage_evidence(
        input.final_snapshot_bytes,
        input.initial_snapshot_bytes,
        list(input.golden_snapshots),
        values,
        max_files,
        store_root,
        close_streams=False,
    )


def _planner_index(evidence: _StagedEvidence, store: GeometryStore) -> str:
    """The compact evidence index the planner sees: files, parts, references."""
    lines = ["Staged CAD files:"]
    for entry in evidence.staged:
        note = " [digest parse failed]" if entry.parse_failed else ""
        lines.append(f"- {entry.path} ({entry.fmt}, {entry.size:,} bytes){note}")
    part_ids = store.part_ids()
    if part_ids:
        lines.append("Loadable parts (for distance/interference probes):")
        lines.extend(f"- {part_id}" for part_id in part_ids)
    if store.reference_paths:
        lines.append("Ground-truth reference files:")
        lines.extend(f"- {path}" for path in sorted(store.reference_paths))
    if evidence.skipped:
        lines.append(
            f"({len(evidence.skipped)} more discovered files are beyond the "
            "prompt cap and cannot be probed)"
        )
    return "\n".join(lines)


def _involved_paths(entry: ValidatedProbe, staged_paths: set[str]) -> set[str]:
    """The staged paths a validated probe touches (files, references, parts).

    Filtered against the staged set so plane/view/number params never read as
    paths and split one file's probes across several workers.
    """
    paths: set[str] = set()
    for value in entry.resolved.values():
        if isinstance(value, str) and value.split("::", 1)[0] in staged_paths:
            paths.add(value.split("::", 1)[0])
    return paths


def _probe_units(ops: list[dict[str, Any]]) -> int:
    """A coarse probe count for a child's deadline: per-probe timeout x units."""
    units = 0
    for op in ops:
        kind = op["kind"]
        if kind == "file_battery":
            units += 2
        elif kind in {"intra_pairs", "cross_pairs"}:
            units += 2
        elif kind in {"golden", "default_renders", "planned_probe"}:
            units += 1
    return max(1, units)


async def _run_worker_ops(
    store: GeometryStore,
    files: list[dict[str, Any]],
    ops: list[dict[str, Any]],
    *,
    render_mode: str,
    render_budget: int,
    phase_deadline: float,
) -> WorkerOutcome:
    """One killable child, deadline = startup allowance + timeout x probe count,
    capped by what is left of the phase's total budget."""
    async with _worker_slots:
        remaining = phase_deadline - time.monotonic()
        deadline = min(remaining, PER_PROBE_TIMEOUT_S * _probe_units(ops))
        if deadline > 0:
            deadline += WORKER_STARTUP_ALLOWANCE_S
        return await asyncio.to_thread(
            run_worker,
            root=store.root,
            files=files,
            ops=ops,
            render_mode=render_mode,
            render_budget=render_budget,
            deadline_s=deadline,
            per_probe_timeout_s=PER_PROBE_TIMEOUT_S,
        )


async def _run_probe_phase(
    input: EvalImplInput,  # noqa: A002
    values: dict[str, Any],
    evidence: _StagedEvidence,
    task_id: str,
    definitions: list[CriterionDefinition],
) -> _ProbePhase:
    """The plan → probe → render phase. Never raises: every failure becomes a
    visible ledger row and the judge still runs.

    All geometry work happens in killable child processes (see
    ``geometry_worker``): one per agent file, one for cross-file pairs, one per
    planned-probe file group. Only the planner LLM calls and plan validation —
    against the part inventory the children report — run in this process.
    """
    store = evidence.store
    if store is None or not store.paths():
        return _ProbePhase()

    mode = _render_mode(values)
    # "off" means NO images reach the judge — planner-requested renders spend
    # the same (zeroed) budget as the default set. Defaults render first: a
    # large plan exhausting the budget then fails visibly instead of silently
    # displacing the default views.
    render_remaining = 0 if mode == "off" else RENDER_IMAGE_BUDGET
    phase_deadline = time.monotonic() + PROBE_TOTAL_BUDGET_S

    # Accumulated as each child completes, so a failure mid-phase surfaces as a
    # visible row WITHOUT discarding the evidence already measured.
    probe_rows: list[dict[str, Any]] = []
    planned_rows: list[dict[str, Any]] = []
    planned_result_rows: list[dict[str, Any]] = []
    views: list[RenderedView] = []
    coverage_rows: list[dict[str, Any]] = []
    try:
        file_table = store.file_table()
        table_by_path = {row["path"]: row for row in file_table}
        reference_table = [row for row in file_table if row["reference"]]
        parts_inventory: dict[str, list[str]] = {}
        features_inventory: dict[str, list[str]] = {}
        pair_budget = MAX_INTERFERENCE_PAIRS
        comparison_budget = MAX_GOLDEN_COMPARISONS

        for path in store.agent_paths():
            ops: list[dict[str, Any]] = [
                {"key": f"battery:{path}", "kind": "file_battery", "path": path},
                {
                    "key": f"intra:{path}",
                    "kind": "intra_pairs",
                    "path": path,
                    "max_pairs": pair_budget,
                },
            ]
            if reference_table:
                ops.append(
                    {
                        "key": f"golden:{path}",
                        "kind": "golden",
                        "path": path,
                        "max_comparisons": comparison_budget,
                    }
                )
            ops.append(
                {"key": f"renders:{path}", "kind": "default_renders", "path": path}
            )
            outcome = await _run_worker_ops(
                store,
                [table_by_path[path], *reference_table],
                ops,
                render_mode=mode,
                render_budget=render_remaining,
                phase_deadline=phase_deadline,
            )
            probe_rows.extend(outcome.rows)
            views.extend(
                RenderedView(label=label, png_bytes=png) for label, png in outcome.views
            )
            render_remaining -= len(outcome.views)
            pair_budget -= sum(
                1 for row in outcome.rows if row["probe_id"] == "interference"
            )
            comparison_budget -= sum(
                1
                for row in outcome.rows
                if row["probe_id"] == "golden_compare"
                and golden_compare_spent_budget(row.get("outcome"))
            )
            parts_inventory.update(outcome.parts)
            features_inventory.update(outcome.features)

        multi_part_paths = [
            row["path"]
            for row in file_table
            if not row["reference"] and parts_inventory.get(row["path"])
        ]
        if len(multi_part_paths) >= 2 and pair_budget > 0:
            outcome = await _run_worker_ops(
                store,
                [table_by_path[path] for path in multi_part_paths],
                [{"key": "cross", "kind": "cross_pairs", "max_pairs": pair_budget}],
                render_mode=mode,
                render_budget=0,
                phase_deadline=phase_deadline,
            )
            probe_rows.extend(outcome.rows)

        store.set_inventory(parts_inventory, features_inventory)
        probe_budget = _probe_budget(input)
        planner_model = _planner_model(input)
        index = _planner_index(evidence, store)
        units = criterion_units(
            _config_str(values, "criteria"), [d.model_dump() for d in definitions]
        )
        planner_prompt = build_planner_user_prompt(
            criteria=_config_str(values, "criteria"),
            criteria_explanation=_config_str(values, "criteria_explanation"),
            criterion_units=units,
            index=index,
            catalog=catalog_text(),
            probe_budget=probe_budget,
        )
        plan, plan_error = await plan_probes(
            model=planner_model,
            system_prompt=PLANNER_SYSTEM_PROMPT,
            user_prompt=planner_prompt,
            extra_args=input.grading_settings.llm_judge_extra_args,
            task_id=task_id,
        )

        validated: list[ValidatedProbe] = []
        if plan is None:
            planned_rows = [{"error": f"planner produced no plan: {plan_error}"}]
            planned_result_rows = [
                failed_probe(
                    "planner", "*", f"planner produced no plan: {plan_error}"
                ).as_row()
            ]
        else:
            validated = validate_plan(plan, store, probe_budget)
            feedback = rejection_feedback(validated)
            if feedback is not None:
                # One repair pass: the rejection reasons go back to the planner
                # so a fixable request is corrected instead of surfacing as a
                # failed row; the first plan's accepted probes always survive.
                logger.info(
                    f"[{LOG_PREFIX}] task={task_id} | repairing plan: "
                    f"{sum(1 for v in validated if v.error)} rejected"
                )
                repaired, _repair_error = await plan_probes(
                    model=planner_model,
                    system_prompt=PLANNER_SYSTEM_PROMPT,
                    user_prompt=f"{planner_prompt}\n\n{feedback}",
                    extra_args=input.grading_settings.llm_judge_extra_args,
                    task_id=task_id,
                    attempts=1,
                )
                if repaired is not None:
                    validated = merge_repaired_plan(
                        validated, repaired, store, probe_budget
                    )

        # Fallback probes may only spend budget the planner left unused.
        coverage_rows, fallback_probes = resolve_coverage(
            plan, units, store.agent_paths()
        )
        if fallback_probes:
            accepted_keys = {
                f"{v.probe_id}|{json.dumps(v.raw_params, sort_keys=True, default=str)}"
                for v in validated
                if v.error is None
            }
            fresh = [
                p
                for p in fallback_probes
                if f"{p.probe_id}|{json.dumps(p.params, sort_keys=True, default=str)}"
                not in accepted_keys
            ]
            validated = [
                *validated,
                *validate_plan(
                    CadProbePlan(probes=fresh),
                    store,
                    max(0, probe_budget - len(accepted_keys)),
                ),
            ]

        if validated:
            planned_rows = [*planned_rows, *(entry.as_row() for entry in validated)]
            executed_rows, planned_views = await _execute_planned_in_workers(
                store,
                table_by_path,
                reference_table,
                validated,
                render_mode=mode,
                render_remaining=render_remaining,
                phase_deadline=phase_deadline,
            )
            planned_result_rows = [*planned_result_rows, *executed_rows]
            views.extend(planned_views)

        logger.info(
            f"[{LOG_PREFIX}] task={task_id} | battery={len(probe_rows)} "
            f"planned={len(planned_result_rows)} renders={len(views)}"
        )
        return _ProbePhase(
            probe_rows=[*probe_rows, *planned_result_rows],
            planned_rows=planned_rows,
            views=views,
            coverage_rows=coverage_rows,
        )
    except Exception as e:
        logger.warning(f"[{LOG_PREFIX}] task={task_id} | probe phase failed: {e!r}")
        return _ProbePhase(
            probe_rows=[
                *probe_rows,
                *planned_result_rows,
                failed_probe("probe_phase", "*", f"{type(e).__name__}: {e}").as_row(),
            ],
            planned_rows=planned_rows,
            views=views,
            coverage_rows=coverage_rows,
        )


async def _execute_planned_in_workers(
    store: GeometryStore,
    table_by_path: dict[str, dict[str, Any]],
    reference_table: list[dict[str, Any]],
    validated: list[ValidatedProbe],
    *,
    render_mode: str,
    render_remaining: int,
    phase_deadline: float,
) -> tuple[list[dict[str, Any]], list[RenderedView]]:
    """Accepted planned probes grouped by the files they touch, one child per
    group; rejections become failed rows here and duplicates reuse the first
    result, mirroring the old in-process ``execute_plan``."""

    def _key(entry: ValidatedProbe) -> str:
        params = json.dumps(entry.raw_params, sort_keys=True, default=str)
        return f"{entry.probe_id}|{params}"

    groups: dict[frozenset[str], list[tuple[int, ValidatedProbe]]] = {}
    rows_by_index: dict[int, dict[str, Any]] = {}
    first_by_key: dict[str, int] = {}
    duplicates: list[tuple[int, int]] = []
    for i, entry in enumerate(validated):
        if entry.error:
            rows_by_index[i] = failed_probe(
                entry.probe_id, str(entry.raw_params), entry.error, entry.raw_params
            ).as_row()
            continue
        key = _key(entry)
        if key in first_by_key:
            duplicates.append((i, first_by_key[key]))
            continue
        first_by_key[key] = i
        group = frozenset(_involved_paths(entry, set(table_by_path)))
        groups.setdefault(group, []).append((i, entry))

    views: list[RenderedView] = []
    for group, entries in groups.items():
        # References ride along so a golden_compare that defaulted its
        # reference param still finds one staged in the child.
        files = [table_by_path[path] for path in sorted(group) if path in table_by_path]
        files += [row for row in reference_table if row["path"] not in group]
        ops: list[dict[str, Any]] = [
            {
                "key": f"planned:{i}",
                "kind": "planned_probe",
                "probe_id": entry.probe_id,
                "raw_params": entry.raw_params,
                "resolved": entry.resolved,
            }
            for i, entry in entries
        ]
        outcome = await _run_worker_ops(
            store,
            files,
            ops,
            render_mode=render_mode,
            render_budget=render_remaining,
            phase_deadline=phase_deadline,
        )
        views.extend(
            RenderedView(label=label, png_bytes=png) for label, png in outcome.views
        )
        render_remaining -= len(outcome.views)
        for i, entry in entries:
            op_rows = outcome.rows_by_key.get(f"planned:{i}") or [
                failed_probe(
                    entry.probe_id,
                    str(entry.raw_params),
                    "geometry worker returned no row for this probe",
                    entry.raw_params,
                ).as_row()
            ]
            rows_by_index[i] = op_rows[0]
    for i, source in duplicates:
        if source in rows_by_index:
            rows_by_index[i] = rows_by_index[source]
    ordered = [rows_by_index[i] for i in range(len(validated)) if i in rows_by_index]
    return ordered, views


def _fit_evidence(
    *,
    probe_rows: list[dict[str, Any]],
    views: list[RenderedView],
    shrink: int,
    model: str,
    system_prompt: str,
    prompt_builder: Callable[[list[dict[str, Any]], list[str], list[str]], str],
) -> tuple[str, list[RenderedView]]:
    """The user prompt and kept renders, fitted to the model's context budget.

    ``shrink`` halves the budget per level (an overflow retry). Trims run
    lowest-priority-first — feature-history rows, then oversized rows, then
    renders beyond a floor — and every omission is named in the prompt so the
    judge treats trimmed evidence as absent, mirroring output_llm's honesty.
    """
    budget = max(
        _MIN_EVIDENCE_BUDGET_TOKENS,
        (get_model_context_limit(model) - _JUDGE_OUTPUT_RESERVE_TOKENS) >> shrink,
    )
    rows = [dict(row) for row in probe_rows]
    kept_views = list(views)
    notes: list[str] = []

    def _prompt() -> str:
        return prompt_builder(rows, [view.label for view in kept_views], notes)

    def _fits(prompt: str) -> bool:
        tokens = (
            count_tokens_uncached(
                system_prompt + prompt, model, conservative_estimate=True
            )
            + len(kept_views) * _IMAGE_TOKEN_ESTIMATE
        )
        return tokens <= budget

    prompt = _prompt()
    if _fits(prompt):
        return prompt, kept_views

    for row in rows:
        outcome = str(row.get("outcome") or "")
        if FEATURE_HISTORY_MARK in str(row.get("target") or "") and len(outcome) > 120:
            row["outcome"] = "[omitted for budget: feature-history detail]"
            notes.append(
                f"{row.get('probe_id')} on {row.get('target')}: feature-history "
                "detail omitted for budget"
            )
    prompt = _prompt()
    if _fits(prompt):
        return prompt, kept_views

    cap = _TRIMMED_ROW_CHARS if shrink == 0 else _MIN_TRIMMED_ROW_CHARS
    for row in rows:
        outcome = str(row.get("outcome") or "")
        if len(outcome) > cap:
            row["outcome"] = outcome[:cap] + " … [trimmed for budget]"
            notes.append(
                f"{row.get('probe_id')} on {row.get('target')}: outcome trimmed "
                f"to {cap:,} of {len(outcome):,} chars for budget"
            )
    prompt = _prompt()
    if _fits(prompt):
        return prompt, kept_views

    if len(kept_views) > _MIN_RENDERS_KEPT:
        notes.extend(
            f"render omitted for budget: {view.label}"
            for view in kept_views[_MIN_RENDERS_KEPT:]
        )
        kept_views = kept_views[:_MIN_RENDERS_KEPT]
        prompt = _prompt()
        if _fits(prompt):
            return prompt, kept_views

    for row in rows:
        outcome = str(row.get("outcome") or "")
        if len(outcome) > _MIN_TRIMMED_ROW_CHARS:
            row["outcome"] = (
                outcome[:_MIN_TRIMMED_ROW_CHARS] + " … [trimmed for budget]"
            )
    if kept_views:
        notes.append(f"all {len(kept_views)} rendered views omitted for budget")
        kept_views = []
    notes.append("probe evidence trimmed to its floor; treat missing detail as absent")
    return _prompt(), kept_views


async def _call_judge(
    *,
    model: str,
    build_messages_at: Callable[[int], list[dict[str, Any]]],
    extra_args: dict[str, Any] | None,
    required_ids: frozenset[str],
    task_id: str,
) -> tuple[CadVerdict | None, str, str]:
    """Return (verdict, last_failure_kind, last_error); kind is one of
    "llm", "overflow", "verdict" (meaningless when a verdict came back).

    When ``required_ids`` is set the ledger must carry exactly one row per
    authored criterion id: a dropped id could silently turn a failed critical
    criterion into a pass and duplicate or invented rows would inflate the
    pass fraction, so any mismatch is retried like an unparseable verdict.
    A ledger whose only flaw is uncategorized refusals comes back with those
    rows coerced to UNCATEGORIZED rather than None.

    A ContextWindowExceededError never resolves by re-sending the same prompt
    (call_llm skips retrying it for the same reason), so each one bumps the
    evidence shrink level and rebuilds the messages before the next attempt,
    up to MAX_EVIDENCE_SHRINKS.
    """
    last_error = "no response"
    failure_kind = "verdict"
    shrink = 0
    feedback = ""
    salvageable: CadVerdict | None = None
    # Token counting over a large prompt is CPU work; keep it off the loop.
    base_messages = await asyncio.to_thread(build_messages_at, shrink)
    for attempt in range(MAX_JSON_RETRIES):
        messages = (
            [*base_messages, {"role": "user", "content": feedback}]
            if feedback
            else base_messages
        )
        try:
            response = await call_llm(
                model=model,
                messages=messages,
                timeout=LLM_JUDGE_TIMEOUT,
                extra_args=extra_args,
                response_format=CadVerdictWire,
            )
        except ContextWindowExceededError as e:
            failure_kind = "overflow"
            last_error = f"judge prompt exceeded the model context window: {e}"
            logger.warning(
                f"[{LOG_PREFIX}] task={task_id} | attempt {attempt + 1}: {last_error}"
            )
            if shrink >= MAX_EVIDENCE_SHRINKS:
                break  # already at the floor; the same prompt cannot succeed
            shrink += 1
            base_messages = await asyncio.to_thread(build_messages_at, shrink)
            continue
        except Exception as e:
            failure_kind = "llm"
            last_error = f"LLM call failed: {e}"
            logger.warning(
                f"[{LOG_PREFIX}] task={task_id} | attempt {attempt + 1}: {last_error}"
            )
            continue
        failure_kind = "verdict"
        choices = response.choices
        raw = ""
        if choices and isinstance(choices[0], Choices):
            raw = choices[0].message.content or ""
        if not raw:
            last_error = "empty response content"
            continue
        try:
            verdict = CadVerdict.model_validate_json(raw)
        except ValidationError as e:
            last_error = f"unparseable verdict: {str(e)[:200]}"
            feedback = _retry_feedback(last_error)
            logger.warning(
                f"[{LOG_PREFIX}] task={task_id} | attempt {attempt + 1}: {last_error}"
            )
            continue
        if not verdict.criteria:
            last_error = "verdict carried an empty criteria ledger"
            feedback = _retry_feedback(last_error)
            continue
        if required_ids:
            returned = sorted(row.criterion_id or "" for row in verdict.criteria)
            if returned != sorted(required_ids):
                last_error = (
                    "ledger must carry exactly one row per authored criterion "
                    f"id {sorted(required_ids)}; got {returned}"
                )
                feedback = _retry_feedback(last_error)
                logger.warning(
                    f"[{LOG_PREFIX}] task={task_id} | attempt {attempt + 1}: "
                    f"{last_error}"
                )
                continue
        # A refusal with no named category is exactly the over-refusal this
        # judge is contracted against; retry it like an unparseable verdict.
        uncategorized = [
            row.criterion_id or row.criterion
            for row in verdict.criteria
            if _uncategorized_refusal(row)
        ]
        if uncategorized:
            salvageable = verdict
            last_error = (
                "unverifiable verdicts must carry an unverifiable_category "
                f"from {list(UNVERIFIABLE_CATEGORIES)} and a specific "
                f"unverifiable_reason; missing on: {uncategorized}"
            )
            feedback = _retry_feedback(last_error)
            logger.warning(
                f"[{LOG_PREFIX}] task={task_id} | attempt {attempt + 1}: {last_error}"
            )
            continue
        return verdict, "", last_error
    if salvageable is not None:
        # 16 good rows must not void over one uncategorized refusal: coerce
        # the offenders to the sentinel (scores 0, never an honest refusal).
        coerced = []
        for row in salvageable.criteria:
            if _uncategorized_refusal(row):
                row.unverifiable_category = UNCATEGORIZED
                coerced.append(row.criterion_id or row.criterion)
        logger.warning(
            f"[{LOG_PREFIX}] task={task_id} | salvaged ledger after "
            f"{MAX_JSON_RETRIES} attempts; coerced to uncategorized: {coerced}"
        )
        return salvageable, "", last_error
    return None, failure_kind, last_error


def _score(
    ledger: list[CadCriterionVerdict],
    definitions: list[CriterionDefinition],
) -> float | None:
    """Fraction of passes over gradeable entries; None when nothing is gradeable.

    A failed criterion whose authored definition is critical zeroes the score.
    An unverifiable critical never reaches here: the eval voids that grade
    before scoring, so filtering it with the other unverifiable rows is safe.
    A coerced UNCATEGORIZED refusal is not an honest refusal: it stays in the
    denominator and scores 0 (a critical one zeroes the score like a fail).
    """
    gradeable = [
        row
        for row in ledger
        if row.verdict != "unverifiable" or row.unverifiable_category == UNCATEGORIZED
    ]
    if not gradeable:
        return None
    critical_ids = {d.criterion_id for d in definitions if d.critical}
    failed = [row for row in gradeable if row.verdict != "pass"]
    if any(row.criterion_id in critical_ids for row in failed if row.criterion_id):
        return 0.0
    passes = sum(1 for row in gradeable if row.verdict == "pass")
    return passes / len(gradeable)


def _normalized_ledger(ledger: list[CadCriterionVerdict]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for row in ledger:
        entry = row.model_dump()
        entry["met"] = None if _honest_refusal(row) else row.verdict == "pass"
        rows.append(entry)
    return rows


def _honest_refusal(row: CadCriterionVerdict) -> bool:
    """Unverifiable with a valid category; a coerced UNCATEGORIZED row is not."""
    return row.verdict == "unverifiable" and row.unverifiable_category != UNCATEGORIZED


def _critical_unverifiable_rows(
    ledger: list[CadCriterionVerdict],
    definitions: list[CriterionDefinition],
) -> list[CadCriterionVerdict]:
    """Ledger rows for authored critical criteria the judge could not verify."""
    rows_by_id = {row.criterion_id: row for row in ledger if row.criterion_id}
    return [
        rows_by_id[d.criterion_id]
        for d in definitions
        if d.critical and _honest_refusal(rows_by_id[d.criterion_id])
    ]


def _critical_count_values(
    ledger: list[CadCriterionVerdict],
    definitions: list[CriterionDefinition],
) -> dict[str, Any]:
    """Critical counts for authored definitions, plus the shared contract keys.

    The per-verifier counts (total / met / unverifiable) are always present so
    an unverified critical criterion stays visible on the grade. The
    ``critical_criteria_metadata`` contract keys additionally need a boolean
    per authored id; an unverifiable authored criterion has none, so those two
    keys are omitted and the run-level counts read as undeterminable rather
    than invented.
    """
    if not definitions:
        return {}
    rows_by_id = {row.criterion_id: row for row in ledger if row.criterion_id}
    criticals = [rows_by_id[d.criterion_id] for d in definitions if d.critical]
    values: dict[str, Any] = {
        "critical_criteria_total": len(criticals),
        "critical_criteria_met": sum(1 for r in criticals if r.verdict == "pass"),
        "critical_criteria_unverifiable": sum(
            1 for r in criticals if _honest_refusal(r)
        ),
    }
    if any(_honest_refusal(rows_by_id[d.criterion_id]) for d in definitions):
        return values
    return {
        **values,
        CRITERION_DEFINITIONS_KEY: [d.model_dump() for d in definitions],
        "criteria": [
            {
                "criterion_id": d.criterion_id,
                "criterion": d.criterion,
                "met": rows_by_id[d.criterion_id].verdict == "pass",
                "source": "guidance",
                "evidence": rows_by_id[d.criterion_id].evidence,
            }
            for d in definitions
        ],
    }


def _evidence_values(evidence: _StagedEvidence, model: str) -> dict[str, Any]:
    return {
        "evaluated_files": [
            f"{f.path} ({f.fmt}, {f.size:,} bytes)" for f in evidence.staged
        ],
        "skipped_files": list(evidence.skipped),
        "parse_failures": [f.path for f in evidence.staged if f.parse_failed],
        "had_reference": bool(evidence.references),
        "grading_model": model,
    }


async def cad_llm_judge_eval(input: EvalImplInput) -> VerifierResult:  # noqa: A002
    """Judge free-text criteria against digested CAD files from the final snapshot."""
    values = dict(input.verifier.verifier_values or {})
    task_id = input.verifier.task_id or "unknown"

    criteria = _config_str(values, "criteria")
    if not criteria:
        return ungradeable_result(
            input,
            "cad_llm_judge requires a non-empty `criteria` field",
            cause="config_error",
        )
    try:
        definitions = parse_criterion_definitions(
            values.get(CRITERION_DEFINITIONS_KEY) or []
        )
    except (ValidationError, ValueError) as e:
        return ungradeable_result(
            input,
            f"invalid criterion_definitions: {e}",
            cause="config_error",
        )

    # ignore_cleanup_errors: a timed-out probe's abandoned thread can still be
    # writing scratch files when the run finishes.
    with tempfile.TemporaryDirectory(
        prefix="cad-judge-", ignore_cleanup_errors=True
    ) as scratch:
        evidence = await _staged_evidence(
            input, values, _max_files_in_prompt(input), Path(scratch)
        )
        if evidence.error == "snapshot_unreadable":
            return ungradeable_result(
                input,
                "final snapshot is missing or not a readable zip",
                cause="snapshot_unreadable",
            )
        if evidence.error == "no_cad_file":
            return no_deliverable_result(
                input,
                "No CAD files found in the final snapshot. The agent did not "
                "produce a CAD deliverable to judge.",
                values={"auto_fail_reason": "no_cad_file"},
            )

        model = input.grading_settings.llm_judge_model
        evidence_values = _evidence_values(evidence, model)

        if all(f.parse_failed for f in evidence.staged):
            return ungradeable_result(
                input,
                "every discovered CAD file failed to parse, so no digest "
                "reached the judge",
                cause="all_files_over_caps",
                values=evidence_values,
            )

        phase = await _run_probe_phase(input, values, evidence, task_id, definitions)
        probe_values: dict[str, Any] = {
            "probe_ledger": phase.probe_rows,
            "planned_probes": phase.planned_rows,
            "probe_failures": [row for row in phase.probe_rows if not row["ok"]],
            "renders_used": [view.label for view in phase.views],
            "criterion_coverage": phase.coverage_rows,
        }

        system_prompt = build_system_prompt(
            input.grading_settings.llm_judge_system_prompt, bool(evidence.references)
        )

        def _prompt_builder(
            rows: list[dict[str, Any]],
            render_labels: list[str],
            budget_notes: list[str],
        ) -> str:
            return build_user_prompt(
                criteria=criteria,
                criteria_explanation=_config_str(values, "criteria_explanation"),
                criterion_definitions=[d.model_dump() for d in definitions],
                staged=evidence.staged,
                references=evidence.references,
                skipped=evidence.skipped,
                probe_rows=rows,
                render_labels=render_labels,
                budget_notes=budget_notes,
                coverage_rows=phase.coverage_rows,
            )

        def _messages_at(shrink: int) -> list[dict[str, Any]]:
            user_prompt, kept_views = _fit_evidence(
                probe_rows=phase.probe_rows,
                views=phase.views,
                shrink=shrink,
                model=model,
                system_prompt=system_prompt,
                prompt_builder=_prompt_builder,
            )
            return build_messages(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                images=[view.as_image(i + 1) for i, view in enumerate(kept_views)],
            )

        logger.info(
            f"[{LOG_PREFIX}] task={task_id} | files={len(evidence.staged)} "
            f"skipped={len(evidence.skipped)} refs={len(evidence.references)} "
            f"probes={len(phase.probe_rows)} renders={len(phase.views)} | "
            f"model={model}"
        )

        verdict, failure_kind, last_error = await _call_judge(
            model=model,
            build_messages_at=_messages_at,
            extra_args=input.grading_settings.llm_judge_extra_args,
            required_ids=frozenset(d.criterion_id for d in definitions),
            task_id=task_id,
        )
    if verdict is None:
        if failure_kind == "overflow":
            biggest = sorted(
                phase.probe_rows,
                key=lambda row: len(str(row.get("outcome") or "")),
                reverse=True,
            )[:3]
            detail = "; ".join(
                f"{row.get('probe_id')} on {row.get('target')} "
                f"(~{len(str(row.get('outcome') or '')):,} chars)"
                for row in biggest
            )
            return ungradeable_result(
                input,
                "judge prompt exceeded the model context window even after "
                f"{MAX_EVIDENCE_SHRINKS} evidence-trim retries; largest probe "
                f"evidence: {detail}",
                cause="no_verdict",
                values={**evidence_values, **probe_values},
            )
        return ungradeable_result(
            input,
            f"judge produced no verdict after {MAX_JSON_RETRIES} attempts: "
            f"{last_error}",
            cause="model_unavailable" if failure_kind == "llm" else "no_verdict",
            values={**evidence_values, **probe_values},
        )

    score = _score(verdict.criteria, definitions)
    kernel_only_aspects = [
        {
            "criterion": row.criterion,
            "criterion_id": row.criterion_id,
            "unverifiable_reason": row.unverifiable_reason,
            "unverifiable_category": row.unverifiable_category,
        }
        for row in verdict.criteria
        if _honest_refusal(row)
    ]
    uncategorized_refusals = [
        {
            "criterion": row.criterion,
            "criterion_id": row.criterion_id,
            "unverifiable_reason": row.unverifiable_reason,
        }
        for row in verdict.criteria
        if row.unverifiable_category == UNCATEGORIZED
    ]
    flag_values: dict[str, Any] = (
        {"uncategorized_refusals": uncategorized_refusals}
        if uncategorized_refusals
        else {}
    )
    critical_unverifiable = _critical_unverifiable_rows(verdict.criteria, definitions)
    if critical_unverifiable:
        ids = ", ".join(
            row.criterion_id or row.criterion for row in critical_unverifiable
        )
        return ungradeable_result(
            input,
            f"critical criteria could not be judged ({ids}): they need facts "
            "this judge cannot establish even with measured probes and "
            "renders, and a grade that skips a critical criterion could pass "
            "a deliverable whose one hard requirement was never checked; "
            "author the criterion from feature trees, measured geometry or "
            "rendered views, or use the agentic verifier's CAD toolkit",
            cause="config_error",
            values={
                **evidence_values,
                **probe_values,
                "judge_grade": _normalized_ledger(verdict.criteria),
                "kernel_only_aspects": kernel_only_aspects,
                **flag_values,
                **_critical_count_values(verdict.criteria, definitions),
            },
        )
    if score is None:
        return ungradeable_result(
            input,
            "every criterion needs facts this judge cannot establish even with "
            "measured probes and renders (simulation results, or FreeCAD "
            "kernel recompute facts); author criteria answerable from feature "
            "trees, measured geometry and rendered views, or use the agentic "
            "verifier's CAD toolkit",
            cause="config_error",
            values={
                **evidence_values,
                **probe_values,
                "kernel_only_aspects": kernel_only_aspects,
                **flag_values,
            },
        )

    logger.info(
        f"[{LOG_PREFIX}] task={task_id} | score={score:.3f} | "
        f"gradeable={len(verdict.criteria) - len(kernel_only_aspects)} "
        f"unverifiable={len(kernel_only_aspects)}"
    )

    return VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=score,
        verifier_result_values={
            **evidence_values,
            **probe_values,
            "score": score,
            "score_100": round(score * 100),
            "judge_grade": _normalized_ledger(verdict.criteria),
            "grade_rationale": verdict.rationale,
            "summary": verdict.summary,
            "kernel_only_aspects": kernel_only_aspects,
            **flag_values,
            **_critical_count_values(verdict.criteria, definitions),
        },
    )

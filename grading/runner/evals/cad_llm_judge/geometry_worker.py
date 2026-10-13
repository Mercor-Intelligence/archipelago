"""Killable subprocess worker for the CAD judge's GIL-holding geometry work.

OCCT native calls do not release the GIL, so a thread timeout cannot preempt
them: one pathological file could eat the whole grading run's wall clock, and
a native crash would take the grading worker with it. All shape loading,
``BRepMesh`` tessellation, booleans, golden compares and render rasterization
therefore run in a spawned child process (spawn, never fork — OCP under fork is
unsafe) that the parent SIGKILLs, whole process group, at a hard deadline.

Batching: one child per agent FILE (load once, run that file's battery, pairs,
golden compares and renders, tessellate once), one child for cross-file pairs,
one child per planned-probe file group. The child appends each completed row to
``results.jsonl`` and flushes, so a killed child loses only its remaining ops;
the parent synthesizes visible ``failed: ...`` rows for those while every other
child's results survive. ``ProbeRunner``'s thread timeout stays as a secondary
bound inside the child.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

WORKER_STARTUP_ALLOWANCE_S = 45.0
_LOG_TAIL_BYTES = 600


def _worker_argv(request_path: str) -> list[str]:
    return [
        sys.executable,
        "-m",
        "runner.evals.cad_llm_judge.geometry_worker",
        request_path,
    ]


def _worker_env() -> dict[str, str]:
    grading_root = str(Path(__file__).resolve().parents[3])
    env = dict(os.environ)
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = (
        f"{grading_root}{os.pathsep}{existing}" if existing else (grading_root)
    )
    return env


@dataclass
class WorkerOutcome:
    rows: list[dict[str, Any]] = field(default_factory=list)
    rows_by_key: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    views: list[tuple[str, bytes]] = field(default_factory=list)  # (label, png)
    parts: dict[str, list[str]] = field(default_factory=dict)
    features: dict[str, list[str]] = field(default_factory=dict)
    failure: str | None = None


def _missing_op_rows(op: dict[str, Any], why: str) -> list[dict[str, Any]]:
    from .probes import failed

    kind = str(op["kind"])
    path = str(op.get("path", "*"))
    if kind == "file_battery":
        return [
            failed("brep_battery", path, why).as_row(),
            failed("mesh_battery", path, why).as_row(),
        ]
    if kind in {"intra_pairs", "cross_pairs"}:
        return [failed("interference", path, why).as_row()]
    if kind == "golden":
        return [failed("golden_compare", path, why).as_row()]
    if kind == "default_renders":
        return [failed("default_renders", path, why).as_row()]
    if kind == "planned_probe":
        raw = dict(op.get("raw_params") or {})
        return [
            failed(
                str(op["probe_id"]), json.dumps(raw, sort_keys=True), why, raw
            ).as_row()
        ]
    return [failed(kind, path, why).as_row()]


def run_worker(
    *,
    root: Path,
    files: list[dict[str, Any]],
    ops: list[dict[str, Any]],
    render_mode: str,
    render_budget: int,
    deadline_s: float,
    per_probe_timeout_s: float,
) -> WorkerOutcome:
    """Run ``ops`` in a killable child; never raises for child trouble.

    ``deadline_s`` is the hard wall-clock bound on the child (startup included);
    at or below zero nothing is spawned and every op comes back as a visible
    budget-exhausted failure.
    """
    from runner.utils.grading_log import logger

    outcome = WorkerOutcome()
    if deadline_s <= 0:
        for op in ops:
            missing = _missing_op_rows(op, "total probe wall-clock budget exhausted")
            outcome.rows_by_key[str(op["key"])] = missing
            outcome.rows.extend(missing)
        outcome.failure = "not started: budget exhausted"
        return outcome

    work_dir = Path(tempfile.mkdtemp(prefix="worker-", dir=root))
    out_dir = work_dir / "out"
    out_dir.mkdir()
    request = {
        "root": str(root),
        "out_dir": str(out_dir),
        "files": files,
        "ops": ops,
        "render_mode": render_mode,
        "render_budget": render_budget,
        "per_probe_timeout_s": per_probe_timeout_s,
        "total_budget_s": deadline_s,
    }
    request_path = work_dir / "request.json"
    request_path.write_text(json.dumps(request))
    log_path = work_dir / "worker.log"

    failure: str | None = None
    with log_path.open("wb") as log_file:
        try:
            proc = subprocess.Popen(
                _worker_argv(str(request_path)),
                stdout=log_file,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
                cwd=str(root),
                env=_worker_env(),
            )
        except OSError as e:
            failure = f"could not start ({e})"
            proc = None
        if proc is not None:
            try:
                code = proc.wait(timeout=deadline_s)
                if code != 0:
                    failure = f"crashed (exit {code})"
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    proc.kill()
                proc.wait()
                failure = "killed at its wall-clock deadline (timeout)"
    if failure is not None:
        tail = b""
        try:
            tail = log_path.read_bytes()[-_LOG_TAIL_BYTES:]
        except OSError:
            pass
        logger.warning(
            f"[CAD_LLM_JUDGE] geometry worker {failure}; "
            f"log tail: {tail.decode('utf-8', 'replace')!r}"
        )
    outcome.failure = failure

    rows_by_key: dict[str, list[dict[str, Any]]] = {}
    completed: set[str] = set()
    results_path = out_dir / "results.jsonl"
    if results_path.exists():
        for line in results_path.read_text(errors="replace").splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue  # a kill can truncate the final line mid-write
            if "op_done" in record:
                completed.add(str(record["op_done"]))
            elif "row" in record:
                rows_by_key.setdefault(str(record["key"]), []).append(record["row"])
            elif "view" in record:
                view = record["view"]
                try:
                    png = (out_dir / str(view["file"])).read_bytes()
                except OSError:
                    continue
                outcome.views.append((str(view["label"]), png))
            elif "inventory" in record:
                inventory = record["inventory"]
                outcome.parts = {
                    str(k): [str(n) for n in v]
                    for k, v in dict(inventory.get("parts") or {}).items()
                }
                outcome.features = {
                    str(k): [str(n) for n in v]
                    for k, v in dict(inventory.get("features") or {}).items()
                }

    why = (
        f"geometry worker {failure} before this probe completed"
        if failure
        else "geometry worker exited without running this probe"
    )
    for op in ops:
        key = str(op["key"])
        have = list(rows_by_key.get(key, []))
        # A planned op is one probe, one row: a row that landed before the kill
        # already answered it. Other op kinds get their missing rows made
        # visible even when part of the op landed.
        if key not in completed and not (op["kind"] == "planned_probe" and have):
            have.extend(_missing_op_rows(op, why))
        outcome.rows_by_key[key] = have
        outcome.rows.extend(have)
    return outcome


# --- child side ---------------------------------------------------------------


def _child_emit(fh: Any, record: dict[str, Any]) -> None:
    fh.write(json.dumps(record) + "\n")
    fh.flush()


def main() -> None:
    request = json.loads(Path(sys.argv[1]).read_text())
    out_dir = Path(request["out_dir"])
    # Imports inside main: the module must stay importable (for _worker_argv)
    # without pulling the geometry stack into the parent.
    from .probe_catalog import ValidatedProbe, _execute_one  # noqa: PLC0415
    from .probes import (  # noqa: PLC0415
        GeometryStore,
        ProbeRunner,
        failed,
        run_cross_pairs,
        run_file_battery,
        run_golden_compares,
        run_intra_pairs,
    )
    from .render import RenderBudget, RenderedView, render_default_set  # noqa: PLC0415

    store = GeometryStore.from_file_table(Path(request["root"]), request["files"])
    runner = ProbeRunner(
        total_budget_s=float(request["total_budget_s"]),
        per_probe_timeout_s=float(request["per_probe_timeout_s"]),
    )
    render_budget = RenderBudget(int(request["render_budget"]))
    view_index = 0

    with (out_dir / "results.jsonl").open("w") as out:
        # Load everything once up front (cached for every op) and report the
        # part inventory the parent's planner will validate against.
        parts: dict[str, list[str]] = {}
        features: dict[str, list[str]] = {}
        for path in store.paths():
            entry = store.loaded(path)
            parts[path] = [name for name, _ in entry.solids]
            features[path] = [name for name, _ in entry.feature_shapes]
        _child_emit(out, {"inventory": {"parts": parts, "features": features}})

        def _emit_views(views: list[RenderedView]) -> None:
            nonlocal view_index
            for view in views:
                file_name = f"view_{view_index:03d}.png"
                view_index += 1
                (out_dir / file_name).write_bytes(view.png_bytes)
                _child_emit(out, {"view": {"label": view.label, "file": file_name}})

        for op in request["ops"]:
            kind, key = str(op["kind"]), str(op["key"])
            try:
                if kind == "file_battery":
                    rows = run_file_battery(store, runner, str(op["path"]))
                elif kind == "intra_pairs":
                    rows = run_intra_pairs(
                        store, runner, str(op["path"]), int(op["max_pairs"])
                    )
                elif kind == "cross_pairs":
                    rows = run_cross_pairs(store, runner, int(op["max_pairs"]))
                elif kind == "golden":
                    rows = run_golden_compares(
                        store, runner, str(op["path"]), int(op["max_comparisons"])
                    )
                elif kind == "default_renders":
                    _emit_views(
                        render_default_set(
                            store,
                            str(request["render_mode"]),
                            render_budget,
                            paths=[str(op["path"])],
                        )
                    )
                    rows = []
                elif kind == "planned_probe":
                    probe = ValidatedProbe(
                        probe_id=str(op["probe_id"]),
                        raw_params=dict(op.get("raw_params") or {}),
                        resolved=dict(op.get("resolved") or {}),
                    )
                    planned_views: list[RenderedView] = []
                    row = _execute_one(
                        probe, store, runner, render_budget, planned_views
                    )
                    _emit_views(planned_views)
                    rows = [row]
                else:
                    rows = [failed(kind, str(op.get("path", "*")), "unknown worker op")]
            except Exception as e:  # one op's failure must stay one visible row
                rows = [
                    failed(
                        str(op.get("probe_id", kind)),
                        str(op.get("path", "*")),
                        f"{type(e).__name__}: {e}",
                    )
                ]
            for row in rows:
                _child_emit(out, {"key": key, "row": row.as_row()})
            _child_emit(out, {"op_done": key})
    runner.close()


if __name__ == "__main__":
    main()

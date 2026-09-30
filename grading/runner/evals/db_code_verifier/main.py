"""DB code verifier — sandboxed code execution with streaming database access.

Extends the llm_code_verifier pattern with ``ctx.query_db()`` backed by a
SQLite database streamed directly from the snapshot zip.  The full SQL dump
is *never* loaded into memory as a single string; the chunked streaming parser
feeds rows into an on-disk SQLite file, optionally filtered to only the
tables referenced in the verifier code.

SECURITY MODEL: identical to llm_code_verifier (AST gate + subprocess sandbox).
The SQLite file is opened read-only (``?mode=ro``) by the subprocess.
"""

from __future__ import annotations

import asyncio
import functools
import io
import json
import os
import shutil
import sys
import tempfile
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any

from loguru import logger

from runner.evals.code_execution.main import (
    _SANDBOX_GID,
    _SANDBOX_UID,
    _build_sandbox_env,
    _prepare_sandbox_fs,
)
from runner.evals.db_code_verifier.config import (
    DB_CODE_VERIFIER_DEFAULT_TIMEOUT_S,
    DB_CODE_VERIFIER_MAX_CODE_LENGTH,
    DB_CODE_VERIFIER_MAX_TIMEOUT_S,
    resolve_db_allowed_imports,
)
from runner.evals.llm_code_verifier.ast_gate import check_code
from runner.evals.llm_code_verifier.config import CTX_ALLOWED_SUBDIRS
from runner.evals.llm_code_verifier.main import (
    _apply_sandbox_rlimits,
    _coerce_positive_int,
    _parse_verdict,
)
from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.helpers.snapshot_dbs.streaming import (
    extract_baseline_flag_from_code,
    extract_declared_tables_from_code,
    load_snapshot_tables,
    resolve_table_filter,
)
from runner.models import VerifierResult, VerifierResultStatus
from runner.utils.trajectory import resolve_lazy_content
from runner.utils.ungradeable import ungradeable_result

_MODULE_DIR = Path(__file__).parent
_SNAPSHOT_MOUNT = "/"


def _build_trajectory_payload(input: EvalImplInput) -> dict[str, Any]:
    """Build a JSON-safe trajectory payload for the sandbox subprocess.

    Uses ``model_dump(mode="json")`` to fully resolve Pydantic internals
    (e.g. ``ValidatorIterator``) that ``model_dump()`` alone may leave
    in union-typed message content fields.
    """
    helpers = input.helper_results or {}
    final_answer = helpers.get(HelperIds.FINAL_ANSWER)
    if not isinstance(final_answer, str):
        final_answer = ""
    messages = []
    for m in getattr(input.trajectory, "messages", []) or []:
        if hasattr(m, "model_dump"):
            messages.append(m.model_dump(mode="json"))
        else:
            messages.append(m)
    return {
        "final_answer": final_answer,
        "status": str(getattr(input.trajectory, "status", "") or ""),
        "messages": messages,
    }


def _resolve_timeout(
    verifier_values: dict[str, Any],
    eval_config_values: dict[str, Any],
) -> int:
    world_max = _coerce_positive_int(
        eval_config_values.get("max_timeout_s"), DB_CODE_VERIFIER_MAX_TIMEOUT_S
    )
    world_default = _coerce_positive_int(
        eval_config_values.get("default_timeout_s"),
        DB_CODE_VERIFIER_DEFAULT_TIMEOUT_S,
    )
    requested = verifier_values.get("timeout_s")
    if requested is None:
        return min(world_default, world_max)
    chosen = _coerce_positive_int(requested, world_default)
    return min(chosen, world_max)


def _sandbox_rlimits_for(timeout_s: int) -> Callable[[], None]:
    # The wall clock is the binding limit; RLIMIT_CPU only backstops it.
    return functools.partial(_apply_sandbox_rlimits, cpu_seconds=timeout_s + 10)


def _stage_sandbox(
    sandbox_root: Path,
    code: str,
    trajectory_payload: str,
    db_path: str | None,
    baseline_db_path: str | None = None,
) -> None:
    """Write the files the subprocess needs into ``sandbox_root``."""
    (sandbox_root / "user_code.py").write_text(code, encoding="utf-8")
    (sandbox_root / "trajectory.json").write_text(trajectory_payload, encoding="utf-8")
    if db_path:
        db_config: dict[str, Any] = {"db_path": db_path}
        if baseline_db_path:
            db_config["baseline_db_path"] = baseline_db_path
        (sandbox_root / "db_config.json").write_text(
            json.dumps(db_config), encoding="utf-8"
        )
    shutil.copy(_MODULE_DIR / "runner_shim.py", sandbox_root / "runner_shim.py")
    shutil.copy(_MODULE_DIR / "snapshot_ctx.py", sandbox_root / "snapshot_ctx.py")


# ctx / stdlib references that mean the verifier actually reads the DB. Checked
# as raw substrings (not comment-stripped): over-matching merely loads a DB the
# code never queries, while under-matching would skip a DB the code needs.
_DB_API_MARKERS: tuple[str, ...] = (
    "query_db",
    "list_tables",
    "has_table",
    "table_columns",
    "table_row_count",
    "sqlite3",
)


def _code_uses_db(code: str) -> bool:
    """True when the verifier code references any DB API (or the baseline flag)."""
    return any(marker in code for marker in _DB_API_MARKERS) or (
        extract_baseline_flag_from_code(code)
    )


def _resolve_verifier_score(verdict: dict[str, Any]) -> float:
    """Resolve the VerifierResult score from a ``check(ctx)`` verdict.

    A verifier may emit an explicit continuous ``score`` in [0, 1] (e.g. an
    objective metric such as MCC computed over the app DB) to set the score
    directly. When absent, the score falls back to the binary pass/fail mapping
    (1.0 if ``passed`` else 0.0), preserving existing behavior.
    """
    raw = verdict.get("score")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return max(0.0, min(1.0, float(raw)))
    return 1.0 if bool(verdict.get("passed")) else 0.0


def _load_tables(
    snapshot: IO[bytes] | None,
    db_path: str,
    table_filter: set[str] | None,
    log_prefix: str = "[DB_CODE_VERIFIER]",
) -> list[str]:
    if snapshot is None:
        return []
    if isinstance(snapshot, tempfile.SpooledTemporaryFile):
        # fileno() rolls over a spool and can move a sibling reader's cursor.
        snapshot = snapshot._file
    if isinstance(snapshot, io.BytesIO):
        independent = io.BytesIO(snapshot.getvalue())
    else:
        # Reopening a descriptor creates an independent offset; dup() does not.
        proc_path = f"/proc/self/fd/{snapshot.fileno()}"
        try:
            independent = open(proc_path, "rb")
        except OSError as exc:
            raise RuntimeError(
                f"cannot reopen the snapshot descriptor via {proc_path}: {exc}. "
                "Independent readers need procfs; is /proc mounted?"
            ) from exc
    with independent:
        return load_snapshot_tables(
            independent, db_path, table_filter, log_prefix=log_prefix
        )


def _prepare_with_trajectory(
    input: EvalImplInput,
    code: str,
    sandbox_root: Path,
    final_filesystem_root: Path,
) -> int | VerifierResult:
    return _prepare_evidence(
        input,
        code,
        sandbox_root,
        final_filesystem_root,
        json.dumps(_build_trajectory_payload(input), default=str),
    )


def _prepare_evidence(
    input: EvalImplInput,
    code: str,
    sandbox_root: Path,
    final_filesystem_root: Path,
    trajectory_payload: str,
) -> int | VerifierResult:
    verifier_id = input.verifier.verifier_id
    verifier_values = input.verifier.verifier_values or {}
    eval_config_values = input.eval_config.eval_config_values or {}

    # Legacy mode reads the per-world ``allowed_imports`` field; new mode
    # (allow_legacy_fields=False) uses the canonical blessed set. The blessed
    # set is a superset of the legacy menu, so new mode never narrows an
    # existing world's allowlist.
    allowed_imports = resolve_db_allowed_imports(eval_config_values)
    # Add sqlite3 to allowed imports — verifier code may import it for
    # advanced usage, though ctx.query_db() is the primary interface.
    if "sqlite3" not in allowed_imports:
        allowed_imports = [*allowed_imports, "sqlite3"]

    gate = check_code(
        code, allowed_imports, max_length=DB_CODE_VERIFIER_MAX_CODE_LENGTH
    )
    if not gate.ok:
        logger.warning(
            f"[DB_CODE_VERIFIER] Gate rejected verifier {verifier_id}: {gate.violations}"
        )
        detail = "; ".join(gate.violations)
        return ungradeable_result(
            input,
            f"AST gate rejected verifier code: {detail}",
            cause="config_error",
            values={
                "details": detail,
                "gate_violations": gate.violations,
                "stdout": "",
                "stderr": "",
            },
        )

    timeout_s = _resolve_timeout(verifier_values, eval_config_values)

    # --- Stream SQL dump into on-disk SQLite, filtered to relevant tables ---
    db_path: str | None = None
    db_tmpfile: str | None = None
    baseline_db_path: str | None = None
    baseline_tmpfile: str | None = None

    if not _code_uses_db(code):
        # Trajectory/filesystem-only verifier: no DB API is ever called, so
        # skip snapshot DB loading — and with it the empty-snapshot ERROR
        # guard, which must never fail a check that can't touch the DB.
        table_filter: set[str] = set()
        logger.info(
            "[DB_CODE_VERIFIER] Code references no DB APIs; skipping snapshot DB load"
        )
    else:
        table_filter = resolve_table_filter(code)
        if table_filter:
            logger.info(
                f"[DB_CODE_VERIFIER] Resolved table filter from code: {sorted(table_filter)}"
            )

        fd, db_tmpfile = tempfile.mkstemp(
            suffix=".db", prefix="dbvrf_", dir=sandbox_root
        )
        os.close(fd)

        # Load referenced tables across all app DBs/dumps/CSVs (multi-app fix:
        # a verifier's tables can live in a non-largest app DB).
        tables = _load_tables(
            input.final_snapshot_bytes,
            db_tmpfile,
            table_filter if table_filter else None,
        )

        if tables:
            db_path = db_tmpfile
            logger.info(f"[DB_CODE_VERIFIER] Loaded tables: {tables} into {db_path}")
        elif (
            table_filter
            # Retry ONLY when nothing authoritative named a table: a declared
            # `# DB_TABLES:` entry that does not load IS a broken capture. The
            # regex half reads prose as SQL ("...from the inbox" -> `the`), and a
            # runtime-resolved table leaves it nothing else, so the phantom
            # becomes the WHOLE filter and errors a healthy snapshot.
            and not extract_declared_tables_from_code(code)
            and (retried := _load_tables(input.final_snapshot_bytes, db_tmpfile, None))
        ):
            # `extract_db_from_snapshot` truncates db_path, so this reloaded.
            db_path = db_tmpfile
            logger.warning(
                f"[DB_CODE_VERIFIER] Verifier {verifier_id} filter "
                f"{sorted(table_filter)} matched no table; loaded the whole "
                f"snapshot instead ({len(retried)} tables)."
            )
            # Cleared so the baseline load below is unfiltered too: left in place
            # it would yield no baseline, and a baseline-comparison verifier would
            # then pass vacuously on the same phantom.
            table_filter = set()
        elif table_filter:
            # The verifier references DB tables (via the ``# DB_TABLES:`` header or
            # SQL in the code) and the snapshot yielded none even unfiltered — e.g.
            # an empty/stale
            # ``database_dump.sql`` (a known failure mode when the snapshot can't see
            # the live server's un-checkpointed WAL). Do NOT silently grade against
            # no DB: that emits a false ``fail``. Surface it as an ERROR so the
            # broken capture is visible instead of corrupting grades.
            try:
                os.unlink(db_tmpfile)
            except OSError:
                pass
            logger.warning(
                f"[DB_CODE_VERIFIER] Verifier {verifier_id} references tables "
                f"{sorted(table_filter)} but the snapshot loaded none — returning ERROR"
            )
            return ungradeable_result(
                input,
                (
                    "snapshot DB empty/unavailable: verifier references "
                    f"{sorted(table_filter)} but no tables loaded from the snapshot"
                ),
                cause="all_databases_failed_to_load",
                values={
                    "details": (
                        "No tables loaded from the snapshot dump/CSVs despite the "
                        f"verifier referencing {sorted(table_filter)}. Either the "
                        "snapshot captured an empty database, or its dump/CSV "
                        "files failed to parse (see container logs for loader "
                        "warnings)."
                    ),
                    "stdout": "",
                    "stderr": "",
                },
            )
        else:
            logger.info(
                "[DB_CODE_VERIFIER] No SQL dump or CSV found; running without DB"
            )

    # --- Optionally load the baseline (initial/post-populate) snapshot DB ---
    if db_path and extract_baseline_flag_from_code(code):
        fd, baseline_tmpfile = tempfile.mkstemp(
            suffix=".db", prefix="dbvrf_base_", dir=sandbox_root
        )
        os.close(fd)
        baseline_tables = _load_tables(
            input.initial_snapshot_bytes,
            baseline_tmpfile,
            table_filter if table_filter else None,
            log_prefix="[DB_CODE_VERIFIER][baseline]",
        )
        if baseline_tables:
            baseline_db_path = baseline_tmpfile
            logger.info(
                f"[DB_CODE_VERIFIER] Loaded baseline tables: {baseline_tables} "
                f"into {baseline_db_path}"
            )
        else:
            # The code demands a baseline (``# DB_BASELINE: true``) that isn't
            # there. Grading against a missing baseline would silently turn
            # every comparison into garbage — fail loud, like the final-snapshot
            # guard above.
            for tmp in (db_tmpfile, baseline_tmpfile):
                if tmp:
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass
            logger.warning(
                f"[DB_CODE_VERIFIER] Verifier {verifier_id} sets DB_BASELINE but "
                "no tables loaded from the initial snapshot — returning ERROR"
            )
            return ungradeable_result(
                input,
                (
                    "baseline DB empty/unavailable: verifier sets DB_BASELINE "
                    "but no tables loaded from the initial snapshot"
                ),
                cause="all_databases_failed_to_load",
                values={
                    "details": (
                        "The verifier opted into baseline DB access via "
                        "'# DB_BASELINE: true' but the initial snapshot yielded "
                        "no tables. Either the world does not capture a usable "
                        "baseline for this trajectory, or the baseline dump "
                        "failed to parse (see container logs)."
                    ),
                    "stdout": "",
                    "stderr": "",
                },
            )

    _stage_sandbox(sandbox_root, code, trajectory_payload, db_path, baseline_db_path)
    sandbox_home = sandbox_root / "tmp"
    _prepare_sandbox_fs(
        sandbox_root, sandbox_root / "runner_shim.py", sandbox_home=sandbox_home
    )
    for subdir in CTX_ALLOWED_SUBDIRS:
        snapshot_subdir = final_filesystem_root / subdir
        if snapshot_subdir.is_dir():
            if not snapshot_subdir.resolve().is_relative_to(final_filesystem_root):
                raise ValueError(f"snapshot subtree escapes evidence root: {subdir}")
            _prepare_sandbox_fs(
                snapshot_subdir,
                sandbox_root / "runner_shim.py",
                sandbox_home=sandbox_home,
            )

    for readable in (db_path, baseline_db_path):
        if readable:
            Path(readable).chmod(0o644)
    return timeout_s


async def _cleanup(
    sandbox_root: Path,
    preparation: asyncio.Task[int | VerifierResult] | None,
    launch: asyncio.Task[asyncio.subprocess.Process] | None,
) -> None:
    try:
        if preparation is not None:
            await asyncio.gather(preparation, return_exceptions=True)
        if launch is not None:
            results = await asyncio.gather(launch, return_exceptions=True)
            process = results[0]
            if isinstance(process, asyncio.subprocess.Process):
                if process.returncode is None:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
                await process.communicate()
    finally:
        try:
            await asyncio.to_thread(shutil.rmtree, sandbox_root)
        except OSError:
            logger.warning(
                f"[DB_CODE_VERIFIER] Failed to remove scratch {sandbox_root}"
            )


async def run_from_evidence(
    input: EvalImplInput,
    *,
    final_filesystem_root: Path,
    owned_scratch_root: Path,
    max_output_bytes: int | None = None,
) -> VerifierResult:
    """Run inside a privileged grading worker with caller-owned evidence.

    ``input`` carries the initial/final archive streams, config and trajectory.
    Roots must exist; the filesystem root contains the usual ``filesystem``
    and ``.apps_data`` subtrees. Only a unique child of scratch is removed.
    Cancellation waits for preparation and reaps the child before cleanup.
    """
    verifier_id = input.verifier.verifier_id
    verifier_version = input.verifier.verifier_version
    code = input.verifier.verifier_values.get("code")
    if not code:
        raise ValueError(
            "db_code_verifier requires verifier_values['code'] (the def check(ctx) source)"
        )

    sandbox_root: Path | None = None
    preparation: asyncio.Task[int | VerifierResult] | None = None
    launch: asyncio.Task[asyncio.subprocess.Process] | None = None
    try:
        final_filesystem_root = final_filesystem_root.resolve(strict=True)
        owned_scratch_root = owned_scratch_root.resolve(strict=True)
        if not final_filesystem_root.is_dir() or not owned_scratch_root.is_dir():
            raise ValueError("evidence and scratch roots must be directories")
        sandbox_root = Path(tempfile.mkdtemp(prefix="dbvrf_", dir=owned_scratch_root))
        for message in input.trajectory.messages:
            resolve_lazy_content(message)
        preparation = asyncio.create_task(
            asyncio.to_thread(
                _prepare_with_trajectory,
                input,
                code,
                sandbox_root,
                final_filesystem_root,
            )
        )
        prepared = await asyncio.shield(preparation)
        if isinstance(prepared, VerifierResult):
            return prepared
        timeout_s = prepared

        env = _build_sandbox_env(sandbox_root)
        env["CODE_RUNNER_SNAPSHOT_DIR"] = str(final_filesystem_root)
        env["CODE_RUNNER_ALLOWED_SUBDIRS"] = ",".join(CTX_ALLOWED_SUBDIRS)
        env["TMPDIR"] = str(sandbox_root / "tmp")
        env["HOME"] = env["TMPDIR"]
        env["OPENBLAS_NUM_THREADS"] = "1"
        env["OMP_NUM_THREADS"] = "1"

        logger.info(
            f"[DB_CODE_VERIFIER] Spawning sandbox for verifier {verifier_id} "
            f"(timeout={timeout_s}s)"
        )
        launch = asyncio.create_task(
            asyncio.create_subprocess_exec(
                sys.executable,
                "runner_shim.py",
                cwd=str(sandbox_root),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                user=_SANDBOX_UID,
                group=_SANDBOX_GID,
                preexec_fn=_sandbox_rlimits_for(timeout_s),
            )
        )
        process = await asyncio.shield(launch)
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate()
                if max_output_bytes is None
                else _bounded_output(process, max_output_bytes),
                timeout=timeout_s,
            )
        except _OutputBudgetExceeded as exc:
            return ungradeable_result(
                input,
                str(exc),
                cause="no_verdict",
                values={"details": str(exc), "stdout": "", "stderr": ""},
            )
        except TimeoutError:
            return ungradeable_result(
                input,
                f"verifier exceeded timeout of {timeout_s}s",
                cause="no_verdict",
                values={
                    "details": f"timeout after {timeout_s}s",
                    "stdout": "",
                    "stderr": "",
                },
            )

        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")

        verdict = _parse_verdict(stdout_text)
        if verdict is None:
            return ungradeable_result(
                input,
                f"verifier produced no parseable verdict (exit {process.returncode})",
                cause="no_verdict",
                values={
                    "details": "no JSON verdict on stdout",
                    "stdout": stdout_text[-4000:],
                    "stderr": stderr_text[-4000:],
                },
            )

        passed = bool(verdict.get("passed"))
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=_resolve_verifier_score(verdict),
            status=VerifierResultStatus.OK,
            message="pass" if passed else "fail",
            verifier_result_values={
                "passed": passed,
                "details": str(verdict.get("details", "")),
                "metrics": verdict.get("metrics") or {},
                "stdout": stdout_text[-4000:],
                "stderr": stderr_text[-4000:],
            },
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception(
            f"[DB_CODE_VERIFIER] Verifier {verifier_id} failed unexpectedly"
        )
        return ungradeable_result(
            input,
            f"unexpected error: {type(exc).__name__}: {exc}",
            cause="no_verdict",
            values={
                "details": traceback.format_exc(),
                "stdout": "",
                "stderr": "",
            },
        )
    finally:
        if sandbox_root is not None:
            cleanup = asyncio.create_task(_cleanup(sandbox_root, preparation, launch))
            cancelled = False
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    cancelled = True
            cleanup.result()
            if cancelled:
                raise asyncio.CancelledError


class _OutputBudgetExceeded(ValueError):
    pass


async def _bounded_output(
    process: asyncio.subprocess.Process, maximum: int
) -> tuple[bytes, bytes]:
    if maximum <= 0 or process.stdout is None or process.stderr is None:
        raise ValueError("invalid subprocess output budget")
    remaining = maximum
    exceeded = False

    async def read(stream: asyncio.StreamReader) -> bytes:
        nonlocal remaining, exceeded
        chunks: list[bytes] = []
        while chunk := await stream.read(65536):
            if exceeded:
                continue
            remaining -= len(chunk)
            if remaining < 0:
                exceeded = True
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                chunks.clear()
                continue
            chunks.append(chunk)
        return b"".join(chunks)

    async with asyncio.TaskGroup() as tasks:
        stdout = tasks.create_task(read(process.stdout))
        stderr = tasks.create_task(read(process.stderr))
        tasks.create_task(process.wait())
    if exceeded:
        raise _OutputBudgetExceeded(
            f"verifier output exceeds budget of {maximum} bytes (stdout + stderr)"
        )
    return stdout.result(), stderr.result()


async def db_code_verifier_eval(input: EvalImplInput) -> VerifierResult:
    """Evaluate a verifier against the production snapshot mount."""
    return await run_from_evidence(
        input,
        final_filesystem_root=Path(_SNAPSHOT_MOUNT),
        owned_scratch_root=Path(tempfile.gettempdir()),
    )

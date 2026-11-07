"""Test-run executor for db_code_verifier.

Mirrors the llm_code_verifier test-run path but additionally loads a SQL dump
from golden files into a temporary SQLite database and passes ``db_path`` to
the sandbox via ``db_config.json``, so ``ctx.query_db()`` works at test-run
time exactly as it does at grading time.

Called from a Modal function on the grader app and invoked by rl-studio-server's
``db_code_verifier_test_run`` Temporal activity.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import shutil
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

from loguru import logger

from runner.evals.code_execution.main import (
    _SANDBOX_GID,
    _SANDBOX_UID,
    _build_sandbox_env,
    _prepare_sandbox_fs,
)
from runner.evals.db_code_verifier.config import (
    DB_CODE_VERIFIER_MAX_CODE_LENGTH,
    DB_CODE_VERIFIER_TEST_RUN_MAX_TIMEOUT_S,
    resolve_db_allowed_imports,
)
from runner.evals.db_code_verifier.main import _resolve_timeout, _sandbox_rlimits_for
from runner.evals.llm_code_verifier.ast_gate import check_code
from runner.evals.llm_code_verifier.main import _parse_verdict
from runner.evals.llm_code_verifier.test_run import (
    LlmCodeVerifierTestRunExecutorResult,
    LlmCodeVerifierTestRunOutcome,
    _stage_golden_filesystem,
)
from runner.helpers.snapshot_dbs.streaming import (
    extract_baseline_flag_from_code,
    load_ddl_to_sqlite,
    load_snapshot_tables,
    resolve_table_filter,
)

_MODULE_DIR = Path(__file__).parent
_SNAPSHOT_MOUNT = "/filesystem"


def _remaining_budget(timeout_s: int, deadline: float | None) -> int:
    if deadline is None:
        return timeout_s
    return min(timeout_s, int(deadline - time.monotonic()))


async def execute_db_code_verifier_test_run(
    code: str,
    golden_text: str,
    golden_files: dict[str, bytes],
    golden_zip_bytes: io.BytesIO | None,
    eval_config_values: dict[str, Any],
    verifier_values: dict[str, Any] | None = None,
    db_schema_ddl: str | None = None,
    deadline: float | None = None,
) -> LlmCodeVerifierTestRunExecutorResult:
    """Run a candidate db_code_verifier against the supplied golden output.

    ``deadline`` is a ``time.monotonic()`` instant the caller must return by;
    the sandbox budget is cut to whatever remains once setup is done.

    ``golden_zip_bytes`` is the raw zip BytesIO of the golden response
    snapshot. It is passed to ``load_sql_dump_to_sqlite_streaming`` to
    populate the SQLite database. ``golden_files`` is used for the
    filesystem snapshot (same as the LLM path).

    When there is no golden snapshot (``golden_zip_bytes is None``) but a
    ``db_schema_ddl`` is supplied, an empty schema-correct SQLite DB is
    built from the DDL so the verifier can still be smoke-tested for
    no-such-table / no-such-column / runtime errors. A clean run in this
    mode is reported as ``OK_RAN_NO_GOLDEN`` (the pass/fail value is not
    authoritative without a golden to compare against).
    """
    verifier_values = verifier_values or {}
    started = time.monotonic()

    # Mirror the grading-time allowlist resolution: legacy reads the
    # per-world allowed_imports field; new mode uses the canonical blessed set.
    allowed_imports = resolve_db_allowed_imports(eval_config_values)
    # db_code_verifier always allows sqlite3 — verifier code may import it.
    if "sqlite3" not in allowed_imports:
        allowed_imports = [*allowed_imports, "sqlite3"]

    gate = check_code(
        code, allowed_imports, max_length=DB_CODE_VERIFIER_MAX_CODE_LENGTH
    )
    if not gate.ok:
        return LlmCodeVerifierTestRunExecutorResult(
            outcome=LlmCodeVerifierTestRunOutcome.GATE_VIOLATION,
            duration_ms=int((time.monotonic() - started) * 1000),
            gate_violations=list(gate.violations),
            error_message="; ".join(gate.violations),
        )

    timeout_s = min(
        _resolve_timeout(verifier_values, eval_config_values),
        DB_CODE_VERIFIER_TEST_RUN_MAX_TIMEOUT_S,
    )

    sandbox_root = Path(tempfile.mkdtemp(prefix="db_test_run_"))
    snapshot_root = Path(tempfile.mkdtemp(prefix="db_test_run_snap_"))
    db_tmpfile: str | None = None
    process: asyncio.subprocess.Process | None = None

    try:
        _stage_golden_filesystem(snapshot_root, golden_files)

        # --- Stream SQL dump from golden zip into SQLite ---
        db_path: str | None = None
        if golden_zip_bytes is not None:
            table_filter = resolve_table_filter(code)
            if table_filter:
                logger.info(
                    f"[DB_CODE_VERIFIER_TEST_RUN] Table filter from code: {sorted(table_filter)}"
                )
            fd, db_tmpfile = tempfile.mkstemp(suffix=".db", prefix="db_test_run_")
            os.close(fd)

            # Mirror grading: load referenced tables across all app DBs/dumps/CSVs.
            tables = load_snapshot_tables(
                golden_zip_bytes,
                db_tmpfile,
                table_filter if table_filter else None,
                log_prefix="[DB_CODE_VERIFIER_TEST_RUN]",
            )

            if tables:
                db_path = db_tmpfile
                logger.info(
                    f"[DB_CODE_VERIFIER_TEST_RUN] Loaded tables: {tables} into {db_path}"
                )
            else:
                logger.info(
                    "[DB_CODE_VERIFIER_TEST_RUN] No SQL dump or CSV in golden files; running without DB"
                )
        elif db_schema_ddl:
            # No golden snapshot — build an empty schema-correct DB from the
            # app's DDL so the verifier can be smoke-tested against correctly
            # named (but empty) tables.
            fd, db_tmpfile = tempfile.mkstemp(suffix=".db", prefix="db_test_run_")
            os.close(fd)
            try:
                tables = load_ddl_to_sqlite(db_schema_ddl, db_tmpfile)
            except Exception as exc:
                logger.warning(
                    f"[DB_CODE_VERIFIER_TEST_RUN] Failed to build DB from DDL: {exc}"
                )
                tables = []
            if tables:
                db_path = db_tmpfile
                logger.info(
                    f"[DB_CODE_VERIFIER_TEST_RUN] Built schema-only DB "
                    f"({len(tables)} tables) from DDL: {db_path}"
                )
            else:
                # A schema was supplied but no tables loaded from it. Don't
                # fall through to a DB-less run (which would crash later with a
                # misleading "no SQL dump" message, or pass spuriously if the
                # verifier never queries) — surface the misconfiguration.
                logger.warning(
                    "[DB_CODE_VERIFIER_TEST_RUN] schema configured but 0 tables "
                    "parsed from DDL"
                )
                return LlmCodeVerifierTestRunExecutorResult(
                    outcome=LlmCodeVerifierTestRunOutcome.CRASH,
                    duration_ms=int((time.monotonic() - started) * 1000),
                    error_message=(
                        "App database schema is configured but no tables could "
                        "be loaded from it — the schema_file may be empty, "
                        "malformed, or written in a non-SQLite dialect. Fix the "
                        "schema_file in the app's arco.toml."
                    ),
                )

        # Stage sandbox files
        (sandbox_root / "user_code.py").write_text(code, encoding="utf-8")
        trajectory_payload = {
            "final_answer": golden_text or "",
            "status": "completed",
            "messages": [],
        }
        (sandbox_root / "trajectory.json").write_text(
            json.dumps(trajectory_payload, default=str), encoding="utf-8"
        )
        if db_path:
            db_config: dict[str, Any] = {"db_path": db_path}
            # Test runs have no initial snapshot; when the code opts into
            # baseline access, attach the same DB as ``baseline`` so
            # baseline-comparing queries execute (a dry-run sees "no changes"
            # rather than crashing on a missing schema).
            if extract_baseline_flag_from_code(code):
                db_config["baseline_db_path"] = db_path
            (sandbox_root / "db_config.json").write_text(
                json.dumps(db_config), encoding="utf-8"
            )
        # Use db_code_verifier's runner_shim and snapshot_ctx (with DB support)
        shutil.copy(_MODULE_DIR / "runner_shim.py", sandbox_root / "runner_shim.py")
        shutil.copy(_MODULE_DIR / "snapshot_ctx.py", sandbox_root / "snapshot_ctx.py")

        _prepare_sandbox_fs(sandbox_root, sandbox_root / "runner_shim.py")
        _prepare_sandbox_fs(snapshot_root, sandbox_root / "runner_shim.py")

        # Grant the unprivileged subprocess read access to the SQLite file
        if db_path:
            Path(db_path).chmod(0o644)

        env = _build_sandbox_env(sandbox_root)
        env["CODE_RUNNER_SNAPSHOT_DIR"] = str(snapshot_root)

        timeout_s = _remaining_budget(timeout_s, deadline)
        if timeout_s <= 0:
            return LlmCodeVerifierTestRunExecutorResult(
                outcome=LlmCodeVerifierTestRunOutcome.TIMEOUT,
                duration_ms=int((time.monotonic() - started) * 1000),
                error_message="test run reached its deadline before the verifier could start",
            )

        logger.info(
            f"[DB_CODE_VERIFIER_TEST_RUN] Spawning sandbox "
            f"(timeout={timeout_s}s, db={'yes' if db_path else 'no'})"
        )
        process = await asyncio.create_subprocess_exec(
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

        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout_s
            )
        except TimeoutError:
            process.kill()
            await process.wait()
            return LlmCodeVerifierTestRunExecutorResult(
                outcome=LlmCodeVerifierTestRunOutcome.TIMEOUT,
                duration_ms=int((time.monotonic() - started) * 1000),
                error_message=f"verifier exceeded timeout of {timeout_s}s",
            )

        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")
        verdict = _parse_verdict(stdout_text)

        if verdict is None:
            return LlmCodeVerifierTestRunExecutorResult(
                outcome=LlmCodeVerifierTestRunOutcome.MALFORMED_RETURN,
                duration_ms=int((time.monotonic() - started) * 1000),
                stdout=stdout_text[-4000:],
                stderr=stderr_text[-4000:],
                error_message=(
                    "verifier produced no parseable verdict on stdout "
                    f"(exit {process.returncode})"
                ),
            )

        if verdict.get("traceback"):
            return LlmCodeVerifierTestRunExecutorResult(
                outcome=LlmCodeVerifierTestRunOutcome.CRASH,
                verdict=verdict,
                duration_ms=int((time.monotonic() - started) * 1000),
                stdout=stdout_text[-4000:],
                stderr=stderr_text[-4000:],
                error_message=str(verdict.get("details", "")),
            )

        passed = bool(verdict.get("passed"))
        if golden_zip_bytes is None:
            # No golden to compare against — a clean, well-formed run is the
            # success signal; the pass/fail value is informational only.
            outcome = LlmCodeVerifierTestRunOutcome.OK_RAN_NO_GOLDEN
        else:
            outcome = (
                LlmCodeVerifierTestRunOutcome.OK_PASSED_ON_GOLDEN
                if passed
                else LlmCodeVerifierTestRunOutcome.REJECTED_ON_GOLDEN
            )
        return LlmCodeVerifierTestRunExecutorResult(
            outcome=outcome,
            verdict=verdict,
            duration_ms=int((time.monotonic() - started) * 1000),
            stdout=stdout_text[-4000:],
            stderr=stderr_text[-4000:],
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[DB_CODE_VERIFIER_TEST_RUN] Unexpected failure")
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except Exception:  # noqa: BLE001
                pass
        return LlmCodeVerifierTestRunExecutorResult(
            outcome=LlmCodeVerifierTestRunOutcome.CRASH,
            duration_ms=int((time.monotonic() - started) * 1000),
            error_message=f"{type(exc).__name__}: {exc}",
            stderr=traceback.format_exc(),
        )
    finally:
        shutil.rmtree(sandbox_root, ignore_errors=True)
        shutil.rmtree(snapshot_root, ignore_errors=True)
        if db_tmpfile:
            try:
                os.unlink(db_tmpfile)
            except OSError:
                pass

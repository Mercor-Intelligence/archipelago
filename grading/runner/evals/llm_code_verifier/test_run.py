"""Test-run executor for llm_code_verifier.

Standalone entry point that runs a candidate `def check(ctx)` against a
synthetic snapshot built from a task's GoldenResponse — without needing a
real EvalImplInput / trajectory. Called from a Modal function on the
grader app (see ``archipelago/grading/llm_code_verifier_test_run_modal.py``) and invoked by
rl-studio-server's ``llm_code_verifier_test_run`` Temporal activity.

Reuses the production sandbox primitives (AST gate, uid 65534 subprocess,
env stripping, rlimits) so test-run semantics match grading-time semantics
exactly. Any divergence here is a bug — the whole point is "passes
test-run on golden ⇒ will pass at grading time on a matching trajectory."
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import tempfile
import time
import traceback
from enum import StrEnum
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import BaseModel

from runner.evals.code_execution.main import (
    _SANDBOX_GID,
    _SANDBOX_UID,
    _build_sandbox_env,
    _prepare_sandbox_fs,
)
from runner.evals.llm_code_verifier.ast_gate import check_code
from runner.evals.llm_code_verifier.config import CTX_ALLOWED_SUBDIRS
from runner.evals.llm_code_verifier.main import (
    _apply_sandbox_rlimits,
    _parse_verdict,
    _resolve_allowed_imports,
    _resolve_max_code_length,
    _resolve_timeout,
)

_MODULE_DIR = Path(__file__).parent


class LlmCodeVerifierTestRunOutcome(StrEnum):
    """The five classes of test-run result the frontend cares about. NO_GOLDEN
    is added by the server-side activity (this executor never sees it)."""

    OK_PASSED_ON_GOLDEN = "ok_passed_on_golden"
    REJECTED_ON_GOLDEN = "rejected_on_golden"
    # The verifier ran to completion against a schema-only (no-golden)
    # database — produced a well-formed verdict without crashing. Used by
    # db_code_verifier when there is no golden DB snapshot to assert against;
    # the pass/fail value is not authoritative, only that the code executes.
    OK_RAN_NO_GOLDEN = "ok_ran_no_golden"
    GATE_VIOLATION = "gate_violation"
    CRASH = "crash"
    TIMEOUT = "timeout"
    MALFORMED_RETURN = "malformed_return"


class LlmCodeVerifierTestRunExecutorResult(BaseModel):
    outcome: LlmCodeVerifierTestRunOutcome
    verdict: dict[str, Any] | None = None
    duration_ms: int = 0
    stdout: str = ""
    stderr: str = ""
    gate_violations: list[str] = []
    error_message: str = ""


def _stage_golden_filesystem(
    snapshot_root: Path, golden_files: dict[str, bytes]
) -> None:
    """Extract the dict of (relpath → bytes) verbatim into ``snapshot_root``.

    Each member's path is preserved as-is, so a golden zip with the
    standard layout (``filesystem/...`` for agent output,
    ``.apps_data/<svc>/data.db`` for per-app state) lands as two sibling
    trees under the per-call sandbox snapshot root. ctx is rooted at
    ``snapshot_root`` for test-run (production roots at ``/``), so the
    net paths a verifier sees are identical across both modes:
    ``ctx.exists("filesystem/result.txt")``,
    ``ctx.read_bytes(".apps_data/xero/data.db")``, etc.

    Each resolved target path is verified to stay inside ``snapshot_root``
    so a malicious zip entry containing ``../`` segments can't escape
    (defense in depth — golden uploads are already authenticated, but the
    cost of the check is trivial).
    """
    resolved_root = snapshot_root.resolve()
    for rel, contents in golden_files.items():
        if not rel:
            continue
        target = (snapshot_root / rel).resolve()
        try:
            target.relative_to(resolved_root)
        except ValueError:
            logger.warning(
                f"[LLM_CODE_VERIFIER_TEST_RUN] Skipping zip entry that escapes "
                f"snapshot root: {rel!r}"
            )
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)


async def execute_llm_code_verifier_test_run(
    code: str,
    golden_text: str,
    golden_files: dict[str, bytes],
    eval_config_values: dict[str, Any],
    verifier_values: dict[str, Any] | None = None,
    initial_files: dict[str, bytes] | None = None,
) -> LlmCodeVerifierTestRunExecutorResult:
    """Run the candidate verifier against the supplied golden output.

    ``golden_files`` is a dict of relative path → file contents that get
    extracted into the per-call sandbox as if they were the task's final
    snapshot. ``golden_text`` becomes ctx.final_answer in the sandbox.

    ``initial_files`` is the task's initial snapshot (``task_data_id``)
    extracted UNDER the golden — files the agent's environment booted
    into. Matches the overlay production grading uses via
    ``filesystem_setup_helper``: initial first, golden over the top, so
    a verifier that inspects an untouched task-input file behaves the
    same in test-run as it does at grading time. Pass ``None`` (or omit)
    for tasks with no initial snapshot.

    ``verifier_values`` mirrors the saved-verifier payload (notably
    ``timeout_s`` — annotators can request a shorter per-verifier timeout
    bounded by ``eval_config_values['max_timeout_s']``). Defaults to an
    empty dict, which yields the world-level default timeout — same as a
    verifier with no per-call override at grading time.
    """
    verifier_values = verifier_values or {}
    initial_files = initial_files or {}
    started = time.monotonic()

    allowed_imports = _resolve_allowed_imports(eval_config_values)
    max_length = _resolve_max_code_length(eval_config_values)
    gate = check_code(code, allowed_imports, max_length=max_length)
    if not gate.ok:
        return LlmCodeVerifierTestRunExecutorResult(
            outcome=LlmCodeVerifierTestRunOutcome.GATE_VIOLATION,
            duration_ms=int((time.monotonic() - started) * 1000),
            gate_violations=list(gate.violations),
            error_message="; ".join(gate.violations),
        )

    timeout_s = _resolve_timeout(verifier_values, eval_config_values)

    sandbox_root = Path(tempfile.mkdtemp(prefix="test_run_"))
    snapshot_root = Path(tempfile.mkdtemp(prefix="test_run_snap_"))
    process: asyncio.subprocess.Process | None = None
    try:
        # Layer initial first, then golden — same write-on-overlap order
        # filesystem_setup_helper uses at grading time. Untouched initial
        # files survive into the sandbox; golden files overwrite where
        # the agent would have modified them.
        _stage_golden_filesystem(snapshot_root, initial_files)
        _stage_golden_filesystem(snapshot_root, golden_files)

        # Stage the per-call sandbox with user code + shim + ctx + trajectory.
        (sandbox_root / "user_code.py").write_text(code, encoding="utf-8")
        trajectory_payload = {
            "final_answer": golden_text or "",
            "status": "completed",
            "messages": [],
        }
        (sandbox_root / "trajectory.json").write_text(
            json.dumps(trajectory_payload, default=str), encoding="utf-8"
        )
        shutil.copy(_MODULE_DIR / "runner_shim.py", sandbox_root / "runner_shim.py")
        shutil.copy(_MODULE_DIR / "snapshot_ctx.py", sandbox_root / "snapshot_ctx.py")

        _prepare_sandbox_fs(sandbox_root, sandbox_root / "runner_shim.py")
        # The golden filesystem also needs to be world-readable by uid 65534.
        # snapshot_root is small (golden output for one task) so the recursive
        # chmod is fine here — unlike production /filesystem which can be huge.
        _prepare_sandbox_fs(snapshot_root, sandbox_root / "runner_shim.py")

        env = _build_sandbox_env(sandbox_root)
        env["CODE_RUNNER_SNAPSHOT_DIR"] = str(snapshot_root)
        # Mirror production's ctx whitelist so test-run semantics stay in
        # lockstep with grading. The tempdir already isolates from the
        # host filesystem, but enforcing the same scope means a verifier
        # author can't accidentally write paths that work in test-run and
        # fail at grading (or vice-versa).
        env["CODE_RUNNER_ALLOWED_SUBDIRS"] = ",".join(CTX_ALLOWED_SUBDIRS)

        logger.info(
            f"[LLM_CODE_VERIFIER_TEST_RUN] Spawning sandbox (timeout={timeout_s}s)"
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
            preexec_fn=_apply_sandbox_rlimits,
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

        # The shim's _coerce_result wraps unhandled exceptions in user code as
        # {"passed": False, "details": "verifier raised: ...", "traceback": ...}.
        # Distinguish that from a deliberate "passed: False" return.
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
        return LlmCodeVerifierTestRunExecutorResult(
            outcome=(
                LlmCodeVerifierTestRunOutcome.OK_PASSED_ON_GOLDEN
                if passed
                else LlmCodeVerifierTestRunOutcome.REJECTED_ON_GOLDEN
            ),
            verdict=verdict,
            duration_ms=int((time.monotonic() - started) * 1000),
            stdout=stdout_text[-4000:],
            stderr=stderr_text[-4000:],
        )
    except Exception as exc:  # noqa: BLE001 — surface unexpected failures as CRASH
        logger.exception("[LLM_CODE_VERIFIER_TEST_RUN] Unexpected failure")
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

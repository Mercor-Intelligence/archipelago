"""Code runner verifier - executes a user-authored def check(ctx) against the snapshot.

This is the grading-time entrypoint. It does NOT call an LLM; the verifier code
was generated and reviewed at authoring time (in rl-studio/server) and lives in
``verifier_values["code"]``.

SECURITY MODEL (see also: ast_gate.py docstring)

  Layer 1 — AST gate (best-effort, NOT a sandbox).
    A pre-flight lint that rejects obvious LLM mistakes and common escape
    patterns. It cannot stop a determined attacker; Python's dynamic name
    binding makes that unsolvable at the AST level.

  Layer 2 — Subprocess privileges & filesystem (load-bearing).
    User code runs in a fresh subprocess with user/group dropped to nobody
    (uid 65534), env stripped to a small allowlist (no API keys, no DB URLs,
    no AWS creds), and cwd locked to a per-verifier scratch dir.

  Layer 3 — Kernel resource limits (load-bearing).
    ``preexec_fn=_apply_sandbox_rlimits`` caps memory, CPU time, open file
    descriptors, and child process count via ``resource.setrlimit``. This
    blocks fork bombs, OOM, and fd-exhaustion attacks even if Layer 1 was
    bypassed.

  Layer 4 — Wall-clock timeout (load-bearing).
    ``asyncio.wait_for`` kills the subprocess if it runs past timeout_s.

  Known gap (post-PR-1 work): the subprocess CAN still open network sockets
  because we don't apply a seccomp-bpf filter or unshare the network
  namespace. A determined attacker who bypasses the AST gate could reach
  cloud metadata endpoints. The grader image should run in a network
  namespace with no egress, and/or we should add ``pyseccomp`` to block
  socket(2). Tracked separately; documented here so it isn't forgotten.
"""

from __future__ import annotations

import asyncio
import json
import resource
import shutil
import sys
import tempfile
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
from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import AgentTrajectoryOutput, VerifierResult, VerifierResultStatus

from .ast_gate import check_code
from .config import (
    DEFAULT_TIMEOUT_S,
    MAX_CODE_LENGTH_CHARS,
    MAX_TIMEOUT_S,
    SAFE_DEFAULT_IMPORTS,
)

_MODULE_DIR = Path(__file__).parent
_SNAPSHOT_MOUNT = "/filesystem"

# Per-subprocess resource caps. Conservative defaults sized for a verifier
# that reads a snapshot folder and runs simple parsing/assertions. Override
# only with strong justification — the whole point of these is to contain a
# misbehaving or malicious verifier without depending on the AST gate.
_RLIMIT_MEMORY_BYTES = 512 * 1024 * 1024  # 512 MiB virtual memory cap
_RLIMIT_CPU_SECONDS = 120  # CPU seconds (wall-clock timeout is separate and shorter)
_RLIMIT_OPEN_FILES = 128
_RLIMIT_CHILD_PROCS = 32


def _apply_sandbox_rlimits() -> None:
    """preexec_fn run in the child between fork() and exec().

    Sets POSIX resource limits on the subprocess. Stdlib only, no extra deps.
    Failures are silently tolerated on platforms where a given limit isn't
    supported (Darwin lacks RLIMIT_NPROC for the user, for example) — the
    other layers still apply.
    """
    for limit, value in (
        (resource.RLIMIT_AS, _RLIMIT_MEMORY_BYTES),
        (resource.RLIMIT_CPU, _RLIMIT_CPU_SECONDS),
        (resource.RLIMIT_NOFILE, _RLIMIT_OPEN_FILES),
        (getattr(resource, "RLIMIT_NPROC", None), _RLIMIT_CHILD_PROCS),
    ):
        if limit is None:
            continue
        try:
            resource.setrlimit(limit, (value, value))
        except (ValueError, OSError):
            # Permission to lower may be revoked depending on the soft/hard
            # state inherited from the parent; we ran what we could.
            continue


def _coerce_positive_int(value: object, default: int) -> int:
    """Coerce ``value`` to a positive int, falling back to ``default``.

    The config dicts (``eval_config_values``, ``verifier_values``) are JSONB
    blobs the admin can hand-edit. Tolerate non-numeric or non-positive
    strings without crashing the grader — a bad row should fall back to a
    safe default, not raise.
    """
    if value is None:
        return default
    try:
        coerced = int(value)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError):
        return default
    return coerced if coerced > 0 else default


def _resolve_timeout(
    verifier_values: dict[str, Any], eval_config_values: dict[str, Any]
) -> int:
    """Resolve the per-verifier timeout against world-level bounds.

    Annotators can request a shorter timeout per verifier but cannot exceed
    ``max_timeout_s``. Missing or non-numeric values fall back to the world
    default; the world default itself is bounded by ``max_timeout_s``.
    """
    world_max = _coerce_positive_int(
        eval_config_values.get("max_timeout_s"), MAX_TIMEOUT_S
    )
    world_default = _coerce_positive_int(
        eval_config_values.get("default_timeout_s"), DEFAULT_TIMEOUT_S
    )
    requested = verifier_values.get("timeout_s")
    if requested is None:
        return min(world_default, world_max)
    chosen = _coerce_positive_int(requested, world_default)
    return min(chosen, world_max)


def _resolve_allowed_imports(eval_config_values: dict[str, Any]) -> list[str]:
    configured = eval_config_values.get("allowed_imports")
    if isinstance(configured, list) and configured:
        return [str(x) for x in configured]
    return list(SAFE_DEFAULT_IMPORTS)


def _resolve_max_code_length(eval_config_values: dict[str, Any]) -> int:
    """Honor the EvalConfig ``max_code_length_chars`` override at grading time."""
    return _coerce_positive_int(
        eval_config_values.get("max_code_length_chars"), MAX_CODE_LENGTH_CHARS
    )


def _build_trajectory_payload(
    trajectory: AgentTrajectoryOutput,
    helper_results: dict[HelperIds, Any] | None,
) -> dict[str, Any]:
    """Pure builder split out from _trajectory_payload for unit-testability.

    ``final_answer`` is sourced from the FINAL_ANSWER helper (canonical last
    message text). We do NOT serialize ``trajectory.output`` directly because
    it's typed ``dict[str, Any] | None`` and a raw str() on it produces a
    Python dict repr, not a useful string.
    """
    helpers = helper_results or {}
    final_answer = helpers.get(HelperIds.FINAL_ANSWER)
    if not isinstance(final_answer, str):
        final_answer = ""
    return {
        "final_answer": final_answer,
        "status": str(getattr(trajectory, "status", "") or ""),
        "messages": [
            m.model_dump() if hasattr(m, "model_dump") else m
            for m in (getattr(trajectory, "messages", []) or [])
        ],
    }


def _trajectory_payload(input: EvalImplInput) -> dict[str, Any]:
    return _build_trajectory_payload(input.trajectory, input.helper_results)


def _stage_sandbox(
    sandbox_root: Path,
    code: str,
    trajectory_payload: dict[str, Any],
) -> None:
    """Write the four files the subprocess needs into ``sandbox_root``."""
    (sandbox_root / "user_code.py").write_text(code, encoding="utf-8")
    (sandbox_root / "trajectory.json").write_text(
        json.dumps(trajectory_payload), encoding="utf-8"
    )
    shutil.copy(_MODULE_DIR / "runner_shim.py", sandbox_root / "runner_shim.py")
    shutil.copy(_MODULE_DIR / "snapshot_ctx.py", sandbox_root / "snapshot_ctx.py")


def _gate_failure_result(input: EvalImplInput, violations: list[str]) -> VerifierResult:
    detail = "; ".join(violations)
    return VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=0.0,
        status=VerifierResultStatus.ERROR,
        message=f"AST gate rejected verifier code: {detail}",
        verifier_result_values={
            "passed": False,
            "details": detail,
            "gate_violations": violations,
            "stdout": "",
            "stderr": "",
        },
    )


async def code_runner_verifier_eval(input: EvalImplInput) -> VerifierResult:
    verifier_id = input.verifier.verifier_id
    verifier_version = input.verifier.verifier_version
    verifier_values = input.verifier.verifier_values or {}
    eval_config_values = input.eval_config.eval_config_values or {}

    code = verifier_values.get("code")
    if not code:
        raise ValueError(
            "code_runner_verifier requires verifier_values['code'] (the def check(ctx) source)"
        )

    allowed_imports = _resolve_allowed_imports(eval_config_values)
    max_length = _resolve_max_code_length(eval_config_values)
    gate = check_code(code, allowed_imports, max_length=max_length)
    if not gate.ok:
        logger.warning(
            f"[CODE_RUNNER] Gate rejected verifier {verifier_id}: {gate.violations}"
        )
        return _gate_failure_result(input, gate.violations)

    timeout_s = _resolve_timeout(verifier_values, eval_config_values)
    trajectory_payload = _trajectory_payload(input)

    sandbox_root = Path(tempfile.mkdtemp(prefix=f"vrf_{verifier_id}_"))
    process: asyncio.subprocess.Process | None = None
    try:
        _stage_sandbox(sandbox_root, code, trajectory_payload)
        _prepare_sandbox_fs(sandbox_root, sandbox_root / "runner_shim.py")

        # Also grant the unprivileged subprocess read access to the snapshot
        # mount. The filesystem_setup_helper extracts files into _SNAPSHOT_MOUNT
        # but does not chmod them; without this, ctx.read_text / list_files
        # would raise PermissionError when running as uid 65534. The second
        # arg (test_file) is reused only for the parent-dir-traversal pass —
        # the runner_shim is already inside sandbox_root from the call above.
        snapshot_mount = Path(_SNAPSHOT_MOUNT)
        if snapshot_mount.is_dir():
            _prepare_sandbox_fs(snapshot_mount, sandbox_root / "runner_shim.py")

        env = _build_sandbox_env(sandbox_root)
        env["CODE_RUNNER_SNAPSHOT_DIR"] = _SNAPSHOT_MOUNT

        logger.info(
            f"[CODE_RUNNER] Spawning sandbox for verifier {verifier_id} (timeout={timeout_s}s)"
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
            return VerifierResult(
                verifier_id=verifier_id,
                verifier_version=verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                message=f"verifier exceeded timeout of {timeout_s}s",
                verifier_result_values={
                    "passed": False,
                    "details": f"timeout after {timeout_s}s",
                    "stdout": "",
                    "stderr": "",
                },
            )

        stdout_text = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace")

        verdict = _parse_verdict(stdout_text)
        if verdict is None:
            return VerifierResult(
                verifier_id=verifier_id,
                verifier_version=verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                message=f"verifier produced no parseable verdict (exit {process.returncode})",
                verifier_result_values={
                    "passed": False,
                    "details": "no JSON verdict on stdout",
                    "stdout": stdout_text[-4000:],
                    "stderr": stderr_text[-4000:],
                },
            )

        passed = bool(verdict.get("passed"))
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=1.0 if passed else 0.0,
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
    except Exception as exc:  # noqa: BLE001 — wrap any unexpected failure as ERROR result
        logger.exception(f"[CODE_RUNNER] Verifier {verifier_id} failed unexpectedly")
        if process is not None and process.returncode is None:
            try:
                process.kill()
            except Exception:  # noqa: BLE001
                pass
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            message=f"unexpected error: {type(exc).__name__}: {exc}",
            verifier_result_values={
                "passed": False,
                "details": traceback.format_exc(),
                "stdout": "",
                "stderr": "",
            },
        )
    finally:
        shutil.rmtree(sandbox_root, ignore_errors=True)


def _parse_verdict(stdout_text: str) -> dict[str, Any] | None:
    """Find the last non-blank line of stdout and parse it as JSON.

    The shim guarantees the verdict is the last printed line; user code prints
    are above it. We walk from the bottom and return the first valid JSON object.
    """
    for line in reversed(stdout_text.splitlines()):
        candidate = line.strip()
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            return None
        if isinstance(obj, dict):
            return obj
        return None
    return None

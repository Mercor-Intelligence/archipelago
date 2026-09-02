"""Static checks for the RLS-9074 concurrency-safety contract.

Since RLS-9074, every agent lane in modal_labs.py runs under
``@modal.concurrent`` — several trajectories share one container/process,
executed as separate asyncio tasks on a single thread (see
https://modal.com/docs/guide/concurrent-inputs). There is no per-call
process or thread isolation: anything process-global rather than call-local
is shared across every concurrent trajectory in that container.

This scans ``runner/agents/`` for the two concrete bug classes that audit
(and one real incident) turned up:

  1. A hardcoded/shared default filesystem path used as a config fallback,
     e.g. ``cfg.get("work_dir") or "/tmp/openhands_workspace"``. Two
     concurrent trajectories that both hit the fallback then point at the
     exact same directory and clobber each other's files. Use
     ``tempfile.mkdtemp()``/``tempfile.TemporaryDirectory()`` for a fresh
     directory per call instead. (The bug this rule was written to catch —
     see PR #15268.)
  2. Direct ``os.environ`` mutation (``os.environ[...] = ...``,
     ``os.environ.pop(...)``, ``os.environ.update(...)``,
     ``del os.environ[...]``) or ``os.chdir(...)``. Both are process-global:
     one trajectory's mutation is visible to every other trajectory sharing
     the container. (See ``runner/agents/lighthouse_code_agent/process_env.py``
     for the one currently-allowlisted, hardened exception.)

A line carrying the marker comment ``# concurrency-check: allow-shared-state``
opts out of both checks for that line — use it only for a reviewed,
intentional exception (ideally one that, like process_env.py, documents why
it's still safe under concurrency).

Run via ``mise run concurrency-safety`` (or
``python -m linting.concurrency_checks``).
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

_AGENTS_ROOT = Path(__file__).resolve().parent.parent / "runner" / "agents"

_ALLOW_MARKER = "# concurrency-check: allow-shared-state"

_SKIP_DIR_NAMES = frozenset(
    {".venv", "__pycache__", ".git", ".mypy_cache", ".ruff_cache", ".pytest_cache"}
)

# A fallback string constant matching this prefix is a hardcoded shared path.
# tempfile.mkdtemp()/TemporaryDirectory() are the correct per-call alternative.
_SHARED_TMP_PREFIXES = ("/tmp/", "/var/tmp/")


def _should_skip(path: Path) -> bool:
    return any(part in _SKIP_DIR_NAMES for part in path.parts) or "test" in path.stem


def _shared_tmp_literal_value(node: ast.expr) -> str | None:
    """The literal string if *node* is a hardcoded shared-tmp path, else None."""
    if (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value.startswith(_SHARED_TMP_PREFIXES)
    ):
        return node.value
    return None


def _is_os_environ_attr(node: ast.expr) -> bool:
    """True for the ``os.environ`` attribute access itself."""
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _is_os_chdir_call(node: ast.Call) -> bool:
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr == "chdir"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "os"
    )


def _is_os_environ_mutating_call(node: ast.Call) -> bool:
    """``os.environ.pop(...)`` / ``os.environ.update(...)``."""
    return (
        isinstance(node.func, ast.Attribute)
        and node.func.attr in ("pop", "update", "clear", "setdefault")
        and _is_os_environ_attr(node.func.value)
    )


class _Finding:
    __slots__ = ("lineno", "message")

    def __init__(self, lineno: int, message: str) -> None:
        self.lineno = lineno
        self.message = message


def _check_file(path: Path, source: str) -> list[_Finding]:
    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        return []

    lines = source.splitlines()
    findings: list[_Finding] = []

    def allowed(lineno: int) -> bool:
        return 0 < lineno <= len(lines) and _ALLOW_MARKER in lines[lineno - 1]

    for node in ast.walk(tree):
        # os.environ[...] = ... / del os.environ[...]
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.Delete)):
            if isinstance(node, ast.AugAssign):
                targets = [node.target]
            else:
                targets = node.targets
            for target in targets:
                if (
                    isinstance(target, ast.Subscript)
                    and _is_os_environ_attr(target.value)
                    and not allowed(node.lineno)
                ):
                    findings.append(
                        _Finding(
                            node.lineno,
                            "os.environ mutation is process-global and shared "
                            "across every concurrent trajectory in this "
                            "container (@modal.concurrent) -- see module "
                            f"docstring. Add `{_ALLOW_MARKER}` on this line "
                            "only for a reviewed, documented exception.",
                        )
                    )

        elif isinstance(node, ast.Call):
            if _is_os_chdir_call(node) and not allowed(node.lineno):
                findings.append(
                    _Finding(
                        node.lineno,
                        "os.chdir() is process-global and shared across "
                        "every concurrent trajectory in this container "
                        f"(@modal.concurrent). Add `{_ALLOW_MARKER}` on this "
                        "line only for a reviewed, documented exception.",
                    )
                )
            elif _is_os_environ_mutating_call(node) and not allowed(node.lineno):
                findings.append(
                    _Finding(
                        node.lineno,
                        "os.environ mutation is process-global and shared "
                        "across every concurrent trajectory in this "
                        "container (@modal.concurrent) -- see module "
                        f"docstring. Add `{_ALLOW_MARKER}` on this line "
                        "only for a reviewed, documented exception.",
                    )
                )
            elif (
                isinstance(node.func, ast.Attribute)
                and node.func.attr == "get"
                and len(node.args) >= 2
                and (literal := _shared_tmp_literal_value(node.args[1])) is not None
                and not allowed(node.args[1].lineno)
            ):
                # dict.get(key, "/tmp/...") — same fallback-default shape as `or`.
                default = node.args[1]
                findings.append(
                    _Finding(
                        default.lineno,
                        f"{literal!r} is a hardcoded shared path "
                        "used as a .get() fallback default. Concurrent "
                        "trajectories that hit this fallback in the same "
                        "container will collide on the same directory "
                        "-- use tempfile.mkdtemp()/TemporaryDirectory() "
                        f"for a fresh path per call. Add `{_ALLOW_MARKER}` "
                        "on this line only for a reviewed, documented "
                        "exception.",
                    )
                )

        elif isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            for value in node.values:
                literal = _shared_tmp_literal_value(value)
                if literal is not None and not allowed(value.lineno):
                    findings.append(
                        _Finding(
                            value.lineno,
                            f"{literal!r} is a hardcoded shared path used "
                            "as a fallback default. Concurrent trajectories "
                            "that hit this fallback in the same container "
                            "will collide on the same directory -- use "
                            "tempfile.mkdtemp()/TemporaryDirectory() for a "
                            f"fresh path per call. Add `{_ALLOW_MARKER}` on "
                            "this line only for a reviewed, documented "
                            "exception.",
                        )
                    )

        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defaults = list(node.args.defaults) + list(node.args.kw_defaults)
            for default in defaults:
                if default is None:
                    continue
                literal = _shared_tmp_literal_value(default)
                if literal is not None and not allowed(default.lineno):
                    findings.append(
                        _Finding(
                            default.lineno,
                            f"{literal!r} is a hardcoded shared path "
                            "used as a parameter default. Concurrent "
                            "trajectories that hit this default in the same "
                            "container will collide on the same directory "
                            "-- use tempfile.mkdtemp()/TemporaryDirectory() "
                            f"for a fresh path per call. Add `{_ALLOW_MARKER}` "
                            "on this line only for a reviewed, documented "
                            "exception.",
                        )
                    )

    return findings


def main() -> int:
    if not _AGENTS_ROOT.is_dir():
        print(f"linting/concurrency_checks: missing {_AGENTS_ROOT}", file=sys.stderr)
        return 1

    errors: list[str] = []
    for path in sorted(_AGENTS_ROOT.rglob("*.py")):
        if _should_skip(path):
            continue
        source = path.read_text(encoding="utf-8", errors="replace")
        rel = path.relative_to(_AGENTS_ROOT.parent.parent)
        for finding in _check_file(path, source):
            errors.append(f"{rel}:{finding.lineno}: {finding.message}")

    if errors:
        print("linting/concurrency_checks: FAIL", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        return 1

    print("linting/concurrency_checks: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Subprocess entrypoint that imports the LLM-authored verifier and runs it.

This file is copied next to ``user_code.py`` in a per-verifier sandbox directory
and executed as ``python runner_shim.py`` under uid 65534. It is the only piece
of trusted Python the subprocess executes; everything else in the sandbox dir is
user-authored or data.

Contract with user_code.py:
    def check(ctx) -> dict   # {"passed": bool, "details": str, "metrics": dict?}

The shim prints exactly one JSON object on the final line of stdout. The parent
process parses that line. Anything before it (prints from user code, warnings)
is captured but ignored for the pass/fail signal.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import traceback
import types
from pathlib import Path
from typing import Any


def _load_trajectory() -> dict[str, Any]:
    path = Path("trajectory.json")
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _load_user_module(module_path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("user_code", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load module from {module_path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _coerce_result(raw: object) -> dict[str, Any]:
    """Normalize whatever user code returned into a structured result dict.

    Required key: ``passed`` (bool). Optional: ``details`` (str), ``metrics`` (dict).
    """
    if not isinstance(raw, dict):
        return {
            "passed": False,
            "details": f"check() must return a dict, got {type(raw).__name__}",
            "metrics": {},
        }
    if "passed" not in raw:
        return {
            "passed": False,
            "details": "check() result missing required key: 'passed'",
            "metrics": {},
        }
    return {
        "passed": bool(raw["passed"]),
        "details": str(raw.get("details", "")),
        "metrics": dict(raw.get("metrics") or {}),
    }


def main() -> int:
    snapshot_dir = os.environ.get("CODE_RUNNER_SNAPSHOT_DIR", "/filesystem")
    sandbox_dir = Path(__file__).parent.resolve()

    sys.path.insert(0, str(sandbox_dir))

    # SnapshotCtx is copied into the sandbox dir alongside this file. The
    # import is intentionally non-package style because this script runs as
    # __main__ with cwd == sandbox_root, not as part of the runner package.
    from snapshot_ctx import (  # pyright: ignore[reportMissingImports, reportImplicitRelativeImport]
        SnapshotCtx,
    )

    try:
        ctx = SnapshotCtx(snapshot_dir=snapshot_dir, trajectory=_load_trajectory())
        user_module = _load_user_module(sandbox_dir / "user_code.py")
        check = getattr(user_module, "check", None)
        if check is None or not callable(check):
            result = {
                "passed": False,
                "details": "user_code.py must define a callable check(ctx)",
                "metrics": {},
            }
        else:
            raw = check(ctx)
            result = _coerce_result(raw)
    except Exception as exc:  # noqa: BLE001 — surface any user-code failure as a verdict
        result = {
            "passed": False,
            "details": f"verifier raised: {type(exc).__name__}: {exc}",
            "metrics": {},
            "traceback": traceback.format_exc(),
        }

    # The parent reads the LAST line of stdout. Force a leading newline so the
    # verdict always starts on a fresh line — without this, user code that
    # printed with end="" would concatenate to our JSON and the parent would
    # see a single un-parseable line, dropping the verdict entirely.
    sys.stdout.write("\n")
    sys.stdout.flush()
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

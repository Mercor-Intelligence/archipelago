"""Subprocess entrypoint for DB code verifier.

Identical to the llm_code_verifier runner_shim but also loads a SQLite
database path from ``db_config.json`` and passes it to SnapshotCtx so
that ``ctx.query_db()`` is available to user code.

Contract with user_code.py:
    def check(ctx) -> dict   # {"passed": bool, "details": str, "metrics": dict?}
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
    """Normalize whatever user code returned into a structured result dict."""
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
    result: dict[str, Any] = {
        "passed": bool(raw["passed"]),
        "details": str(raw.get("details", "")),
        "metrics": dict(raw.get("metrics") or {}),
    }
    # Pass through an optional continuous score (numeric) so it survives the
    # sandbox -> parent JSON boundary; the parent eval validates + clamps it to
    # [0, 1]. bool is excluded (it is an int subclass, not a real score).
    raw_score = raw.get("score")
    if isinstance(raw_score, (int, float)) and not isinstance(raw_score, bool):
        result["score"] = float(raw_score)
    return result


def main() -> int:
    snapshot_dir = os.environ.get("CODE_RUNNER_SNAPSHOT_DIR", "/")
    sandbox_dir = Path(__file__).parent.resolve()

    # Parse allowed-subdirs whitelist from the environment (comma-separated).
    allowed_subdirs: list[str] | None = None
    raw_subdirs = os.environ.get("CODE_RUNNER_ALLOWED_SUBDIRS", "")
    if raw_subdirs:
        allowed_subdirs = [s.strip() for s in raw_subdirs.split(",") if s.strip()]

    sys.path.insert(0, str(sandbox_dir))

    from snapshot_ctx import (  # pyright: ignore[reportMissingImports, reportImplicitRelativeImport]
        SnapshotCtx,
    )

    try:
        # Load optional database config
        db_path: str | None = None
        baseline_db_path: str | None = None
        db_config_path = sandbox_dir / "db_config.json"
        if db_config_path.exists():
            db_config = json.loads(db_config_path.read_text())
            db_path = db_config.get("db_path")
            baseline_db_path = db_config.get("baseline_db_path")

        ctx = SnapshotCtx(
            snapshot_dir=snapshot_dir,
            trajectory=_load_trajectory(),
            db_path=db_path,
            allowed_subdirs=allowed_subdirs,
            baseline_db_path=baseline_db_path,
        )
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
    except Exception as exc:  # noqa: BLE001
        result = {
            "passed": False,
            "details": f"verifier raised: {type(exc).__name__}: {exc}",
            "metrics": {},
            "traceback": traceback.format_exc(),
        }

    # Parent reads the LAST line of stdout. Force a leading newline so the
    # verdict always starts on a fresh line.
    sys.stdout.write("\n")
    sys.stdout.flush()
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())

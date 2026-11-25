#!/usr/bin/env python3
"""Check that docs/schema.sql matches the schema declared by SQLAlchemy models.

Derives the ground-truth schema from your app's SQLAlchemy ``Base.metadata``
(via ``create_all`` into a fresh temporary SQLite file) and diffs it against
the committed ``docs/schema.sql``.  Does **not** read a live database — so
the check works on fresh checkouts and CI without ever booting the app, and
without any per-repo ``DATABASE_PATH`` / ``DATABASE_URL`` wiring.

Exit codes
----------
0  The committed ``docs/schema.sql`` matches the declared models.
1  Drift detected — models changed since ``docs/schema.sql`` was last
   regenerated.
2  Usage error (missing ``--models`` argument, import failure, missing
   attribute, …).

Usage
-----
::

    # Pre-commit hook (declarative)
    uv run python -m mcp_scripts.check_schema_drift --models db.models:Base

    # Custom schema location
    uv run python -m mcp_scripts.check_schema_drift \\
        --models db.models:Base \\
        --output docs/schema.sql

Pre-commit wiring
-----------------
In ``.pre-commit-config.yaml``::

    - repo: https://github.com/Mercor-Intelligence/mercor-mcp-shared
      hooks:
        - id: check-schema-drift
          args: ["--models", "db.models:Base"]

The hook entry uses ``language: system`` so it runs inside your project's
virtualenv (via ``uv run``) and can import your app's SQLAlchemy models.

Resolving the import path
-------------------------
If your code lives at the repo root (``db/models.py``), no extra wiring
is needed.  If it lives under ``mcp_servers/<server>/`` (the Foundry-*
convention), the script auto-reads ``[tool.pytest.ini_options].pythonpath``
from ``pyproject.toml`` and prepends each entry to ``sys.path`` before
importing.  For explicit control, pass ``--models-root <dir>`` one or
more times.
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Check docs/schema.sql is up to date with SQLAlchemy models.\n\n"
            "The schema is derived from Base.metadata.create_all() into a\n"
            "temporary SQLite file (no live database needed).\n\n"
            "Import resolution: --models-root values take precedence; if\n"
            "omitted, [tool.pytest.ini_options].pythonpath from pyproject.toml\n"
            "is auto-read so Foundry-* layouts (mcp_servers/<name>/) work\n"
            "without extra wiring."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--models",
        metavar="MODULE:ATTR",
        required=True,
        help=(
            "Import path of the SQLAlchemy declarative Base, in the form "
            "'module.path:Attr' (e.g. 'db.models:Base')."
        ),
    )
    parser.add_argument(
        "--models-root",
        metavar="DIR",
        action="append",
        default=None,
        help=(
            "Directory to prepend to sys.path before resolving --models. "
            "Repeat for multiple roots.  When omitted, "
            "[tool.pytest.ini_options].pythonpath from pyproject.toml is "
            "used; this lets repos that put server code under "
            "mcp_servers/<name>/ resolve 'db.models:Base' without any "
            "extra wiring."
        ),
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        default="docs/schema.sql",
        help="Path to the committed schema file (default: docs/schema.sql).",
    )
    parser.add_argument(
        "--title",
        metavar="TEXT",
        default="Database schema",
        help="Title comment expected at the top of the schema file.",
    )
    args = parser.parse_args(argv)

    # Import here (not at module level) so this script is safe to use as a
    # pre-commit hook without the full mcp_scripts package on sys.path —
    # it resolves relative to the script's own directory.
    try:
        from mcp_scripts.generate_schema_sql import (
            generate_schema_from_models,
            import_base,
            prepend_models_paths,
        )
    except ImportError:
        _here = Path(__file__).parent
        sys.path.insert(0, str(_here.parent))
        from mcp_scripts.generate_schema_sql import (  # type: ignore[no-redef]
            generate_schema_from_models,
            import_base,
            prepend_models_paths,
        )

    # Prepend --models-root (or pyproject pythonpath, or CWD) so the
    # import of `db.models:Base` resolves in Foundry-* style layouts
    # (mcp_servers/<server>/db/models.py with no __init__.py chain).
    prepend_models_paths(args.models_root)

    base = import_base(args.models)
    fresh_sql = generate_schema_from_models(base, title=args.title)

    output_path = Path(args.output)

    # First-time setup: no committed schema yet — write it.
    if not output_path.exists():
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(fresh_sql, encoding="utf-8")
        print(
            f"[schema-drift] Created {output_path} from declared models.\n"
            "Commit this file to enable drift detection on future changes."
        )
        return 0

    # ------------------------------------------------------------------
    # Compare (DDL only — strip comments / blank lines / whitespace so
    # tab-vs-space and trailing-whitespace differences in the committed
    # schema.sql don't trigger false positives).
    # ------------------------------------------------------------------
    committed_sql = output_path.read_text(encoding="utf-8")

    def _ddl_only(sql: str) -> str:
        """Return normalised DDL lines for comparison.

        Strips comment lines and blank lines, then normalises every
        remaining line by collapsing all runs of whitespace to a single
        space.  This makes the comparison immune to any whitespace-only
        change in the committed ``docs/schema.sql`` while still catching
        every structural change (new/removed column, changed type or
        constraint, new index, etc.).
        """
        lines = []
        for line in sql.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("--"):
                continue
            lines.append(" ".join(stripped.split()))
        return "\n".join(lines)

    if _ddl_only(fresh_sql) == _ddl_only(committed_sql):
        print(f"[schema-drift] {output_path} matches declared models.")
        return 0

    # Build a human-readable diff using the normalised DDL so whitespace
    # differences don't pollute the output (cap at 80 lines).
    diff_lines = list(
        difflib.unified_diff(
            (_ddl_only(committed_sql) + "\n").splitlines(keepends=True),
            (_ddl_only(fresh_sql) + "\n").splitlines(keepends=True),
            fromfile=f"{args.output} (committed)",
            tofile=f"{args.output} (from {args.models})",
            n=3,
        )
    )
    diff_preview = "".join(diff_lines[:80])
    if len(diff_lines) > 80:
        diff_preview += f"\n... ({len(diff_lines) - 80} more lines) ...\n"

    # Rebuild the exact invocation the user would need: include every
    # --models-root they passed so the remediation works in Foundry-*
    # layouts where the explicit path is what made --models resolvable.
    regen_parts = [
        "uv run python -m mcp_scripts.generate_schema_sql",
        f"--models {args.models}",
    ]
    for root in args.models_root or []:
        regen_parts.append(f"--models-root {root}")
    regen_parts.append(f"--output {args.output}")
    regen_cmd = " ".join(regen_parts)

    print(
        "[schema-drift] DRIFT DETECTED — docs/schema.sql is out of date.\n"
        "\n"
        "The committed schema does not match what Base.metadata.create_all()\n"
        "would produce. Regenerate and commit the result:\n"
        f"\n    {regen_cmd}\n"
        "\n"
        "Diff (committed → declared):\n"
        f"{diff_preview}"
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())

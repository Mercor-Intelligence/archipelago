#!/usr/bin/env python3
"""Documentation-drift check for MCP server apps.

Regenerates the committed documentation artifacts from ground-truth code and
fails if any of them are stale. Two artifacts are covered:

1. ``docs/schema.sql`` — the database schema, derived from your app's
   SQLAlchemy ``Base.metadata`` (via ``create_all`` into a fresh temporary
   SQLite file). No live database is read, so the check works on fresh
   checkouts and in CI without any per-repo ``DATABASE_PATH`` wiring.

2. ``docs/populate_and_run.md`` (optional) — a handover doc describing how to
   populate/run the server plus the app-specific CSV/source formats, derived
   from the app's ``snapshot_config.yaml``. Only checked when
   ``--snapshot-config`` is supplied; otherwise the check behaves exactly like
   the schema-only check it grew out of.

Exit codes
----------
0  Every checked artifact is up to date.
1  Drift detected — regenerate and commit the reported artifact(s).
2  Usage error (missing ``--models`` argument, import failure, missing
   attribute, missing snapshot config, …).

Usage
-----
::

    # Schema only (back-compatible)
    uv run python -m mcp_scripts.check_schema_drift --models db.models:Base

    # Schema + handover docs (CSV formats from the snapshot config)
    uv run python -m mcp_scripts.check_schema_drift \\
        --models db.models:Base \\
        --snapshot-config mcp_servers/zoho/snapshot_config.yaml \\
        --server-name zoho \\
        --db-path studio.db

Pre-commit wiring
-----------------
In ``.pre-commit-config.yaml``::

    - repo: https://github.com/Mercor-Intelligence/mercor-mcp-shared
      hooks:
        - id: check-schema-drift
          args: ["--models", "db.models:Base",
                 "--snapshot-config", "mcp_servers/<name>/snapshot_config.yaml"]

The hook entry uses ``language: system`` so it runs inside your project's
virtualenv (via ``uv run``) and can import your app's SQLAlchemy models and
the snapshot-config loader.

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
from typing import Any

# ---------------------------------------------------------------------------
# Comparison helpers (shared by both artifacts)
# ---------------------------------------------------------------------------


def _normalize(text: str, *, strip_sql_comments: bool) -> str:
    """Return normalised lines for structural comparison.

    Drops blank lines, collapses every run of whitespace to a single space,
    and (for SQL) drops ``--`` comment lines.  This makes the comparison
    immune to whitespace-only or comment-only edits in the committed file
    while still catching every structural change.
    """
    out: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if strip_sql_comments and stripped.startswith("--"):
            continue
        out.append(" ".join(stripped.split()))
    return "\n".join(out)


def _diff_preview(
    committed: str,
    fresh: str,
    *,
    fromfile: str,
    tofile: str,
    strip_sql_comments: bool,
    cap: int = 80,
) -> str:
    """Return a capped unified diff of the two normalised texts."""
    diff_lines = list(
        difflib.unified_diff(
            (_normalize(committed, strip_sql_comments=strip_sql_comments) + "\n").splitlines(
                keepends=True
            ),
            (_normalize(fresh, strip_sql_comments=strip_sql_comments) + "\n").splitlines(
                keepends=True
            ),
            fromfile=fromfile,
            tofile=tofile,
            n=3,
        )
    )
    preview = "".join(diff_lines[:cap])
    if len(diff_lines) > cap:
        preview += f"\n... ({len(diff_lines) - cap} more lines) ...\n"
    return preview


# ---------------------------------------------------------------------------
# Per-artifact checks
# ---------------------------------------------------------------------------


def _resolve_base(args: argparse.Namespace) -> Any:
    """Prepend model paths and import the SQLAlchemy Base named by ``--models``.

    Resolved once and shared by both the schema check and (for column
    enumeration) the docs check. ``import_base`` exits(2) with a readable
    message on failure, so callers don't need to handle import errors.
    """
    try:
        from mcp_scripts.generate_schema_sql import import_base, prepend_models_paths
    except ImportError:
        _here = Path(__file__).parent
        sys.path.insert(0, str(_here.parent))
        from mcp_scripts.generate_schema_sql import (  # type: ignore[no-redef]
            import_base,
            prepend_models_paths,
        )

    # Prepend --models-root (or pyproject pythonpath, or CWD) so the
    # import of `db.models:Base` resolves in Foundry-* style layouts
    # (mcp_servers/<server>/db/models.py with no __init__.py chain).
    prepend_models_paths(args.models_root)
    return import_base(args.models)


def _check_schema_sql(args: argparse.Namespace, base: Any) -> int:
    """Check (and bootstrap) ``docs/schema.sql`` against the declared models."""
    # Import here (not at module level) so this script is safe to use as a
    # pre-commit hook without the full mcp_scripts package on sys.path —
    # it resolves relative to the script's own directory.
    try:
        from mcp_scripts.generate_schema_sql import generate_schema_from_models
    except ImportError:
        _here = Path(__file__).parent
        sys.path.insert(0, str(_here.parent))
        from mcp_scripts.generate_schema_sql import (  # type: ignore[no-redef]
            generate_schema_from_models,
        )

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

    committed_sql = output_path.read_text(encoding="utf-8")

    if _normalize(fresh_sql, strip_sql_comments=True) == _normalize(
        committed_sql, strip_sql_comments=True
    ):
        print(f"[schema-drift] {output_path} matches declared models.")
        return 0

    diff_preview = _diff_preview(
        committed_sql,
        fresh_sql,
        fromfile=f"{args.output} (committed)",
        tofile=f"{args.output} (from {args.models})",
        strip_sql_comments=True,
    )

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


def _check_docs_md(args: argparse.Namespace, base: Any) -> int:
    """Check (and bootstrap) the handover Markdown doc against the snapshot config."""
    try:
        from mcp_scripts.generate_server_docs import (
            render_markdown,
            resolve_config,
        )
    except ImportError:
        _here = Path(__file__).parent
        sys.path.insert(0, str(_here.parent))
        from mcp_scripts.generate_server_docs import (  # type: ignore[no-redef]
            render_markdown,
            resolve_config,
        )

    # Only require the YAML on disk when we're loading from it (a --config-factory
    # builds the config in-process instead).
    if not args.config_factory:
        config_path = Path(args.snapshot_config)
        if not config_path.exists():
            print(
                f"[docs-drift] ERROR: snapshot config not found: {config_path}",
                file=sys.stderr,
            )
            return 2

    source_label = args.config_factory or args.snapshot_config

    config = resolve_config(
        snapshot_config_path=args.snapshot_config,
        config_factory=args.config_factory,
        models_root=args.models_root,
    )
    fresh_md = render_markdown(
        config,
        base=base,
        server_name=args.server_name,
        db_path=args.db_path,
        snapshot_config_path=args.snapshot_config,
    )

    output_path = Path(args.docs_output)

    if not output_path.exists():
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(fresh_md, encoding="utf-8")
        print(
            f"[docs-drift] Created {output_path} from {source_label}.\n"
            "Commit this file to enable documentation-drift detection."
        )
        return 0

    committed_md = output_path.read_text(encoding="utf-8")

    if _normalize(fresh_md, strip_sql_comments=False) == _normalize(
        committed_md, strip_sql_comments=False
    ):
        print(f"[docs-drift] {output_path} matches the snapshot config.")
        return 0

    diff_preview = _diff_preview(
        committed_md,
        fresh_md,
        fromfile=f"{args.docs_output} (committed)",
        tofile=f"{args.docs_output} (from {source_label})",
        strip_sql_comments=False,
    )

    print(
        f"[docs-drift] DRIFT DETECTED — {args.docs_output} is out of date.\n"
        "\n"
        "The committed handover doc does not match the snapshot config.\n"
        "Regenerate it with your app's docs task (parameters live in mise.toml,\n"
        "not in the doc) and commit the result:\n"
        "\n    mise run generate-docs\n"
        "\n"
        "Diff (committed → generated):\n"
        f"{diff_preview}"
    )
    return 1


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Check the committed docs (docs/schema.sql and, optionally, the\n"
            "handover Markdown) are up to date with the code.\n\n"
            "schema.sql is derived from Base.metadata.create_all() into a\n"
            "temporary SQLite file (no live database needed). The handover\n"
            "Markdown is derived from the app's snapshot_config.yaml and is\n"
            "only checked when --snapshot-config is supplied.\n\n"
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
    # --- Handover-docs options (enabled by --snapshot-config/--config-factory) --
    parser.add_argument(
        "--snapshot-config",
        metavar="PATH",
        default=None,
        help=(
            "Path to the app's snapshot_config.yaml. When set, also checks the "
            "handover Markdown doc (source files & columns + lifecycle) for drift."
        ),
    )
    parser.add_argument(
        "--config-factory",
        metavar="MODULE:ATTR",
        default=None,
        help=(
            "Import path of a zero-arg callable returning a fully-registered "
            "SnapshotConfig ('module.path:build_config'). Use instead of "
            "--snapshot-config to also document registered custom hooks. When "
            "set, the handover Markdown is checked for drift."
        ),
    )
    parser.add_argument(
        "--docs-output",
        metavar="PATH",
        default="docs/populate_and_run.md",
        help="Path to the committed handover Markdown (default: docs/populate_and_run.md).",
    )
    parser.add_argument(
        "--server-name",
        metavar="NAME",
        default=None,
        help="Server name used in the handover doc title / lifecycle text.",
    )
    parser.add_argument(
        "--db-path",
        metavar="PATH",
        default="workspace.db",
        help="On-disk SQLite DB path shown in the handover doc (default: workspace.db).",
    )
    args = parser.parse_args(argv)

    # Resolve the SQLAlchemy Base once (exits 2 with a readable message on
    # failure) and share it: the schema check needs it, and the docs check
    # uses it to enumerate columns for flat entities without an explicit
    # header signature.
    base = _resolve_base(args)

    rc = _check_schema_sql(args, base)

    if args.snapshot_config is not None or args.config_factory is not None:
        docs_rc = _check_docs_md(args, base)
        # Usage error (2) dominates; otherwise surface drift (1) over ok (0).
        if docs_rc == 2 or rc == 2:
            rc = 2
        else:
            rc = max(rc, docs_rc)

    return rc


if __name__ == "__main__":
    sys.exit(main())

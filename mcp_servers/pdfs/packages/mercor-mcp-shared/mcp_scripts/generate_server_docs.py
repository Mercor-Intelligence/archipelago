#!/usr/bin/env python3
"""Generate a client-facing handover ``.md`` for populating and running a server.

The audience is a **client/data provider**: someone who needs to know how to
populate and run the server, and — above all — **what source files to provide
and which columns each file must contain**. It deliberately does NOT document
database structure or internal routing; it answers "what do I put in each file".

Two sections are produced:

* **Lifecycle (populate & run)** — the standard mise lifecycle
  (``install`` / ``populate`` / ``start``) plus the ``MCP_TRANSPORT`` /
  ``MCP_PORT`` knobs and the on-disk DB path.

* **Source files & columns** — one entry per source file. For each file it
  lists the columns and whether each is required. Columns are taken from the
  app's ``snapshot_config.yaml`` (explicit header signatures) and, when a
  SQLAlchemy ``Base`` is supplied, filled in from the model for files that
  don't declare an explicit signature. For files whose rows *fan out* into
  several records (directive mode), it documents the **differentiator** (what
  splits/keys the fan-out) and the **defaults applied** (constant + derived
  values the importer fills in). When the app registers **custom hooks** on
  its ``SnapshotConfig`` (row/group/pre-transform/post-import/batch-observer
  callables), each hook's docstring is surfaced so app-specific behaviour is
  documented too — this requires a fully-registered config via
  ``--config-factory`` (a plain ``snapshot_config.yaml`` declares no hooks).

Alongside the document, a ``csv_templates/`` folder of **header-only** ``.csv``
files is generated — one per documented source file (mirroring the app
subfolders), whose single header row is that file's documented columns in order.
A client fills these in. Regeneration is authoritative: templates that no longer
map to an entity (and now-empty folders) are pruned, so they track the document
without a separate drift check. Pass ``--no-csv-templates`` to skip them.

The rendered output is **deterministic** for a given ``(config, base, inputs)``
so it can be diffed by :mod:`mcp_scripts.check_schema_drift` as a
documentation-drift gate.

Usage
-----
::

    uv run python -m mcp_scripts.generate_server_docs \\
        --snapshot-config mcp_servers/zoho/snapshot_config.yaml \\
        --models db.models:Base \\
        --server-name zoho \\
        --db-path studio.db

Config factory (registering hooks & declared inputs)
----------------------------------------------------
``--config-factory`` must name a **zero-arg** callable returning a fully
registered ``SnapshotConfig`` — that is the only channel through which
registered hooks and :class:`~mcp_middleware.csv_engine.DeclaredInput`
declarations reach the doc (a raw ``snapshot_config.yaml`` carries neither). An
app whose hook registrar takes the ``config_registrar`` facade (2-arg) wraps it
in a small zero-arg factory that loads the config, registers the hooks, then
declares every input a hook consumes that the config does **not** already
capture::

    # mcp_servers/zoho/docs_factory.py
    from pathlib import Path

    from mcp_middleware.csv_engine import DeclaredInput, load_config

    def build_config():
        config = load_config(Path("mcp_servers/zoho/snapshot_config.yaml"))
        register_hooks(config)  # the app's existing hook registration

        # A sidecar file a hook reads directly by path (never a table row):
        config.register_input("drive_files", DeclaredInput(
            name="_filename_map.json", kind="file", required=True,
            description="original_basename -> anonymized on-disk name."))
        # A column a hook consumes that no signature/model surfaces, so a client
        # would not otherwise know to put it in the CSV:
        config.register_input("drive_files", DeclaredInput(
            name="content_file", kind="column", file="google/drive_files.csv",
            required=True, description="Relative path to the file body to ingest."))
        return config

Then ``--config-factory mcp_servers.zoho.docs_factory:build_config``. Declare
**only** inputs the config does not already capture — a column a client must
put in a CSV that the schema never surfaces because a hook reads it
programmatically, or a sidecar file a hook opens directly. Columns already
present in a file's column table are documented already and need no
declaration.
"""

from __future__ import annotations

import argparse
import functools
import inspect
import re
import sys
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from mcp_middleware.csv_engine.config import (
        DeclaredInput,
        EntityConfig,
        ImportDirectives,
        SnapshotConfig,
    )

_DEFAULT_OUTPUT = "docs/populate_and_run.md"
_DEFAULT_DB = "workspace.db"

# A column entry rendered in a file's column table: (name, required).
_Col = tuple[str, bool]


class _Section(NamedTuple):
    """A rendered source-file section: a representative entity, the (possibly
    merged) file list, and its de-duplicated hooks. Same-shape entities are
    collapsed into one section before sections are grouped by target table.

    ``members`` lists every entity name collapsed into this section, so the
    ``csv_templates`` generator can verify a candidate template filename routes
    back to one of them via the engine's own filename matcher."""

    rep: EntityConfig
    files: list[str]
    hooks: list[tuple[str, str, str | None]]
    members: list[str]
    # Hook-consumed inputs (files/columns) declared on the config, unioned
    # across the collapsed members. Trailing default keeps positional
    # construction working for any caller that predates this field.
    declared: tuple[DeclaredInput, ...] = ()


# --- File-index tree model (Item 3) ---------------------------------------
# Nodes captured while the body is rendered, so the "File index" tree above the
# sections can anchor-link to the exact heading slugs the body produced.


class _Leaf(NamedTuple):
    """One source file (or cross-format set) linking to its column section."""

    files: list[str]
    anchor: str


class _TableNode(NamedTuple):
    """A target table within a group; ``anchor`` is set when the body rendered a
    ``Table`` heading for it (else the leaves sit directly under the app)."""

    table: str
    anchor: str | None
    leaves: list[_Leaf]


class _AppNode(NamedTuple):
    """An app section (``app is None`` → the top-level bucket)."""

    app: str | None
    anchor: str | None
    tables: list[_TableNode]


def _github_slug(text: str) -> str:
    """Approximate GitHub's heading-anchor slug for *text* (the heading body,
    without the leading ``#``).

    Mirrors ``github-slugger``: lowercase, drop every character that is not a
    word char (``[A-Za-z0-9_]``), whitespace, or hyphen (so backticks and
    punctuation vanish), then turn runs of whitespace into single hyphens.
    """
    kept = [ch for ch in text.strip().lower() if ch.isalnum() or ch in " -_"]
    slug = "".join(kept)
    return "-".join(slug.split())


class _Slugger:
    """Assigns GitHub-style anchor slugs, disambiguating duplicates with the
    ``-1`` / ``-2`` suffixes GitHub appends when a slug repeats in a document."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}

    def slug(self, heading_text: str) -> str:
        base = _github_slug(heading_text)
        seen = self._counts.get(base, 0)
        self._counts[base] = seen + 1
        return base if seen == 0 else f"{base}-{seen}"


# ---------------------------------------------------------------------------
# Config / model loading
# ---------------------------------------------------------------------------


def load_snapshot_config(path: Path) -> SnapshotConfig:
    """Load and parse an app ``snapshot_config.yaml`` into a ``SnapshotConfig``.

    Imported lazily so this module stays importable (e.g. for ``--help`` or
    argv-parse tests) in environments where ``mcp_middleware`` / ``polars``
    aren't installed.
    """
    try:
        from mcp_middleware.csv_engine.config import load_config
    except ImportError as exc:  # pragma: no cover - environment guard
        print(
            f"[server-docs] ERROR: could not import the snapshot-config loader: {exc}\n"
            "Make sure you are running via 'uv run' inside a project that depends "
            "on mercor-mcp-shared.",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc
    return load_config(path)


def call_config_factory(spec: str, models_root: list[str] | None = None) -> SnapshotConfig:
    """Import and call a zero-arg factory returning a fully-registered config.

    *spec* uses the ``module.path:attr`` convention (same as ``--models``). The
    named attribute must be a callable that returns a ``SnapshotConfig`` with
    the app's hooks already registered — this is how custom-hook docstrings make
    it into the generated doc (a raw ``snapshot_config.yaml`` carries no hooks).
    """
    from mcp_scripts.generate_schema_sql import import_base, prepend_models_paths

    prepend_models_paths(models_root)
    factory = import_base(spec, flag="--config-factory", tool="server-docs")
    if not callable(factory):
        print(
            f"[server-docs] ERROR: --config-factory {spec!r} did not resolve to a "
            "callable returning a SnapshotConfig.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return factory()


def resolve_config(
    *,
    snapshot_config_path: str | None,
    config_factory: str | None = None,
    models_root: list[str] | None = None,
) -> SnapshotConfig:
    """Return a ``SnapshotConfig`` from a factory (preferred) or a YAML path.

    When ``config_factory`` is given it wins — its result includes any
    registered hooks. Otherwise the static ``snapshot_config.yaml`` is loaded
    (no hooks). Exactly one source must be resolvable.
    """
    if config_factory:
        return call_config_factory(config_factory, models_root)
    if not snapshot_config_path:
        print(
            "[server-docs] ERROR: provide either --snapshot-config or --config-factory.",
            file=sys.stderr,
        )
        raise SystemExit(2)
    return load_snapshot_config(Path(snapshot_config_path))


# Columns the csv engine auto-fills on import, so a client never supplies
# them. Mirrors ``mcp_middleware.csv_engine.schema._AUTO_MANAGED_COLUMNS``:
# these are NOT NULL in the schema but stamped by the engine and excluded from
# ``required_columns``, so listing them (let alone as required) would wrongly
# demand columns the provider does not need to supply.
_AUTO_MANAGED_COLUMNS = frozenset({"created_at", "updated_at"})


def columns_from_base(base: Any) -> dict[str, list[_Col]]:
    """Return ``{table_name: [(column, required), ...]}`` from a SQLAlchemy Base.

    ``required`` means the client must supply a non-empty value: the column is
    NOT NULL and has no default. Auto-increment integer primary keys and
    engine-managed timestamps (``created_at`` / ``updated_at``) are omitted
    entirely — the database or the csv engine fills those in, so a client never
    provides them.
    """
    if base is None:
        return {}
    out: dict[str, list[_Col]] = {}
    for table in base.metadata.tables.values():
        cols: list[_Col] = []
        for col in table.columns:
            if _is_autoincrement_pk(col) or col.name in _AUTO_MANAGED_COLUMNS:
                continue
            required = not col.nullable and col.default is None and col.server_default is None
            cols.append((col.name, required))
        out[table.name] = cols
    return out


def _is_autoincrement_pk(col: Any) -> bool:
    """True for a single integer auto-increment primary key (DB-generated).

    SQLAlchemy's default ``autoincrement="auto"`` only actually auto-increments
    the *sole* integer primary key of a table; the integer parts of a composite
    primary key are NOT DB-generated and must be supplied by the client, so they
    are not omitted here. An explicit ``autoincrement=True`` is honoured as-is.
    """
    if not col.primary_key:
        return False
    type_name = type(col.type).__name__.lower()
    if "int" not in type_name:
        return False
    if col.autoincrement is True:
        return True
    if col.autoincrement == "auto":
        return len(col.table.primary_key.columns) == 1
    return False


# ---------------------------------------------------------------------------
# Small rendering helpers
# ---------------------------------------------------------------------------


def _fmt_globs(globs: list[str]) -> str:
    if not globs:
        return "_(no filename pattern configured — add a `files:` glob for this entity)_"
    return ", ".join(f"`{g}`" for g in globs)


def _column_table(rows: list[_Col]) -> list[str]:
    """Render a Column | Required table (rows already ordered)."""
    out = ["  | Column | Required |", "  | --- | --- |"]
    for name, required in rows:
        out.append(f"  | `{name}` | {'yes' if required else 'no'} |")
    return out


def _describe_computed(name: str, cf: Any) -> str:
    """Human phrase for a ComputedField default."""
    if cf.template is not None:
        return f"`{name}` is filled from the template `{cf.template}`"
    if cf.copy_from is not None:
        return f"`{name}` is copied from `{cf.copy_from}`"
    if cf.split_first is not None:
        return f"`{name}` is the first word of `{cf.split_first}`"
    if cf.split_rest is not None:
        return f"`{name}` is the remaining words of `{cf.split_rest}`"
    return f"`{name}` is derived automatically"


# ---------------------------------------------------------------------------
# Custom hooks (registered programmatically on the SnapshotConfig)
# ---------------------------------------------------------------------------

# (label, SnapshotConfig getter name) for each registrable hook kind.
_HOOK_KINDS: list[tuple[str, str]] = [
    ("Row hook", "get_row_hooks"),
    ("Group hook", "get_group_hooks"),
    ("Pre-transform hook", "get_pre_transform_hooks"),
    ("Post-import hook", "get_post_import_hooks"),
    ("Batch observer", "get_batch_observers"),
]


# Generic inner-function names apps commonly use for a hook built by a factory
# (``def _make_x_hook(): def hook(row): ...; return hook``). When we see one of
# these we look through to the enclosing factory for a meaningful name/docstring
# so apps don't have to hand-stamp ``__name__``/``__doc__`` on every closure.
_GENERIC_HOOK_NAMES = frozenset(
    {
        "hook",
        "_hook",
        "inner",
        "wrapper",
        "wrapped",
        "fn",
        "func",
        "_fn",
        "_func",
        "observe",
        "_observe",
        "observer",
        "_observer",
        "callback",
        "_callback",
        "cb",
        "_cb",
        "<lambda>",
    }
)


def _unwrap(fn: Any) -> Any:
    """Peel ``functools.partial`` layers to reach the underlying callable."""
    seen = 0
    while isinstance(fn, functools.partial):
        fn = fn.func
        seen += 1
        if seen > 16:  # pathological guard
            break
    return fn


def _enclosing_factory(fn: Any) -> Any:
    """Resolve the module-level factory that produced a closure hook, or None.

    For the common ``def _make_x_hook(): def hook(): ...; return hook`` pattern
    the returned closure's ``__qualname__`` is ``_make_x_hook.<locals>.hook`` and
    its ``__globals__`` still holds the factory. Recovering the factory lets us
    document the hook's real name and docstring without app-side stamping.
    """
    target = _unwrap(fn)
    qual = getattr(target, "__qualname__", "") or ""
    if ".<locals>." not in qual:
        return None
    factory_qual = qual.split(".<locals>.", 1)[0]
    if "." in factory_qual:  # nested/method factory — can't resolve via globals
        return None
    glb = getattr(target, "__globals__", None)
    if not isinstance(glb, dict):
        return None
    factory = glb.get(factory_qual)
    return factory if callable(factory) else None


def _hook_name(fn: Any) -> str:
    """A readable name for a hook callable (function, partial, or object)."""
    target = _unwrap(fn)
    name = getattr(target, "__name__", None)
    if name and name not in _GENERIC_HOOK_NAMES:
        return name
    # Generic closure name → prefer the enclosing factory's name.
    factory = _enclosing_factory(fn)
    if factory is not None:
        fname = getattr(factory, "__name__", None)
        if fname:
            return fname
    if name and name != "<lambda>":
        return name
    return type(target).__name__ if not inspect.isfunction(target) else "anonymous"


def _hook_doc(fn: Any) -> str | None:
    """First paragraph of a hook's docstring, collapsed to one line.

    Falls back to the enclosing factory's docstring when a closure hook has no
    docstring of its own (the ``_make_x_hook`` pattern).
    """
    doc = inspect.getdoc(_unwrap(fn))
    if not doc:
        factory = _enclosing_factory(fn)
        if factory is not None:
            doc = inspect.getdoc(factory)
    if not doc:
        return None
    para: list[str] = []
    for line in doc.splitlines():
        if not line.strip():
            if para:
                break
            continue
        para.append(line.strip())
    return " ".join(para) or None


def describe_hooks(config: SnapshotConfig, entity_name: str) -> list[tuple[str, str, str | None]]:
    """Return ``[(label, name, docstring)]`` for every hook on *entity_name*."""
    out: list[tuple[str, str, str | None]] = []
    for label, getter_name in _HOOK_KINDS:
        getter = getattr(config, getter_name, None)
        if getter is None:
            continue
        for fn in getter(entity_name):
            out.append((label, _hook_name(fn), _hook_doc(fn)))
    return out


# ---------------------------------------------------------------------------
# Column resolution per entity
# ---------------------------------------------------------------------------


def _directive_source_columns(directives: ImportDirectives) -> list[str]:
    """Collect the source (input) columns a directive block reads from."""
    names: list[str] = []
    if directives.id_from:
        names.append(directives.id_from)
    for e in directives.extract:
        names.extend(e.fields.values())
    for m in directives.multi_value:
        names.append(m.column)
    for n in directives.nested:
        names.append(n.base)
        # The importer also reads the paired id column ``<base><id_suffix>``
        # (e.g. ``Owner`` + ``Owner.id``); a client must supply both headers.
        names.append(f"{n.base}{n.id_suffix}")
    names.extend(directives.columns.keys())
    # De-dup preserving first-seen order.
    seen: set[str] = set()
    ordered: list[str] = []
    for n in names:
        if n not in seen:
            seen.add(n)
            ordered.append(n)
    return ordered


def _entity_columns(
    entity: EntityConfig,
    table_columns: dict[str, list[_Col]],
) -> list[_Col] | None:
    """Resolve the column list a client must provide for *entity*'s file.

    Returns ``None`` when the columns can't be determined from the available
    inputs (no explicit signature and no model metadata).
    """
    imp = entity.import_config
    if imp is None:
        return None
    sig = imp.signatures

    # Directive (fanned) entities: prefer the recorded wide-CSV header order,
    # else the union of columns the directives read + the detection signature.
    if imp.directives is not None:
        req = set(sig.required)
        # The record-identity column (``id_from``) is always required — without
        # it populate can't key rows — even when no signatures block declares a
        # required set (the common ``import: { directives: ... }`` shape).
        if imp.directives.id_from:
            req.add(imp.directives.id_from)
        names = list(imp.directives.wide_columns)
        if not names:
            names = _directive_source_columns(imp.directives)
            names += sorted(req - set(names))
            names += sorted(sig.optional - set(names))
        return [(n, n in req) for n in names]

    model_cols = table_columns.get(entity.table)

    # Flat entity with an explicit required set declared in the YAML — that set
    # is authoritative (the app opted out of model-derived requiredness).
    if not sig.auto_required:
        rows = [(c, True) for c in sorted(sig.required)]
        rows += [(c, False) for c in sorted(sig.optional - sig.required)]
        if rows:
            return _demote_auto_id(rows, imp)
        # Empty explicit signature: fall through to the model.

    # Auto-required flat entity (e.g. ``import: {}``, or a signature that only
    # names optional columns): the *required* columns are the model's NOT NULL
    # columns, so prefer the model. Any columns the YAML additionally names as
    # optional are appended (they may not be model columns — e.g. header-only
    # aliases). This is why ``--models`` matters for pass-through entities.
    if model_cols is not None:
        known = {c for c, _ in model_cols}
        extra = [(c, False) for c in sorted(sig.optional - known)]
        return _demote_auto_id(model_cols + extra, imp)

    # No model metadata available — best effort from any declared optional cols.
    if sig.optional:
        return _demote_auto_id([(c, False) for c in sorted(sig.optional)], imp)
    return None


def _demote_auto_id(rows: list[_Col], imp: Any) -> list[_Col]:
    """Mark ``id`` optional when the importer auto-assigns it.

    When ``import_config.id_prefix`` is set, the csv engine fills in a prefixed
    ``id`` for any row that omits one (importer: ``if id_prefix and not
    record.get("id"): record["id"] = ...``). Such an ``id`` is therefore NOT
    something the client must supply, so it must not be marked Required — even
    though the SQLAlchemy model reports it as a NOT NULL primary key.
    """
    if not getattr(imp, "id_prefix", ""):
        return rows
    return [(name, required and name != "id") for name, required in rows]


# ---------------------------------------------------------------------------
# Fanned-rows (directive) documentation
# ---------------------------------------------------------------------------


def _render_fanned(directives: ImportDirectives) -> list[str]:
    """Render the differentiator + defaults for a directive (fanned) file."""
    lines: list[str] = []

    diffs: list[str] = []
    for e in directives.extract:
        key = ", ".join(f"`{c}`" for c in e.dedup_on) or "`id`"
        srcs = ", ".join(f"`{c}`" for c in e.fields.values())
        diffs.append(
            f"  - Each distinct **{e.into}** is identified by {key} "
            f"(from {srcs}); repeated values reuse the same record."
        )
    for m in directives.multi_value:
        dedup = " (duplicates removed)" if m.dedup else ""
        diffs.append(
            f"  - `{m.column}` is a `{m.delimiter}`-separated list; each value "
            f"becomes a separate **{m.into}** entry{dedup}."
        )
    for n in directives.nested:
        diffs.append(
            f"  - `{n.base}` (with its id column) is grouped into a nested **{n.key}** object."
        )
    if directives.json_collapse is not None:
        diffs.append(
            "  - Any remaining columns are kept as-is (no fixed set — extra columns are preserved)."
        )

    if diffs:
        lines.append("- **Fanned rows:** one row in this file expands into several records:")
        lines.extend(diffs)

    defaults: list[str] = []
    for col, val in directives.constants.items():
        defaults.append(f"  - `{col}` defaults to `{val}` on every record.")
    for name, cf in directives.computed.items():
        defaults.append(f"  - {_describe_computed(name, cf)}.")
    for e in directives.extract:
        for col, val in e.constants.items():
            defaults.append(f"  - On each **{e.into}**, `{col}` defaults to `{val}`.")
        for name, cf in e.computed.items():
            defaults.append(f"  - On each **{e.into}**, {_describe_computed(name, cf)}.")

    if defaults:
        lines.append("- **Defaults applied** (filled in for you — do not provide):")
        lines.extend(defaults)

    return lines


# ---------------------------------------------------------------------------
# Source-files section
# ---------------------------------------------------------------------------


def _render_hooks(hooks: list[tuple[str, str, str | None]]) -> list[str]:
    """Render the 'Custom processing' subsection from described hooks."""
    if not hooks:
        return []
    lines = ["- **Custom processing** (app-specific behaviour applied on import):"]
    for label, name, doc in hooks:
        suffix = f" — {doc}" if doc else ""
        lines.append(f"  - **{label}** `{name}`{suffix}")
    return lines


def _merge_declared_columns(
    cols: list[_Col] | None, declared: tuple[DeclaredInput, ...]
) -> list[_Col] | None:
    """Fold hook-consumed columns into the resolved column list so they show up
    inline in the file's column table.

    Deduped by name (a column already resolved from the signature/model is left
    as-is — the declared entry only surfaces its prose in the "Additional
    inputs" subsection). Appended in name order for a deterministic render.
    """
    col_inputs = [d for d in declared if d.kind == "column"]
    if not col_inputs:
        return cols
    rows: list[_Col] = list(cols or [])
    have = {name for name, _ in rows}
    for d in sorted(col_inputs, key=lambda x: x.name):
        if d.name not in have:
            rows.append((d.name, d.required))
            have.add(d.name)
    return rows or None


def _render_declared_inputs(declared: tuple[DeclaredInput, ...]) -> list[str]:
    """Render the 'Additional inputs' subsection.

    Lists hook-consumed inputs the plain column table can't fully describe:
    every ``file``-kind sidecar (not a column at all), and any ``column``-kind
    input carrying a ``description`` the inline table row has no room for. Bare
    declared columns (no description) are represented by their inline table row
    alone and not repeated here.

    The doc is client-facing — it says *what to put in the file*, not where the
    data goes. ``source_ref`` (a pointer into the engine code that consumes the
    input) is internal metadata and is deliberately NOT rendered here.
    """
    items = [d for d in declared if d.kind == "file" or d.description]
    if not items:
        return []
    lines = [
        "- **Additional inputs** (consumed by import hooks — provide these even "
        "though they are not ordinary columns):"
    ]
    for d in sorted(items, key=lambda x: (x.kind, x.name)):
        if d.kind == "file":
            head = f"**File** `{d.name}`"
        else:
            loc = f" on `{d.file}`" if d.file else ""
            head = f"**Column** `{d.name}`{loc}"
        req = " (required)" if d.required else " (optional)"
        desc = f" — {d.description}" if d.description else ""
        lines.append(f"  - {head}{req}{desc}")
    return lines


# Internal snapshot_config key suffixes that are meaningless to a client
# (used to disambiguate same-table entities in the config, e.g. a JSON-reuse
# ``tags`` alongside the CSV ``tags_tbl``). Stripped from section headings so
# the heading reads as the logical file name.
_KEY_SUFFIXES = ("_tbl", "_csv", "_json")


def _display_name(name: str) -> str:
    """A client-facing name for an entity: internal key suffixes removed."""
    for suffix in _KEY_SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


def _has_glob_meta(segment: str) -> bool:
    """True if a path segment contains glob metacharacters (``*``, ``?``, ``[``)."""
    return any(ch in segment for ch in "*?[")


def _entity_app(files: list[str]) -> str | None:
    """The "app" a source file belongs to: the first directory segment of its glob.

    ``jira/issues.csv`` → ``"jira"``; a top-level glob (``issues.csv``,
    ``*.csv``) or a wildcard-led glob (``**/x.csv``) → ``None`` (top-level).
    The first glob that yields a concrete folder segment wins.
    """
    for f in files:
        parts = PurePosixPath(f).parts
        if len(parts) >= 2 and not _has_glob_meta(parts[0]):
            return parts[0]
    return None


def _app_display(app: str) -> str:
    """A client-facing heading for an app folder (``jira`` → ``Jira``)."""
    return app.replace("_", " ").replace("-", " ").title()


def _formats_for_files(files: list[str]) -> list[str]:
    """Distinct upper-cased formats implied by a list of file globs, in order."""
    out: list[str] = []
    for f in files:
        suffix = Path(f).suffix.lstrip(".").upper()
        if suffix and suffix not in out:
            out.append(suffix)
    return out


def _render_file_line(files: list[str]) -> str:
    """Render the ``- **File:**`` line, noting cross-format interchangeability."""
    globs = _fmt_globs(files)
    formats = _formats_for_files(files)
    if len(files) > 1 and len(formats) > 1:
        return f"- **File:** {globs} — provide as {' or '.join(formats)} (one format is enough)."
    return f"- **File:** {globs}"


def _render_entity(
    entity: EntityConfig,
    table_columns: dict[str, list[_Col]],
    hooks: list[tuple[str, str, str | None]] | None = None,
    *,
    display_name: str | None = None,
    files: list[str] | None = None,
    heading_level: int = 3,
    declared_inputs: tuple[DeclaredInput, ...] = (),
) -> list[str]:
    """Render one source file's client-facing entry.

    ``display_name`` overrides the heading (used to show a clean logical name
    when collapsing internal ``*_tbl`` / ``*_csv`` keys). ``files`` overrides
    the file list (used when several same-shape entities are merged into one
    "provide as CSV or JSON" section). ``heading_level`` sets the Markdown
    heading depth (``3`` for a standalone file, ``4`` when nested under a shared
    "Table" heading for multiple files that populate the same table).
    """
    hooks = hooks or []
    files = files if files is not None else entity.files
    hashes = "#" * heading_level
    lines: list[str] = [f"{hashes} `{display_name or entity.name}`", ""]
    lines.append(_render_file_line(files))

    imp = entity.import_config
    if imp is None:
        lines.append("- _This entity is not populated from a source file._")
        lines.extend(_render_declared_inputs(declared_inputs))
        lines.extend(_render_hooks(hooks))
        lines.append("")
        return lines

    # Header aliases: alternative accepted spellings for a column.
    aliases = imp.signatures.aliases
    if aliases:
        alias_pairs = ", ".join(
            f"`{src}` (accepted as `{dst}`)" for src, dst in sorted(aliases.items())
        )
        lines.append(f"- **Alternate column names accepted:** {alias_pairs}")

    cols = _merge_declared_columns(_entity_columns(entity, table_columns), declared_inputs)
    if cols:
        lines.append("- **Columns:**")
        lines.append("")
        lines.extend(_column_table(cols))
        lines.append("")
    else:
        lines.append(
            "- **Columns:** _not fixed — provide the columns for this entity's "
            "records; required fields are validated when you populate._"
        )

    lines.extend(_render_declared_inputs(declared_inputs))

    if imp.directives is not None:
        lines.extend(_render_fanned(imp.directives))

    lines.extend(_render_hooks(hooks))

    lines.append("")
    return lines


def _entities_needing_model(config: SnapshotConfig) -> list[str]:
    """Names of flat pass-through entities whose columns come from the model.

    These are flat (non-directive) entities with an auto-required signature and
    no explicit required set — ``import: {}`` in particular. Without ``--models``
    their columns can't be resolved and they render as "not fixed", a silent
    downgrade of the handover doc.
    """
    out: list[str] = []
    for entity in config.entities.values():
        imp = entity.import_config
        if imp is None or imp.directives is not None:
            continue
        sig = imp.signatures
        if sig.auto_required and not sig.required:
            out.append(entity.name)
    return out


def _build_sections(config: SnapshotConfig, table_columns: dict[str, list[_Col]]) -> list[_Section]:
    """Collapse same-shape flat entities (same logical file in different formats)
    into one client-facing section, preserving first-appearance order.

    Shared by the doc renderer and the ``csv_templates`` generator so a template
    is produced for exactly the files the document describes.
    """
    sections: list[_Section] = []
    for group in _group_entities(config, table_columns):
        rep = group[0]
        merged_files: list[str] = []
        for entity in group:
            for f in entity.files:
                if f not in merged_files:
                    merged_files.append(f)
        hooks: list[tuple[str, str, str | None]] = []
        for entity in group:
            for hook in describe_hooks(config, entity.name):
                if hook not in hooks:
                    hooks.append(hook)
        members = [entity.name for entity in group]
        declared: list[DeclaredInput] = []
        for entity in group:
            for spec in config.get_declared_inputs(entity.name):
                if spec not in declared:
                    declared.append(spec)
        sections.append(
            _Section(
                rep=rep,
                files=merged_files,
                hooks=hooks,
                members=members,
                declared=tuple(declared),
            )
        )
    return sections


def render_source_files(config: SnapshotConfig, base: Any = None) -> str:
    """Render the 'Source files & columns' section."""
    table_columns = columns_from_base(base)
    if base is None:
        needing = _entities_needing_model(config)
        if needing:
            shown = ", ".join(sorted(needing)[:5])
            more = "…" if len(needing) > 5 else ""
            print(
                f"[server-docs] WARNING: {len(needing)} pass-through "
                f"entit{'y' if len(needing) == 1 else 'ies'} ({shown}{more}) "
                "resolve their columns from the SQLAlchemy model, but no "
                "--models was given — these will render as 'not fixed'. "
                "Pass --models MODULE:Base to include their columns.",
                file=sys.stderr,
            )
    lines: list[str] = ["## Source files & columns", ""]

    if not config.entities:
        lines.append("_No source files are declared for this server._")
        lines.append("")
        return "\n".join(lines)

    intro = (
        "Provide the following source files when populating. Each file and the "
        "columns it should contain are listed below. A column marked "
        "**Required = yes** must be present and non-empty."
    )
    formats = sorted({s.format for s in config.sources})
    if formats and formats != ["csv"]:
        intro += " Files may be provided as " + ", ".join(f.upper() for f in formats) + "."
    lines.append(intro)
    lines.append("")

    # First, collapse same-shape flat entities (same logical file in different
    # formats) into one merged "section". Then group those sections by the DB
    # table they populate so a client sees, under one heading, every distinct
    # file that feeds a given table.
    sections = _build_sections(config, table_columns)

    # Group sections by "app" (first folder segment of the file glob). Top-level
    # files (no folder) render first, with no app heading. Then each app gets its
    # own ``###`` section, in first-appearance order. A single-app config with no
    # top-level files needs no redundant app wrapper — it renders flat.
    top_level: list[_Section] = []
    by_app: dict[str, list[_Section]] = {}
    app_order: list[str] = []
    for section in sections:
        app = _entity_app(section.files)
        if app is None:
            top_level.append(section)
        else:
            if app not in by_app:
                by_app[app] = []
                app_order.append(app)
            by_app[app].append(section)

    # Render the body first so heading slugs are assigned in document order;
    # the "File index" tree above the sections then reuses those exact slugs.
    slugger = _Slugger()
    body: list[str] = []
    app_nodes: list[_AppNode] = []

    if not top_level and len(app_order) == 1:
        # Whole doc is one app: skip the redundant heading, render flat.
        grp_lines, tnodes = _render_group(
            by_app[app_order[0]],
            table_columns,
            base_level=3,
            table_heading_on_multi=False,
            slugger=slugger,
        )
        body.extend(grp_lines)
        app_nodes.append(_AppNode(app=None, anchor=None, tables=tnodes))
    else:
        if top_level:
            grp_lines, tnodes = _render_group(
                top_level,
                table_columns,
                base_level=3,
                table_heading_on_multi=False,
                slugger=slugger,
            )
            body.extend(grp_lines)
            app_nodes.append(_AppNode(app=None, anchor=None, tables=tnodes))
        for app in app_order:
            display = _app_display(app)
            app_anchor = slugger.slug(display)
            body.append(f"### {display}")
            body.append("")
            grp_lines, tnodes = _render_group(
                by_app[app],
                table_columns,
                base_level=4,
                table_heading_on_multi=True,
                slugger=slugger,
            )
            body.extend(grp_lines)
            app_nodes.append(_AppNode(app=display, anchor=app_anchor, tables=tnodes))

    tree = _render_file_tree(app_nodes)
    if tree:
        lines.extend(tree)
        lines.append("")
    lines.extend(body)
    return "\n".join(lines).rstrip() + "\n"


def _render_file_tree(app_nodes: list[_AppNode]) -> list[str]:
    """Render the nested-bullet 'File index' above the source-file sections.

    Mirrors the app → table → file structure; each file leaf anchor-links to
    its column section. Kept as a plain bullet list (not an ASCII/code-fence
    tree) so the links render — less pretty, more navigable.
    """
    if not any(node.tables for node in app_nodes):
        return []
    out = ["**File index** — jump to the columns for any source file:", ""]
    for node in app_nodes:
        if node.app is not None:
            label = f"[**{node.app}**](#{node.anchor})" if node.anchor else f"**{node.app}**"
            out.append(f"- {label}")
            base_indent = "  "
        else:
            base_indent = ""
        for tnode in node.tables:
            if tnode.anchor is not None:
                out.append(f"{base_indent}- [Table `{tnode.table}`](#{tnode.anchor})")
                leaf_indent = base_indent + "  "
            else:
                leaf_indent = base_indent
            for leaf in tnode.leaves:
                for f in leaf.files:
                    out.append(f"{leaf_indent}- [`{f}`](#{leaf.anchor})")
    return out


def _render_group(
    sections: list[_Section],
    table_columns: dict[str, list[_Col]],
    *,
    base_level: int,
    table_heading_on_multi: bool,
    slugger: _Slugger,
) -> tuple[list[str], list[_TableNode]]:
    """Render one group of sections (a single app, or the top-level bucket).

    Within the group, sections are grouped by their target table, preserving
    first-appearance order. A ``Table`` heading is emitted for a table when
    either

    * more than one distinct file populates it (so the client sees the
      alternatives grouped), or
    * ``table_heading_on_multi`` is set *and* the group spans more than one
      table (used inside an app section so its tables are enumerated).

    Entities under a ``Table`` heading render one level deeper; otherwise they
    render at ``base_level``. Returns the rendered lines plus the table/leaf
    nodes (with their heading slugs) for the File-index tree. Heading slugs are
    drawn from *slugger* in document order so they match GitHub's anchors.
    """
    by_table: dict[str, list[_Section]] = {}
    table_order: list[str] = []
    for section in sections:
        table = section.rep.table
        if table not in by_table:
            by_table[table] = []
            table_order.append(table)
        by_table[table].append(section)

    multi_table = len(table_order) > 1
    lines: list[str] = []
    nodes: list[_TableNode] = []
    for table in table_order:
        table_sections = by_table[table]
        show_table_heading = len(table_sections) > 1 or (table_heading_on_multi and multi_table)
        table_anchor: str | None = None
        if show_table_heading:
            hashes = "#" * base_level
            table_anchor = slugger.slug(f"Table `{table}`")
            lines.append(f"{hashes} Table `{table}`")
            lines.append("")
            if len(table_sections) > 1:
                lines.append(
                    f"The following source files each populate the `{table}` table. "
                    "They are distinct inputs — provide the ones that match your "
                    "data. Each has its own required columns, listed separately "
                    "below."
                )
                lines.append("")
            entity_level = base_level + 1
        else:
            entity_level = base_level
        leaves: list[_Leaf] = []
        for section in table_sections:
            display = _display_name(section.rep.name)
            entity_anchor = slugger.slug(f"`{display}`")
            lines.extend(
                _render_entity(
                    section.rep,
                    table_columns,
                    section.hooks,
                    display_name=display,
                    files=section.files,
                    heading_level=entity_level,
                    declared_inputs=section.declared,
                )
            )
            leaves.append(_Leaf(files=section.files, anchor=entity_anchor))
        nodes.append(_TableNode(table=table, anchor=table_anchor, leaves=leaves))
    return lines, nodes


def _collapse_key(entity: EntityConfig, table_columns: dict[str, list[_Col]]) -> tuple:
    """Key used to merge same-shape entities into one client-facing section.

    Only *flat* entities (no directives) that represent the **same logical file
    in different formats** are merged — same file *stem* (basename minus
    extension), same target ``table``, same resolved columns and same accepted
    aliases. This collapses the JSON-reuse pattern (``tags.csv`` / ``tags.json``
    on one table) into a single "provide as CSV or JSON" entry, while keeping
    two genuinely distinct files that happen to land in the same table (e.g.
    ``contact_roles.csv`` and ``settings_resource.csv`` → ``settings_resources``)
    as separate sections. Directive (fanned) entities and entities with no fixed
    filename are always kept separate.
    """
    imp = entity.import_config
    if imp is None or imp.directives is not None or not entity.files:
        # Never merge: give each a unique key (its own name).
        return ("__unique__", entity.name)
    cols = _entity_columns(entity, table_columns)
    col_sig = tuple(cols) if cols is not None else None
    alias_sig = tuple(sorted(imp.signatures.aliases.items()))
    # Key on the extension-stripped path (folder + stem), not the bare stem, so
    # only the same logical file in different *formats* collapses (``tags.csv`` /
    # ``tags.json``). Two same-named files in different app folders
    # (``jira/attachments.csv`` vs ``confluence/attachments.csv``) are distinct
    # inputs and must each be documented under their own app.
    stems = tuple(sorted({str(PurePosixPath(f).with_suffix("")) for f in entity.files}))
    return ("flat", entity.table, stems, col_sig, alias_sig)


def _group_entities(
    config: SnapshotConfig, table_columns: dict[str, list[_Col]]
) -> list[list[EntityConfig]]:
    """Group entities for rendering, preserving first-appearance order.

    Same-shape flat entities (see :func:`_collapse_key`) are collapsed into one
    group; everything else is its own singleton group.
    """
    groups: dict[tuple, list[EntityConfig]] = {}
    order: list[tuple] = []
    for entity in config.entities.values():
        key = _collapse_key(entity, table_columns)
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(entity)
    return [groups[key] for key in order]


# ---------------------------------------------------------------------------
# csv_templates: header-only files a client fills in (Item 4)
# ---------------------------------------------------------------------------


def _literalize_stem(stem: str) -> str:
    """Turn a glob stem into a concrete, still-matching filename stem.

    ``*``/``?`` runs become a readable ``example`` token (``*`` matches it, so
    the result still satisfies the source glob); char-class brackets are
    dropped. ``deals_*`` → ``deals_example``; ``*Leads*`` → ``exampleLeadsexample``
    (both route back through the entity's own ``files:`` glob).
    """
    out = re.sub(r"[*?]+", "example", stem)
    out = out.replace("[", "").replace("]", "")
    return out.strip("_-. ") or "example"


def _template_relpath(section: _Section, config: SnapshotConfig) -> str | None:
    """The ``csv_templates``-relative path for a section's header-only file, or
    ``None`` when no ``.csv`` name can be built that the entity would route back.

    Always ``.csv`` (even when the real source is xlsx/json — the importer routes
    a ``.csv`` variant via its cross-format pass when both formats are declared,
    and the client fills in one flat sheet). The path is built from the section's
    representative glob so the emitted file lands where the entity's ``files:``
    glob will still match it:

    * concrete directory segments of the glob are kept (``jira/issues.csv`` →
      ``jira/``); glob-wildcard segments (``**``, ``*``), ``..`` and any absolute
      anchor are dropped so the file sits at the shallowest matching location and
      can never escape the templates directory;
    * the basename comes from the glob's stem, a literalized form of a wildcard
      stem (``deals_*`` → ``deals_example``), or the section's display name —
      **whichever the engine's own filename matcher routes back to a member
      entity.** Candidates that would route to no entity (or a different one) are
      rejected; if none survive, ``None`` is returned and the caller skips the
      template rather than emit one a client can't populate.

    ``section.files`` is guaranteed non-empty by the caller.
    """
    from mcp_middleware.csv_engine.importer import _match_entity_by_filename

    files = section.files
    # Prefer a concrete .csv glob, then any concrete glob, then the first glob.
    concrete = [f for f in files if not any(_has_glob_meta(p) for p in PurePosixPath(f).parts)]
    pool = concrete or files
    rep = next((f for f in pool if f.lower().endswith(".csv")), pool[0])

    p = PurePosixPath(rep)
    # Keep only concrete, in-tree directory segments: drop ``.``/``..``, the
    # POSIX root anchor (``/``), and any glob-wildcard segment. This keeps the
    # written path inside ``templates_dir`` even for pathological globs.
    dir_parts = [
        seg for seg in p.parent.parts if seg not in ("", ".", "..", "/") and not _has_glob_meta(seg)
    ]

    # Candidate basenames, most-readable first. Each is verified against the
    # engine's real router so a generated template is guaranteed to populate.
    stem = p.stem
    candidates: list[str] = []
    if stem and not _has_glob_meta(stem):
        candidates.append(stem)
    if stem and _has_glob_meta(stem):
        candidates.append(_literalize_stem(stem))
    candidates.append(_display_name(section.rep.name))

    seen: set[str] = set()
    members = set(section.members)
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        rel = "/".join([*dir_parts, f"{cand}.csv"])
        routed = _match_entity_by_filename(PurePosixPath(rel).name, config, rel_path=rel)
        if routed in members:
            return rel
    return None


def _template_header(section: _Section, table_columns: dict[str, list[_Col]]) -> list[str] | None:
    """The header row (documented columns, in doc order) for a section's template.

    Returns ``None`` when the section has no fixed column set (nothing to
    template) — those entities are documented as "not fixed" and get no file.
    """
    cols = _entity_columns(section.rep, table_columns)
    if not cols:
        return None
    return [name for name, _ in cols]


def _render_template_csv(header: list[str]) -> str:
    """A single header-only CSV line (RFC-4180 quoting), newline-terminated."""
    import csv
    import io

    buf = io.StringIO()
    # Explicit LF terminator so output is identical across platforms (drift-safe).
    csv.writer(buf, lineterminator="\n").writerow(header)
    return buf.getvalue()


def _section_is_binary(section: _Section, config: SnapshotConfig) -> bool:
    """True when the client provides *files* for this entity, not a table.

    Binary/file-content entities (PDF / DOCX / images / archives — anything the
    engine reads as raw bytes) have no columns a client fills in: the "columns"
    are extracted/derived at import (``extracted_text``, ``sha256``, …). Emitting
    a header-only ``.csv`` for them is actively misleading, so they get no
    template — the client drops the actual source files instead.

    Classification reuses the importer's own ``resolve_format`` /
    ``is_binary_reader`` so the generator and the engine always agree. Each of
    the section's globs is probed with a concrete filename built from its
    directory + a placeholder stem + the glob's real suffix; the section is
    binary only when **every** probe resolves to a binary reader (a mixed
    tabular+binary section still gets a template for its tabular shape).
    """
    from mcp_middleware.csv_engine.readers import is_binary_reader, resolve_format

    probed = False
    for f in section.files:
        p = PurePosixPath(f)
        suffix = p.suffix
        if not suffix or _has_glob_meta(suffix):
            continue
        dir_parts = [
            seg
            for seg in p.parent.parts
            if seg not in ("", ".", "..", "/") and not _has_glob_meta(seg)
        ]
        stem = p.stem
        if not stem or _has_glob_meta(stem):
            stem = "probe"
        probe = "/".join([*dir_parts, f"{stem}{suffix}"])
        fmt = resolve_format(PurePosixPath(probe).name, config.sources, rel_path=probe)
        probed = True
        if not is_binary_reader(fmt):
            return False
    return probed


def write_csv_templates(
    config: SnapshotConfig,
    templates_dir: Path,
    *,
    base: Any = None,
) -> list[Path]:
    """Write one header-only ``.csv`` per documented source file under
    *templates_dir*, pruning any ``.csv`` no longer mapped to an entity.

    Mirrors the document's app subfolders. Regeneration is authoritative: files
    that no longer correspond to an entity (and now-empty directories) are
    removed, so the templates never drift from the documented set. Returns the
    list of written template paths (sorted).
    """
    table_columns = columns_from_base(base)
    sections = _build_sections(config, table_columns)

    wanted: dict[str, list[str]] = {}
    for section in sections:
        header = _template_header(section, table_columns)
        if header is None:
            continue
        # An entity with no ``files:`` glob can't be routed at populate time, so
        # there is no filename to place a template at (and nothing to derive one
        # from) — skip it rather than crash.
        if not section.files:
            continue
        # Binary/file-content entities (PDF/DOCX/images/…): the client provides
        # the actual files, not a table. Its documented "columns" are derived at
        # import, so a header-only .csv would read as "author this metadata",
        # which is wrong. Skip — the file-index still lists the real file globs.
        if _section_is_binary(section, config):
            continue
        rel = _template_relpath(section, config)
        if rel is None:
            # No ``.csv`` name routes back to this entity under glob-only
            # routing (e.g. a wildcard glob whose literalized name still
            # doesn't match, or a json-only source with no csv format
            # declared). Emitting one would mislead the client, so skip + warn.
            print(
                f"[server-docs] WARNING: no populatable .csv template name for "
                f"'{section.rep.name}' (files: {', '.join(section.files)}); "
                "skipping — add a concrete or csv-compatible 'files:' glob.",
                file=sys.stderr,
            )
            continue
        # First writer wins on the rare path collision; keep output deterministic.
        wanted.setdefault(rel, header)

    written: list[Path] = []
    for rel, header in sorted(wanted.items()):
        dest = templates_dir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(_render_template_csv(header), encoding="utf-8")
        written.append(dest)

    _prune_templates(templates_dir, {templates_dir / rel for rel in wanted})
    return sorted(written)


def _prune_templates(templates_dir: Path, keep: set[Path]) -> None:
    """Delete ``.csv`` files under *templates_dir* not in *keep*, then remove any
    directories left empty. No-op if the directory doesn't exist."""
    if not templates_dir.is_dir():
        return
    for path in templates_dir.rglob("*.csv"):
        if path.is_file() and path not in keep:
            path.unlink()
    # Remove empty directories bottom-up (deepest first).
    for path in sorted(templates_dir.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            path.rmdir()


# ---------------------------------------------------------------------------
# Lifecycle (populate & run) section
# ---------------------------------------------------------------------------


def render_lifecycle(
    *,
    server_name: str | None,
    db_path: str,
    snapshot_config_path: str | None,
) -> str:
    """Render the 'Lifecycle: populate & run' section."""
    server = f"`{server_name}`" if server_name else "this server"
    lines: list[str] = [
        "## Lifecycle: populate & run",
        "",
        f"How to install, populate, and run {server}. All commands use "
        "[mise](https://mise.jdx.dev/) task runner.",
        "",
        "### 1. Install",
        "",
        "```bash",
        "mise install        # fetch the pinned toolchain (python, uv, …)",
        "mise run install    # install project dependencies",
        "```",
        "",
        "### 2. Populate the database",
        "",
        "Place your source files in the snapshot directory (see "
        "**Source files & columns** below), then build the database:",
        "",
        "```bash",
        "mise run populate",
        "```",
        "",
        f"This imports your source files and writes the database to `{db_path}`.",
        "",
        "> **Note:** You do not need a default user to populate the database. "
        "The default user is only required when the server runs (step 3).",
        "",
        "### 3. Run the server",
        "",
        "```bash",
        "mise run start",
        "```",
        "",
        "Transport and port are controlled by environment variables:",
        "",
        "| Variable | Default | Description |",
        "| --- | --- | --- |",
        "| `MCP_TRANSPORT` | `stdio` | Transport type: `stdio` or `http`. |",
        "| `MCP_PORT` | `5000` | Port for the `http` transport. |",
        f"| `DATABASE_PATH` | `{db_path}` | On-disk path to the database. |",
        "",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Top-level document
# ---------------------------------------------------------------------------


def render_markdown(
    config: SnapshotConfig,
    *,
    base: Any = None,
    server_name: str | None = None,
    db_path: str = _DEFAULT_DB,
    snapshot_config_path: str | None = None,
) -> str:
    """Render the full client-facing handover Markdown document.

    Deterministic for a given set of inputs so the output can be drift-checked.
    The heading is derived from *server_name* only (no free-form override) so the
    drift checker can reproduce it from the same inputs.

    The document intentionally contains **no internal tooling references** (no
    "regenerate with …" hint, no editor markers): it is a client deliverable.
    The knowledge of how to regenerate lives in the app's ``generate-docs`` mise
    task, not in the doc. Keeping the body purely config-derived also means the
    drift check is invariant to invocation details (``--models-root``,
    ``--output``), so it never reports spurious drift.
    """
    heading = f"{server_name} — populate & run" if server_name else "Populate & run"

    parts: list[str] = [
        f"# {heading}",
        "",
        "This guide explains how to populate and run the server and which "
        "columns each source file must contain.",
        "",
        render_lifecycle(
            server_name=server_name,
            db_path=db_path,
            snapshot_config_path=snapshot_config_path,
        ),
        render_source_files(config, base=base),
    ]
    return "\n".join(parts).rstrip() + "\n"


def write_markdown(
    config: SnapshotConfig,
    output: Path,
    *,
    csv_templates_dir: Path | None = None,
    **kwargs: Any,
) -> str:
    """Render and write the Markdown doc to *output*. Returns the rendered text.

    When *csv_templates_dir* is given, also (re)writes the header-only
    ``csv_templates`` alongside the doc, pruning any that no longer map to an
    entity so the templates track the documented set exactly.
    """
    md = render_markdown(config, **kwargs)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(md, encoding="utf-8")
    if csv_templates_dir is not None:
        write_csv_templates(config, csv_templates_dir, base=kwargs.get("base"))
    return md


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Generate a client-facing handover Markdown doc (populate/run "
            "lifecycle + source files & columns) from an app's "
            "snapshot_config.yaml (and, optionally, its SQLAlchemy models)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--snapshot-config",
        metavar="PATH",
        default=None,
        help="Path to the app's snapshot_config.yaml (required unless --config-factory is used).",
    )
    parser.add_argument(
        "--config-factory",
        metavar="MODULE:ATTR",
        default=None,
        help=(
            "Import path of a zero-arg callable returning a fully-registered "
            "SnapshotConfig ('module.path:build_config'). Use this instead of "
            "--snapshot-config when you want registered custom hooks (and their "
            "docstrings) documented."
        ),
    )
    parser.add_argument(
        "--models",
        metavar="MODULE:ATTR",
        default=None,
        help=(
            "Optional SQLAlchemy declarative Base ('module.path:Attr'). When "
            "given, columns for files without an explicit header signature are "
            "filled in from the model."
        ),
    )
    parser.add_argument(
        "--models-root",
        metavar="DIR",
        action="append",
        default=None,
        help="Directory to prepend to sys.path before resolving --models (repeatable).",
    )
    parser.add_argument(
        "--output",
        metavar="PATH",
        default=_DEFAULT_OUTPUT,
        help=f"Output Markdown path (default: {_DEFAULT_OUTPUT}).",
    )
    parser.add_argument(
        "--csv-templates-dir",
        metavar="DIR",
        default=None,
        help=(
            "Directory for the generated header-only csv_templates (default: a "
            "'csv_templates' folder next to --output). Regeneration prunes "
            "templates that no longer map to an entity."
        ),
    )
    parser.add_argument(
        "--no-csv-templates",
        action="store_true",
        help="Skip generating the header-only csv_templates folder.",
    )
    parser.add_argument(
        "--server-name",
        metavar="NAME",
        default=None,
        help="Server name used in the document title / lifecycle text.",
    )
    parser.add_argument(
        "--db-path",
        metavar="PATH",
        default=_DEFAULT_DB,
        help=f"On-disk database path shown in the lifecycle section (default: {_DEFAULT_DB}).",
    )
    return parser


def _resolve_base(models: str | None, models_root: list[str] | None) -> Any:
    """Import the SQLAlchemy Base named by *models*, or return None."""
    if not models:
        return None
    from mcp_scripts.generate_schema_sql import import_base, prepend_models_paths

    prepend_models_paths(models_root)
    return import_base(models, flag="--models", tool="server-docs")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if not args.snapshot_config and not args.config_factory:
        print(
            "[server-docs] ERROR: provide either --snapshot-config or --config-factory.",
            file=sys.stderr,
        )
        return 2
    if args.snapshot_config and not args.config_factory:
        config_path = Path(args.snapshot_config)
        if not config_path.exists():
            print(
                f"[server-docs] ERROR: snapshot config not found: {config_path}",
                file=sys.stderr,
            )
            return 2

    base = _resolve_base(args.models, args.models_root)
    config = resolve_config(
        snapshot_config_path=args.snapshot_config,
        config_factory=args.config_factory,
        models_root=args.models_root,
    )
    output = Path(args.output)
    if args.no_csv_templates:
        templates_dir = None
    elif args.csv_templates_dir:
        templates_dir = Path(args.csv_templates_dir)
    else:
        templates_dir = output.parent / "csv_templates"
    write_markdown(
        config,
        output,
        csv_templates_dir=templates_dir,
        base=base,
        server_name=args.server_name,
        db_path=args.db_path,
        snapshot_config_path=args.snapshot_config,
    )
    n = len(config.entities)
    source = args.config_factory or args.snapshot_config
    print(f"Server docs written to {output}  ({n} source files from {source})")
    if templates_dir is not None:
        print(f"CSV templates written to {templates_dir}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())

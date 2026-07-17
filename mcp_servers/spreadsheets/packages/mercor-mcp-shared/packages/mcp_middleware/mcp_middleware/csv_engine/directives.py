"""Declarative fan-out transform: a wide CSV row -> an :class:`EmitSet`.

Turns one wide domain-export row (e.g. a Zoho CRM module export) into rows
across several tables using the directives declared in
:class:`~mcp_middleware.csv_engine.config.ImportDirectives`:

- ``id_from``       -> the main record's id column.
- ``extract``       -> hoist a sub-entity (owner/account/user) into another
                       table, deduplicated, with an FK written back to the main
                       record (resolved via :class:`EmitRef`).
- ``multi_value``   -> explode a delimited column into junction-table rows.
- ``nested``        -> pair ``<base>`` + ``<base><id_suffix>`` into a nested
                       ``{id, name}`` object inside the main record's JSON column.
- ``json_collapse`` -> collapse every remaining column into a JSON/EAV column,
                       keyed by a (pluggable) key normalizer.

The directives operate on the *original* CSV headers (not snake_cased), because
the key normalizer (e.g. Zoho ``api_name``) defines the EAV keys. The produced
EmitSet is resolved + inserted by ``insert_emit_set``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from .config import ComputedField, ImportDirectives
from .emit import Emit, EmitRef, EmitSet
from .types import normalize_header

__all__ = [
    "directive_table_order",
    "get_key_normalizer",
    "register_key_normalizer",
    "transform_with_directives",
]


_ZOHO_API_NAME_RE = re.compile(r"[^A-Za-z0-9_]+")


def _zoho_api_name(header: str) -> str:
    """Zoho ``api_name`` normalization for EAV keys.

    Replaces any run of non-alphanumeric/underscore characters with a single
    ``_`` and trims leading/trailing underscores, preserving case (Zoho api
    names are e.g. ``Deal_Name``):

    * ``"Probability (%)"``       -> ``"Probability"``
    * ``"Advance Payment %"``     -> ``"Advance_Payment"``
    * ``"Margin Planning Done?"`` -> ``"Margin_Planning_Done"``

    Apps with different rules can override via :func:`register_key_normalizer`.
    """
    return _ZOHO_API_NAME_RE.sub("_", header.strip()).strip("_")


# Registry of named key normalizers used by ``json_collapse``.
_KEY_NORMALIZERS: dict[str, Callable[[str], str]] = {
    "default": normalize_header,
    "zoho_api_name": _zoho_api_name,
}


def register_key_normalizer(name: str, fn: Callable[[str], str]) -> None:
    """Register a named key normalizer for use by ``json_collapse``.

    The registry is **process-global** and not isolated across threads or
    test workers (matching the stdlib ``codecs`` / matplotlib backend
    pattern). Register custom normalizers once at process startup, before
    any concurrent imports run; do not mutate the registry from worker
    threads or from a per-test fixture that runs under ``pytest-xdist``.
    Re-registering an existing name silently overwrites the previous
    binding — last write wins. See ``register_reader`` for the same
    concurrency contract on the format-reader side.
    """
    _KEY_NORMALIZERS[name] = fn


def get_key_normalizer(name: str) -> Callable[[str], str]:
    """Return the named key normalizer, falling back to the default."""
    return _KEY_NORMALIZERS.get(name, normalize_header)


def _clean(value: Any) -> str:
    """Coerce a raw CSV cell to a stripped string ("" for None)."""
    if value is None:
        return ""
    return str(value).strip()


_SENTINEL = object()


def _resolve_path(row: dict[str, Any], key: str) -> Any:
    """Look up ``key`` in ``row``, with dotted-path fallback for nested dicts.

    Resolution order:

    1. **Literal key** — ``row[key]`` if present. Preserves Zoho-style columns
       whose name *contains* a dot (e.g. ``"Owner.id"``); a flat CSV reader
       emits these as literal keys and they must keep winning.
    2. **Dot-walk** — split ``key`` on ``.`` and walk through nested dicts:
       ``row["owner"]["id"]``. Lets a JSON reader emit a structured
       ``"owner": {"id": "U1"}`` value and have ``id_from: "owner.id"`` /
       ``extract.fields: {id: "owner.id"}`` find it.

    Returns ``None`` if the path doesn't exist or steps off a non-dict.
    """
    if key in row:
        return row[key]
    if "." not in key:
        return None
    cur: Any = row
    for part in key.split("."):
        if not isinstance(cur, dict):
            return None
        nxt = cur.get(part, _SENTINEL)
        if nxt is _SENTINEL:
            return None
        cur = nxt
    return cur


class _BlankDict(dict):
    """dict that returns "" for missing keys (for safe ``str.format_map``)."""

    def __missing__(self, key: str) -> str:
        return ""


def _split_name(full: str) -> tuple[str, str]:
    """Split on the first run of whitespace: (first token, remainder)."""
    parts = (full or "").strip().split(maxsplit=1)
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], parts[1]


def _apply_computed(
    record: dict[str, Any],
    links: dict[str, EmitRef] | None,
    computed: dict[str, ComputedField],
) -> None:
    """Fill declaratively-derived columns on ``record`` (import-only).

    Applied after the record's mapped fields/constants exist so templates and
    copies can reference them. ``copy_from`` of an FK link becomes the same link.
    """
    for col, spec in computed.items():
        if spec.template is not None:
            fmt = _BlankDict({k: ("" if v is None else v) for k, v in record.items()})
            record[col] = spec.template.format_map(fmt)
        elif spec.copy_from is not None:
            if links is not None and spec.copy_from in links:
                links[col] = links[spec.copy_from]
            else:
                record[col] = record.get(spec.copy_from)
        elif spec.split_first is not None:
            first, _rest = _split_name(_clean(record.get(spec.split_first)))
            record[col] = first or None
        elif spec.split_rest is not None:
            _first, rest = _split_name(_clean(record.get(spec.split_rest)))
            record[col] = rest or None


def _claim(consumed: set[str], key: str) -> None:
    """Mark ``key`` as consumed, plus its parent for dotted paths.

    CSV readers produce literal keys (``"Owner.id"`` is a single header) so
    consuming the literal string is sufficient. JSON readers produce
    nested objects (``{"owner": {"id": "U1"}}``) and the row's actual key
    is the parent ``"owner"`` — :func:`_resolve_path` handles the
    dot-walk at read time, but ``json_collapse`` decides what to emit by
    comparing row keys to the consumed set. Without claiming the parent,
    the entire ``"owner"`` object leaks into the JSON remainder,
    duplicating data already extracted via ``id_from`` / ``extract`` /
    ``nested`` / ``columns`` directives.
    """
    consumed.add(key)
    if "." in key:
        consumed.add(key.split(".", 1)[0])


def _consumed_columns(directives: ImportDirectives) -> set[str]:
    """Source columns claimed by id/extract/multi_value/nested/columns directives.

    These are excluded from ``json_collapse`` so the JSON remainder only holds
    columns not otherwise mapped. See :func:`_claim` for the dotted-path
    handling that lets JSON readers (which emit parent objects, not literal
    dotted keys) work alongside ``json_collapse``.
    """
    consumed: set[str] = set()
    if directives.id_from:
        _claim(consumed, directives.id_from)
    for ex in directives.extract:
        for source in ex.fields.values():
            _claim(consumed, source)
    for mv in directives.multi_value:
        _claim(consumed, mv.column)
    for ne in directives.nested:
        _claim(consumed, ne.base)
        _claim(consumed, ne.base + ne.id_suffix)
    # ``columns`` directives map source -> main-record DB column; their source
    # side is claimed and must not bleed into ``json_collapse``.
    for source in directives.columns:
        _claim(consumed, source)
    return consumed


def directive_table_order(directives: ImportDirectives, table: str) -> list[str]:
    """FK-topological table order for a directive fan-out.

    Extracted sub-entities (parents) come first, then the main table, then
    multi-value junction tables (children). Used as ``table_order`` for
    ``insert_emit_set`` so inserts are FK-safe on databases that enforce
    constraints during insert.
    """
    order: list[str] = []
    for ex in directives.extract:
        if ex.into not in order:
            order.append(ex.into)
    if table not in order:
        order.append(table)
    for mv in directives.multi_value:
        if mv.into not in order:
            order.append(mv.into)
    return order


def transform_with_directives(
    rows: list[dict[str, str]],
    directives: ImportDirectives,
    *,
    table: str,
) -> EmitSet:
    """Produce an :class:`EmitSet` from wide rows using fan-out directives.

    Args:
        rows: CSV rows keyed by their *original* headers.
        directives: The entity's declarative fan-out directives.
        table: The main record's destination table.

    Returns:
        An EmitSet (main records + extracted sub-entities + junction rows),
        ready for ``resolve_emits`` / ``insert_emit_set``.
    """
    normalizer = get_key_normalizer(directives.key_normalizer)
    consumed = _consumed_columns(directives)
    aliases = directives.aliases
    jc = directives.json_collapse
    json_into = jc.into if jc else None
    nest_pairs = bool(jc and jc.nest_id_pairs)
    suffix = jc.id_suffix if jc else ".id"
    # In an EAV/sparse-JSON column, missing key and explicit null are
    # semantically identical. Default: drop the key. Opt-out via
    # ``omit_nulls: false`` on the JsonCollapseDirective.
    omit_nulls = jc.omit_nulls if jc else True
    emit_set = EmitSet()

    def _key(col: str) -> str:
        # Normalize then apply the post-normalizer alias map (api_name drift).
        norm = normalizer(col)
        return aliases.get(norm, norm)

    for row in rows:
        if not any(_clean(v) for v in row.values()):
            continue  # skip fully-empty rows

        record: dict[str, Any] = {}
        links: dict[str, EmitRef] = {}

        # 1. Main record id.
        main_id: str | None = None
        if directives.id_from is not None:
            main_id = _clean(_resolve_path(row, directives.id_from)) or None
            if main_id is not None:
                record[directives.id_column] = main_id

        # 2. Constant columns on the main record.
        record.update(directives.constants)

        # 2b. Direct source -> main-record column passthrough. Applied after
        # constants so a ``columns`` mapping can override a constant when both
        # are configured (last-write-wins; configuring both for the same target
        # is a config-author choice, not an engine concern). Non-string values
        # are kept native — insert_emit_set JSON-encodes dict/list and the
        # rest pass straight to the DB driver.
        for source, target in directives.columns.items():
            value = _resolve_path(row, source)
            if value is not None:
                record[target] = value

        # 3. Extract sub-entities (e.g. owner -> users): map fields, apply
        #    constants + computed, dedupe, and FK-link back to the main record.
        for ex in directives.extract:
            sub: dict[str, Any] = {
                target: (_clean(_resolve_path(row, source)) or None)
                for target, source in ex.fields.items()
            }
            sub.update(ex.constants)
            _apply_computed(sub, None, ex.computed)
            key_vals = tuple(sub.get(col) for col in ex.dedup_on)
            if any(v is not None for v in key_vals):
                emit_set.add(
                    Emit(
                        table=ex.into,
                        record=sub,
                        upsert_key=tuple(ex.dedup_on),
                        id_column=ex.id_column,
                    )
                )
                links[ex.fk] = EmitRef(ex.into, key_vals)

        # 4. Declared nested lookup objects -> JSON column on the main record.
        for ne in directives.nested:
            name_val = _clean(_resolve_path(row, ne.base))
            id_val = _clean(_resolve_path(row, ne.base + ne.id_suffix))
            if name_val or id_val:
                container = record.setdefault(ne.into, {})
                container[ne.key] = {
                    ne.id_field: id_val or None,
                    ne.name_field: name_val or None,
                }

        # 5. Collapse remaining columns into the JSON/EAV column, optionally
        #    auto-nesting <X> + <X><id_suffix> pairs into {id, name}.
        if json_into is not None:
            container = record.setdefault(json_into, {})
            remaining = [col for col in row if col not in consumed]
            remaining_set = set(remaining)
            handled: set[str] = set()
            for col in remaining:
                if col in handled:
                    continue
                if nest_pairs and suffix and col.endswith(suffix):
                    base = col[: -len(suffix)]
                    if base in remaining_set:
                        id_val = _clean(row.get(col))
                        name_val = _clean(row.get(base))
                        if id_val or name_val:
                            container[_key(base)] = {"id": id_val or None, "name": name_val or None}
                        handled.add(base)
                        handled.add(col)
                        continue
                if nest_pairs and suffix and (col + suffix) in remaining_set:
                    # Base whose .id sibling exists -> emitted by the .id branch.
                    handled.add(col)
                    continue
                key = _key(col)
                if not key:
                    continue
                raw = row.get(col)
                # Preserve native JSON-shaped values (dict/list) verbatim — the
                # source format already gave us structure, no point flattening
                # to a stringified repr. Scalars and None go through _clean()
                # for whitespace-stripping + str coercion consistency.
                if isinstance(raw, dict | list):
                    value: Any = raw
                else:
                    value = _clean(raw) or None
                if value is None and omit_nulls:
                    continue
                container[key] = value

        # 6. Computed main-record columns (after fields/links exist).
        _apply_computed(record, links, directives.computed)

        # 7. Emit the main record (links resolved to ids by resolve_emits).
        emit_set.add(
            Emit(
                table=table,
                record=record,
                upsert_key=(directives.id_column,) if main_id is not None else None,
                links=links,
                id_column=directives.id_column,
            )
        )

        # 8. Multi-value columns -> junction rows linked to the main record.
        for mv in directives.multi_value:
            resolved = _resolve_path(row, mv.column)
            # Accept either a pre-split list (JSON reader emitting a native
            # array) or a delimiter-joined string (CSV-style).
            if isinstance(resolved, list):
                pieces: list[str] = [str(item) for item in resolved]
            else:
                raw_value = _clean(resolved)
                if not raw_value:
                    continue
                pieces = raw_value.split(mv.delimiter)
            seen: set[str] = set()
            for piece in pieces:
                value = piece.strip() if isinstance(piece, str) else str(piece).strip()
                if not value or (mv.dedup and value in seen):
                    continue
                seen.add(value)
                junction: dict[str, Any] = {mv.value: value}
                if main_id is not None:
                    junction[mv.fk] = main_id
                emit_set.add(Emit(table=mv.into, record=junction))

    return emit_set

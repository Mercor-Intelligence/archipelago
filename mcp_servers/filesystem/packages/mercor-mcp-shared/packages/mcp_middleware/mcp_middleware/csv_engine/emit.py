"""Multi-emit transform primitive for the CSV import engine.

The classic engine maps one CSV row to one row in one table (or, via
``group_by``, to a parent+child pair). Real-world domain imports need a single
input row to fan out into *several* tables — e.g. a wide CRM export row that
produces a main record, hoists its owner into a deduplicated ``users`` table,
and explodes a delimited tag column into a junction table.

This module introduces that capability as a small, declarative data model:

- :class:`Emit` — "insert this ``record`` into this ``table``", optionally
  deduplicated on an ``upsert_key`` and optionally carrying foreign-key
  ``links`` to rows emitted into other tables.
- :class:`EmitSet` — an ordered collection of emits produced from a CSV.
- :func:`resolve_emits` — collapses an EmitSet into ordered, per-table insert
  batches: it deduplicates upsert rows, assigns surrogate ids where needed,
  resolves FK links to the (possibly merged) target row's id, and orders the
  batches so parent tables are inserted before children.

The existing flat / denormalized transforms are expressible as EmitSets (see
``EntityImporter.to_emits``), so this generalizes the current model without
changing its behavior. Declarative fan-out directives (EAV/JSON collapse,
sub-entity extraction, multi-value junctions, nested lookups) will compile
down to EmitSets in a later phase.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "Emit",
    "EmitRef",
    "EmitSet",
    "TableBatch",
    "resolve_emits",
]


@dataclass(frozen=True)
class EmitRef:
    """A reference to another emitted row, resolved to its id at resolve time.

    Used to express a foreign key whose target id is not known when the emit is
    produced (e.g. a surrogate id assigned to a deduplicated sub-entity).

    Attributes:
        table: Target table the referenced row is emitted into.
        key: The target row's ``upsert_key`` *values*, in ``upsert_key`` order.
    """

    table: str
    key: tuple[Any, ...]


@dataclass
class Emit:
    """A single "insert ``record`` into ``table``" instruction.

    Attributes:
        table: Destination table name.
        record: Column -> value mapping for the row.
        upsert_key: Column names whose values uniquely identify the row. When
            set, rows sharing the same key values are deduplicated (first wins)
            and the row becomes referenceable via :class:`EmitRef`.
        links: Foreign-key columns to fill in after resolution, mapping a
            column name to the :class:`EmitRef` whose resolved id is written
            there.
        id_column: Name of the row's primary-key column (default ``"id"``).
    """

    table: str
    record: dict[str, Any]
    upsert_key: tuple[str, ...] | None = None
    links: dict[str, EmitRef] = field(default_factory=dict)
    id_column: str = "id"

    def key_values(self) -> tuple[Any, ...] | None:
        """Return this emit's upsert key values, or ``None`` if not an upsert."""
        if self.upsert_key is None:
            return None
        return tuple(self.record.get(col) for col in self.upsert_key)


@dataclass
class EmitSet:
    """An ordered collection of :class:`Emit` instructions."""

    emits: list[Emit] = field(default_factory=list)

    def add(self, emit: Emit) -> None:
        """Append a single emit."""
        self.emits.append(emit)

    def extend(self, emits: Iterable[Emit]) -> None:
        """Append several emits."""
        self.emits.extend(emits)

    def tables(self) -> list[str]:
        """Distinct destination tables, in first-seen order."""
        seen: dict[str, None] = {}
        for emit in self.emits:
            seen.setdefault(emit.table, None)
        return list(seen)

    def __iter__(self) -> Iterator[Emit]:
        return iter(self.emits)

    def __len__(self) -> int:
        return len(self.emits)


@dataclass
class TableBatch:
    """Resolved rows destined for a single table, in insertion order."""

    table: str
    records: list[dict[str, Any]]


def resolve_emits(
    emit_set: EmitSet | Iterable[Emit],
    *,
    table_order: Sequence[str] | None = None,
    id_factory: Callable[[str], Any] | None = None,
) -> list[TableBatch]:
    """Collapse emits into ordered, per-table insert batches.

    Steps:
        1. Deduplicate upsert rows (first occurrence wins) and assign a
           surrogate id via ``id_factory`` when an upsert row has no value in
           its id column.
        2. Index every upsert row by ``(table, key_values)`` -> id so links can
           resolve.
        3. Resolve each emit's ``links`` (``fk_column`` <- referenced row id).
        4. Emit batches ordered by ``table_order`` (FK-topological) when given,
           otherwise by first-seen table order. Tables absent from
           ``table_order`` are appended in first-seen order.

    Args:
        emit_set: The emits to resolve.
        table_order: Optional table ordering (e.g. from a FK-topological sort)
            so parent tables are inserted before children.
        id_factory: Optional ``table -> id`` callable used to mint surrogate
            ids for upsert rows lacking one. If omitted, such rows keep whatever
            (possibly missing) id they already have.

    Returns:
        One :class:`TableBatch` per destination table that has rows, ordered.
    """
    emits = list(emit_set)

    # (table, key_values) -> resolved id, for link resolution.
    ref_index: dict[tuple[str, tuple[Any, ...]], Any] = {}
    # Deduplicated upsert rows per table (key_values -> record), order-preserving.
    upsert_rows: dict[str, dict[tuple[Any, ...], dict[str, Any]]] = {}
    # Plain (non-upsert) rows per table, in insertion order.
    plain_rows: dict[str, list[dict[str, Any]]] = {}
    first_seen: list[str] = []

    # Links are resolved in a second pass once every id is known.
    deferred_links: list[tuple[dict[str, Any], dict[str, EmitRef]]] = []

    for emit in emits:
        if emit.table not in first_seen:
            first_seen.append(emit.table)

        key = emit.key_values()
        is_duplicate = False
        if key is not None:
            table_map = upsert_rows.setdefault(emit.table, {})
            existing = table_map.get(key)
            if existing is not None:
                # Duplicate upsert row: first wins; reuse the stored record.
                record = existing
                is_duplicate = True
            else:
                record = emit.record
                if id_factory is not None and not record.get(emit.id_column):
                    record[emit.id_column] = id_factory(emit.table)
                table_map[key] = record
                ref_index[(emit.table, key)] = record.get(emit.id_column)
        else:
            record = emit.record
            plain_rows.setdefault(emit.table, []).append(record)

        # Skip the duplicate's links to keep first-wins semantics for the FK
        # columns on the kept record. The original emit's links were already
        # appended on its own pass; appending the duplicate's would race the
        # original in pass 2 and make FK resolution non-deterministic
        # whenever the two emits carry different ``EmitRef`` values for the
        # same FK column.
        if emit.links and not is_duplicate:
            deferred_links.append((record, emit.links))

    # Pass 2: resolve foreign-key links now that all ids are known.
    for record, links in deferred_links:
        for fk_col, ref in links.items():
            resolved = ref_index.get((ref.table, ref.key))
            if resolved is not None:
                record[fk_col] = resolved

    # Pass 3: assemble ordered batches.
    ordered_tables: list[str] = []
    if table_order:
        for table in table_order:
            if (table in upsert_rows or table in plain_rows) and table not in ordered_tables:
                ordered_tables.append(table)
    for table in first_seen:
        if table not in ordered_tables:
            ordered_tables.append(table)

    batches: list[TableBatch] = []
    for table in ordered_tables:
        records = list(upsert_rows.get(table, {}).values()) + plain_rows.get(table, [])
        if records:
            batches.append(TableBatch(table=table, records=records))
    return batches

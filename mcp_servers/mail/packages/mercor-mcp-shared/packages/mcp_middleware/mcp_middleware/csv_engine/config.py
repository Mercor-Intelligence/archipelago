"""Configuration loader for snapshot YAML files."""

from __future__ import annotations

from collections.abc import AsyncIterable, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

if TYPE_CHECKING:
    import polars as pl


@dataclass
class ColumnMapping:
    """Maps a database column to a wire-side field with optional formatting.

    Field naming (``csv``) is historic — this dataclass predates the JSON
    reader (see :mod:`~mcp_middleware.csv_engine.readers`), when the
    wire format was CSV-only. Semantically it is now the *wire-side
    field name regardless of serialization*: for a CSV-configured
    entity the CSV header string, for a JSON-configured entity the JSON
    key string. The reader registry routes bytes → records identically
    for both formats, and downstream directive transforms + inserts
    treat records as plain ``dict[str, Any]``; the wire name is the
    same object in both worlds, just serialized differently.

    The name is left as ``csv`` rather than renamed because every
    existing ``snapshot_config.yaml`` and every generated column
    mapping in the Foundry-* apps refers to this attribute by name; a
    rename would ripple through every consumer for zero functional
    gain. Reading a JSON-configured entity's ``csv: "Deal Name"`` as
    "the wire-side name is ``Deal Name``" is the intended parse.

    Attributes:
        db: DB column name.
        csv: Wire-side field name — CSV header OR JSON key, depending
            on the entity's format (resolved via
            :func:`~mcp_middleware.csv_engine.readers.resolve_format`
            against ``entity.files[0]``).
        format: Optional value coercion — ``"date"``, ``"decimal"``,
            ``"bool"``. Applies identically across serializations; the
            underlying DB value is what's coerced, wire representation
            follows.
        default: Fallback value emitted when the DB column is NULL.
    """

    db: str
    csv: str
    format: str | None = None  # "date", "decimal", "bool"
    default: str | None = None


@dataclass
class ExportConfig:
    """Export configuration for an entity.

    Both ``query`` and ``columns`` may be empty, in which case the engine
    auto-generates them from the DB schema at runtime.
    """

    query: str = ""
    columns: list[ColumnMapping] = field(default_factory=list)


@dataclass
class ImportSignature:
    """Header signature for detecting entity type from CSV headers.

    When ``auto_required`` is ``True``, the ``required`` set was not
    explicitly provided in the YAML and will be filled from the DB schema
    at runtime (NOT NULL columns without defaults).
    """

    required: set[str] = field(default_factory=set)
    optional: set[str] = field(default_factory=set)
    aliases: dict[str, str] = field(default_factory=dict)
    auto_required: bool = False


@dataclass
class ComputedField:
    """A declaratively-derived column value (import-only; never a source CSV
    column, so export ignores it).

    Exactly one strategy is used, checked in order:

    - ``template``: a ``str.format`` template over the record built so far,
      e.g. ``"{id}@imported.local"`` (missing/None values format as ``""``).
    - ``copy_from``: copy another column's value. If that column is a resolved
      FK *link*, the copy becomes the same link (resolves to the same id).
    - ``split_first`` / ``split_rest``: split the named column on the first run
      of whitespace; ``split_first`` is the first token, ``split_rest`` the rest.
    """

    template: str | None = None
    copy_from: str | None = None
    split_first: str | None = None
    split_rest: str | None = None


@dataclass
class ExtractDirective:
    """Hoist a sub-entity (e.g. an owner/account/user) into another table.

    The extracted row is deduplicated on ``dedup_on`` and the resolved target
    id is written back onto the main record's ``fk`` column.

    Attributes:
        into: Target table for the extracted sub-entity.
        fields: Mapping of target column -> source CSV column.
        dedup_on: Target columns forming the dedup/upsert key.
        fk: Column on the *main* record that receives the resolved target id.
        id_column: Primary-key column of the target table (default ``"id"``).
        constants: Constant column values set on every extracted row (e.g. a
            NOT NULL ``status`` / ``role_id``). Not source columns.
        computed: Derived columns for the extracted row (see ``ComputedField``).
    """

    into: str
    fields: dict[str, str]
    dedup_on: list[str]
    fk: str
    id_column: str = "id"
    constants: dict[str, str] = field(default_factory=dict)
    computed: dict[str, ComputedField] = field(default_factory=dict)


@dataclass
class MultiValueDirective:
    """Explode a delimited column into rows in a junction table.

    Attributes:
        column: Source CSV column holding the delimited values.
        into: Junction table to receive one row per value.
        value: Junction column that stores each split value.
        fk: Junction column that stores the main record's id.
        delimiter: Value separator (default ``","``).
    """

    column: str
    into: str
    value: str
    fk: str
    delimiter: str = ","
    dedup: bool = True


@dataclass
class NestedDirective:
    """Pair ``<base>`` and ``<base><id_suffix>`` columns into a nested
    ``{id, name}`` object stored under ``key`` inside the main record's JSON
    column (``into``).

    Attributes:
        base: Source CSV column holding the display name.
        into: JSON column on the main record to store the nested object.
        key: Object key inside the JSON column.
        id_suffix: Suffix of the paired id column (default ``".id"``).
        name_field: Key for the name inside the nested object (default ``"name"``).
        id_field: Key for the id inside the nested object (default ``"id"``).
    """

    base: str
    into: str
    key: str
    id_suffix: str = ".id"
    name_field: str = "name"
    id_field: str = "id"


@dataclass
class JsonCollapseDirective:
    """Collapse all otherwise-unconsumed columns into one JSON column.

    Keys are normalized via the directives' ``key_normalizer``.

    Attributes:
        into: JSON column on the main record that receives the remainder.
        nest_id_pairs: When True, auto-detect any ``<X>`` + ``<X><id_suffix>``
            column pair in the remainder and emit ``{id, name}`` under
            ``normalizer(X)`` instead of two flat scalar keys (generalizes
            ``nested`` for arbitrary lookup columns).
        id_suffix: Suffix marking the id half of an auto-nest pair
            (default ``".id"``).
        omit_nulls: When True (default), drop keys whose source CSV value is
            empty/None from the JSON blob. In an EAV/sparse-JSON column, a
            missing key and an explicit ``null`` carry the same meaning, and
            omitting empty cells matches the conventional bespoke importer
            output. Set to ``false`` only when a downstream consumer needs to
            distinguish "column declared but empty on this row" from "column
            absent from this module's schema".
    """

    into: str
    nest_id_pairs: bool = False
    id_suffix: str = ".id"
    omit_nulls: bool = True


@dataclass
class ImportDirectives:
    """Declarative one-row -> many-tables fan-out for ETL-style imports.

    Lets a single wide CSV row produce a main record plus hoisted sub-entities,
    junction rows, nested lookup objects, and a JSON/EAV remainder column —
    without bespoke importer code. Keep ``table ~= CSV`` as the default; this is
    opt-in per entity.

    Directive mode vs. flat mode
    ----------------------------

    Setting ``directives:`` on an entity is **not** a layer added on top of
    flat-mode auto-mapping: it replaces it entirely. With directives, the only
    columns written to the main record are the ones explicitly produced by a
    directive (``id_from`` / ``columns`` / ``constants`` / ``computed`` /
    derived sub-records via ``extract`` / nested objects via
    ``nested`` / ``json_collapse``). Any source column **not** consumed by a
    directive is silently dropped (or, if ``json_collapse`` is present, swept
    into the EAV remainder column).

    Flat mode — the default when no ``directives:`` block is set —
    auto-copies every source column whose name matches a DB column on the
    entity's table, after running the registered ``RowHook``s. Use it when
    your tables are strongly-typed per domain (Drive / Gmail / Docs / …);
    use ``directives:`` when one wide source row needs to fan out into
    several DB tables (Zoho-style record + tags + extracted owner).

    Snapshot
    --------

    The snapshot inverse follows the same split:

    * Flat mode  -> :func:`~mcp_middleware.csv_engine.sync.export_snapshot_sync`
      (schema-driven, one CSV per entity, no directive support needed).
    * Directive mode -> :func:`~mcp_middleware.csv_engine.sync.snapshot_directory_sync`
      (directive-driven; silently skips entities without ``directives:``
      because there is no wide-CSV shape to reconstruct).
    """

    id_from: str | None = None
    id_column: str = "id"
    key_normalizer: str = "default"
    extract: list[ExtractDirective] = field(default_factory=list)
    multi_value: list[MultiValueDirective] = field(default_factory=list)
    nested: list[NestedDirective] = field(default_factory=list)
    json_collapse: JsonCollapseDirective | None = None
    # Constant column values set on every main record (e.g. a NOT NULL
    # ``module_api_name`` / ``approval_state`` / sample timestamps). Not source
    # columns, so export ignores them.
    constants: dict[str, str] = field(default_factory=dict)
    # Derived main-record columns (see ``ComputedField``). Import-only.
    computed: dict[str, ComputedField] = field(default_factory=dict)
    # Post-normalizer key rename for EAV/nested keys (api_name drift), e.g.
    # ``{"Deal_Name": "Potential_Name"}``. Inverted on export.
    aliases: dict[str, str] = field(default_factory=dict)
    # Source-column -> main-record-column mapping for typed sources whose
    # input columns map 1:1 to DB columns (e.g. the ``file_content`` reader
    # emitting ``filename`` / ``mime_type`` / ``size_bytes`` directly into a
    # ``documents`` table). Each ``{source: target}`` pair sets
    # ``record[target] = row[source]`` on the main record before any
    # collapse/EAV step, and the source column is considered consumed (so
    # ``json_collapse`` doesn't duplicate it into the JSON remainder). Useful
    # for narrow, schema-aligned sources where ``extract`` / ``multi_value`` /
    # ``nested`` / ``json_collapse`` would be the wrong shape. Snapshot
    # inversion is not yet supported for ``columns`` — binary-format entities
    # are skipped by ``snapshot_directory`` and typed-JSON entities should
    # rely on ``wide_columns`` for round-trip ordering. Future work can wire
    # the inverse if a use case emerges.
    columns: dict[str, str] = field(default_factory=dict)
    # Ordered original wide-CSV headers, used to reconstruct a byte-stable wide
    # CSV on export (round-trip). When empty, export derives a deterministic
    # column order from the directives + observed EAV keys.
    wide_columns: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ColumnTransformsConfig:
    """Parsed ``column_transforms`` block from YAML.

    Maps each (canonical, post-key-normalizer) column name to an ordered
    pipeline of :class:`~mcp_middleware.csv_engine.transforms.Transform`
    instances. The instances are constructed at parse time from the YAML-
    supplied kwargs — there is no late binding between YAML strings and
    classes during import. Re-registering a name after a config has been
    parsed has no effect on already-instantiated pipelines.

    Example YAML::

        import:
          column_transforms:
            title:       [{ spongebob: { start_index: 3 } }]
            summary:     [strip, lowercase, { default: { value: "(none)" } }]

    Parses to::

        ColumnTransformsConfig(columns={
            "title":   [SpongebobTransform(start_index=3)],
            "summary": [StripTransform(), LowercaseTransform(),
                        DefaultTransform(value="(none)")],
        })
    """

    columns: dict[str, list[Any]] = field(default_factory=dict)


@dataclass
class ImportConfig:
    """Import configuration for an entity."""

    signatures: ImportSignature
    id_prefix: str = ""
    child_id_prefix: str = ""
    dedup_key: list[str] = field(default_factory=list)
    group_by: str | None = None
    parent_columns: list[str] = field(default_factory=list)
    child_columns: list[str] = field(default_factory=list)
    child_column_strip_prefix: str = ""
    parent_field_map: dict[str, str] = field(default_factory=dict)
    child_field_map: dict[str, str] = field(default_factory=dict)
    total_field: str | None = None
    total_from: str | None = None
    directives: ImportDirectives | None = None
    column_transforms: ColumnTransformsConfig | None = None


# Strategies :func:`~mcp_middleware.csv_engine.importer.apply_import_always`
# knows how to apply. Only ``"clear"`` (empty the target table(s), then insert
# the shipped rows) is implemented today; declared as a set so the loader can
# reject unknown values up front and future strategies plug in here.
IMPORT_ALWAYS_STRATEGIES = frozenset({"clear"})


@dataclass
class EntityConfig:
    """Configuration for a single entity type."""

    name: str
    table: str
    child_table: str | None = None
    export: ExportConfig | None = None
    import_config: ImportConfig | None = None
    # Filename globs that route a CSV to this entity, taking precedence over
    # header-signature detection (needed when several modules share the same
    # envelope headers). Multiple entities may target the same table.
    files: list[str] = field(default_factory=list)
    # When True, this entity's shipped source rows are applied even when the
    # snapshot pipeline harvested an SME-shipped pre-built DB (the branch that
    # normally skips *all* raw-source import). Lets an SME ship an authoritative
    # world DB *plus* a small override CSV/JSON for this one entity — e.g. a
    # ``current_user.csv`` that re-points a singleton without rebuilding the
    # world. See :func:`~mcp_middleware.csv_engine.snapshot_with_populate`.
    import_always: bool = False
    # How the override is reconciled against the pre-built table when
    # ``import_always`` is set. Only ``"clear"`` (empty the target table(s),
    # then insert the shipped rows) is implemented; validated at load against
    # :data:`IMPORT_ALWAYS_STRATEGIES`.
    import_strategy: str = "clear"


@dataclass
class SourceMapping:
    """Maps a filename glob to a registered reader format.

    Used at the snapshot level (``SnapshotConfig.sources``) to pick the right
    reader (CSV, JSON, custom-registered) for each input file before any
    entity-routing or directive transform runs. The first matching glob wins.

    Attributes:
        glob: Glob pattern matched via :func:`~readers._glob_match`
              (e.g. ``"*.csv"``, ``"**/*.json"``, ``"*/**/*.csv"``).
        format: A name registered in ``readers._READERS`` (``"csv"`` and
            ``"json"`` ship by default; apps add more via
            :func:`~mcp_middleware.csv_engine.readers.register_reader`).
    """

    glob: str
    format: str


# Hook types
RowHook = Callable[[dict[str, Any]], dict[str, Any] | None]  # transform or skip (None)
GroupHook = Callable[[list[dict[str, Any]]], None]  # validate grouped rows (raise on error)
PreTransformHook = Callable[
    [list[dict[str, str]]], list[dict[str, str]]
]  # all rows before transform — sync or async (engine awaits if coroutine)
PostImportHook = Callable[[str, AsyncIterable[dict[str, Any]], Any], Awaitable[None]]
BatchObserver = Callable[["pl.DataFrame"], None]
"""Side-effect-only callback invoked after each batch's polars_expr hook chain
runs and is collected for insert.

Receives the collected :class:`polars.DataFrame` for that batch. Observers are
guaranteed to:

* Be called exactly once per batch, in registration order.
* Receive the *same* DataFrame reference the insert step uses (no copy).
* See the final post-hook state — every ``polars_expr`` and per-row callable
  hook has already run.

Observers return ``None``; the engine ignores any return value. Exceptions
raised inside an observer propagate to the caller — the import aborts. This is
intentional: an observer that needs error-tolerance has to do its own
``try / except`` internally.

The primary use case is **side-effect accumulation** across batches without
forcing the records-form materialisation upstream. The motivating example —
Foundry-Google-Workspace's email-ref accumulator — reads derived columns off
the collected DataFrame and folds them into a long-lived ``dict[email, name]``
that the post-import synthesis pass consumes. The accumulator state lives on
the consumer side; the engine just gives them the hook point.
"""
"""Post-insert hook signature: ``(table_name, records, db_conn) -> Awaitable[None]``.

``records`` is an :class:`~collections.abc.AsyncIterable` of record dicts —
hooks consume it with ``async for record in records: …`` so the engine can
stream records to the hook lazily. For the **accumulation** import path the
async iterable yields from the per-entity records list that was already
built in memory (zero extra cost). For the **streaming** flat-entity path
the async iterable wraps a paginated DB cursor (``SELECT * FROM <table>``
with ``LIMIT/OFFSET``), so a hook on a 118k-row entity never has to
materialise the whole list — it iterates one page at a time.

The signature was tightened from ``list[dict[…]]`` to
``AsyncIterable[dict[…]]`` in the streaming-insert work so flat-entity
post-hooks (e.g. Foundry-Google-Workspace's ``_synthesize_gmail_users``
PostImportHook on ``gmail-settings``) keep working without forcing the
streaming path to re-accumulate every record into memory.
"""


@dataclass
class SnapshotConfig:
    """Top-level snapshot configuration loaded from YAML."""

    entities: dict[str, EntityConfig] = field(default_factory=dict)
    import_options: dict[str, Any] = field(default_factory=dict)
    # Ordered list of (glob, reader-format) mappings, opt-in. When empty (no
    # ``sources:`` block in the YAML), the engine behaves exactly as a CSV-only
    # importer — file discovery picks up ``*.csv``, every file is parsed as
    # CSV, and existing applications continue to work without any config
    # change. Apps that want JSON or custom format support declare globs here.
    sources: list[SourceMapping] = field(default_factory=list)
    _row_hooks: dict[str, list[RowHook]] = field(default_factory=dict)
    _group_hooks: dict[str, list[GroupHook]] = field(default_factory=dict)
    _pre_transform_hooks: dict[str, list[PreTransformHook]] = field(default_factory=dict)
    _post_import_hooks: dict[str, list[PostImportHook]] = field(default_factory=dict)
    _batch_observers: dict[str, list[BatchObserver]] = field(default_factory=dict)

    def register_row_hook(self, entity_name: str, hook: RowHook) -> None:
        """Register a hook to transform/filter rows during import."""
        self._row_hooks.setdefault(entity_name, []).append(hook)

    def register_group_hook(self, entity_name: str, hook: GroupHook) -> None:
        """Register a hook to validate grouped rows during import."""
        self._group_hooks.setdefault(entity_name, []).append(hook)

    def register_pre_transform_hook(self, entity_name: str, hook: PreTransformHook) -> None:
        """Register a hook that receives all rows before transformation.

        Useful for format detection that needs the full dataset (e.g. detecting
        whether accounts CSV is in QB export format vs DB format).
        """
        self._pre_transform_hooks.setdefault(entity_name, []).append(hook)

    def register_post_import_hook(self, entity_name: str, hook: PostImportHook) -> None:
        """Register an async hook that runs after records are inserted.

        Receives (table_name, inserted_records, db_connection). Runs inside the
        same transaction so failures trigger rollback. Used for things like
        auto-creating journal entries after importing bills/invoices/payments.
        """
        self._post_import_hooks.setdefault(entity_name, []).append(hook)

    def register_batch_observer(self, entity_name: str, observer: BatchObserver) -> None:
        """Register a side-effect callback invoked after each batch's hook
        chain runs and is collected for insert.

        See :data:`BatchObserver` for the full contract. The motivating use
        case is **cross-batch side-effect accumulation** without forcing the
        records-form materialisation upstream — e.g. reducing derived
        columns from each batch's collected DataFrame into a long-lived
        accumulator the consumer owns.

        Multiple observers can be registered per entity; they fire in
        registration order. The engine guarantees exactly one call per
        batch with the same DataFrame reference the insert step uses.
        """
        self._batch_observers.setdefault(entity_name, []).append(observer)

    def get_row_hooks(self, entity_name: str) -> list[RowHook]:
        return self._row_hooks.get(entity_name, [])

    def get_group_hooks(self, entity_name: str) -> list[GroupHook]:
        return self._group_hooks.get(entity_name, [])

    def get_pre_transform_hooks(self, entity_name: str) -> list[PreTransformHook]:
        return self._pre_transform_hooks.get(entity_name, [])

    def get_post_import_hooks(self, entity_name: str) -> list[PostImportHook]:
        return self._post_import_hooks.get(entity_name, [])

    def get_batch_observers(self, entity_name: str) -> list[BatchObserver]:
        return self._batch_observers.get(entity_name, [])

    def import_always_entities(self) -> list[str]:
        """Names of entities flagged ``import_always: true``, in config order.

        These are applied on top of a harvested pre-built DB by
        :func:`~mcp_middleware.csv_engine.snapshot_with_populate` even though
        the normal import step is skipped in that regime. Empty list when no
        entity opts in (the common case), so the override machinery is a no-op.
        """
        return [name for name, entity in self.entities.items() if entity.import_always]

    def find_entity_by_table(self, table_name: str) -> EntityConfig | None:
        """Look up an entity config by its table name (or child_table name).

        Prioritizes exact ``table`` matches over ``child_table`` matches
        so that entities whose primary table happens to share a name with
        another entity's child table are resolved correctly.
        """
        # First pass: exact primary table match
        for entity in self.entities.values():
            if entity.table == table_name:
                return entity
        # Second pass: child table match
        for entity in self.entities.values():
            if entity.child_table == table_name:
                return entity
        return None


def _parse_column_mapping(raw: dict[str, Any]) -> ColumnMapping:
    return ColumnMapping(
        db=raw["db"],
        csv=raw["csv"],
        format=raw.get("format"),
        default=raw.get("default"),
    )


def _parse_export_config(raw: dict[str, Any]) -> ExportConfig:
    return ExportConfig(
        query=raw.get("query", "").strip(),
        columns=[_parse_column_mapping(c) for c in raw.get("columns", [])],
    )


def _parse_import_signature(raw: dict[str, Any]) -> ImportSignature:
    has_required = "required" in raw
    return ImportSignature(
        required=set(raw["required"]) if has_required else set(),
        optional=set(raw.get("optional", [])),
        aliases=dict(raw.get("aliases", {})),
        auto_required=not has_required,
    )


def _parse_computed(raw: dict[str, Any]) -> dict[str, ComputedField]:
    return {
        col: ComputedField(
            template=spec.get("template"),
            copy_from=spec.get("copy_from"),
            split_first=spec.get("split_first"),
            split_rest=spec.get("split_rest"),
        )
        for col, spec in raw.items()
    }


def _parse_directives(raw: dict[str, Any]) -> ImportDirectives:
    json_collapse_raw = raw.get("json_collapse")
    return ImportDirectives(
        id_from=raw.get("id_from"),
        id_column=raw.get("id_column", "id"),
        key_normalizer=raw.get("key_normalizer", "default"),
        extract=[
            ExtractDirective(
                into=e["into"],
                fields=dict(e["fields"]),
                dedup_on=list(e.get("dedup_on", ["id"])),
                fk=e["fk"],
                id_column=e.get("id_column", "id"),
                constants=dict(e.get("constants", {})),
                computed=_parse_computed(e.get("computed", {})),
            )
            for e in raw.get("extract", [])
        ],
        multi_value=[
            MultiValueDirective(
                column=m["column"],
                into=m["into"],
                value=m["value"],
                fk=m["fk"],
                delimiter=m.get("delimiter", ","),
                dedup=m.get("dedup", True),
            )
            for m in raw.get("multi_value", [])
        ],
        nested=[
            NestedDirective(
                base=n["base"],
                into=n["into"],
                key=n["key"],
                id_suffix=n.get("id_suffix", ".id"),
                name_field=n.get("name_field", "name"),
                id_field=n.get("id_field", "id"),
            )
            for n in raw.get("nested", [])
        ],
        json_collapse=(
            JsonCollapseDirective(
                into=json_collapse_raw["into"],
                nest_id_pairs=json_collapse_raw.get("nest_id_pairs", False),
                id_suffix=json_collapse_raw.get("id_suffix", ".id"),
                omit_nulls=json_collapse_raw.get("omit_nulls", True),
            )
            if json_collapse_raw
            else None
        ),
        constants=dict(raw.get("constants", {})),
        computed=_parse_computed(raw.get("computed", {})),
        aliases=dict(raw.get("aliases", {})),
        columns=dict(raw.get("columns", {})),
        wide_columns=list(raw.get("wide_columns", [])),
    )


def _parse_column_transforms(raw: Any, entity_label: str = "<entity>") -> ColumnTransformsConfig:
    """Parse the YAML ``column_transforms`` block into instantiated transforms.

    The YAML shape is ``dict[str, list[entry]]`` where each entry is one of:

    * a bare string — names a registered parameter-less transform
      (``strip``, ``lowercase``);
    * a single-key mapping ``{ name: { …kwargs } }`` — names a registered
      transform and supplies its ``__init__`` kwargs.

    All names must be registered (built-in or via ``register_transform``)
    by the time this parser runs; ``get_transform`` raises ``KeyError``
    on unknown names with a typo-friendly list of known names.

    The ``entity_label`` parameter just shows up in error messages so a
    misconfigured pipeline points at the right entity.
    """
    # Late import: csv_engine.transforms imports config-adjacent symbols, and
    # importing it at module top would cycle.
    from .transforms import get_transform

    if not isinstance(raw, dict):
        raise ValueError(
            f"{entity_label}.column_transforms: expected a mapping of "
            f"column → list-of-transforms, got {type(raw).__name__}"
        )

    columns: dict[str, list[Any]] = {}
    for col_name, entries in raw.items():
        if not isinstance(entries, list):
            raise ValueError(
                f"{entity_label}.column_transforms.{col_name}: expected a "
                f"list of transforms, got {type(entries).__name__}"
            )
        pipeline: list[Any] = []
        for entry in entries:
            if isinstance(entry, str):
                cls = get_transform(entry)
                pipeline.append(cls())
            elif isinstance(entry, dict) and len(entry) == 1:
                name, params = next(iter(entry.items()))
                cls = get_transform(name)
                if params is None:
                    pipeline.append(cls())
                    continue
                if not isinstance(params, dict):
                    raise ValueError(
                        f"{entity_label}.column_transforms.{col_name}: "
                        f"transform {name!r} params must be a mapping, got "
                        f"{type(params).__name__}"
                    )
                pipeline.append(cls(**params))
            else:
                raise ValueError(
                    f"{entity_label}.column_transforms.{col_name}: invalid "
                    f"entry {entry!r}; expected a string name or a single-key "
                    f"mapping like {{ name: {{ …kwargs }} }}"
                )
        columns[col_name] = pipeline
    return ColumnTransformsConfig(columns=columns)


def _parse_import_config(raw: dict[str, Any], entity_label: str = "<entity>") -> ImportConfig:
    if "signatures" in raw:
        signatures = _parse_import_signature(raw["signatures"])
    else:
        signatures = ImportSignature(auto_required=True)
    column_transforms = (
        _parse_column_transforms(raw["column_transforms"], entity_label=entity_label)
        if "column_transforms" in raw
        else None
    )
    return ImportConfig(
        signatures=signatures,
        id_prefix=raw.get("id_prefix", ""),
        child_id_prefix=raw.get("child_id_prefix", ""),
        dedup_key=list(raw.get("dedup_key", [])),
        group_by=raw.get("group_by"),
        parent_columns=list(raw.get("parent_columns", [])),
        child_columns=list(raw.get("child_columns", [])),
        child_column_strip_prefix=raw.get("child_column_strip_prefix", ""),
        parent_field_map=dict(raw.get("parent_field_map", {})),
        child_field_map=dict(raw.get("child_field_map", {})),
        total_field=raw.get("total_field"),
        total_from=raw.get("total_from"),
        directives=_parse_directives(raw["directives"]) if "directives" in raw else None,
        column_transforms=column_transforms,
    )


def _parse_entity(name: str, raw: dict[str, Any]) -> EntityConfig:
    # Export: present by default (use ``export: false`` to opt out).
    # ``export: true`` or omitting the key both produce an empty ExportConfig
    # whose query/columns will be auto-resolved from the DB schema at runtime.
    export_raw = raw.get("export", True)
    if export_raw is False:
        export = None
    elif export_raw is True:
        export = ExportConfig()
    elif isinstance(export_raw, dict):
        export = _parse_export_config(export_raw)
    else:
        export = None

    import_cfg = _parse_import_config(raw["import"], entity_label=name) if "import" in raw else None

    # Accept either ``files: [glob, ...]`` or ``file: "glob"`` (singular scalar
    # convenience for the common one-file-per-entity case).
    files_raw = raw.get("files", raw.get("file"))
    if files_raw is None:
        files = []
    elif isinstance(files_raw, str):
        files = [files_raw]
    else:
        files = list(files_raw)

    import_always = bool(raw.get("import_always", False))
    import_strategy = str(raw.get("import_strategy", "clear"))
    if import_always and import_strategy not in IMPORT_ALWAYS_STRATEGIES:
        raise ValueError(
            f"entity {name!r}: import_strategy {import_strategy!r} is not supported "
            f"(choose from {sorted(IMPORT_ALWAYS_STRATEGIES)})"
        )

    return EntityConfig(
        name=name,
        table=raw.get("table", name),
        child_table=raw.get("child_table"),
        export=export,
        import_config=import_cfg,
        files=files,
        import_always=import_always,
        import_strategy=import_strategy,
    )


def load_config(yaml_path: Path) -> SnapshotConfig:
    """Load snapshot configuration from a YAML file.

    Returns an empty ``SnapshotConfig`` if the file is empty or contains
    no ``entities`` key, rather than crashing.
    """
    with open(yaml_path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not raw or not isinstance(raw, dict):
        return SnapshotConfig()

    entities = {}
    for name, entity_raw in raw.get("entities", {}).items():
        entities[name] = _parse_entity(name, entity_raw)

    sources: list[SourceMapping] = []
    for item in raw.get("sources", []) or []:
        if not isinstance(item, dict) or "glob" not in item or "format" not in item:
            raise ValueError(
                f"Invalid `sources` entry: {item!r}. "
                "Expected `{glob: <pattern>, format: <name>}`."
            )
        sources.append(SourceMapping(glob=str(item["glob"]), format=str(item["format"])))

    # Pass-through bag of app-level import options (e.g. a hook can reach into
    # ``config.import_options`` for project-specific flags). Empty dict by
    # default — apps that don't set ``import_options:`` in YAML see the same
    # behavior as before this parse was added.
    import_options_raw = raw.get("import_options", {}) or {}
    if not isinstance(import_options_raw, dict):
        raise ValueError(
            f"Invalid `import_options`: expected a mapping, got {type(import_options_raw).__name__}"
        )

    return SnapshotConfig(
        entities=entities,
        sources=sources,
        import_options=dict(import_options_raw),
    )

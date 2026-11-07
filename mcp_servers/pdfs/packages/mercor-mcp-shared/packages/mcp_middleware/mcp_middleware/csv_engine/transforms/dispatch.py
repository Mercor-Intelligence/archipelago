"""Dispatch helpers — apply parsed ``column_transforms`` pipelines per row
(slow path) or vectorised on a polars DataFrame (fast path).

The importer picks between the two based on whether the entity also has
:class:`~mcp_middleware.csv_engine.config.RowHook`\\ s registered:

* **No row hooks → vectorised** (``apply_to_dataframe``).
  Each column's pipeline becomes ``pl.col(name).pipe(t1.apply_polars).pipe(t2.apply_polars)…``,
  chained inside a single ``df.with_columns([…])``. Columns whose pipelines
  include a transform that lacks ``apply_polars`` fall back to
  ``pl.col(name).map_elements(composed_value_fn, return_dtype=pl.Utf8)`` —
  isolated to that column, not the whole entity (so the rest stay
  vectorised).

* **Row hooks present → per-row** (``apply_to_row``).
  csv_engine walks the pipeline per row per column, calling
  :meth:`apply_value` so the hook sees the post-transform values when it
  runs immediately after. No use of ``apply_polars``.

Both helpers key column_transforms by the *original* CSV column name
(matches the polars DataFrame column names directly and avoids a rename
step in the vectorised path). When the YAML names a column that isn't
present in the row / DataFrame (e.g. a column removed from a later
export), the entry is skipped without raising — consistent with the
"transforms are best-effort cell rewrites" mental model.
"""

from __future__ import annotations

from typing import Any

import polars as pl

from ..config import ColumnTransformsConfig


def apply_to_row(row: dict[str, Any], transforms: ColumnTransformsConfig) -> dict[str, Any]:
    """Apply each column's transform pipeline to a single row dict.

    Returns a new dict — does not mutate ``row``. Columns named in
    ``transforms`` but absent from ``row`` are skipped silently.

    The pipeline runs in declared order: the first transform's output
    feeds the second, and so on. A transform's :meth:`apply_value` may
    raise — the exception propagates to the caller (matching the
    per-row hook semantics inside the importer).
    """
    if not transforms.columns:
        return row
    out = dict(row)
    for col_name, pipeline in transforms.columns.items():
        if col_name not in out:
            continue
        v = out[col_name]
        for t in pipeline:
            v = t.apply_value(v)
        out[col_name] = v
    return out


def apply_to_dataframe(df: pl.DataFrame, transforms: ColumnTransformsConfig) -> pl.DataFrame:
    """Apply each column's transform pipeline vectorised on a polars DataFrame.

    Returns a new DataFrame with the transformed columns substituted in.
    Columns named in ``transforms`` but absent from ``df`` are skipped
    silently.

    Per-column dispatch rule:

    * **All transforms in the pipeline expose** ``apply_polars`` →
      chain ``pl.col(name).pipe(t1.apply_polars).pipe(t2.apply_polars)…``.
      Stays entirely in the Rust execution engine.

    * **Any transform lacks** ``apply_polars`` →
      this column falls back to ``pl.col(name).map_elements(fn,
      return_dtype=pl.Utf8)`` where ``fn`` is the per-row composition of
      every transform in the pipeline. The fallback is per-column, not
      per-entity — sibling columns whose pipelines are fully
      vectorisable still run on the Rust side.

    The ``Utf8`` return_dtype on the fallback matches the Phase A
    all-strings reader contract. Transforms that change dtype (e.g.
    :class:`BoolToIntTransform`) handle that themselves on the
    ``apply_polars`` side — their per-row :meth:`apply_value` returning
    an int is fine because ``map_elements`` only runs when
    ``apply_polars`` is missing, and a transform that intentionally
    changes dtype is expected to provide ``apply_polars``.
    """
    if not transforms.columns:
        return df

    available = set(df.columns)
    exprs: list[pl.Expr] = []
    for col_name, pipeline in transforms.columns.items():
        if col_name not in available or not pipeline:
            continue
        expr: pl.Expr = pl.col(col_name)
        if all(hasattr(t, "apply_polars") for t in pipeline):
            for t in pipeline:
                expr = expr.pipe(t.apply_polars)
        else:
            # Per-column fallback: compose the per-row pipeline into one
            # callable and hand to map_elements. Capture the pipeline by
            # default argument so each closure binds its own pipeline.
            def _compose(v: Any, _pipeline: list = pipeline) -> Any:
                for t in _pipeline:
                    v = t.apply_value(v)
                return v

            expr = expr.map_elements(_compose, return_dtype=pl.Utf8)
        exprs.append(expr.alias(col_name))

    if not exprs:
        return df
    return df.with_columns(exprs)


def apply_row_hooks_in_order(
    records: list[dict[str, Any]], hooks: list[Any]
) -> list[dict[str, Any]]:
    """Apply row hooks to records in **registration order**, supporting both
    per-row callables (``dict → dict | None``) and ``polars_expr``-bearing
    hooks (``LazyFrame → LazyFrame``) on the same hook list.

    Walks ``hooks`` in order, maintaining a "current form" — either a
    ``list[dict]`` (records form) or a ``pl.LazyFrame`` (lazy form). A
    hook's kind dictates the form it consumes:

    * **Per-row hook** (no ``polars_expr``): consumes records form. If the
      current form is lazy, collect → iter_rows → list[dict] first. Then
      walk records calling ``hook(record)`` per row; a hook returning
      ``None`` drops the record without releasing its dedup slot
      (eager-dedup contract documented in
      :meth:`EntityImporter.transform_flat`).
    * **polars_expr hook**: consumes lazy form. If the current form is
      records, ``pl.DataFrame(records, infer_schema_length=None).lazy()``
      first — full-batch schema inference is required so that columns
      with optional / variable-length strings (e.g. multi-recipient
      address columns whose first 100 rows happen to be null or short)
      don't lock polars into a Null / short-Utf8 dtype that later batch
      rows can't widen. Then ``state = hook.polars_expr(state)``.
      Consecutive ``polars_expr`` hooks chain on the same LazyFrame so
      polars's planner can fuse them — no intermediate collect.

    Form switches incur a single collect / iter or DataFrame round-trip
    each, bounded by the batch size (~10 000 rows). In the common cases
    (all per-row, or all polars_expr) zero form switches occur. Mixed
    sequences pay one switch per kind transition.

    The brief's "polars_expr is a perf hint with the same observational
    semantics as the dict→dict callable" contract is now exact: both
    forms see records with ``id``, ``created_at``, ``updated_at`` already
    synthesized (the caller, :meth:`transform_flat`, runs ID + timestamp
    assignment in pass 1 before invoking this dispatcher in pass 2). A
    ``polars_expr`` author can reference any column the dict→dict
    equivalent would have seen, including those synthesized IDs.

    Returns the surviving records (after every hook's filtering /
    transformation). Mutates neither the input list nor the hook objects.
    """
    if not records or not hooks:
        return records

    state: list[dict[str, Any]] | pl.LazyFrame = records
    form: str = "records"

    for hook in hooks:
        # Step 1: if state was emptied, stop early.
        if form == "records" and not state:
            return state  # type: ignore[return-value]

        if hook_has_polars_expr(hook):
            # polars_expr hook needs lazy form.
            if form == "records":
                # ``infer_schema_length=None`` scans the entire record list
                # (bounded by ``_IMPORT_BATCH_SIZE`` ≈ 10k rows) so polars
                # picks a dtype wide enough for the longest value in each
                # column. The default of 100 was firing schema-overflow
                # errors on entities where the first 100 records had short
                # / null values in a column but later rows held long
                # strings (e.g. multi-recipient address fields on
                # ``gmail-messages`` — first 100 mails were single-
                # recipient or null, record 101+ carried a comma-joined
                # multi-address string that didn't fit the inferred
                # Null/short-Utf8 dtype). The scan cost is bounded by the
                # batch size and pays for itself the first time it spares
                # us a ``ComputeError`` mid-batch.
                state = pl.DataFrame(state, infer_schema_length=None).lazy()
                form = "lazy"
            state = hook.polars_expr(state)
        else:
            # Per-row hook needs records form.
            if form == "lazy":
                state = list(state.collect().iter_rows(named=True))
                form = "records"
            new_records: list[dict[str, Any]] = []
            for record in state:  # type: ignore[union-attr]
                result = hook(record)
                if result is not None:
                    new_records.append(result)
            state = new_records

    # Final convert if we ended in lazy form.
    if form == "lazy":
        return list(state.collect().iter_rows(named=True))  # type: ignore[union-attr]
    return state  # type: ignore[return-value]


def apply_row_hooks_lazy(lf: pl.LazyFrame, hooks: list[Any]) -> pl.LazyFrame:
    """Apply row hooks to a :class:`polars.LazyFrame` in **registration order**,
    staying in lazy form except when a hook needs Python execution.

    This is the lazy-first counterpart to :func:`apply_row_hooks_in_order`.
    Where the legacy dispatcher takes ``list[dict]`` and pays one
    records→lazy round-trip on the first ``polars_expr`` hook, this
    dispatcher takes a ``LazyFrame`` and never materialises records
    *unless* a per-row callable hook is encountered (the "I/O escape
    hatch" — file reads, decryption, network lookups).

    Per-hook semantics:

    * **polars_expr hook** (vectorised, the normal case)::

        lf = hook.polars_expr(lf)

      Stays entirely in polars's Rust execution engine; the planner can
      fuse consecutive ``polars_expr`` hooks because no collect happens
      between them.

    * **per-row callable hook** (slow path, I/O only)::

        df = lf.collect()
        records = [hook(r) for r in df.iter_rows(named=True) if hook(r) is not None]
        lf = pl.DataFrame(records, schema=<from contract>).lazy()

      The per-row path requires the hook to declare its schema effect
      via optional attributes — without them, the engine has no way to
      recover the schema after the Python walk and would have to fall
      back to inference (which is exactly the failure mode this rewrite
      is removing). Required attributes when the hook adds/removes/
      changes columns:

      * ``hook.added_columns: dict[str, pl.DataType]`` — new columns
        this hook outputs that don't exist on the input schema.
      * ``hook.removed_columns: set[str] | list[str]`` — columns this
        hook drops from the record.
      * ``hook.changed_columns: dict[str, pl.DataType]`` — columns
        whose dtype this hook changes (e.g. ``"priority": Utf8 → Int64``).

      Hooks that touch only existing columns without changing dtypes
      need no contract — the dispatch infers the output schema from the
      LazyFrame's input schema, untouched. If a hook produces records
      with columns not in the (input + contract) schema, the engine
      raises a clear error naming the hook and the undeclared columns.

    Mixed chains (polars_expr → per-row → polars_expr) work but each
    per-row hook forces a ``collect()``. The cost dominates anything the
    surrounding polars_expr fusion would save, so the strong default is
    "stay vectorised; per-row only for genuine I/O."
    """
    for hook in hooks:
        if hook_has_polars_expr(hook):
            lf = hook.polars_expr(lf)
            continue

        # Per-row callable hook: collect, walk, rebuild lazy. The hook
        # contract supplies explicit dtypes for the columns it
        # touches; everything else is inferred from the *full* batch
        # of records (``infer_schema_length=None``) so polars can't
        # lock onto a too-narrow dtype from the first 100 rows.
        input_schema = lf.collect_schema()
        output_schema = _apply_hook_contract(dict(input_schema), hook)

        df = lf.collect()
        new_records: list[dict[str, Any]] = []
        for record in df.iter_rows(named=True):
            result = hook(record)
            if result is not None:
                new_records.append(result)

        if not new_records:
            # All records dropped. Preserve the schema so subsequent
            # polars_expr hooks see the right column types on an empty
            # LazyFrame.
            lf = pl.DataFrame(schema=output_schema).lazy()
            continue

        _validate_records_match_schema(new_records, output_schema, hook)

        # Build the LazyFrame back from records. ``schema_overrides``
        # carries the contract's dtypes (added_columns / changed_columns)
        # — they're authoritative because the hook author declared them.
        # Columns the contract doesn't mention fall back to full-batch
        # inference: polars walks every record (bounded by the batch
        # size ``_IMPORT_BATCH_SIZE``) and picks a dtype wide enough to
        # hold every observed value, so a column whose first 100 rows
        # are null and whose 101st row carries a long string doesn't
        # crash. This is the "contract is permissive about non-declared
        # columns" stance — the alternative (passing ``schema=`` as
        # strict) trips a ``ComputeError`` whenever the records' actual
        # value types disagree with the upstream LazyFrame schema (e.g.
        # the hook itself silently converted ``created_at`` from
        # ``Datetime`` to ``str``). Letting polars infer for
        # non-declared columns means the schema follows the records,
        # not the upstream LazyFrame — which is what the contract
        # actually promises ("the hook's declared dtypes are correct;
        # everything else is up to the hook body").
        overrides = _contract_overrides(hook)
        lf = pl.DataFrame(
            new_records,
            schema_overrides=overrides or None,
            infer_schema_length=None,
        ).lazy()

    return lf


def _contract_overrides(hook: Any) -> dict[str, pl.DataType]:
    """Collect a hook's contract-declared dtype map for use as
    ``schema_overrides`` at the records → lazy re-entry boundary.

    Unlike :func:`_apply_hook_contract` (which evolves a full schema map
    by applying adds / removes / changes on top of an input schema), this
    helper returns *only* the columns the contract explicitly types —
    the hook's ``added_columns`` plus ``changed_columns``. Those are the
    columns where polars must use the contract dtype, not infer. Every
    other column on the rebuilt LazyFrame is inferred from the records.
    """
    overrides: dict[str, pl.DataType] = {}
    added: dict[str, pl.DataType] | None = getattr(hook, "added_columns", None)
    changed: dict[str, pl.DataType] | None = getattr(hook, "changed_columns", None)
    if added:
        overrides.update(added)
    if changed:
        overrides.update(changed)
    return overrides


def _apply_hook_contract(schema: dict[str, pl.DataType], hook: Any) -> dict[str, pl.DataType]:
    """Return ``schema`` updated by the hook's optional schema-contract
    attributes. No-op when the hook declares nothing — the schema stays
    identical to the input.

    Attribute precedence within the contract (applied in this order):
    ``removed_columns`` → ``added_columns`` → ``changed_columns``. The
    order matters when a single hook both removes and re-adds the same
    column name (rare but legal).
    """
    added: dict[str, pl.DataType] | None = getattr(hook, "added_columns", None)
    removed: set[str] | list[str] | None = getattr(hook, "removed_columns", None)
    changed: dict[str, pl.DataType] | None = getattr(hook, "changed_columns", None)

    if not (added or removed or changed):
        return schema

    out = dict(schema)
    if removed:
        for col in removed:
            out.pop(col, None)
    if added:
        out.update(added)
    if changed:
        for col, dtype in changed.items():
            if col in out:
                out[col] = dtype
    return out


def _validate_records_match_schema(
    records: list[dict[str, Any]],
    schema: dict[str, pl.DataType],
    hook: Any,
) -> None:
    """Raise :class:`ValueError` if ``records`` carry columns not in ``schema``
    or are missing columns the schema requires.

    The error message names the offending hook and the specific columns
    so the author knows which ``added_columns`` / ``removed_columns`` /
    ``changed_columns`` declaration is missing.
    """
    if not records:
        return
    schema_keys = set(schema)
    sample_keys = set(records[0])
    extra = sample_keys - schema_keys
    missing = schema_keys - sample_keys
    name = _hook_name(hook)
    if extra:
        raise ValueError(
            f"row hook {name!r} produced records carrying columns "
            f"{sorted(extra)!r} that are not declared in its schema "
            f"contract. Add them to "
            f"``{name}.added_columns = {{'col': pl.Utf8, ...}}`` (or set "
            f"``changed_columns`` if the column already existed on input "
            f"and the hook changes its dtype), or remove the assignment "
            f"from the hook body. The lazy dispatcher cannot infer "
            f"schemas after a per-row Python walk."
        )
    if missing:
        raise ValueError(
            f"row hook {name!r} produced records missing columns "
            f"{sorted(missing)!r} that the input schema (or its declared "
            f"contract) required. Add the columns to "
            f"``{name}.removed_columns = {{'col', ...}}`` if the drop is "
            f"intentional, or keep the columns in the hook body."
        )


def _hook_name(hook: Any) -> str:
    """Best-effort display name for ``hook`` — used in error messages so
    consumers see which hook violated the contract."""
    return getattr(hook, "__qualname__", None) or getattr(hook, "__name__", None) or repr(hook)


def hook_has_polars_expr(hook: Any) -> bool:
    """True when ``hook`` exposes a callable ``polars_expr`` attribute.

    The :class:`~mcp_middleware.csv_engine.config.RowHook` protocol is
    ``Callable[[dict], dict | None]`` — purely a per-row callable. Phase D
    adds an *opt-in* fast path: consumers attach a
    ``polars_expr: Callable[[pl.LazyFrame], pl.LazyFrame]`` attribute to
    their hook (function attribute, or class attribute on a callable
    class). When detected, the importer applies ``polars_expr`` once per
    batch of rows in the Rust execution engine and **skips** the per-row
    dispatch for the same hook — single source of truth stays the
    per-row callable, the expr is a perf hint with the same
    observational semantics on the columns it touches.

    Returns ``False`` for hooks without the attribute, hooks where it's
    set to ``None``, and hooks where the attribute exists but isn't
    callable. Defensive in nature — the contract is documented as "set
    it to a callable taking and returning a LazyFrame".
    """
    expr = getattr(hook, "polars_expr", None)
    return callable(expr)


def apply_polars_expr_hooks(df: pl.DataFrame, hooks: list[Any]) -> pl.DataFrame:
    """Apply every hook in ``hooks`` whose ``polars_expr`` attribute is callable.

    Each ``polars_expr`` is invoked on the batch as
    ``df.lazy().pipe(hook.polars_expr).collect()`` so multiple Phase D
    hooks on the same entity can chain — polars's planner can fuse them.
    Hooks without ``polars_expr`` are ignored here (they run on the
    per-row path inside ``transform_flat``).

    Returns a (possibly new) DataFrame; the input is not mutated.
    """
    relevant = [h for h in hooks if hook_has_polars_expr(h)]
    if not relevant:
        return df
    lf = df.lazy()
    for h in relevant:
        lf = h.polars_expr(lf)
    return lf.collect()


__all__ = [
    "apply_polars_expr_hooks",
    "apply_row_hooks_lazy",
    "apply_to_dataframe",
    "apply_to_row",
    "hook_has_polars_expr",
]

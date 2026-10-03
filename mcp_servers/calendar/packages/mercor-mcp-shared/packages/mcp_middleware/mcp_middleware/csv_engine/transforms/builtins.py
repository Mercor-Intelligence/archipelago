"""Built-in ``column_transforms`` shipped with csv_engine.

Six transforms, all self-registering on import:

``strip``
    Strip leading/trailing whitespace from string cells. No-op on non-strings.

``lowercase``
    Lowercase string cells. No-op on non-strings.

``default(value=…)``
    Replace empty / null cells with a literal default. "Empty" means
    ``None`` or ``""`` after preceding pipeline steps; non-empty strings
    pass through unchanged.

``cast(to=…)``
    Cast the cell to a Python primitive type (``"int"``, ``"float"``,
    ``"bool"``, ``"str"``). Empty / null cells pass through unchanged so
    a later ``default`` can supply a value.

``bool_to_int``
    Map a truthy / falsey string into ``1`` / ``0``. Recognises the same
    truthy tokens as :func:`mcp_middleware.csv_engine.types.parse_bool`.

``int_or_zero``
    Parse the cell as ``int``; return ``0`` on any failure (empty cell,
    non-numeric text, ``None``). Mirrors a common
    ``int(x) if x else 0`` idiom seen in row hooks.

Each class is registered at module-load time via
:func:`register_transform`. Vectorised :meth:`apply_polars` methods
preserve ``Utf8`` output unless the transform's semantic intent is to
change dtype (``bool_to_int``, ``int_or_zero``).
"""

from __future__ import annotations

from typing import Any

import polars as pl

from . import register_transform

# ---------------------------------------------------------------------------
# strip
# ---------------------------------------------------------------------------


class StripTransform:
    """Strip leading/trailing whitespace from string cells.

    Non-strings pass through unchanged — most importantly ``None``, which
    a row's polars-side null becomes after the legacy ``None → ""``
    coercion only kicks in inside the importer's batched iterator.
    """

    def apply_value(self, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip()
        return v

    def apply_polars(self, col: pl.Expr) -> pl.Expr:
        return col.str.strip_chars()


# ---------------------------------------------------------------------------
# lowercase
# ---------------------------------------------------------------------------


class LowercaseTransform:
    """Lowercase string cells; no-op on non-strings."""

    def apply_value(self, v: Any) -> Any:
        if isinstance(v, str):
            return v.lower()
        return v

    def apply_polars(self, col: pl.Expr) -> pl.Expr:
        return col.str.to_lowercase()


# ---------------------------------------------------------------------------
# default
# ---------------------------------------------------------------------------


class DefaultTransform:
    """Replace empty / null cells with a literal ``value``.

    "Empty" means ``None`` or the empty string ``""``. Useful as the last
    step of a pipeline that may produce blanks: e.g.
    ``[strip, lowercase, { default: { value: "(none)" } }]`` ensures every
    cell ends up with at least the default.
    """

    def __init__(self, value: Any):
        self.value = value

    def apply_value(self, v: Any) -> Any:
        if v is None or v == "":
            return self.value
        return v

    def apply_polars(self, col: pl.Expr) -> pl.Expr:
        # Treat both null and empty string as "missing" — matches apply_value.
        return pl.when(col.is_null() | (col == "")).then(pl.lit(self.value)).otherwise(col)


# ---------------------------------------------------------------------------
# cast
# ---------------------------------------------------------------------------


class CastTransform:
    """Cast non-empty cells to a Python primitive type.

    Empty / null cells pass through unchanged so a later ``default`` can
    supply a value without colliding with this transform's type check.

    Args:
        to: One of ``"int"``, ``"float"``, ``"bool"``, ``"str"``.
    """

    _PY_TYPES = {"int": int, "float": float, "bool": bool, "str": str}
    _PL_TYPES = {
        "int": pl.Int64,
        "float": pl.Float64,
        "bool": pl.Boolean,
        "str": pl.Utf8,
    }

    def __init__(self, to: str):
        if to not in self._PY_TYPES:
            raise ValueError(
                f"cast: unsupported target type {to!r}; expected one of {sorted(self._PY_TYPES)}"
            )
        self.to = to

    def apply_value(self, v: Any) -> Any:
        if v is None or v == "":
            return v
        try:
            return self._PY_TYPES[self.to](v)
        except (ValueError, TypeError):
            return v  # leave as-is; downstream handles malformed cells

    def apply_polars(self, col: pl.Expr) -> pl.Expr:
        # ``strict=False`` keeps unparseable cells as null instead of raising,
        # matching the per-row "leave as-is" behaviour above. A subsequent
        # ``default`` in the pipeline can backfill.
        return col.cast(self._PL_TYPES[self.to], strict=False)


# ---------------------------------------------------------------------------
# bool_to_int
# ---------------------------------------------------------------------------


# Truthy tokens — matches the legacy :func:`parse_bool` semantics used
# inside row hooks today (lowercase compare).
_TRUTHY = {"true", "1", "yes", "y", "t", "on"}


class BoolToIntTransform:
    """Convert truthy/falsey string cells into ``1`` / ``0``.

    Empty / null cells become ``0`` (matches the
    ``int(parse_bool(x)) if x else 0`` idiom seen in several Foundry
    consumers' row hooks).
    """

    def apply_value(self, v: Any) -> Any:
        if v is None or v == "":
            return 0
        if isinstance(v, bool):
            return 1 if v else 0
        if isinstance(v, (int, float)):
            return 1 if v else 0
        return 1 if str(v).strip().lower() in _TRUTHY else 0

    def apply_polars(self, col: pl.Expr) -> pl.Expr:
        # Lowercase + strip first so "True " and "yes" both map.
        normalised = col.str.strip_chars().str.to_lowercase()
        # `is_in` returns a Boolean column; cast to Int8 for compactness.
        return (
            pl.when(normalised.is_in(list(_TRUTHY)))
            .then(pl.lit(1, dtype=pl.Int8))
            .otherwise(pl.lit(0, dtype=pl.Int8))
        )


# ---------------------------------------------------------------------------
# int_or_zero
# ---------------------------------------------------------------------------


class IntOrZeroTransform:
    """Parse the cell as :class:`int`; return ``0`` on any failure.

    Mirrors the ``try: int(x); except: 0`` pattern seen across several
    Foundry row hooks. Empty / null cells, non-numeric text, and
    floats-with-decimals all map to ``0``.
    """

    def apply_value(self, v: Any) -> Any:
        if v is None or v == "":
            return 0
        try:
            return int(v)
        except (ValueError, TypeError):
            return 0

    def apply_polars(self, col: pl.Expr) -> pl.Expr:
        # ``strict=False`` → unparseable cells become null; fill_null(0)
        # to match apply_value's "0 on failure" semantics.
        return col.cast(pl.Int64, strict=False).fill_null(0)


# ---------------------------------------------------------------------------
# Self-registration
# ---------------------------------------------------------------------------

register_transform("strip", StripTransform)
register_transform("lowercase", LowercaseTransform)
register_transform("default", DefaultTransform)
register_transform("cast", CastTransform)
register_transform("bool_to_int", BoolToIntTransform)
register_transform("int_or_zero", IntOrZeroTransform)


__all__ = [
    "BoolToIntTransform",
    "CastTransform",
    "DefaultTransform",
    "IntOrZeroTransform",
    "LowercaseTransform",
    "StripTransform",
]

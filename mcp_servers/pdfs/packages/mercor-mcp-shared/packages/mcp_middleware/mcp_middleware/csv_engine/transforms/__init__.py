"""Per-column ``column_transforms`` registry and protocol.

YAML shape — declarative, per-column transform pipelines that run
*before* row hooks during import::

    posts:
      table: posts
      import:
        column_transforms:
          title:       [{ spongebob: { start_index: 3 } }]
          description: [{ spongebob: { start_index: 5 } }, strip]
          summary:     [strip, lowercase, { default: { value: "(none)" } }]

Each list entry is either a bare string (``strip``) — a parameter-less
transform — or a single-key dict mapping a transform name to its
``__init__`` kwargs. Transforms run in declared order; the output of one
feeds the next.

Application-side registration
=============================

A transform is any class with at least :meth:`apply_value` (called once
per row per column, slow path) and optionally :meth:`apply_polars` (a
``pl.Expr`` transformation, vectorised fast path). Register at startup::

    config.register_transform("spongebob", SpongebobTransform)

When a column's full pipeline is ``apply_polars``-able the engine chains
the expressions via ``df.with_columns(pl.col(name).pipe(t1).pipe(t2)…)``.
If any transform on a column lacks ``apply_polars``, that one column
falls back to ``pl.col(name).map_elements(fn, return_dtype=pl.Utf8)`` —
isolated to that column, not the whole entity.

The :class:`Transform` Protocol below is structural; you do **not** need
to inherit from it. Any class with the right method signatures is
accepted (matches the existing :class:`~mcp_middleware.csv_engine.config.RowHook`
duck-typing pattern).

Built-in transforms
===================

Six transforms ship in :mod:`csv_engine.transforms.builtins` and
self-register on import: ``strip``, ``lowercase``, ``default``, ``cast``,
``bool_to_int``, ``int_or_zero``.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

import polars as pl


@runtime_checkable
class Transform(Protocol):
    """Structural protocol for ``column_transforms`` entries.

    Implementations must define :meth:`apply_value` (slow row path).
    :meth:`apply_polars` is optional — when present, the engine chains
    it on a polars expression for a vectorised fast path.
    """

    def apply_value(self, v: Any) -> Any:
        """Transform a single cell value (slow / per-row path).

        Receives the cell value as it arrived from the polars-backed
        reader (typically :class:`str` because the reader pins every
        column to ``Utf8``; sometimes ``None`` for missing cells when an
        upstream override returns one). Return the new value.

        Raising from inside ``apply_value`` propagates up through the
        importer and aborts that file's import. Most built-in transforms
        return the original value unchanged when given a type they
        can't operate on (e.g. :class:`LowercaseTransform` returns
        non-strings as-is).
        """
        ...


@runtime_checkable
class VectorisableTransform(Transform, Protocol):
    """A :class:`Transform` whose pipeline step can be expressed as a polars expr.

    Implementations define :meth:`apply_polars` in addition to
    :meth:`apply_value`. When every transform on a column has this
    method, csv_engine chains them via
    ``pl.col(name).pipe(t1.apply_polars).pipe(t2.apply_polars)…`` for
    Rust-side vectorised execution. Mixing with non-vectorisable
    transforms is supported — the column falls back to
    ``pl.col(name).map_elements`` per-cell while siblings stay
    vectorised.
    """

    def apply_polars(self, col: pl.Expr) -> pl.Expr:
        """Lift the transform to a ``pl.Expr`` operation on a column.

        Receives the column as a ``pl.Expr`` (typically a ``pl.col(name)``
        chain so far) and returns the same shape with the transform
        applied. Implementations should preserve ``Utf8`` output for the
        Phase A all-strings contract unless they intentionally cast (e.g.
        :class:`BoolToIntTransform`).
        """
        ...


# ---------------------------------------------------------------------------
# Global registry (process-wide; mirrors :func:`register_key_normalizer`)
# ---------------------------------------------------------------------------

# Stores **classes**, not instances. The YAML parser builds an instance
# per use-site by calling ``cls(**params)`` with the YAML-supplied kwargs.
_TRANSFORMS: dict[str, type[Transform]] = {}


def register_transform(name: str, transform_cls: type[Transform]) -> None:
    """Register a ``column_transforms`` class globally.

    Args:
        name: YAML identifier used in ``column_transforms`` entries.
        transform_cls: A class whose instances satisfy :class:`Transform`
            (i.e. expose ``apply_value(self, v)``). Optionally also
            satisfy :class:`VectorisableTransform` (expose
            ``apply_polars(self, col)``) for the fast path.

    The registry is process-global and unsynchronised — register at
    application startup before any concurrent imports run, same threading
    contract as :func:`register_reader` / :func:`register_key_normalizer`.
    Re-registering an existing name silently overwrites the previous
    binding (last write wins).
    """
    _TRANSFORMS[name] = transform_cls


def get_transform(name: str) -> type[Transform]:
    """Return the class registered under ``name``.

    Raises:
        KeyError: when no transform is registered for ``name``. The error
            lists known names for the typo-friendly case.
    """
    try:
        return _TRANSFORMS[name]
    except KeyError as exc:
        known = ", ".join(sorted(_TRANSFORMS)) or "<none>"
        raise KeyError(f"No column_transform registered for name {name!r}. Known: {known}") from exc


def is_transform_registered(name: str) -> bool:
    """True when ``name`` is in the registry; ``False`` otherwise."""
    return name in _TRANSFORMS


def registered_transforms() -> dict[str, type[Transform]]:
    """Return a defensive copy of the current registry (name → class)."""
    return dict(_TRANSFORMS)


# Trigger built-in auto-registration on first import of this sub-package.
# ``builtins`` registers ``strip``, ``lowercase``, ``default``, ``cast``,
# ``bool_to_int``, ``int_or_zero`` at module-load time.
from . import builtins  # noqa: E402, F401  (side-effect import)

__all__ = [
    "Transform",
    "VectorisableTransform",
    "get_transform",
    "is_transform_registered",
    "register_transform",
    "registered_transforms",
]

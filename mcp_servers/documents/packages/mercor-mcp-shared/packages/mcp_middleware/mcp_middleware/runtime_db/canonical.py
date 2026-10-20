"""Typed canonical-path resolution that makes ``:memory:`` corruption unrepresentable.

Every Foundry-* MCP server has a near-identical ``db/paths.py`` (~50 lines)
that reads an env var (``DATABASE_PATH`` or app-specific equivalent), falls
back to a default file under the repo, and ``Path(...).expanduser().resolve()``s
the result. The bug: when the env var holds the literal ``":memory:"``,
``Path(":memory:").resolve()`` silently expands to ``$PWD/:memory:`` — a real
filesystem path. SQLAlchemy then opens a file-backed DB instead of the
intended in-memory one, and the writer silently corrupts a real file. Confirmed
in Foundry_Boilerplate cf5045b → fcfe78e; almost certainly latent in every
sibling that copied the pattern.

This module replaces the per-app boilerplate with a typed resolver that
returns a sum type. Callers ``match`` on the result; ``Path(":memory:")``
becomes unrepresentable because the resolver never produces it — memory mode
is :class:`MemoryMode`, file mode is :class:`CanonicalPath`, and there is no
third option.

Usage
-----

Replace per-app ``db/paths.py``::

    # Old (every Foundry-* repo shipped a variant of this)
    def get_canonical_db_path() -> str:
        raw = os.environ.get("DATABASE_PATH", DEFAULT_PATH)
        return str(Path(raw).expanduser().resolve())  # BUG: ":memory:" → "$PWD/:memory:"

with::

    from mcp_middleware.runtime_db import (
        CanonicalPath, MemoryMode, resolve_canonical_db_path,
    )

    result = resolve_canonical_db_path(
        default_filename="atlassian.db",
        fallback_anchor=Path(__file__).parents[2],
    )
    match result:
        case MemoryMode():
            engine = create_engine(result.as_url())   # "sqlite://"
            # ...skip cold_seed_runtime etc. for memory mode
        case CanonicalPath():
            engine = create_engine(result.as_url())   # "sqlite:///abs/path"
            # ...file-mode lifecycle (cold_seed_runtime, snapshot_with_populate, ...)

If you only need the SQLAlchemy URL and don't need to branch on the mode,
``result.as_url()`` works on both branches — both classes implement the same
``.as_url()`` method.

Defaults match the Foundry convention: env var ``DATABASE_PATH``, fallback
filename ``studio.db``, anchor ``Path.cwd()``. Override any of them at the
call site.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "Canonical",
    "CanonicalPath",
    "MemoryMode",
    "resolve_canonical_db_path",
]


# Literal value of the SQLite memory-mode sentinel. Exact-match (not
# case-insensitive) because SQLite's own URL parser is case-sensitive on this
# token — ``":Memory:"`` would open a file at ``$PWD/:Memory:`` just like the
# bug this module exists to prevent. If the caller's env var holds a
# weird-cased variant, that's the caller's bug to surface, not ours to paper
# over.
_MEMORY_SENTINEL = ":memory:"

# Default env var name. Picked to match the Foundry-* convention; consumers
# with their own naming (``ATLASSIAN_DB_PATH``, ``ZOHO_DB_PATH``) pass
# ``env_var=`` to override.
_DEFAULT_ENV_VAR = "DATABASE_PATH"

# Default filename joined onto the fallback anchor when the env var is unset.
# Matches the Foundry-* convention; consumers override per-app.
_DEFAULT_FILENAME = "studio.db"


@dataclass(frozen=True)
class CanonicalPath:
    """A resolved on-disk canonical DB path.

    ``path`` is always absolute, expanded (``~`` → home), and canonicalised
    via :meth:`pathlib.Path.resolve` (symlinks followed). Construction from
    a non-absolute or non-expanded path is allowed but unusual — the
    resolver always passes the already-normalised path; direct construction
    is mainly for tests.

    Implements :meth:`as_url` so callers handing the result to SQLAlchemy
    don't have to remember the ``sqlite:///`` prefix or care about whether
    they're in memory mode.
    """

    path: Path

    def as_url(self) -> str:
        """SQLAlchemy URL for :func:`sqlalchemy.create_engine`.

        Returns the absolute file URL form (``sqlite:///{abs_path}``) — note
        the triple slash, which SQLAlchemy uses to disambiguate the empty
        host segment from an absolute path. A two-slash form
        (``sqlite://{abs_path}``) is parsed as host=abs_path with no path
        component, which silently routes to memory mode on most platforms.
        """
        return f"sqlite:///{self.path}"


class MemoryMode:
    """Marker for in-memory SQLite (``:memory:``) mode.

    Singleton-like in behaviour: any two instances compare equal and hash
    equal, so they're interchangeable. There's no per-instance state to
    carry — the only information is "the canonical resolved to the literal
    ``:memory:`` sentinel, and the engine should be in-memory." Constructing
    one directly (``MemoryMode()``) and constructing it via
    :func:`resolve_canonical_db_path` produce indistinguishable values.

    Implements :meth:`as_url` so callers don't have to know the
    ``sqlite://`` (memory) URL form by heart and so the same call site
    works against both :class:`MemoryMode` and :class:`CanonicalPath`.
    """

    __slots__ = ()

    def __eq__(self, other: object) -> bool:
        return isinstance(other, MemoryMode)

    def __hash__(self) -> int:
        # All instances are equal → all instances must hash to the same value.
        # Hashing on the class itself is the simplest stable choice.
        return hash(MemoryMode)

    def __repr__(self) -> str:
        return "MemoryMode()"

    def as_url(self) -> str:
        """SQLAlchemy URL for :func:`sqlalchemy.create_engine`.

        Returns ``"sqlite://"`` — the SQLAlchemy idiom for an anonymous
        in-memory SQLite database. Note this is two slashes (no path
        component), not three; a triple-slash form is interpreted as an
        absolute path to a file literally named ``""``.
        """
        return "sqlite://"


# Sum type the resolver returns. Callers ``match`` on this; direct
# construction of either variant is also legitimate (e.g. in tests).
Canonical = CanonicalPath | MemoryMode


def resolve_canonical_db_path(
    *,
    env_var: str = _DEFAULT_ENV_VAR,
    default_filename: str = _DEFAULT_FILENAME,
    fallback_anchor: Path | None = None,
) -> Canonical:
    """Resolve a canonical DB path or memory-mode marker from the environment.

    Resolution order (first match wins):

    1. ``$<env_var>`` is set and equals ``":memory:"`` (exact-match,
       case-sensitive) → :class:`MemoryMode`.
    2. ``$<env_var>`` is set to anything else → :class:`CanonicalPath` with
       the value passed through :meth:`Path.expanduser` then
       :meth:`Path.resolve`.
    3. ``$<env_var>`` is unset → :class:`CanonicalPath` for
       ``(fallback_anchor or Path.cwd()) / default_filename``, also
       ``expanduser().resolve()``d.

    Args:
        env_var: Environment variable consulted first. Defaults to
            ``"DATABASE_PATH"`` — the Foundry convention. Per-app callers
            with their own naming (e.g. ``"ATLASSIAN_DB_PATH"``) pass
            ``env_var="ATLASSIAN_DB_PATH"``.
        default_filename: Filename joined onto ``fallback_anchor`` when
            ``$env_var`` is unset. Defaults to ``"studio.db"``.
        fallback_anchor: Directory under which ``default_filename`` lives
            when ``$env_var`` is unset. Use this to root the default under
            the app's repo (``fallback_anchor=Path(__file__).parents[2]``
            in a ``db/paths.py``) so a misconfigured deploy writes to a
            predictable location rather than wherever the process happened
            to be ``cd``'d into. ``None`` means use :func:`Path.cwd` — the
            historic default of the per-app ``db/paths.py`` files, kept
            for back-compat.

    Returns:
        Either :class:`CanonicalPath` (file mode, absolute resolved path)
        or :class:`MemoryMode` (in-memory). Callers ``match`` on the type
        to branch, or call ``.as_url()`` for the SQLAlchemy URL form on
        either branch.

    Memory mode is detected only via the env var. Falling back to a
    default filename never produces :class:`MemoryMode` — there's no
    sensible "default is in-memory" interpretation for production code.
    """
    raw = os.environ.get(env_var)
    # Strip and treat empty / whitespace-only as unset — matches how
    # `_resolve_state_dir` handles STATE_LOCATION on the wrapper side.
    # Without this, `DATABASE_PATH=""` would resolve to `Path("").resolve()`
    # (== cwd) and populate/snapshot would silently write outside the
    # documented fallback_anchor.
    if raw is not None:
        raw = raw.strip()
        if not raw:
            raw = None
    if raw == _MEMORY_SENTINEL:
        return MemoryMode()
    if raw is not None:
        return CanonicalPath(Path(raw).expanduser().resolve())
    anchor = fallback_anchor if fallback_anchor is not None else Path.cwd()
    return CanonicalPath((anchor / default_filename).expanduser().resolve())

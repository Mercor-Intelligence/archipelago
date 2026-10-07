"""JSON path matching with ``**`` globstar support (spec §2).

The legacy ``parity`` comparator matched dotted paths where ``*`` is any dict
key and ``[*]``/``[N]`` any list index, but every segment had to be spelled out
by depth. This module adds the **globstar** ``**`` — matching zero or more
segments across **both** dict and list boundaries — so ``**.created_at`` hits
that field at any depth, and ``data.**.id`` hits every ``id`` under ``data``.
It subsumes the old field-name-ignore dimension.

Grammar (segments split on ``.`` and ``[]``):

- ``**``      — globstar: zero or more segments, crosses dicts *and* lists
- ``*``       — exactly one dict key
- ``[*]``     — exactly one list index
- ``[N]``     — the list index ``N``
- ``<name>``  — the literal dict key ``<name>``

The primary entry point is :func:`find_paths`, which returns every concrete
path (a tuple of ``str`` keys / ``int`` indices) in a JSON value that a pattern
matches. Callers (``sanitize`` for ``ignore``/``type_only``, ``identity`` for
``reference`` tokenization) then read/replace/drop at those concrete paths, so
the matcher stays a pure, independently-testable function.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any, Final

Path = tuple[Any, ...]  # concrete path: str keys and int indices

_GLOBSTAR: Final = "**"
_ANY_KEY: Final = "*"
_ANY_INDEX: Final = "[*]"


def compile_pattern(pattern: str) -> list[str]:
    """Tokenize a path pattern into segments.

    ``"data[*].**.Created_By.id"`` → ``["data", "[*]", "**", "Created_By", "id"]``.
    List markers are normalized so ``[*]`` and ``[3]`` survive as distinct
    segments (``"[*]"`` and ``"3"``) rather than being flattened into keys.
    """
    out: list[str] = []
    i = 0
    n = len(pattern)
    buf = ""

    def flush() -> None:
        nonlocal buf
        if buf:
            out.append(buf)
            buf = ""

    while i < n:
        ch = pattern[i]
        if ch == ".":
            flush()
        elif ch == "[":
            flush()
            j = pattern.find("]", i)
            if j == -1:
                # Unterminated bracket — treat the rest as a literal key.
                buf = pattern[i + 1 :]
                i = n
                break
            inner = pattern[i + 1 : j]
            out.append(_ANY_INDEX if inner == "*" else inner)
            i = j
        else:
            buf += ch
        i += 1
    flush()
    return out


def find_paths(value: Any, pattern: str) -> list[Path]:
    """Every concrete path in ``value`` matched by ``pattern`` (deduped, ordered).

    A concrete path is a tuple whose elements are ``str`` dict keys and ``int``
    list indices, suitable for :func:`get_at` / :func:`set_at` / :func:`drop_at`.
    """
    segs = compile_pattern(pattern)
    seen: set[Path] = set()
    result: list[Path] = []
    for p in _walk((), value, segs):
        if p not in seen:
            seen.add(p)
            result.append(p)
    return result


def _walk(prefix: Path, value: Any, segs: list[str]) -> Iterator[Path]:
    if not segs:
        yield prefix
        return
    head, rest = segs[0], segs[1:]

    if head == _GLOBSTAR:
        # Zero-segment match: globstar consumes nothing.
        yield from _walk(prefix, value, rest)
        # One-or-more: descend a level and keep the globstar in play.
        for key, child in _children(value):
            yield from _walk((*prefix, key), child, segs)
        return

    for key, child in _children(value):
        if _seg_matches(head, key):
            yield from _walk((*prefix, key), child, rest)


def _children(value: Any) -> Iterator[tuple[Any, Any]]:
    if isinstance(value, dict):
        yield from value.items()
    elif isinstance(value, list):
        yield from enumerate(value)


def _seg_matches(seg: str, key: Any) -> bool:
    if isinstance(key, int):  # list index
        if seg == _ANY_INDEX:
            return True
        try:
            return int(seg) == key
        except ValueError:
            return False
    # dict key
    if seg in (_ANY_KEY, _ANY_INDEX):
        return seg == _ANY_KEY
    return seg == key


# --- Concrete-path accessors -------------------------------------------------


_MISSING: Final = object()


def get_at(value: Any, path: Path) -> Any:
    """Value at ``path``, or ``None`` if any segment is absent."""
    cur = value
    for seg in path:
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        elif isinstance(cur, list) and isinstance(seg, int) and 0 <= seg < len(cur):
            cur = cur[seg]
        else:
            return None
    return cur


def set_at(value: Any, path: Path, new: Any) -> None:
    """Set ``path`` to ``new`` in-place. No-op if the parent is absent."""
    if not path:
        return
    parent = get_at(value, path[:-1])
    leaf = path[-1]
    if isinstance(parent, dict):
        parent[leaf] = new
    elif isinstance(parent, list) and isinstance(leaf, int) and 0 <= leaf < len(parent):
        parent[leaf] = new


def drop_at(value: Any, path: Path) -> None:
    """Remove ``path`` in-place. Dict keys are deleted; list elements are set to
    ``None`` so sibling indices don't shift (matching legacy ``parity``)."""
    if not path:
        return
    parent = get_at(value, path[:-1])
    leaf = path[-1]
    if isinstance(parent, dict):
        parent.pop(leaf, None)
    elif isinstance(parent, list) and isinstance(leaf, int) and 0 <= leaf < len(parent):
        parent[leaf] = None

"""Sanitize-on-capture (spec §7).

Turns a raw response into the canonical *expected artifact*, using the field
classes from :class:`parity_test.rules.ComparisonRules` and the ``**`` matcher
in :mod:`parity_test.paths`:

- ``reference``  paths → replaced with a stable symbolic token (via the injected
                 ``tokenize`` value-mapper), so ids are neither ignored nor
                 value-compared
- ``type_only``  paths → replaced with a typed placeholder ``{"$type": "..."}``
- ``ignore``     paths → dropped (:func:`paths.drop_at`)

Applied in that order (reference → type_only → ignore) so a field claimed by a
more specific class isn't first flattened by a broader one. ``ignore`` drops run
last, deepest-path-first, so list-index paths stay valid as we mutate.

The **same** function sanitizes both sides: the reference body at capture (to
persist) and the SUT body at compare (before diffing). Identical transforms ⇒
symmetric comparison and churn-free full-refresh (spec §7).
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any

from .paths import drop_at, find_paths, get_at, set_at
from .rules import ComparisonRules


def typed_placeholder(value: Any) -> Any:
    """The ``type_only`` stand-in.

    A scalar collapses to ``{"$type": "<json-type>"}`` — presence and JSON type
    are verified, the value is waived. **Containers recurse**: an object/array
    keeps its shape and each leaf becomes its own placeholder, so ``type_only``
    on an object still asserts the *right set of columns* (every key, at every
    depth) and each leaf's type while waiving only the leaf scalar values. Both
    sides run the identical transform, so the diff compares keys-and-types and
    ignores leaf values (spec §7). A ``reference`` token nested inside a waived
    object is itself a scalar by the time this runs (reference sanitizes first),
    so it too collapses to a type placeholder — an audit-stamp id is verified as
    present-and-string, not value-correlated.
    """
    if isinstance(value, dict):
        return {k: typed_placeholder(v) for k, v in value.items()}
    if isinstance(value, list):
        return [typed_placeholder(v) for v in value]
    if value is None:
        return {"$type": "null"}
    if isinstance(value, bool):
        return {"$type": "boolean"}
    if isinstance(value, int):
        return {"$type": "integer"}
    if isinstance(value, float):
        return {"$type": "number"}
    if isinstance(value, str):
        return {"$type": "string"}
    return {"$type": "object"}


# Fillers must be *representative, round-trip-safe* values of each JSON type,
# not merely the type's zero value. When a de-sanitized placeholder is seeded,
# it passes through a real DB column: an empty string is silently coerced to
# SQL NULL by the csv-engine importer, which would flip a ``type_only`` string
# field's read back from ``string`` to ``null`` and fail the type check. So the
# string filler is a non-empty sentinel that also happens to be a valid
# ISO-8601 timestamp — the common ``type_only`` string is a ``*_time`` column —
# so it survives datetime-typed columns too.
_PLACEHOLDER_FILLER: dict[str, Any] = {
    "null": None,
    "boolean": False,
    "integer": 0,
    "number": 0,
    "string": "1970-01-01T00:00:00+00:00",
    "array": [],
    "object": {},
}


def desanitize_placeholders(value: Any) -> Any:
    """Inverse of :func:`typed_placeholder` for seeding (spec §8.1).

    A sanitized snapshot body carries ``{"$type": "..."}`` stand-ins where
    ``type_only`` fields were waived. Those can't be stored as-is (a DB column
    won't take a dict), and their *value* is waived at compare time anyway — so
    collapse each placeholder to a neutral scalar of the right JSON type. Every
    other value passes through untouched (``reference`` tokens are already plain
    scalars via identity passthrough). Because compare re-derives the placeholder
    from whatever the clone serves, the exact filler never affects the result.
    """
    if isinstance(value, dict):
        t = value.get("$type")
        if set(value) == {"$type"} and isinstance(t, str):
            return _PLACEHOLDER_FILLER.get(t)
        return {k: desanitize_placeholders(v) for k, v in value.items()}
    if isinstance(value, list):
        return [desanitize_placeholders(v) for v in value]
    return value


def sanitize_response(
    body: Any,
    rules: ComparisonRules,
    *,
    tokenize: Callable[[Any], Any] = lambda v: v,
) -> Any:
    """Return the canonical, sanitized body (a deep copy; input untouched)."""
    out = copy.deepcopy(body)

    # reference → token (value-level; passthrough until a link/fixture registers)
    for pattern in rules.reference:
        for p in find_paths(out, pattern):
            set_at(out, p, tokenize(get_at(out, p)))

    # type_only → typed placeholder
    for pattern in rules.type_only:
        for p in find_paths(out, pattern):
            set_at(out, p, typed_placeholder(get_at(out, p)))

    # ignore → drop (deepest paths first so list indices stay valid)
    ignore_paths: list[tuple[Any, ...]] = []
    for pattern in rules.ignore:
        ignore_paths.extend(find_paths(out, pattern))
    for p in sorted(ignore_paths, key=len, reverse=True):
        drop_at(out, p)

    return out


def redact_credentials(request: dict[str, Any], response: dict[str, Any]) -> None:
    """Strip auth material in-place before persist (spec §9): ``Authorization``,
    API keys, cookies, token-endpoint bodies, hosted MCP ``url``."""
    for container in (request, response):
        headers = container.get("headers")
        if isinstance(headers, dict):
            for key in list(headers):
                if key.lower() in {"authorization", "cookie", "set-cookie", "x-api-key"}:
                    headers[key] = "<redacted>"
    # Hosted MCP reference url can embed a token in the path.
    if "url" in request:
        request["url"] = "<redacted>"

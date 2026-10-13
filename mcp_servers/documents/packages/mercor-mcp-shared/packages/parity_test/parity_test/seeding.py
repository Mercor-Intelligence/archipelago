"""Decorator-based seeding (spec §8) — the same shape as the legacy library.

Tests declare the state they need with markers; a fixture materializes them into
a tempdir and hands that to the app's ``populate`` hook (typically
``mcp_middleware.csv_engine`` driven by ``snapshot_config.yaml``). The library
knows nothing about the wire-fixture → table mapping — that lives in the app's
config — which is the whole point: schema drift stays in one place.

Two markers::

    @populate_seed("users.csv", '''
    id,email
    1,a@b.com
    ''')                                  # inline (filename, content)

    @populate_seed("tests/fixtures/baseline")   # a file or a shallow dir

    @seed_from_snapshot("organization.json", label="get_org", path="org",
                        operation="getOrganization", transform=org_row_from_api)

``seed_from_snapshot`` is the read-side complement (spec §8.1): it reconstructs a
*given* from a captured read rather than a hand-authored fixture. The record
source is resolved by mode —

- the committed snapshot for this test (replay, or capture once recorded), or
- the live reference for ``operation`` (capture bootstrap, when no snapshot
  exists yet and a reference is available).

Typed placeholders in a sanitized snapshot are de-sanitized to seed-able scalars
first (:func:`parity_test.sanitize.desanitize_placeholders`); an optional
``transform`` then reshapes each API record into the app's column layout (the
same job ``seed_from_live.seed_org`` does for the org).
"""

from __future__ import annotations

import csv as _csv
import io
import json
import shutil
import textwrap
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import pytest

from .hooks import Hooks
from .sanitize import desanitize_placeholders

# Markers are plain pytest marks the fixture introspects — no magic.
populate_seed = pytest.mark.populate_seed
seed_from_snapshot = pytest.mark.seed_from_snapshot

__all__ = ["populate_seed", "seed_from_snapshot", "apply_seed_markers", "materialize_seeds"]


# --- content helpers ---------------------------------------------------------


def _normalize_inline_content(raw: str) -> str:
    """Dedent triple-quoted decorator content and ensure a trailing newline."""
    if not raw:
        return ""
    if raw.startswith("\n"):
        raw = raw[1:]
    dedented = textwrap.dedent(raw)
    if not dedented.endswith("\n"):
        dedented += "\n"
    return dedented


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (Mapping, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _records_to_csv(records: list[dict[str, Any]]) -> str:
    columns: list[str] = []
    seen: set[str] = set()
    for record in records:
        for key in record:
            if key not in seen:
                seen.add(key)
                columns.append(key)
    buf = io.StringIO()
    writer = _csv.writer(buf, lineterminator="\n")
    writer.writerow(columns)
    for record in records:
        writer.writerow([_cell(record.get(col)) for col in columns])
    return buf.getvalue()


def _dig(data: Any, path: str | None) -> Any:
    """Walk a dotted ``path`` (mapping keys / numeric list indices) into ``data``."""
    if not path:
        return data
    cur = data
    for seg in path.split("."):
        if not seg:
            continue
        if isinstance(cur, Mapping):
            if seg not in cur:
                raise KeyError(f"seed_from_snapshot path segment {seg!r} not in snapshot body")
            cur = cur[seg]
        elif isinstance(cur, list) and seg.isdigit():
            cur = cur[int(seg)]
        else:
            raise KeyError(
                f"seed_from_snapshot cannot descend into {seg!r}: got {type(cur).__name__}"
            )
    return cur


def _as_records(data: Any, node_id: str) -> list[dict[str, Any]]:
    if isinstance(data, Mapping):
        return [dict(data)]
    if isinstance(data, list):
        return [dict(r) for r in data if isinstance(r, Mapping)]
    raise TypeError(
        f"seed_from_snapshot expected an object or list of objects, got "
        f"{type(data).__name__} (on {node_id})"
    )


# --- marker materialization --------------------------------------------------


def _write_inline_seed(target: Path, filename: str, content: str, written: set[str]) -> None:
    if filename in written:
        return
    (target / filename).write_text(_normalize_inline_content(content), encoding="utf-8")
    written.add(filename)


def _copy_path_seed(target: Path, source: Path, written: set[str]) -> None:
    if source.is_file():
        if source.name not in written:
            shutil.copy2(source, target / source.name)
            written.add(source.name)
        return
    for child in sorted(source.iterdir()):
        if child.is_file() and child.name not in written:
            shutil.copy2(child, target / child.name)
            written.add(child.name)


def _resolve_snapshot_records(marker: pytest.Mark, harness: Any, node_id: str) -> list[dict]:
    """Resolve a ``seed_from_snapshot`` marker's records from the committed
    snapshot (offline) or, in capture bootstrap, the live reference."""
    binding = marker.kwargs.get("binding", "rest")
    label = marker.kwargs.get("label")
    ordinal = marker.kwargs.get("ordinal")
    path = marker.kwargs.get("path")
    operation = marker.kwargs.get("operation")

    snap = harness.store.get(binding, label, ordinal)
    if snap is not None:
        body = snap.body
    elif harness.mode == "capture" and harness.reference is not None and operation:
        op = harness.oas.resolve_operation(operation)
        url = harness.oas.build_url(op)
        resp = harness.reference.request(op.method.upper(), url)
        body = resp.json()
    else:
        raise FileNotFoundError(
            f"seed_from_snapshot: no snapshot for binding={binding!r} label={label!r} "
            f"and no live reference to bootstrap {operation!r} (on {node_id}). "
            "Run once in capture mode with a reference to record it."
        )

    records = _as_records(_dig(body, path), node_id)
    return [desanitize_placeholders(r) for r in records]


def _apply_transform(records: list[dict], transform: Any, node_id: str) -> list[dict]:
    if transform is None:
        return records
    out: list[dict] = []
    for record in records:
        result = transform(record)
        if result is None:
            continue
        if not isinstance(result, Mapping):
            raise TypeError(
                f"seed_from_snapshot transform must return a mapping or None, got "
                f"{type(result).__name__} (on {node_id})"
            )
        out.append(dict(result))
    return out


def _write_snapshot_seed(
    target: Path, marker: pytest.Mark, harness: Any, node_id: str, written: set[str]
) -> None:
    args = marker.args
    if not args or not isinstance(args[0], str):
        raise TypeError(
            "seed_from_snapshot accepts (filename, *, binding, label, ordinal, path, "
            f"operation, transform); got args={args!r} on {node_id}"
        )
    filename = args[0]
    if filename in written:
        return
    suffix = Path(filename).suffix.lower()
    if suffix not in (".json", ".csv"):
        raise ValueError(f"seed_from_snapshot filename must end .json/.csv (got {filename!r})")

    records = _resolve_snapshot_records(marker, harness, node_id)
    records = _apply_transform(records, marker.kwargs.get("transform"), node_id)

    if suffix == ".json":
        content = json.dumps(records, indent=2, ensure_ascii=False) + "\n"
    else:
        content = _records_to_csv(records)
    (target / filename).write_text(content, encoding="utf-8")
    written.add(filename)


def materialize_seeds(request: pytest.FixtureRequest, target: Path, harness: Any) -> None:
    """Write every ``populate_seed`` and ``seed_from_snapshot`` marker into ``target``.

    ``populate_seed`` files are materialized first, so a hand-authored fixture
    wins over a snapshot-derived file of the same name (first-wins by basename,
    matching legacy behavior)."""
    written: set[str] = set()
    for marker in request.node.iter_markers("populate_seed"):
        args = marker.args
        if len(args) == 2 and isinstance(args[0], str) and isinstance(args[1], str):
            _write_inline_seed(target, args[0], args[1], written)
        elif len(args) == 1 and isinstance(args[0], (str, Path)):
            source = Path(args[0])
            if not source.exists():
                raise FileNotFoundError(
                    f"populate_seed path not found: {source} (on {request.node.nodeid})"
                )
            _copy_path_seed(target, source, written)
        else:
            raise TypeError(f"populate_seed accepts (filename, content) or (path,); got {args!r}")
    for marker in request.node.iter_markers("seed_from_snapshot"):
        _write_snapshot_seed(target, marker, harness, request.node.nodeid, written)


# --- orchestration -----------------------------------------------------------


def apply_seed_markers(request: pytest.FixtureRequest, harness: Any) -> None:
    """Reset (if a hook is registered) → materialize markers → populate.

    A no-op when the test declares no seed markers. Raises if seed files are
    present but no populate hook is registered.

    Seeding runs the app's *own* end-to-end populate lifecycle (the ``populate``
    hook — see :func:`parity_test.defaults.script_populate`), so its exit code +
    internal asserts are the success contract; the parity response comparison is
    the backstop that a seed actually landed. No row-count heuristic lives here —
    it would false-fail on a legitimate clear-then-reinsert entity that nets zero
    new rows."""
    if Hooks.db_reset is not None:
        Hooks.db_reset()

    has_seeds = (
        request.node.get_closest_marker("populate_seed") is not None
        or request.node.get_closest_marker("seed_from_snapshot") is not None
    )
    if not has_seeds:
        return

    with TemporaryDirectory(prefix="parity-seed-") as tmp:
        target = Path(tmp)
        materialize_seeds(request, target, harness)
        if Hooks.populate is None:
            raise NotImplementedError(
                "No populate hook registered. Set Hooks.populate in parity_test_ext "
                "(parity_test.defaults.script_populate(populate_engine.main))."
            )
        Hooks.populate(target)

"""Shared single-flight cache for style-metadata extraction.

A grading run evaluates every verifier for a trajectory concurrently in one
process (see runner.main's asyncio.gather), and they all inspect the same
output files. Without coalescing, a 70-verifier rubric parses the same
workbook or deck 70 times over — tens of seconds each, all at once.

Used by both the spreadsheet and pptx extractors, so the concurrency
handling (single-flight, shielding, loop-binding) lives in one place rather
than being duplicated per format.

The module-level dicts are deliberately unlocked. Grading starts exactly one
event loop per process (asyncio.run in runner.main and runner.k8s_worker), so
all access is from a single thread and the check-then-insert on _INFLIGHT
cannot interleave. If grading ever ran concurrent loops in separate threads of
one process, that check would become racy — the consequence being a duplicated
parse rather than corrupted state, since entries are keyed by content hash.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import zipfile
from collections import OrderedDict
from collections.abc import Awaitable, Callable

from loguru import logger

# Completed results are stored as plain strings, NOT as the asyncio.Task that
# produced them: a Task is bound to the event loop it was created on, so a
# task left behind by one grading run would be unusable (or worse, still
# pending) if the process starts a fresh loop for the next run. Only in-flight
# work is held as a task, and a done-callback promotes it to a plain string
# and clears the in-flight entry in every outcome — success, failure, or
# cancellation — so nothing loop-bound outlives the run that created it.
# Sized to hold every distinct style-eligible output of a trajectory, not just
# a working set. Eviction here is expensive and silent: a rubric can run ~80
# verifiers over the same files, and re-parsing an evicted 17MB workbook costs
# ~37s each time it is asked for again. Retention is cheap by comparison —
# entries are the bounded extracted *text* (~21KB for an 8-sheet workbook), not
# the parsed workbook, so this ceiling is a few MB at worst.
CACHE_MAX_ENTRIES = 32
_RESULTS: OrderedDict[str, str] = OrderedDict()
_INFLIGHT: dict[str, asyncio.Future[str]] = {}


def is_ooxml_package(file_bytes: bytes) -> bool:
    """Whether these bytes are an OOXML package python-docx/pptx can open.

    Zip-ness alone is not enough: .odt and .odp are zip packages too, so they
    pass an is_zipfile check and then raise KeyError deep inside the parser
    looking for [Content_Types].xml. Legacy .doc/.ppt are OLE2 and fail the zip
    check. Testing for the OOXML content-types part covers both.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as zf:
            return "[Content_Types].xml" in zf.namelist()
    except Exception:
        return False


def cache_key(kind: str, file_bytes: bytes) -> str:
    """Cache key for an extraction. Exposed so tests reference the real
    derivation instead of reimplementing it and silently drifting.

    Keyed on content, not path: this only coalesces the parse, so each verifier
    still reads its bytes out of the snapshot and hashes them. Measured on the
    17.6MB workbook from DEP-846, that residual is 0.9ms to read the zip member
    (an .xlsx is stored rather than deflated, being already compressed, so there
    is no real decompression pass) plus 5.4ms to hash — 0.5s total across a
    79-verifier rubric, against the ~37s parse this saves 78 times over.

    Deliberately not keyed on (path, size) or backed by a byte cache: the first
    would stop identical files at different paths sharing an entry and would
    trust a path to mean the same content, and the second would trade that 0.5s
    of CPU for tens of MB of resident memory per file, which is the wrong
    direction given how tight grading-pod memory already is on these workbooks.
    """
    return f"{kind}:{hashlib.sha256(file_bytes).hexdigest()}"


# Marker a degraded extraction puts in its own output. Such a result is shared
# with everyone already waiting — so a slow LibreOffice conversion is attempted
# once, not once per verifier — but is not persisted, so a later verifier or a
# later grading run retries instead of inheriting a stale failure.
DEGRADED_MARKER = "data-degraded"


def is_degraded(text: str) -> bool:
    return DEGRADED_MARKER in text


def _settle(key: str, task: asyncio.Future[str]) -> None:
    """Promote a finished extraction and clear its in-flight entry.

    Runs as a done-callback rather than in the awaiting coroutine so the
    bookkeeping happens exactly once and in every outcome — including when
    the original requester was cancelled and nobody is left awaiting.
    """
    if _INFLIGHT.get(key) is task:
        del _INFLIGHT[key]

    # Never cache a cancellation or a failure — a transient conversion hiccup
    # shouldn't poison every later verifier in the run.
    if task.cancelled() or task.exception() is not None:
        return

    result = task.result()
    # Degraded output is a statement about the environment, not the file: the
    # conversions behind it return the same empty answer whether LibreOffice is
    # missing, the subprocess exited non-zero, or it hit its 120s timeout. Those
    # are transient, so persisting the degraded text would replay it for every
    # later verifier and — since _RESULTS outlives one run — for later runs of
    # the same bytes. In-flight coalescing above still bounds the cost to one
    # attempt per concurrent wave, which is what the caching was for.
    if is_degraded(result):
        return

    _RESULTS[key] = result
    while len(_RESULTS) > CACHE_MAX_ENTRIES:
        _RESULTS.popitem(last=False)


async def _await_shared(task: asyncio.Future[str]) -> str:
    """Await shared extraction without letting one waiter break the rest.

    asyncio.shield matters here: awaiting a task directly from a coroutine
    that gets cancelled propagates the cancellation *into* the shared task,
    which would then complete as cancelled for every other verifier waiting
    on the same file. Shielding means an individual verifier being abandoned
    only cancels its own wait.
    """
    return await asyncio.shield(task)


async def cached_style_text(
    file_bytes: bytes,
    file_name: str,
    kind: str,
    extract: Callable[[bytes, str], Awaitable[str]],
) -> str:
    """Extract style text for a file, coalescing concurrent identical work.

    Args:
        file_bytes: Raw file bytes; hashed to form the cache key.
        file_name: Filename, for logging and passed through to `extract`.
        kind: Extractor identity, so two formats can never share an entry.
        extract: Async extractor producing the judge-readable text.
    """
    key = cache_key(kind, file_bytes)

    completed = _RESULTS.get(key)
    if completed is not None:
        _RESULTS.move_to_end(key)
        logger.debug(f"[TRANSFORM] style metadata cache hit for {file_name}")
        return completed

    inflight = _INFLIGHT.get(key)
    if inflight is not None:
        # Only join work belonging to this event loop. The done-callback
        # normally clears the entry in every outcome, and an orderly
        # asyncio.run() shutdown cancels pending tasks (which fires it) — but
        # if a loop is torn down abruptly the callback may never run. Awaiting
        # a future from another loop raises RuntimeError ("attached to a
        # different loop"), which would fail the verifier, so start fresh.
        if inflight.get_loop() is asyncio.get_running_loop():
            logger.debug(f"[TRANSFORM] joining in-flight style parse for {file_name}")
            return await _await_shared(inflight)
        logger.warning(
            f"[TRANSFORM] discarding style parse for {file_name} left behind by "
            f"a previous event loop"
        )
        del _INFLIGHT[key]

    task = asyncio.ensure_future(extract(file_bytes, file_name))
    _INFLIGHT[key] = task
    task.add_done_callback(lambda t: _settle(key, t))

    return await _await_shared(task)

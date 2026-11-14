"""``POST /grade/relay/inputs``: a relayed grade's collect, prepared exactly as ``/grade/relay``
prepares it and streamed for the grader sidecar (``grader_sidecar_server.py``) to run."""

from __future__ import annotations

import asyncio
import json
import shutil
import tarfile
import tempfile
from collections.abc import Iterator
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

from .grade import _GRADE_LOCK, RelayGradeRequest, _prepare, _work_base

# The sidecar ships as that one file, so the format it reads is defined there.
from .grader_sidecar_server import (
    GRADE_DIR_PLACEHOLDER,
    INPUTS_MANIFEST,
    INPUTS_ROUTE,
    INPUTS_VERSION,
)
from .utils.signing import signing_enabled

router = APIRouter()

_CHUNK_BYTES = 1024 * 1024


@router.post(INPUTS_ROUTE)
async def grade_relay_inputs(request: RelayGradeRequest) -> StreamingResponse:
    """Prepare a relayed grade's collect pass and stream it, without running the engine.

    One uncompressed tar: ``manifest.json`` first, ``{"version", "args"}``, the engine's
    arguments with ``GRADE_DIR_PLACEHOLDER`` for the grade dir; then every file of the grade dir.
    No credential travels in it. Prepared under the grade lock, like every grade here; the dir is
    removed however the response ends. Signed callers only (404 otherwise).
    """
    if not signing_enabled():
        raise HTTPException(status_code=404, detail="Not Found")
    # A named capture lives outside the grade dir (_snapshot_args), so its path could not be portable.
    if request.capture_id:
        raise HTTPException(
            status_code=422,
            detail="inputs are captured at grade time, never from a capture_id",
        )
    d = Path(tempfile.mkdtemp(prefix="grade-", dir=_work_base()))
    try:
        async with _GRADE_LOCK:
            _, args = await _prepare(request, d)
        manifest = {"version": INPUTS_VERSION, "args": _portable(args, d)}
    except BaseException:
        shutil.rmtree(d, ignore_errors=True)
        raise
    return _GradeDirStream(d, json.dumps(manifest).encode())


class _GradeDirStream(StreamingResponse):
    """The tar of a grade dir, removed however the response ends. Neither the stream (never started
    when the reader leaves before the first byte) nor ``background=`` (skipped when a send fails)
    would run then."""

    def __init__(self, d: Path, manifest: bytes) -> None:
        super().__init__(_tar_stream(d, manifest), media_type="application/x-tar")
        self.grade_dir = d

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await asyncio.to_thread(shutil.rmtree, self.grade_dir, True)


def _portable(args: list[str], d: Path) -> list[str]:
    """``args`` with this container's grade dir replaced by ``GRADE_DIR_PLACEHOLDER``."""
    prefix = f"{d}/"
    return [
        GRADE_DIR_PLACEHOLDER + "/" + a[len(prefix) :] if a.startswith(prefix) else a
        for a in args
    ]


# By hand: tarfile writes a whole member per call, so a generator over it would hold each file in memory.
def _tar_stream(d: Path, manifest: bytes) -> Iterator[bytes]:
    """``manifest``, then each file under ``d``, as one tar a chunk at a time."""
    yield from _member(INPUTS_MANIFEST, len(manifest), iter([manifest]))
    for path in sorted(p for p in d.rglob("*") if p.is_file()):
        yield from _member(
            path.relative_to(d).as_posix(), path.stat().st_size, _chunks(path)
        )
    yield b"\0" * (2 * tarfile.BLOCKSIZE)


def _member(name: str, size: int, chunks: Iterator[bytes]) -> Iterator[bytes]:
    info = tarfile.TarInfo(name)
    info.size, info.mode = size, 0o600
    yield info.tobuf(format=tarfile.PAX_FORMAT)
    yield from chunks
    yield b"\0" * (-size % tarfile.BLOCKSIZE)


def _chunks(path: Path) -> Iterator[bytes]:
    with path.open("rb") as f:
        while chunk := f.read(_CHUNK_BYTES):
            yield chunk

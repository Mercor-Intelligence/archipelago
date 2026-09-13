"""In-container grading: run the grading engine against the LIVE sandbox state.

``POST /grade`` grades the episode IN the same container it ran in, against the live filesystem —
no dispatch to the separate Modal grading lane, no snapshot round-trip, no queue. It captures the
live ``filesystem`` + ``.apps_data`` (streamed to a temp file), writes the caller-supplied grading
config to temp files, and runs the grading engine's CLI (``runner.main``) as a SUBPROCESS in its own venv, then
returns the parsed result. It never persists — the caller (the hosted-envs servicer) records it in
Studio. Signature-protected automatically: ``/grade`` is not in ``_SIGNING_EXEMPT_PATHS``.

Why subprocess (not in-process import): the grading engine's package is ALSO named ``runner`` (it
imports itself as ``from runner.main import …``), so it cannot be imported alongside this env runner
(also ``runner``) without renaming its whole package. Running it in its own venv via the CLI —
exactly how GDM docker worlds run ``score.command`` — sidesteps the collision and keeps this endpoint
free of any grading-package import (the config is opaque JSON, passed straight to the CLI as files).

PACKAGING (deploy-gating): the grading engine is MOUNTED into the sandbox as a Modal Volume at
``/app/grading`` (see hosted-envs ``sandbox.py`` / ``grading_volume.py``) — NOT baked into the shared
platform image. Its interpreter is ``GRADING_VENV_PYTHON`` (default below), with ``runner.main``
importable there; ``grade()`` creates ``GRADING_WORK_DIR`` (default ``/app/.grading``, root-owned
``0700``) inside the model-denied ``/app`` tree so the rubric scratch inherits the two-user boundary.
"""

import asyncio
import functools
import hashlib
import json
import os
import shutil
import signal
import tarfile
import tempfile
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import zstandard
from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel

from runner.data.populate.utils import download_objects
from runner.utils.s3 import S3Credentials

from .data.snapshot.streaming import create_tar_gz_stream
from .data.snapshot.utils import iter_paths
from .grading_paths import (
    BASELINE_ARCHIVE_NAME as _BASELINE_ARCHIVE_NAME,
)
from .grading_paths import (
    BASELINE_DIGEST_CHUNK as _BASELINE_DIGEST_CHUNK,
)
from .grading_paths import GRADE_WORK_DIR
from .grading_paths import baseline_dir as _baseline_dir
from .grading_paths import capture_dir as _capture_dir

router = APIRouter()

# Serialize /grade: a grade captures the live filesystem + runs verifiers against ONE mutable
# sandbox, so overlapping requests would race (and double the resource load). One process per
# sandbox, so a module-level lock is a per-sandbox lock.
_GRADE_LOCK = asyncio.Lock()

# The two subsystems the env snapshots — the live agent workspace + per-app state.
_SNAPSHOT_SUBSYSTEMS = ["filesystem", ".apps_data"]
# Interpreter of the grading venv MOUNTED at /app/grading (where `runner` == grading engine).
_GRADING_VENV_PYTHON = os.environ.get(
    "GRADING_VENV_PYTHON", "/app/grading/.venv/bin/python"
)
# Root of the mounted grading project (parent of its .venv, e.g. /app/grading) — the CLI runs with this
# as cwd so `runner` resolves to the grading engine, not the env runner (same package name).
_GRADING_INSTALL_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(_GRADING_VENV_PYTHON))
)
_GRADING_TIMEOUT_S = float(os.environ.get("GRADING_SUBPROCESS_TIMEOUT_S", "900"))
# Root-owned 0700 scratch base for the grade run, INSIDE the model-denied `/app` tree (the two-user
# boundary: start.sh's build-time `setfacl -R -m u:$CODE_EXEC_RUN_AS_USER:--- /app` denies the model
# user access to /app). Writing the rubric (verifiers/eval/scoring) + result.json here — instead of a
# world-traversable /tmp — keeps them unreadable/untamperable by the unprivileged model user. Falls
# back to the system default only when absent (dev/test, single-user worlds). See PR security notes.
_GRADE_WORK_DIR = GRADE_WORK_DIR


def capture_dir(capture_id: str) -> Path:
    """Where a pre-taken final snapshot lives, under the root-owned work dir."""
    try:
        return _capture_dir(capture_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# SSRF boundary for snapshot downloads: /grade can be POSTed by the confined model over localhost,
# and _download fetches URLs AS ROOT. Only fetch https URLs whose host ends with an allowed suffix
# (the presigned-snapshot S3 host) so a crafted /grade can't turn the downloader into an SSRF against
# internal services / cloud metadata. Comma-separated; default covers AWS S3. Empty disables the
# suffix check ONLY for a custom single-host deploy that sets GRADING_SNAPSHOT_URL_HOSTS instead.
_SNAPSHOT_HOST_SUFFIXES = tuple(
    s.strip()
    for s in os.environ.get("GRADING_SNAPSHOT_HOST_SUFFIXES", ".amazonaws.com").split(
        ","
    )
    if s.strip()
)
# Exact-host allowlist (comma-separated) for custom S3 endpoints (MinIO, etc.); empties by default.
_SNAPSHOT_HOSTS = frozenset(
    h.strip()
    for h in os.environ.get("GRADING_SNAPSHOT_URL_HOSTS", "").split(",")
    if h.strip()
)
_SNAPSHOT_DOWNLOAD_TIMEOUT_S = float(
    os.environ.get("GRADING_SNAPSHOT_TIMEOUT_S", "120")
)
# Allowlist of env-var names accepted from the request's grading_credentials_json — LLM grading
# credentials ONLY. Mirrors GRADING_CREDENTIAL_ENV_NAMES + DIRECT_GRADING_RUNTIME_ENV_NAMES in
# rl-studio packages/islands/shared/grading_credentials.py (duplicated because the env-runner cannot
# import from the server). This is a SECURITY BOUNDARY, not a convenience filter: /grade is
# unauthenticated in hosted-envs (no API_SIGNING_PUBLIC_KEY) and the runner binds 0.0.0.0, so the
# confined model user CAN POST /grade over localhost. Merging arbitrary keys into the ROOT grade
# subprocess env would let it inject loader/exec controls (LD_PRELOAD, LD_LIBRARY_PATH, PYTHONPATH,
# PATH) and run code AS ROOT — bypassing the uid split this endpoint depends on. Only these names
# pass; every other key (loader/exec controls included) is dropped.
_ALLOWED_GRADING_CRED_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "GOOGLE_API_KEY",
        "LITELLM_PROXY_API_BASE",
        "LITELLM_PROXY_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "MERCOR_DOCUMENT_API",
        "MERCOR_DOCUMENT_API_KEY",
        "REDUCTO_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "GEMINI_API_KEY",
    }
)


def _snapshot_url_allowed(url: str) -> bool:
    """True if ``url`` is safe for the root downloader to fetch (SSRF guard). Requires https and a
    host on the exact-host allowlist or ending with an allowed suffix (see _SNAPSHOT_HOST_*)."""
    try:
        p = urlparse(url)
    except ValueError:
        return False
    if p.scheme != "https" or not p.hostname:
        return False
    host = p.hostname
    if host in _SNAPSHOT_HOSTS:
        return True
    return any(host.endswith(sfx) for sfx in _SNAPSHOT_HOST_SUFFIXES)


def _transcode_archive_to_zip(src: Path, dest: Path) -> None:
    """Normalize a downloaded snapshot archive at ``src`` into a ZIP at ``dest`` (the grading CLI
    reads ZIP). Detects the format by magic bytes: a ``.tar.zst`` (the current prebuilt form) is
    zstd-decompressed and re-zipped; an already-ZIP archive is copied through. Streamed throughout
    (bounded memory) so a large snapshot can't OOM the sandbox."""
    with open(src, "rb") as fh:
        magic = fh.read(4)
    if magic[:2] == b"PK":  # already a zip
        shutil.copyfile(src, dest)
        return
    if magic != b"\x28\xb5\x2f\xfd":  # zstd magic
        raise HTTPException(
            status_code=502, detail="snapshot archive is neither zip nor zstd"
        )
    dctx = zstandard.ZstdDecompressor()
    with (
        open(src, "rb") as raw,
        dctx.stream_reader(raw) as reader,
        tarfile.open(fileobj=reader, mode="r|") as tf,  # streaming tar (no seek)
        # ZIP_STORED, not DEFLATE: the source is already zstd-compressed and this zip is a transient
        # artifact the grader reads once — recompressing wastes CPU in the sandbox for a temporary
        # size win. Mirrors the lane's transcode (modal_helpers) + the grading-probe zip.
        zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as zf,
    ):
        for member in tf:
            if not member.isfile():
                continue
            fsrc = tf.extractfile(member)
            if fsrc is not None:
                with zf.open(member.name, "w", force_zip64=True) as zdst:
                    shutil.copyfileobj(fsrc, zdst)


async def _materialize_baseline(request: "GradeRequest", dest: Path) -> None:
    """Put the diff baseline at ``dest`` as a ZIP, by whichever route it came.

    The baseline is the world-SEED state, which is not secret — the model
    already ran against those seeded files — so it is materialized whenever one
    is provided. Neither route leaves an empty baseline, where every seeded file
    reads as agent-created, so diff verifiers are gated off upstream in that
    case.

    A URL WINS over the local copy. The server resolves which snapshot the
    baseline is and only withholds the URL when that resolution is the capture
    this sandbox kept; a URL arriving beside a local id therefore means the two
    describe different trees, and the server's is the one the lane diffs
    against.

    ``prefer_local_baseline`` resolves on its own: the baked copy when this image
    carries it, its S3 prefix when it does not, and a refusal when neither. A caller
    setting it has a world seed, so the empty archive below is never the right answer
    there.
    """
    if request.prefer_local_baseline:
        await _materialize_preferring_local(request, dest)
        return
    if request.initial_snapshot_url:
        await _download_snapshot(request.initial_snapshot_url, dest)
    elif request.initial_snapshot_local_id:
        await asyncio.to_thread(_local_baseline, request, dest)
    elif (
        world_prefix := _prefix_source(request, request.baseline_s3_world_prefix)
    ) is not None:
        await _prefix_half(request, world_prefix, dest)
    else:
        with zipfile.ZipFile(dest, "w"):
            pass


async def _materialize_preferring_local(request: "GradeRequest", dest: Path) -> None:
    """Baked copy, then the world prefix, then refuse."""
    if _local_baseline_present(request):
        await asyncio.to_thread(_local_baseline, request, dest)
        return
    world_prefix = _prefix_source(request, request.baseline_s3_world_prefix)
    if world_prefix is None:
        raise HTTPException(status_code=409, detail="no source for the world baseline")
    await _prefix_half(request, world_prefix, dest)


def _validated_baseline_prefix(prefix: str) -> str:
    """Reject a prefix that could widen or escape the intended read.

    The prefix arrives in the request, so it is untrusted: a wildcard or a traversal
    segment could list outside the world's own tree. Per-OBJECT confinement is handled
    downstream by the downloader's validate_path_safety; this guards the listing itself.
    """
    if not prefix or any(c in prefix for c in "*?[") or ".." in prefix.split("/"):
        raise HTTPException(
            status_code=400, detail="refusing an unsafe baseline prefix"
        )
    return prefix if prefix.endswith("/") else prefix + "/"


async def _materialize_task_half(request: "GradeRequest", dest: Path) -> None:
    """The authored ``tasks/`` overlay, or nothing when this world has none.

    Two sources, mirroring the world half: the presigned archive when Studio has one, the
    task's own S3 prefix when it does not. Left as its own archive for the CLI to merge.

    Writes nothing when neither source is armed.
    """
    if request.task_snapshot_url:
        await _download_snapshot(request.task_snapshot_url, dest)
        return
    prefix = _prefix_source(request, request.baseline_s3_task_prefix)
    if prefix is not None:
        await _prefix_half(request, prefix, dest, allow_empty=True)


def _prefix_source(request: "GradeRequest", prefix: str) -> str | None:
    """The validated prefix to assemble a half from, or ``None`` if this route is not armed.

    Credentials are required: without them ``get_s3_client`` falls back to ambient identity.
    """
    if not request.baseline_s3_bucket or not prefix:
        return None
    if request.baseline_s3_credentials is None:
        return None
    return _validated_baseline_prefix(prefix)


async def _prefix_half(
    request: "GradeRequest", prefix: str, dest: Path, *, allow_empty: bool = False
) -> None:
    """Assemble ONE baseline half from its S3 prefix, mirroring the async lane.

    The prebuilt ``snapshot_zips/`` archive is a read optimization, not the only source: the
    async lane's ``download_world_snapshot`` falls through to a per-file walk when it is
    absent, and so does this, so an archive-less world still grades in-container.

    One half per call, and no merging: the CLI merges the halves because the merge is
    subtraction-aware. Reuses ``download_objects`` for concurrency, ranged reads, retries and
    path-safety confinement, writing into a temp dir via ``dest_root``.

    A prefix MUST be a snapshot ROOT (``worlds/<id>/``, ``tasks/<id>/``), not a per-subsystem
    populate source: entry names are made relative to it, reproducing the archive layout.

    ``allow_empty`` writes nothing for an empty listing instead of refusing. Set for the task
    overlay, where "no overlay" is a valid state; the world seed fails closed.
    """
    with tempfile.TemporaryDirectory(prefix="baseline-") as tmp:
        total = await download_objects(
            bucket=request.baseline_s3_bucket,
            key=prefix,
            subsystem="",
            s3_credentials=request.baseline_s3_credentials,
            dest_root=tmp,
        )
        if total == 0 and allow_empty:
            return
        pack = asyncio.ensure_future(
            asyncio.to_thread(_pack_baseline_tree, Path(tmp), dest)
        )
        try:
            packed = await asyncio.shield(pack)
        finally:
            if not pack.done():
                await asyncio.gather(pack, return_exceptions=True)
        if packed == 0:
            dest.unlink(missing_ok=True)
            if allow_empty:
                return
            raise HTTPException(status_code=409, detail="empty baseline prefix listing")
    logger.info(f"[GRADE] baseline assembled from prefix: {packed} file(s)")


def _pack_baseline_tree(root: Path, dest: Path) -> int:
    """Zip ``root`` into ``dest``, dropping each file as it is archived.

    Entry names are relative to ``root``, reproducing ``build_snapshot_zip``'s layout. The
    archive is ZIP_STORED, so unlinking as we go keeps peak disk at tree + largest file
    instead of ~2x the snapshot.
    """
    packed = 0
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as zf:
        for path in sorted(root.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(root).as_posix())
                path.unlink()
                packed += 1
    return packed


def _local_baseline_present(request: "GradeRequest") -> bool:
    """Whether the named baked baseline is on disk in this image."""
    if not request.initial_snapshot_local_id:
        return False
    try:
        return (
            _baseline_dir(request.initial_snapshot_local_id) / _BASELINE_ARCHIVE_NAME
        ).is_file()
    except (ValueError, OSError):
        return False


def _local_baseline(request: "GradeRequest", dest: Path) -> None:
    """Normalize the kept post-populate archive to a ZIP at ``dest``.

    The file sat on disk for the whole agent loop, in a tree the model ran in as
    root wherever `CODE_EXEC_RUN_AS_USER` is unset. So it is checked against the
    size and digest the runner recorded when it was written, which the model
    cannot reach. A mismatch refuses.

    The digest is the check and not the file's mtime, because root can set an
    mtime with one `utime` call and cannot produce a sha256 collision. The mtime
    is logged on a mismatch, where it separates a rewrite during the run from a
    file the disk lost.

    Each refusal names which one it is. "Not on disk" and "does not match" are
    the same lost optimization to the caller and different events to whoever
    reads the log: the first is a full disk or a cleanup, the second is a
    rewrite.
    """
    src = _baseline_dir(request.initial_snapshot_local_id) / _BASELINE_ARCHIVE_NAME
    if not src.is_file():
        raise HTTPException(
            status_code=409, detail="the named local baseline is not on disk"
        )
    if not request.initial_snapshot_local_sha256:
        _transcode_archive_to_zip(src, dest)
        return
    stat = src.stat()
    size = stat.st_size
    if size != request.initial_snapshot_local_bytes:
        logger.error(
            f"local baseline {request.initial_snapshot_local_id} is "
            f"{size} bytes against the {request.initial_snapshot_local_bytes} "
            f"recorded when it was written; last modified {stat.st_mtime}"
        )
        raise HTTPException(
            status_code=409, detail="the local baseline does not match its record"
        )
    digest = hashlib.sha256()
    with src.open("rb") as fh:
        while chunk := fh.read(_BASELINE_DIGEST_CHUNK):
            digest.update(chunk)
    if digest.hexdigest() != request.initial_snapshot_local_sha256:
        logger.error(
            f"local baseline {request.initial_snapshot_local_id} digests to "
            f"{digest.hexdigest()} against the recorded "
            f"{request.initial_snapshot_local_sha256}; last modified "
            f"{stat.st_mtime}, which the populate log dates against the run"
        )
        raise HTTPException(
            status_code=409, detail="the local baseline does not match its record"
        )
    _transcode_archive_to_zip(src, dest)


async def _download_snapshot(url: str, dest: Path) -> None:
    """Download a presigned snapshot archive and normalize it to a ZIP at ``dest`` (streamed to disk,
    bounded memory). Rejects a URL that fails the SSRF allowlist and does NOT follow redirects (a
    redirect could point the root downloader past the host allowlist). Raises on a non-2xx so the
    caller falls back to the async lane rather than grading against a missing baseline."""
    if not _snapshot_url_allowed(url):
        raise HTTPException(
            status_code=400,
            detail=f"snapshot URL host not allowed: {urlparse(url).hostname!r}",
        )
    with tempfile.NamedTemporaryFile(dir=dest.parent, suffix=".dl") as tmp:
        async with httpx.AsyncClient(
            timeout=_SNAPSHOT_DOWNLOAD_TIMEOUT_S, follow_redirects=False
        ) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes():
                    tmp.write(chunk)
        tmp.flush()
        await asyncio.to_thread(_transcode_archive_to_zip, Path(tmp.name), dest)


def _filter_grading_credentials(raw_json: str) -> dict[str, str]:
    """Parse ``grading_credentials_json`` and keep ONLY allowlisted LLM credential keys.

    Security boundary (see ``_ALLOWED_GRADING_CRED_KEYS``): everything not on the allowlist — most
    importantly loader/exec controls like ``LD_PRELOAD`` / ``PYTHONPATH`` / ``PATH`` — is dropped, so
    a crafted ``/grade`` cannot steer the root grade subprocess. Malformed or non-object JSON yields
    an empty dict (grade proceeds with no injected creds → LLM verifiers fail → lane fallback)."""
    try:
        creds = json.loads(raw_json or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(creds, dict):
        return {}
    return {
        str(k): str(v) for k, v in creds.items() if str(k) in _ALLOWED_GRADING_CRED_KEYS
    }


def grading_available() -> bool:
    """True when the grading venv is present (mounted at /app/grading), so ``/grade`` can actually run.

    The handlers check this per request, so a sandbox WITHOUT the grader answers 404 instead of
    500-ing at request time. It used to gate ``include_router`` in ``main.py``, which reads the disk
    once at import and so never registered the route for a sandbox that mounts its engine after the
    agent loop.
    """
    return os.path.exists(_GRADING_VENV_PYTHON)


class GradeRequest(BaseModel):
    """Everything the grading CLI needs, supplied by Studio/servicer as opaque JSON (never parsed
    here — written straight to files for the CLI). The graded (FINAL) state is captured live in this
    container; the diff BASELINE arrives as a presigned ZIP URL (optional — empty when the world
    provides none). Goldens are never fetched here; a request carrying them is refused."""

    grading_run_id: str
    trajectory_id: str
    trajectory_json: str
    grading_settings_json: str
    verifiers_json: str
    eval_configs_json: str
    scoring_config_json: str
    # JSON object {env_name: value} of LLM grading credentials, injected into the grade subprocess
    # env ONLY (see grade()). Defaults to empty so an older Studio that doesn't send it still grades
    # (deterministic verifiers work; LLM verifiers then auth-fail → the run errors → lane fallback).
    grading_credentials_json: str = "{}"
    # Presigned snapshot ZIP URL (empty = unsupported/none) for the diff BASELINE (the world-seed
    # state), downloaded here for the CLI's --initial-snapshot. Empty → an empty baseline (every
    # seeded file reads as agent-created) so diff verifiers should be gated off upstream in that case.
    initial_snapshot_url: str = ""
    # The baseline kept on local disk when the snapshot was taken, which spares
    # the download above. Preferred when set. The digest and size are the
    # runner's record of what it wrote: they travel out of this sandbox and back,
    # so a tree the model rewrote during its run fails the check here instead of
    # deciding its own diff. See `_local_baseline`.
    baseline_s3_bucket: str = ""
    baseline_s3_world_prefix: str = ""
    baseline_s3_task_prefix: str = ""
    baseline_s3_credentials: S3Credentials | None = None
    prefer_local_baseline: bool = False
    task_baseline_required: bool = False
    initial_snapshot_local_id: str = ""
    initial_snapshot_local_sha256: str = ""
    initial_snapshot_local_bytes: int = 0
    # The authored `tasks/` overlay. The grading CLI merges it with the baseline
    # above, where the subtraction rule lives.
    task_snapshot_url: str = ""
    # Whether the capture's environment resolved its subtraction markers. The
    # server answers this; nothing here may infer it.
    subtraction_resolved: bool = False
    # A capture taken by POST /grade/capture before anything mutated the tree.
    # Empty means capture now, which is what hosted-envs and the blocking route
    # do. See `capture_dir`.
    capture_id: str = ""
    # Still accepted so a request carrying goldens is refused, not silently graded without them.
    golden_snapshot_urls: list[str] = []
    # Spend attribution and the batch spend unit. Budget metering itself rides
    # in grading_settings_json.
    account_id: str = ""
    studio_actor_user_id: str = ""
    trajectory_batch_id: str = ""
    # Paths the trajectory snapshot drops; the live capture must drop them too.
    snapshot_exclude_globs: list[str] = []


class RelayVerdict(BaseModel):
    """One relayed judge answer."""

    content: str


class RelayGradeRequest(GradeRequest):
    """The COLLECT pass of a relayed grade: builds the judge prompts and returns them with the rows."""


class RelayScoreRequest(GradeRequest):
    """The SCORE pass: the collect pass's rows plus the caller's verdicts; reads no filesystem."""

    verdicts: dict[str, RelayVerdict]
    score_from: dict[str, Any]


class GradeResponse(BaseModel):
    """The grading CLI's raw output JSON (verifier_results + scoring_results), passed through for the
    caller to record in Studio."""

    result: dict[str, Any]


def _capture_live_final_snapshot(
    dest: Path, exclude_globs: Sequence[str] | None = None
) -> None:
    """Zip the LIVE sandbox state (``filesystem`` + ``.apps_data``) to ``dest`` — no S3 round-trip.

    The env produces ``tar.gz`` but the grading engine reads ``zip`` (see the environment README), so
    convert in place: this is the snapshot the lane would otherwise upload + re-download.

    Streams throughout so peak memory is a small copy buffer, NOT the whole workspace: the tar goes
    to a temp FILE (not an in-RAM BytesIO), and each member is copied into the zip with
    ``copyfileobj`` (not read fully into RAM). Runs in a worker thread (see ``grade()``), off the
    event loop. The temp tar lives next to ``dest`` (the root-owned 0700 grade dir) and is removed on
    close.
    """
    with tempfile.NamedTemporaryFile(dir=dest.parent, suffix=".tar.gz") as tmp:
        for chunk in create_tar_gz_stream(
            _SNAPSHOT_SUBSYSTEMS,
            "live-grade",
            functools.partial(iter_paths, exclude_globs=exclude_globs),
        ):
            tmp.write(chunk)
        tmp.flush()
        tmp.seek(0)
        with (
            tarfile.open(tmp.name, mode="r:gz") as tf,
            # ZIP_STORED (see _transcode_archive_to_zip): the source is already gzip'd and this zip is
            # read once by the grader — don't spend sandbox CPU recompressing a transient artifact.
            zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as zf,
        ):
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                src = tf.extractfile(member)
                if src is not None:
                    # force_zip64: streaming with zf.open() writes members of UNKNOWN size, so
                    # zipfile can't infer ZIP64 the way writestr(len(data)) did — a >2 GiB live-state
                    # file would raise LargeZipFile on close and 500 the grade. Mirrors the rest of
                    # the snapshot-zip path, which passes this for the same reason.
                    with zf.open(member.name, "w", force_zip64=True) as zdst:
                        shutil.copyfileobj(src, zdst)


@router.post("/grade")
async def grade(request: GradeRequest) -> GradeResponse:
    """Grade the live episode in-container via the grading CLI and return its result JSON.

    Serialized: overlapping grades would race the one mutable sandbox filesystem (see _GRADE_LOCK).
    """
    # Per request, not at import. main.py used to gate `include_router` on this,
    # which reads the disk once at boot and so never registered the route for a
    # sandbox that mounts the engine after its agent loop.
    if not grading_available():
        raise HTTPException(status_code=404, detail="No grading engine in this sandbox")
    async with _GRADE_LOCK:
        return await _grade(request)


@router.post("/grade/relay")
async def grade_relay(request: RelayGradeRequest) -> GradeResponse:
    """Grade the live episode with LLM judging relayed to the caller.

    Synchronous, and a grade like any other in this namespace: same engine, same
    bundle, same lock as ``/grade`` — only the judge transport differs, so the two
    cannot drift apart on anything else.
    """
    if not grading_available():
        raise HTTPException(status_code=404, detail="No grading engine in this sandbox")
    async with _GRADE_LOCK:
        return await _grade(request)


@router.post("/grade/relay/score")
async def grade_relay_score(request: RelayScoreRequest) -> GradeResponse:
    """Score a collect pass's rows with the caller's verdicts; same engine and lock, no filesystem."""
    if not grading_available():
        raise HTTPException(status_code=404, detail="No grading engine in this sandbox")
    async with _GRADE_LOCK:
        return await _grade(request)


def _relay_args(request: GradeRequest, d: Path) -> list[str]:
    """Client-relayed judging, as CLI arguments. A plain grade judges locally.

    The file is always written, empty included: an empty map IS the collect pass,
    so its presence alone tells the CLI to relay and no second flag is needed.
    """
    if isinstance(request, RelayScoreRequest):
        verdicts = {k: v.content for k, v in request.verdicts.items()}
    elif isinstance(request, RelayGradeRequest):
        verdicts = {}
    else:
        return []
    verdicts_path = d / "relay_verdicts.json"
    verdicts_path.write_text(json.dumps(verdicts))
    return ["--relay-verdicts", str(verdicts_path)]


def _attribution_args(request: GradeRequest) -> list[str]:
    """Spend attribution and the batch spend unit, as CLI arguments.

    The CLI sets the same context variables `run_grading` sets on the lane, so a
    judge call is billed to the same account and counted against the same batch
    cap. Absent, the call is billed to nobody and no cap sees it. Budget
    metering is not here: it rides in `grading_settings_json`, which the CLI
    parses, so it keeps the one source the lane reads.
    """
    args: list[str] = []
    for flag, value in (
        ("--account-id", request.account_id),
        ("--actor-user-id", request.studio_actor_user_id),
        ("--trajectory-batch-id", request.trajectory_batch_id),
    ):
        if value:
            args += [flag, value]
    return args


def _log_engine_failure(
    grading_run_id: str, result: dict[str, Any], stderr: bytes
) -> None:
    """Put the grading engine's own account of a failed grade in the log.

    The engine exits 0 whether the grade passed or failed, so its stderr is
    read nowhere else, and the caller records a status without a reason.
    """
    status = str(result.get("grading_run_status") or "").lower()
    if status == "completed":
        return
    logger.error(
        f"grading run {grading_run_id} ended {status or 'with no status'}: "
        f"{stderr.decode(errors='replace')[-2000:]}"
    )


async def _snapshot_args(request: GradeRequest, d: Path) -> list[str]:
    """Materialize the baseline and live final state into ``d``; returns the CLI arguments."""
    # Goldens are baked into the image, never fetched here; refuse rather than grade without them.
    if request.golden_snapshot_urls:
        raise HTTPException(
            status_code=409,
            detail="golden snapshots are not downloaded at grade time; bake them into the image",
        )
    initial = d / "initial.zip"
    await _materialize_baseline(request, initial)
    task_snapshot = d / "task.zip"
    await _materialize_task_half(request, task_snapshot)
    if request.task_baseline_required and not task_snapshot.exists():
        raise HTTPException(
            status_code=409, detail="the task baseline half could not be assembled"
        )
    # A capture taken earlier is the tree as it stood when the S3 snapshot was taken. Capturing
    # here instead would read whatever the end-of-run image builders left behind, and the lane
    # scores the S3 copy, so the two would disagree.
    captured: Path | None = None
    if request.capture_id:
        captured = capture_dir(request.capture_id) / "final.zip"
        if not captured.is_file():
            raise HTTPException(
                status_code=409, detail="the named capture is not on disk"
            )
    if captured is not None:
        final = captured
    else:
        final = d / "final.zip"
        # Offload the synchronous tar→zip capture to a thread: it is CPU-bound and would
        # otherwise block the FastAPI event loop (stalling /health + MCP) for the whole capture.
        await asyncio.to_thread(
            _capture_live_final_snapshot,
            final,
            request.snapshot_exclude_globs or None,
        )
    args = [
        "--initial-snapshot",
        str(initial),
        "--final-snapshot",
        str(final),
        "--trajectory",
        str(d / "trajectory.json"),
    ]
    # The CLI merges the two halves, because `runner.utils.file_subtraction`
    # owns the marker rule and its arming answer already has three copies.
    if task_snapshot.exists():
        args += ["--task-snapshot", str(task_snapshot)]
        if request.subtraction_resolved:
            args += ["--subtraction-resolved"]
    return args


async def _grade(request: GradeRequest) -> GradeResponse:
    # Scratch dir INSIDE the model-denied /app tree (root-owned, 0700) so the rubric config +
    # result.json can't be read or raced by the unprivileged model user. Create it if absent — the
    # grading engine now arrives via a mounted volume, so nothing pre-makes this dir (the runner runs
    # as root, and /app is root-owned, so the model cannot pre-empt it).
    work_base: str | None = _GRADE_WORK_DIR
    try:
        os.makedirs(work_base, mode=0o700, exist_ok=True)
        os.chmod(work_base, 0o700)
    except OSError:
        work_base = None
    # FAIL CLOSED, not open: falling back to a model-readable /tmp is only safe with NO separate
    # model user. Under uid separation (CODE_EXEC_RUN_AS_USER set) an unusable GRADING_WORK_DIR is a
    # misconfig — refuse rather than silently leak the rubric to /tmp.
    if work_base is None and os.environ.get("CODE_EXEC_RUN_AS_USER"):
        raise HTTPException(
            status_code=500,
            detail=(
                f"GRADING_WORK_DIR {_GRADE_WORK_DIR!r} is missing under uid separation "
                "(CODE_EXEC_RUN_AS_USER set); refusing to grade into a model-readable /tmp"
            ),
        )
    with tempfile.TemporaryDirectory(prefix="grade-", dir=work_base) as tmp:
        # Defensive: TemporaryDirectory is already 0700, but re-assert it so a permissive umask or a
        # pre-existing work_base can't leave the rubric group/other-readable.
        os.chmod(tmp, 0o700)
        d = Path(tmp)

        # 1. Caller-supplied config -> files (opaque; the CLI validates them).
        (d / "trajectory.json").write_text(request.trajectory_json)
        (d / "grading_settings.json").write_text(request.grading_settings_json)
        (d / "verifiers.json").write_text(request.verifiers_json)
        (d / "eval_configs.json").write_text(request.eval_configs_json)
        (d / "scoring_config.json").write_text(request.scoring_config_json)

        # 2. Snapshots — a score pass has none.
        if isinstance(request, RelayScoreRequest):
            score_from_path = d / "score_from.json"
            score_from_path.write_text(json.dumps(request.score_from))
            module = "runner.relay_scoring"
            source_args = ["--score-from", str(score_from_path)]
        else:
            module = "runner.main"
            source_args = await _snapshot_args(request, d)

        # 3. Run the grading engine CLI in its own venv (no in-process import — see module docstring).
        out = d / "result.json"
        cmd = [
            _GRADING_VENV_PYTHON,
            "-m",
            module,
            "--grading-run-id",
            request.grading_run_id,
            "--trajectory-id",
            request.trajectory_id,
            *source_args,
            "--grading-settings",
            str(d / "grading_settings.json"),
            "--verifiers",
            str(d / "verifiers.json"),
            "--eval-configs",
            str(d / "eval_configs.json"),
            "--scoring-config",
            str(d / "scoring_config.json"),
            "--output",
            str(out),
        ]
        cmd += _relay_args(request, d)
        cmd += _attribution_args(request)

        # Run in the grading install dir with PYTHONPATH stripped so `-m runner.main` resolves to the
        # GRADING runner (mounted at _GRADING_INSTALL_DIR/.venv), NOT the env runner package — which is
        # ALSO named `runner` (/app/runner on the server's path). Without this, cwd/PYTHONPATH inherited
        # from the env runner would shadow the grading engine. Config paths passed to the CLI are
        # absolute, so the cwd change is safe.
        # Start from the runner's env MINUS PYTHONPATH (see above) AND minus every grading-credential
        # name. Dropping the inherited creds is a SECURITY boundary, not cleanup: the whole credential
        # set (keys AND *_BASE_URL endpoints) must come only from the request, together. Otherwise a
        # crafted /grade could supply just a BASE_URL override (allowlisted) and pair it with a REAL
        # key inherited from os.environ — root grading would then send that key to an attacker's URL.
        sub_env = {
            k: v
            for k, v in os.environ.items()
            if k != "PYTHONPATH" and k not in _ALLOWED_GRADING_CRED_KEYS
        }
        # LLM grading credentials arrive in the request body and are injected into THIS subprocess's
        # env only — never into os.environ (which the model agent inherits via env=os.environ.copy())
        # and never to disk. The subprocess runs as root inside model-denied /app, so the unprivileged
        # model user can't read them. Empty when Studio omits them.
        # ALLOWLIST (security-critical): /grade is unauthenticated in hosted-envs and reachable by the
        # confined model over localhost, so only KNOWN credential names pass — never loader/exec
        # controls (LD_PRELOAD, PYTHONPATH, PATH, …) that would let a crafted /grade run code as root.
        sub_env.update(_filter_grading_credentials(request.grading_credentials_json))
        sub_cwd = _GRADING_INSTALL_DIR if os.path.isdir(_GRADING_INSTALL_DIR) else None
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=sub_cwd,
            env=sub_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Own process group so a timeout can kill the WHOLE tree, not just the CLI parent —
            # code-executing verifiers (llm_code_verifier / agentic_verifier) spawn grandchildren
            # that would otherwise keep consuming sandbox resources after we've fallen back.
            start_new_session=True,
        )
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_GRADING_TIMEOUT_S
            )
        except TimeoutError:
            # Kill the process GROUP, not just proc, so verifier grandchildren die too.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass  # already gone
            # reap the killed child so repeated timeouts don't accumulate zombies
            await proc.wait()
            raise HTTPException(status_code=504, detail="grading timed out") from None
        if proc.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"grading failed (exit {proc.returncode}): {stderr.decode()[-500:]}",
            )

        # 4. Return the CLI's result JSON verbatim for the caller to record in Studio.
        result = json.loads(out.read_text())
        _log_engine_failure(request.grading_run_id, result, stderr)
        return GradeResponse(result=result)

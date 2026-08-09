"""Centralized, backend-pluggable S3 download interface for grading.

This module exists so we can A/B-experiment the default ``boto3``/``aioboto3``
download path against the ``s5cmd`` Go binary on the *same* live grading traffic,
without forking call sites or losing the existing metrics.

Design:
  * :class:`S3Downloader` is the interface. ``download_object`` fetches a single
    object to a local path; ``download_prefix`` fetches every object under a
    prefix into a local directory.
  * The default backend is the in-place ``boto3`` code in ``modal_helpers`` (its
    battle-tested parallel byte-range primitives) — it is *not* wrapped here, to
    avoid a circular import and avoid duplicating its retry logic. This module
    only owns the **s5cmd** implementation plus the **selection** logic and
    safety guardrails shared by both.
  * :func:`resolve_backend` reads a ContextVar set once per grading run from the
    server-resolved ``GradingConfig.s3_transfer_backend`` (a PostHog multivariate
    flag, evaluated server-side). It defaults to ``"boto3"`` so the path is
    backwards-compatible and self-disables when anything is off.

Security (per Corridor guardrails):
  * subprocess is always invoked with an argv list, never ``shell=True``.
  * Every bucket / key / prefix is validated against a strict allowlist regex and
    pinned to the known snapshot namespace before being interpolated into an
    ``s3://`` URI — no DB-sourced string reaches the shell or the filesystem
    unsanitized.
  * Local destinations are confined (realpath) to the intended temp directory.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
import time
import zipfile
from contextvars import ContextVar
from dataclasses import dataclass
from typing import IO, Literal, Protocol, runtime_checkable

from loguru import logger

# ---------------------------------------------------------------------------
# Backend selection
# ---------------------------------------------------------------------------

S3Backend = Literal["boto3", "s5cmd"]
DEFAULT_BACKEND: S3Backend = "boto3"

# Set once per grading run (in ``run_grading`` / ``run_validate_verifiers``) from
# the server-resolved ``GradingConfig.s3_transfer_backend``. Mirrors the existing
# ``campaign_id_ctx`` / ``lane_ctx`` ContextVar pattern in the grading runner so
# the download primitives stay parameter-free.
s3_backend_ctx: ContextVar[str | None] = ContextVar("s3_backend_ctx", default=None)


def resolve_backend() -> S3Backend:
    """Return the active backend for the current grading run.

    Order of precedence: per-run ContextVar (server flag) → ``S3_TRANSFER_BACKEND``
    env override → default. Anything unrecognized resolves to the default so a
    bad flag value can never break grading.
    """
    raw = s3_backend_ctx.get() or os.getenv("S3_TRANSFER_BACKEND")
    if raw == "s5cmd":
        return "s5cmd"
    return DEFAULT_BACKEND


def s5cmd_available() -> bool:
    """True if the ``s5cmd`` binary is on PATH in this container/image."""
    return shutil.which("s5cmd") is not None


# Records which backend *actually* moved the bytes for the current download —
# distinct from the *selected* backend (resolve_backend). The download primitives
# set this (s5cmd on success, boto3 on fallback/default) so the caller can tag
# metrics with the path that really ran; otherwise a download that selected
# s5cmd but fell back to boto3 (missing binary or s5cmd error) would mislabel the
# A/B as s5cmd.
s3_backend_used_ctx: ContextVar[S3Backend] = ContextVar(
    "s3_backend_used", default="boto3"
)


def note_backend_used(backend: S3Backend) -> None:
    """Record the backend that actually performed the current download."""
    s3_backend_used_ctx.set(backend)


def backend_used() -> S3Backend:
    """The backend that actually performed the current download (default boto3)."""
    return s3_backend_used_ctx.get()


# Splits the s5cmd loose-download→pack wall time into ``(cp_seconds,
# pack_seconds)`` — the s5cmd ``cp`` transfer vs the serial STORED-zip pack that
# follows it. Set by ``_s5cmd_prefixes_to_zip`` and read at the
# ``snapshot_download`` phase site so the boto3-vs-s5cmd A/B can attribute the
# latency to transfer vs serialization. Same task-scoped ContextVar discipline
# as ``s3_backend_used_ctx``: written inside the download task and read later in
# the same task, so concurrent sibling downloads (asyncio copies the context per
# Task) never clobber each other. Default ``(0.0, 0.0)`` means the loose-pack
# path didn't run (boto3 / prebuilt archive), so callers skip emitting.
s3_loose_timing_ctx: ContextVar[tuple[float, float]] = ContextVar(
    "s3_loose_timing", default=(0.0, 0.0)
)


def note_loose_timing(cp_seconds: float, pack_seconds: float) -> None:
    """Record the (cp, pack) split of the current s5cmd loose-pack download."""
    s3_loose_timing_ctx.set((cp_seconds, pack_seconds))


def loose_timing() -> tuple[float, float]:
    """``(cp_seconds, pack_seconds)`` of the current download, or ``(0.0, 0.0)``
    when the s5cmd loose-pack path didn't run."""
    return s3_loose_timing_ctx.get()


# ---------------------------------------------------------------------------
# Stats — returned by every transfer so callers can emit metrics tagged by
# backend. Comparison then falls out of the existing ``snapshot_download_*``
# Datadog metrics by splitting on the ``backend`` tag.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TransferStats:
    backend: S3Backend
    op: Literal["object", "prefix"]
    num_bytes: int
    num_files: int
    seconds: float

    @property
    def mbps(self) -> float:
        return self.num_bytes / 1e6 / max(self.seconds, 1e-6)


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------


@runtime_checkable
class S3Downloader(Protocol):
    """A pluggable S3 download backend.

    Implementations must be safe to call concurrently and must leave no
    partial files behind on failure (raise instead).
    """

    name: S3Backend

    async def download_object(
        self, bucket: str, key: str, dest_path: str, *, size: int | None = None
    ) -> TransferStats:
        """Download a single object to ``dest_path`` (overwriting)."""
        ...

    async def download_prefix(
        self, bucket: str, prefix: str, dest_dir: str
    ) -> TransferStats:
        """Download every object under ``prefix`` into ``dest_dir``.

        Object keys are reproduced as paths relative to ``prefix`` under
        ``dest_dir``.
        """
        ...


# ---------------------------------------------------------------------------
# Safety helpers (shared by every backend that builds URIs / paths)
# ---------------------------------------------------------------------------

# S3 object keys / prefixes we ever transfer are snapshot paths of the form
# ``{namespace}/{snapshot_id}/{relative_path}``. They are alphanumeric plus a
# small punctuation set — anything else is rejected before it can reach argv.
# NB: matched with ``fullmatch`` (not ``match`` + ``$``) so a trailing newline
# can't slip through — ``$`` matches just before a final ``\n`` in Python.
_SAFE_TOKEN_RE = re.compile(r"[A-Za-z0-9._\-/]+")

# Known snapshot namespaces (the first path segment). Pinning to these prevents
# a malformed snapshot_id from escaping the snapshot keyspace.
_ALLOWED_NAMESPACES = frozenset(
    {
        "worlds",
        "tasks",
        "trajectories",
        "playgrounds",
        "golden-responses",
        "snapshot_zips",
    }
)


def _validate_bucket(bucket: str) -> str:
    if not bucket or not re.fullmatch(r"[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]", bucket):
        raise ValueError(f"unsafe S3 bucket name: {bucket!r}")
    return bucket


def _validate_key(key: str, *, require_namespace: bool = True) -> str:
    if (
        not key
        or ".." in key
        or key.startswith("/")
        or not _SAFE_TOKEN_RE.fullmatch(key)
    ):
        raise ValueError(f"unsafe S3 key/prefix: {key!r}")
    if require_namespace and key.split("/", 1)[0] not in _ALLOWED_NAMESPACES:
        raise ValueError(f"S3 key outside the snapshot namespace: {key!r}")
    return key


def _confine(path: str, *, root: str | None = None) -> str:
    """Return the realpath of ``path``, asserting it stays under ``root``.

    ``root`` defaults to the system temp dir, which is where every grading
    download lands. Guards against path traversal via crafted keys.
    """
    real = os.path.realpath(path)
    base = os.path.realpath(root or _temp_root())
    if real != base and not real.startswith(base + os.sep):
        raise ValueError(f"destination escapes confinement root: {path!r}")
    return real


def _temp_root() -> str:
    import tempfile

    return tempfile.gettempdir()


# ---------------------------------------------------------------------------
# s5cmd backend
# ---------------------------------------------------------------------------


class S5cmdDownloader:
    """Download backend that shells out to the ``s5cmd`` Go binary.

    Authentication uses the default AWS credential chain (the IAM role attached
    to the Modal container), exactly like the boto3 path — no new secrets. The
    region comes from ``AWS_DEFAULT_REGION`` / ``AWS_REGION`` in the environment.
    """

    name: S3Backend = "s5cmd"

    def __init__(
        self,
        *,
        numworkers: int = 256,
        part_concurrency: int = 16,
        binary: str = "s5cmd",
    ) -> None:
        # ``--numworkers`` parallelizes across objects; ``--concurrency``
        # parallelizes the parts of a single (large) object.
        self._numworkers = numworkers
        self._part_concurrency = part_concurrency
        self._binary = binary

    async def _run(
        self, args: list[str], extra_env: dict[str, str] | None = None
    ) -> None:
        argv = [self._binary, *args]
        logger.debug(f"s5cmd: {' '.join(argv)}")
        # Grading assumes an OIDC role in-process, so the temp AWS creds aren't in
        # the container env — a subprocess can't see them. When extra_env is given
        # we merge it over os.environ; otherwise pass None to inherit the default
        # credential chain (local dev / role on env vars).
        env = {**os.environ, **extra_env} if extra_env else None
        proc = await asyncio.create_subprocess_exec(
            *argv,  # argv list — never shell=True
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0:
            msg = stderr.decode("utf-8", "replace").strip()[:2000]
            raise RuntimeError(f"s5cmd exited {proc.returncode}: {msg}")

    async def download_object(
        self,
        bucket: str,
        key: str,
        dest_path: str,
        *,
        size: int | None = None,
        extra_env: dict[str, str] | None = None,
    ) -> TransferStats:
        _validate_bucket(bucket)
        _validate_key(key)
        dest = _confine(dest_path)
        t0 = time.perf_counter()
        await self._run(
            [
                "cp",
                "--concurrency",
                str(self._part_concurrency),
                f"s3://{bucket}/{key}",
                dest,
            ],
            extra_env=extra_env,
        )
        elapsed = time.perf_counter() - t0
        num_bytes = size if size is not None else _safe_size(dest)
        return TransferStats("s5cmd", "object", num_bytes, 1, elapsed)

    async def download_prefix(
        self,
        bucket: str,
        prefix: str,
        dest_dir: str,
        *,
        extra_env: dict[str, str] | None = None,
    ) -> TransferStats:
        _validate_bucket(bucket)
        norm = prefix.rstrip("/")
        _validate_key(norm)
        dest = _confine(dest_dir)
        os.makedirs(dest, exist_ok=True)
        t0 = time.perf_counter()
        # Trailing "/" on dest tells s5cmd to recreate the key tree under it;
        # the "/*" wildcard selects every object under the prefix.
        await self._run(
            [
                "--numworkers",
                str(self._numworkers),
                "cp",
                "--concurrency",
                str(self._part_concurrency),
                f"s3://{bucket}/{norm}/*",
                dest + os.sep,
            ],
            extra_env=extra_env,
        )
        elapsed = time.perf_counter() - t0
        num_bytes, num_files = _dir_size_and_count(dest)
        return TransferStats("s5cmd", "prefix", num_bytes, num_files, elapsed)


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def get_s5cmd_downloader() -> S5cmdDownloader | None:
    """Return an s5cmd backend iff it's both selected and available.

    Returns ``None`` when the active backend is boto3, or when s5cmd was
    requested but the binary is missing — callers treat ``None`` as "use the
    default boto3 path", so a misconfigured flag silently falls back instead of
    failing the grade.
    """
    if resolve_backend() != "s5cmd":
        return None
    if not s5cmd_available():
        logger.warning(
            "s3_transfer: backend=s5cmd requested but binary not found on PATH; "
            "falling back to boto3"
        )
        return None
    return S5cmdDownloader()


# ---------------------------------------------------------------------------
# Packaging — turn a downloaded directory into the zip the verifiers expect
# ---------------------------------------------------------------------------


def pack_dir_to_stored_zip(
    src_dir: str,
    *,
    spill_threshold_bytes: int = 256 * 1024 * 1024,
    unlink: bool = True,
) -> IO[bytes]:
    """Pack every file under ``src_dir`` into a STORED (uncompressed) zip spool.

    Used after an s5cmd prefix download lands loose files on disk: grading
    verifiers consume a snapshot as a seekable zip, so we still package it, but
    STORED skips the per-grade DEFLATE CPU. Entries are named by path relative to
    ``src_dir``. With ``unlink=True`` each source file is removed right after
    it's packed, so peak disk stays ~= one copy of the data, not two. Returns a
    disk-backed SpooledTemporaryFile seeked to 0 — it streams + spills past
    ``spill_threshold_bytes``, so RAM stays bounded even for huge snapshots.
    """
    zip_spool: IO[bytes] = tempfile.SpooledTemporaryFile(
        max_size=spill_threshold_bytes, mode="w+b"
    )
    with zipfile.ZipFile(zip_spool, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for root, _dirs, files in os.walk(src_dir):
            for name in sorted(files):
                fpath = os.path.join(root, name)
                zf.write(fpath, arcname=os.path.relpath(fpath, src_dir))
                if unlink:
                    os.unlink(fpath)
    zip_spool.seek(0)
    return zip_spool


# ---------------------------------------------------------------------------
# small fs helpers
# ---------------------------------------------------------------------------


def _safe_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _dir_size_and_count(root: str) -> tuple[int, int]:
    total = 0
    count = 0
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            total += _safe_size(os.path.join(dirpath, f))
            count += 1
    return total, count

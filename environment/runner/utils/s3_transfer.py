"""Pluggable S3 download backend for the environment-runner populate path.

Lets us A/B the default boto3/aioboto3 per-object download against the ``s5cmd``
Go binary when populating a sandbox filesystem from a snapshot prefix. The
populate path is loose-file (no prebuilt archive), so this is the case where
s5cmd's many-worker ``cp`` is most likely to win over per-object byte-range GETs.

This is a sibling of the grading-runner module of the same name — kept separate
because the environment runner is its own package with a different confinement
root (the sandbox subsystem dirs, not a temp dir) and a different credential
model (short-lived creds arrive in the populate request body and must be injected
into the subprocess env, since s5cmd can't read aioboto3's in-process session).

Selection is explicit (the server-resolved backend rides in on the populate
request) rather than via a ContextVar — the environment runner threads config as
parameters, not context.

Security (Corridor): subprocess is argv-only (never shell=True); the S3 key comes
from a DB-sourced source URL, so it's allowlist-validated and pinned to the
snapshot namespace before reaching argv; the local destination is realpath-
confined to the sandbox subsystem roots; AWS creds are passed ephemerally via the
subprocess ``env`` dict, never hardcoded or logged.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import time
from typing import Literal

from loguru import logger

S3Backend = Literal["boto3", "s5cmd"]
DEFAULT_BACKEND: S3Backend = "boto3"


def resolve_backend(backend: str | None) -> S3Backend:
    """Normalize the requested backend to a known value.

    Order: explicit ``backend`` (from the populate request) → ``S3_TRANSFER_BACKEND``
    env override → default. Anything other than "s5cmd" resolves to "boto3" so a
    bad/unknown value can never break populate.
    """
    raw = backend or os.getenv("S3_TRANSFER_BACKEND")
    return "s5cmd" if raw == "s5cmd" else DEFAULT_BACKEND


def s5cmd_available() -> bool:
    """True if the ``s5cmd`` binary is on PATH in this sandbox image."""
    return shutil.which("s5cmd") is not None


# --- safety -----------------------------------------------------------------

# Snapshot keys are ``{namespace}/{snapshot_id}/{relative_path}`` — alphanumeric
# plus a small punctuation set. fullmatch (not match + "$") so a trailing newline
# can't slip past ("$" matches just before a final "\n" in Python).
_SAFE_TOKEN_RE = re.compile(r"[A-Za-z0-9._\-/]+")
# The exact set of top-level namespaces the server's
# ``build_sandbox_populate_sources`` ever emits as populate source URLs:
# ``playgrounds/``, ``worlds/``, ``tasks/`` and ``pipelines/`` (pipeline-output
# populate). Tailored to the env runner — do NOT reuse the grading sibling's set
# (``trajectories``/``golden-responses``/``snapshot_zips``), which populate never
# reads; including them would be dead allowlist surface, and omitting
# ``pipelines`` silently forces pipeline-output populate onto boto3.
_ALLOWED_NAMESPACES = frozenset(
    {
        "playgrounds",
        "worlds",
        "tasks",
        "pipelines",
    }
)
# Sandbox subsystem roots that populate is allowed to write into. Mirrors
# PopulateSource.validate_subsystem on the request model.
_ALLOWED_DEST_ROOTS = ("/filesystem", "/.apps_data")


def key_is_eligible(key: str) -> bool:
    """Non-raising allowlist check used to decide whether s5cmd may handle a key.

    Returns False (→ caller falls back to boto3) for anything not in the snapshot
    namespace, rather than raising — populate must still work for any legitimate
    non-snapshot source, just on the default backend.
    """
    norm = key.rstrip("/")
    return bool(
        norm
        and ".." not in norm
        and not norm.startswith("/")
        and _SAFE_TOKEN_RE.fullmatch(norm)
        and norm.split("/", 1)[0] in _ALLOWED_NAMESPACES
    )


def _validate_bucket(bucket: str) -> str:
    if not bucket or not re.fullmatch(r"[a-z0-9][a-z0-9.\-]{1,61}[a-z0-9]", bucket):
        raise ValueError(f"unsafe S3 bucket name: {bucket!r}")
    return bucket


def _validate_key(key: str) -> str:
    if not key_is_eligible(key):
        raise ValueError(f"unsafe or out-of-namespace S3 key/prefix: {key!r}")
    return key.rstrip("/")


def _confine_to_subsystem(path: str) -> str:
    """Realpath-confine the local destination to an allowed sandbox subsystem root."""
    real = os.path.realpath(path)
    for root in _ALLOWED_DEST_ROOTS:
        base = os.path.realpath(root)
        if real == base or real.startswith(base + os.sep):
            return real
    raise ValueError(f"destination escapes the sandbox subsystem roots: {path!r}")


# --- s5cmd backend ----------------------------------------------------------


class S5cmdDownloader:
    """Downloads an S3 prefix into a local dir by shelling out to ``s5cmd``."""

    name: S3Backend = "s5cmd"

    def __init__(
        self,
        *,
        numworkers: int = 256,
        part_concurrency: int = 16,
        binary: str = "s5cmd",
    ) -> None:
        # --numworkers parallelizes across objects (the populate win); --concurrency
        # parallelizes parts of a single large object.
        self._numworkers = numworkers
        self._part_concurrency = part_concurrency
        self._binary = binary

    async def download_prefix(
        self,
        bucket: str,
        prefix: str,
        dest_dir: str,
        *,
        extra_env: dict[str, str] | None = None,
    ) -> float:
        """Run ``s5cmd cp s3://{bucket}/{prefix}/* {dest_dir}/``. Returns elapsed seconds.

        Reproduces boto3's layout: each object under ``prefix`` lands at
        ``dest_dir/<key-relative-to-prefix>``. ``extra_env`` (AWS creds) is merged
        over the process environment for the subprocess; pass None to inherit the
        default credential chain. Raises on a non-zero exit so the caller can fall
        back to boto3.
        """
        _validate_bucket(bucket)
        norm = _validate_key(prefix)
        dest = _confine_to_subsystem(dest_dir)
        env = {**os.environ, **extra_env} if extra_env else None

        argv = [
            self._binary,
            "--numworkers",
            str(self._numworkers),
            "cp",
            "--concurrency",
            str(self._part_concurrency),
            f"s3://{bucket}/{norm}/*",
            dest + os.sep,
        ]
        logger.debug(f"s5cmd populate: {' '.join(argv)}")
        t0 = time.perf_counter()
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
        return time.perf_counter() - t0


def get_s5cmd_downloader(backend: str | None) -> S5cmdDownloader | None:
    """Return an s5cmd backend iff it's selected and the binary is available.

    None means "use boto3" — so an unset/other backend, or a missing binary,
    transparently falls back to the default path.
    """
    if resolve_backend(backend) != "s5cmd":
        return None
    if not s5cmd_available():
        logger.warning(
            "s3_transfer: backend=s5cmd requested but binary not on PATH; using boto3"
        )
        return None
    return S5cmdDownloader()

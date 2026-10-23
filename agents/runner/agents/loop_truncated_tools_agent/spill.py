"""Spill an over-budget tool result into the sandbox filesystem.

A tool result that exceeds the step's tool-result budget is refused today: the
model is told to narrow its query and none of the payload reaches it. That is
the right answer for a *narrowable* call, but not for an atomic fetch — id in,
whole object out, no range parameter — because there is no tighter query to
make. The observed case is Google Workspace's ``drive_file_download``, which
returns file bytes base64-encoded; base64 measures ~2.1 tokens/char, so a
100k-char export bills ~210k tokens against a 150k budget and is discarded
whole, leaving the task unanswerable (BELLA-675 / BELLA-634).

Spilling writes those bytes to the environment's ``/filesystem/`` instead and
hands the model a path. That subsystem is shared with the Code Execution tool —
verified on a live run, where ``code_exec`` wrote ``build_wb.py`` and its xlsx
output and both appeared under ``filesystem/`` in the trajectory snapshot — so
the model can compute over the full payload at zero context cost.

The upload itself is ``upload_archive_to_filesystem`` in ``runner/utils``,
rather than a second populate client written here: it signs the exact
multipart bytes on the wire (an empty-body signature is rejected by a
protected sandbox), carries the bearer token, and absorbs transient transport
faults.

Everything here is best-effort: any failure returns ``None`` and the caller
falls back to the existing refusal, so a spill that cannot land degrades to
today's behaviour rather than erroring the run.

Spills are awaited one at a time inside the step's result loop, so several
over-budget results in one parallel batch pay their uploads (and any retries)
in sequence. Left serial deliberately: a spill only happens on a result that
was about to be thrown away, it is rare by construction, and gathering them
would interleave uploads with the retry backoff of a sandbox that is already
struggling. Worth revisiting if a world starts spilling several per step.
"""

import io
import re
import tarfile
from urllib.parse import urlparse
from uuid import uuid4

from loguru import logger

from runner.utils.sandbox_files import upload_archive_to_filesystem

# The runner builds the gateway URL as f"{sandbox_url}/mcp/" (agents/
# modal_labs.py and runner/k8s_worker.py). AgentRunInput carries no sandbox
# URL of its own, so the origin is recovered by stripping that suffix rather
# than by widening a model shared with every other agent.
_GATEWAY_SUFFIX = "/mcp/"

# Only the two schemes the sandbox is ever served over. The gateway URL is
# server-constructed, but a derived request target still gets validated before
# it receives signed headers and a bearer token: an origin that slipped through
# from elsewhere would otherwise be an SSRF that forwards tool-result bytes
# with valid credentials attached.
_ALLOWED_SCHEMES = frozenset({"http", "https"})

# Spill names are built from the tool name, so they are ours rather than the
# model's — but they still get reduced to this alphabet before being used as an
# archive member, so nothing can walk out of the subsystem it extracts into.
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]+")

_SPILL_TIMEOUT_SECONDS = 120.0


def sandbox_origin_from_gateway_url(gateway_url: str | None) -> str | None:
    """Recover the sandbox origin from the MCP gateway URL, or None.

    Returns None when the URL is absent, is not http(s), has no host, or does
    not carry the expected gateway suffix — in every case the caller keeps the
    refusal path rather than posting somewhere unexpected.
    """
    if not gateway_url:
        return None
    trimmed = gateway_url.rstrip("/")
    suffix = _GATEWAY_SUFFIX.rstrip("/")
    if not trimmed.endswith(suffix):
        return None
    origin = trimmed[: -len(suffix)].rstrip("/")
    parsed = urlparse(origin)
    if parsed.scheme not in _ALLOWED_SCHEMES or not parsed.netloc:
        return None
    # Reject anything carrying a path, query or fragment: the origin is only
    # ever scheme://host[:port], and a residual path would let the populate
    # route be addressed somewhere other than the sandbox root.
    if parsed.path or parsed.query or parsed.fragment:
        return None
    return origin


def safe_spill_name(tool_name: str, index: int, extension: str = "txt") -> str:
    """A filesystem-safe, collision-free name for one spilled result.

    The counter alone is not unique. A harness agent re-instantiates its inner
    agent per turn, which resets the counter, and /data/populate overwrites by
    path — so turn 2's first spill would silently replace turn 1's while the
    earlier path is still cited in the conversation. The random suffix makes
    the name unique for the sandbox's lifetime; the counter and tool name stay
    only because they make the file recognisable in a trajectory.
    """
    stem = _UNSAFE_NAME_CHARS.sub("_", tool_name).strip("._-") or "tool_result"
    ext = _UNSAFE_NAME_CHARS.sub("", extension).strip(".") or "txt"
    return f"spill_{index:03d}_{stem[:60]}_{uuid4().hex[:8]}.{ext}"


def _tar_gz_single_file(name: str, payload: bytes) -> bytes:
    """Build a one-member tar.gz. ``name`` must already be sanitised."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name=name)
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


async def spill_to_filesystem(
    *,
    gateway_url: str | None,
    auth_token: str | None,
    tool_name: str,
    index: int,
    payload: str,
    extension: str = "txt",
) -> str | None:
    """Write ``payload`` to the sandbox's /filesystem/ and return its path.

    ``payload`` must be the ORIGINAL tool result, not the character-truncated
    copy prepared for inline delivery: the notice this enables tells the model
    the file is complete, and a truncated base64 body does not even decode.

    Returns None on any failure so the caller can fall back to the refusal.
    """
    origin = sandbox_origin_from_gateway_url(gateway_url)
    if origin is None:
        logger.warning(
            "Cannot spill oversized tool result: no usable sandbox origin from "
            f"gateway URL {gateway_url!r}"
        )
        return None

    name = safe_spill_name(tool_name, index, extension)
    raw = payload.encode("utf-8", errors="replace")
    try:
        archive = _tar_gz_single_file(name, raw)
    except Exception as exc:
        logger.warning(f"Cannot spill oversized tool result: tar failed ({exc!r})")
        return None

    try:
        added = await upload_archive_to_filesystem(
            archive,
            sandbox_url=origin,
            auth_token=auth_token,
            timeout=_SPILL_TIMEOUT_SECONDS,
        )
        if added < 1:
            # A 2xx that extracted nothing still leaves no file at the path we
            # are about to promise. Treated as a failure so the caller refuses
            # rather than pointing the model at something that is not there.
            logger.warning(
                f"Spill upload to {origin} reported {added} file(s) added — "
                "falling back to the refusal"
            )
            return None
    except Exception as exc:
        # Includes SandboxUploadError (sandbox unreachable or rejecting).
        # Deliberately broad: a spill is an optimisation over the refusal, so
        # nothing it can do should be able to fail the step.
        logger.warning(
            f"Cannot spill oversized tool result to {origin}: {exc!r} — "
            "falling back to the refusal"
        )
        return None

    path = f"/filesystem/{name}"
    logger.bind(message_type="configure").info(
        f"Spilled oversized {tool_name} result ({len(raw)} bytes) → {path}"
    )
    return path

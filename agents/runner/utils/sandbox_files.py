"""Upload a tar.gz into a running sandbox's filesystem via /data/populate.

Extracted from ``scripted_turns_agent.file_staging`` so agents outside that
package can reuse it. The OSS-published agent set (``INCLUDED_AGENT_FOLDERS``)
does not include ``scripted_turns_agent``, so importing it from
``loop_truncated_tools_agent`` pulled an unpublished agent into the OSS import
closure — this is the shared home that avoids that, rather than a second copy
of a signing flow it would be easy to get subtly wrong.

The contract worth preserving, and the reason not to hand-roll this: the
signature must cover the EXACT multipart bytes on the wire. httpx only
materialises them at ``request.read()``, so the request is built, read, and
only then signed. Signing an empty body is accepted by an unprotected sandbox
and rejected by a protected one.
"""

import asyncio

import httpx
from loguru import logger

from runner.utils.signing import sign_env_runner_request

# The env-runner subsystem that /data/populate extracts into, and the mount
# Code Execution sees. Defined here rather than imported from
# scripted_turns_agent.constants: that package is outside the OSS-published
# agent set, and importing it here would put it back in the import closure.
#
# Deliberately NOT a parameter. /data/populate will just as happily extract
# into `.apps_data`, which holds the seeded app state the apps read and the
# graders diff — writing an agent-supplied archive there mid-run could corrupt
# a world's data or a score, and no caller has ever wanted it. Pinning the
# constant means a future caller has to come here and argue for it rather than
# reach for a keyword argument.
FILESYSTEM_SUBSYSTEM = "filesystem"

# Transient-fault absorption. Deliberately small: the point is to ride out a
# blip, not to wait on a dead sandbox while the run's wall-clock budget drains.
_UPLOAD_MAX_ATTEMPTS = 3
_UPLOAD_RETRY_BASE_DELAY = 1.0


class SandboxUploadError(RuntimeError):
    """An archive could not be staged into the sandbox filesystem."""


async def upload_archive_to_filesystem(
    archive: bytes,
    *,
    sandbox_url: str,
    auth_token: str | None,
    timeout: float,
    filename: str = "turn_files.tar.gz",
) -> int:
    """Upload one turn's archive into the sandbox filesystem; return files added.

    RETRIES, BECAUSE THE ALTERNATIVE IS PAID IN LLM TOKENS. This runs at a turn
    BOUNDARY, i.e. after every earlier turn's model calls have already been paid
    for. A transport error escaping here used to surface as an unhandled
    exception — a RETRYABLE trajectory error — so a sub-second sandbox blip threw
    away a completed turn 1 and bought the whole thing again. Under a batch, once
    per member. So transient faults are absorbed in place (cheap: one HTTP call)
    and only a persistently unreachable sandbox becomes a controlled turn failure,
    which is terminal precisely so the tokens are not spent twice.

    Retrying is safe: `/data/populate` extracts to fixed paths, so a repeat writes
    the same bytes to the same places. Same posture as the rest of the repo —
    callers own their retry loops (see CLAUDE.md on LLM retry ownership).

    Never logs the bearer token or the archive body — only counts and byte totals,
    which is what a run being debugged actually needs.
    """
    url = f"{sandbox_url}/data/populate?subsystem={FILESYSTEM_SUBSYSTEM}"
    headers: dict[str, str] = {}
    token = (auth_token or "").strip()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    last_error: Exception | None = None
    for attempt in range(_UPLOAD_MAX_ATTEMPTS):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                request = client.build_request(
                    "POST",
                    url,
                    headers=headers,
                    files={"archive": (filename, archive, "application/gzip")},
                )
                # Read the multipart body BEFORE signing: the signature covers the
                # exact bytes on the wire, and httpx only materializes them here.
                # `read()` also swaps the stream for a replayable one, so `send`
                # still works. Rebuilt per attempt so each carries a fresh
                # timestamp/nonce — a replayed signature can be rejected as stale.
                body = request.read()
                request.headers.update(sign_env_runner_request("POST", url, body=body))
                response = await client.send(request)
        except httpx.HTTPError as exc:
            # Transport-level only (connect/read/write/pool). Contained here
            # rather than allowed to propagate: see the docstring.
            last_error = exc
            if attempt + 1 < _UPLOAD_MAX_ATTEMPTS:
                delay = _UPLOAD_RETRY_BASE_DELAY * (2**attempt)
                logger.warning(
                    f"Sandbox upload attempt {attempt + 1} failed "
                    f"({type(exc).__name__}); retrying in {delay:.1f}s"
                )
                await asyncio.sleep(delay)
                continue
            raise SandboxUploadError(
                f"sandbox unreachable for the sandbox upload after "
                f"{_UPLOAD_MAX_ATTEMPTS} attempts: {type(exc).__name__}: {exc}"
            ) from exc

        if response.status_code >= 400:
            # A 4xx/5xx is the sandbox answering, so the request shape or the
            # sandbox state is wrong. Retrying an identical request would not
            # change either, and would cost a second upload of the same bytes.
            raise SandboxUploadError(
                f"sandbox rejected the sandbox upload: HTTP "
                f"{response.status_code} {response.text[:500]}"
            )
        try:
            added = int(response.json().get("objects_added", 0))
        except (ValueError, AttributeError):
            added = 0
        logger.bind(message_type="sandbox_files_staged").info(
            f"Staged {added} file(s), {len(archive)} archive bytes, into "
            f"{FILESYSTEM_SUBSYSTEM} (attempt {attempt + 1})"
        )
        return added

    # Unreachable: the loop either returns or raises.
    raise SandboxUploadError(f"sandbox upload did not complete: {last_error}")

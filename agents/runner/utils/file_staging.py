"""Moving a turn's attached files out of the LLM context and onto the sandbox.

WHY THIS EXISTS. A task's prompt messages can carry attachments, and the server
materializes them into the agent's `initial_messages` as litellm file blocks
holding base64 bytes (`models/msg_representations.py`). Once such a block is in
the conversation it is re-sent on EVERY subsequent LLM call for the rest of the
run, so a single 3 MB brief costs ~4 MB of base64 per call across a 100-turn
agentic loop. For a document-heavy benchmark that is the dominant input cost and
it buys nothing: an agent with shell/file tools would rather read the file.

So the harness decodes the blocks once, uploads them into the already-running
sandbox, and rewrites the message to name the paths. The bytes cross the wire
once (server → runner, which already happened) and reach zero LLM calls.

THE UPLOAD IS THE ENV RUNNER'S OWN INGEST PATH, `POST /data/populate`
(`environment/runner/data/router.py`), the same endpoint the worker uses to seed
the world before the agent starts. It extracts a tar.gz into a subsystem and
MERGES: paths not in the archive are left alone. That merge is what makes a
later turn add files rather than replace the earlier turn's, which is the
behaviour the two-turn task design depends on.

WHEN these functions run is a correctness property for `scripted_turns_agent`,
which invokes them once per turn so a later turn's files reach the sandbox only
after the earlier turn has finished (mechanism 2 of the isolation guarantee in
its `main.py`). `stage_message_files` stages a whole history at once, so it is
only for callers whose every message is already visible to the model, such as
`loop_agent`.

TWO THINGS THE CALLER CANNOT CHOOSE, both least-privilege:

  * The subsystem is pinned to `filesystem`. `.apps_data` holds live service
    state that apps read at boot, and annotator-authored documents landing there
    would corrupt an app rather than inform a model.
  * Every archive member is built here, as an explicit REGTYPE `TarInfo`. Nothing
    is copied from a caller-supplied tar, so a symlink or hardlink member — the
    usual way an archive escapes its extraction root — cannot be expressed.

FILENAMES ARE UNTRUSTED. They come from whatever an annotator uploaded, and they
are used to build the archive member path, so they are reduced to a basename and
re-parented under the turn's own directory. Absolute paths, `..`, backslashes and
the `.`/`..` degenerate names are rejected rather than sanitized into something
that silently writes elsewhere.

SIGNING IS NOT OPTIONAL WHEN CONFIGURED. The runner enforces an Ed25519
signature over `METHOD\\npath?query\\ntimestamp\\nnonce\\nsha256(body)` on every
non-exempt route, and `/data/populate` is not exempt (`environment/runner/main.py`
`_SIGNING_EXEMPT_PREFIXES` covers only `/mcp` and `/rest`). The signature covers
the RAW body, which for a multipart upload is the encoded multipart payload — so
the request is built, read to bytes, and only then signed. Getting that order
wrong yields a 403 that looks like an auth misconfiguration.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import io
import tarfile
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from runner.agents.models import get_msg_content, get_msg_role
from runner.utils.sandbox_files import (
    SandboxUploadError,
    upload_archive_to_filesystem,
)

# Twin of scripted_turns_agent.constants.FILESYSTEM_ROOT; that package is outside
# the OSS-published agent set, so it cannot be imported here.
FILESYSTEM_ROOT = "/filesystem"

_BYTES_PER_MB = 1024 * 1024

# Fixed member metadata. Real uid/gid/mtime would make an otherwise identical
# archive differ run to run, and the extracted files are read by whatever user
# the apps run as, so 0644/root is both reproducible and sufficient.
_MEMBER_MODE = 0o644
_MEMBER_MTIME = 0

# Transient-fault absorption at the turn boundary. Deliberately small: the
# point is to ride out a blip, not to wait on a dead sandbox while the run's
# wall-clock budget drains.
_UPLOAD_MAX_ATTEMPTS = 3
_UPLOAD_RETRY_BASE_DELAY = 1.0


class TurnFileStagingError(RuntimeError):
    """A turn's files could not be staged. Fails the turn rather than degrading.

    Staging silently is the one behaviour to avoid: a task whose second turn
    depends on documents that never arrived produces a confidently wrong
    trajectory, and it grades as a model failure.
    """


@dataclass(frozen=True)
class DecodedTurnFile:
    """One attachment lifted out of a turn's message content."""

    filename: str
    data: bytes


def _basename_or_raise(raw_name: str, *, index: int) -> str:
    """Reduce an untrusted attachment name to a safe basename.

    Rejects rather than repairs. A name that needed repairing is a name whose
    author's intent we are guessing at, and the failure mode of guessing wrong
    is a file written somewhere the task did not mean.
    """
    name = (raw_name or "").strip()
    if not name:
        return f"attachment_{index}"
    if "\\" in name:
        raise TurnFileStagingError(
            f"attachment name {name!r} contains a backslash; use '/' or a bare filename"
        )
    if name.startswith("/"):
        raise TurnFileStagingError(f"attachment name {name!r} must not be absolute")
    if ".." in PurePosixPath(name).parts:
        raise TurnFileStagingError(f"attachment name {name!r} must not contain '..'")
    basename = PurePosixPath(name).name
    if basename in ("", ".", ".."):
        raise TurnFileStagingError(f"attachment name {name!r} has no usable filename")
    return basename


def validate_turn_files_dir(raw_dir: str) -> str:
    """Validate the admin-authored destination directory, returning it normalized.

    Admin-authored rather than annotator-authored, but still validated: an agent
    config is cloned between worlds and edited by hand, and the blast radius of a
    `../.apps_data` here is a corrupted app image rather than a bad task.
    """
    value = (raw_dir or "").strip().strip("/")
    if not value:
        raise TurnFileStagingError("turn_files_dir must not be empty")
    if "\\" in value:
        raise TurnFileStagingError(
            f"turn_files_dir {raw_dir!r} contains a backslash; use '/' for nesting"
        )
    # Only '..' is checked: PurePosixPath already drops '.' segments, so
    # './inputs' normalizes to 'inputs' and there is nothing left to reject.
    parts = PurePosixPath(value).parts
    if ".." in parts:
        raise TurnFileStagingError(
            f"turn_files_dir {raw_dir!r} must not contain '..' segments"
        )
    return "/".join(parts)


def _decode_data_uri(raw: Any, *, filename: str) -> bytes:
    """Decode a `data:<mime>;base64,<payload>` value, or a bare base64 string.

    Defensive because the value has been through a server-side conversion and a
    JSON round trip: a missing comma or a truncated payload must surface as this
    turn's staging error, not as an unhandled exception that kills the run.
    """
    if isinstance(raw, bytes):
        return raw
    if not isinstance(raw, str) or not raw:
        raise TurnFileStagingError(f"attachment {filename!r} carries no file data")

    payload = raw
    if raw.startswith("data:"):
        _, _, after = raw.partition(",")
        if not after:
            raise TurnFileStagingError(
                f"attachment {filename!r} has a malformed data URI (no payload)"
            )
        payload = after
    try:
        return base64.b64decode(payload, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise TurnFileStagingError(
            f"attachment {filename!r} is not valid base64: {exc}"
        ) from exc


def split_file_blocks(
    content: Any,
) -> tuple[Any, list[DecodedTurnFile]]:
    """Split a message's content into (content without file blocks, decoded files).

    Only `type: "file"` blocks are lifted. Image / audio / video blocks stay in
    context on purpose: a model is asked to LOOK at those, and staging them to
    disk would remove the very thing the turn is testing. A document is asked to
    be READ, which a file tool does better and cheaper.
    """
    if not isinstance(content, list):
        return content, []

    kept: list[Any] = []
    files: list[DecodedTurnFile] = []
    for index, block in enumerate(content):
        if not isinstance(block, dict) or block.get("type") != "file":
            kept.append(block)
            continue
        spec = block.get("file")
        if not isinstance(spec, dict):
            raise TurnFileStagingError(
                "a file block on this turn has no 'file' payload to stage"
            )
        filename = _basename_or_raise(str(spec.get("filename") or ""), index=index)
        files.append(
            DecodedTurnFile(
                filename=filename,
                data=_decode_data_uri(spec.get("file_data"), filename=filename),
            )
        )
    return kept, files


def build_turn_archive(
    files: list[DecodedTurnFile],
    *,
    dest_dir: str,
    turn_number: int,
    max_file_mb: int,
    max_total_mb: int,
) -> tuple[bytes, list[str]]:
    """Build the tar.gz for one turn, returning (archive bytes, absolute paths).

    Size caps are enforced here rather than at upload so an oversized attachment
    is reported with its own name. They fail the turn: truncating or dropping a
    document the rubric grades against would score the model on inputs it never
    received.
    """
    if not files:
        return b"", []

    turn_dir = f"{dest_dir}/turn_{turn_number}"
    max_file_bytes = max_file_mb * _BYTES_PER_MB
    max_total_bytes = max_total_mb * _BYTES_PER_MB

    seen: set[str] = set()
    total = 0
    for staged in files:
        if staged.filename in seen:
            # Two same-named attachments on one turn would collapse into one
            # extracted file, so the model would silently receive fewer
            # documents than the author attached.
            raise TurnFileStagingError(
                f"turn {turn_number} attaches more than one file named "
                f"{staged.filename!r}; give them distinct names"
            )
        seen.add(staged.filename)
        size = len(staged.data)
        if size > max_file_bytes:
            raise TurnFileStagingError(
                f"attachment {staged.filename!r} is {size / _BYTES_PER_MB:.1f} MB, "
                f"over the {max_file_mb} MB per-file limit"
            )
        total += size
    if total > max_total_bytes:
        raise TurnFileStagingError(
            f"turn {turn_number} attaches {total / _BYTES_PER_MB:.1f} MB, over the "
            f"{max_total_mb} MB per-turn limit"
        )

    buffer = io.BytesIO()
    paths: list[str] = []
    # mtime=0 on the gzip member too — the default stamps wall-clock time into
    # the header, which is the one byte that would differ between two otherwise
    # identical archives.
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        for staged in files:
            member = tarfile.TarInfo(name=f"{turn_dir}/{staged.filename}")
            member.type = tarfile.REGTYPE
            member.size = len(staged.data)
            member.mode = _MEMBER_MODE
            member.mtime = _MEMBER_MTIME
            member.uid = 0
            member.gid = 0
            member.uname = ""
            member.gname = ""
            tar.addfile(member, io.BytesIO(staged.data))
            paths.append(f"{FILESYSTEM_ROOT}/{turn_dir}/{staged.filename}")
    return buffer.getvalue(), paths


def sandbox_url_from_gateway(mcp_gateway_url: str | None) -> str:
    """Derive the sandbox root from the MCP gateway URL.

    The gateway URL is built as ``f"{sandbox_url}/mcp/"`` in every launch path
    (`modal_labs.py`, `runner/k8s_worker.py`), and the sandbox root is not passed
    to agents separately — so trimming that suffix is how an agent addresses the
    runner's own API without widening `AgentRunInput`.
    """
    url = (mcp_gateway_url or "").strip()
    if not url:
        raise TurnFileStagingError(
            "no MCP gateway URL on this run, so the sandbox API cannot be "
            "addressed to stage turn files; set stage_files_to_env=false to "
            "carry them in context instead"
        )
    trimmed = url.rstrip("/")
    if trimmed.endswith("/mcp"):
        trimmed = trimmed[: -len("/mcp")]
    return trimmed


async def upload_turn_archive(
    archive: bytes,
    *,
    sandbox_url: str,
    auth_token: str | None,
    timeout: float,
) -> int:
    """Upload one turn's archive into the sandbox filesystem; return files added.

    Thin wrapper over the shared ``upload_archive_to_filesystem``. The body
    moved to ``runner/utils`` so the OSS-published agents can reuse it without
    importing this package; the translation back to ``TurnFileStagingError``
    keeps this agent's failure handling (`main.py`'s `except`) unchanged.
    """
    try:
        return await upload_archive_to_filesystem(
            archive,
            sandbox_url=sandbox_url,
            auth_token=auth_token,
            timeout=timeout,
            filename="turn_files.tar.gz",
        )
    except SandboxUploadError as exc:
        raise TurnFileStagingError(str(exc)) from exc


STAGED_FILES_NOTICE = (
    "The following files are attached to this message and are in your working "
    "environment. Read them from disk with your tools; they are not included inline.\n"
)


async def stage_message_files(
    messages: list[Any],
    *,
    mcp_gateway_url: str | None,
    auth_token: str | None,
    dest_dir: str = "turn_inputs",
    max_file_mb: int = 25,
    max_total_mb: int = 100,
    timeout: float = 300,
) -> list[Any]:
    """Upload each user message's file blocks to the sandbox; return messages naming the paths.

    The n-th user message stages into ``turn_<n>``, so a chat that re-sends its
    history on every run writes the same files to the same paths each time.
    """
    staged: list[Any] = []
    turn_number = 0
    for msg in messages:
        if get_msg_role(msg) != "user":
            staged.append(msg)
            continue
        turn_number += 1
        kept_content, files = await asyncio.to_thread(
            split_file_blocks, get_msg_content(msg)
        )
        if not files:
            staged.append(msg)
            continue
        archive, paths = await asyncio.to_thread(
            build_turn_archive,
            files,
            dest_dir=dest_dir,
            turn_number=turn_number,
            max_file_mb=max_file_mb,
            max_total_mb=max_total_mb,
        )
        await upload_turn_archive(
            archive,
            sandbox_url=sandbox_url_from_gateway(mcp_gateway_url),
            auth_token=auth_token,
            timeout=timeout,
        )
        notice = {"type": "text", "text": STAGED_FILES_NOTICE + "\n".join(paths)}
        staged.append({**dict(msg), "content": [*kept_content, notice]})
    return staged

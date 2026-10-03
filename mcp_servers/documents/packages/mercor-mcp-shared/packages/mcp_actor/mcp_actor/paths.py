# pyright: reportMissingImports=false, reportUnknownVariableType=false, reportUnknownMemberType=false, reportUnknownParameterType=false, reportUntypedBaseClass=false
"""
MCP tool calls are made by three kinds of actors:
- Target Agent (TA)
- Virtual Coworker Agents (VCAs)
- Environment Coordinator

To distinguish between the three, we add "actor_id" FastMCP metadata.

The TA is "actor_id: target_agent", VCAs "actor_id: <vca_id>", and
the Coordinator with "actor_id: coordinator".

These actors all see the same virtual public filesystem rooted at "/".
Physically, that maps to:
- TA and Coordinator: "/filesystem"
- VCAs: "/.apps_data/.coordinator/agent_filesystems/<vca_id>"

MCP tools should therefore resolve incoming virtual paths against the current
actor root, and redact physical roots from outputs before returning results to
agents.

What output redaction protects, per actor:
- VCAs: their root under the coordinator's private state directory is hidden,
  so it is rewritten to "/".
- TA and Coordinator: "/filesystem" is the shared public mount (the
  code-execution app's working directory, reported verbatim when
  EXPOSE_PHYSICAL_PATHS is on), so it is NOT rewritten. Tools already emit
  virtual paths, and a seeded tree can hold a top-level "filesystem/" folder:
  rewriting turned that folder's valid path "/filesystem/x" into "/x", and
  echoed requests like "/filesystem/filesystem/x" into "/filesystem/x" (ENV-494).
- Out-of-root physical paths are replaced with OUTSIDE_ACTOR_ROOT by
  virtual_path_from_physical, where their provenance is known. Arbitrary output
  strings may already contain virtual paths, including valid "/.apps_data/..."
  paths, so they cannot safely be scrubbed by that prefix alone.

Paths outside the actor root (e.g. "/app", "/opt") are kept out by
containment, not by rewriting: resolve_virtual_path rejects anything that
resolves outside the root, and virtual_path_from_physical reports it as
OUTSIDE_ACTOR_ROOT. They are not scrubbed lexically because "/app/files" is a
documented alias of the root that agents send and that errors echo back.
"""

import os
import re
from collections.abc import Mapping
from contextvars import ContextVar
from os import PathLike
from pathlib import Path
from typing import Any, Literal

from fastmcp.server.dependencies import get_http_request  # type: ignore[reportMissingImports]
from fastmcp.server.middleware import (  # type: ignore[reportMissingImports]
    CallNext,
    Middleware,
    MiddlewareContext,
)
from fastmcp.tools.tool import ToolResult  # type: ignore[reportMissingImports]

# These constants intentionally duplicate Archipelago Environment Coordinator
# invariants. Keep them in sync with:
# - archipelago/environment/runner/coordinator/agents/models.py
# - archipelago/environment/runner/coordinator/state/store.py
TARGET_AGENT_ACTOR_ID = "target_agent"
COORDINATOR_ACTOR_ID = "coordinator"
AUTHORIZATION_HEADER = "authorization"
BEARER_PREFIX = "bearer "

TARGET_AGENT_FILESYSTEM_ROOT = "/filesystem"
PRIVATE_APPS_DATA_ROOT = "/.apps_data"
COORDINATOR_ROOT = f"{PRIVATE_APPS_DATA_ROOT}/.coordinator"
ACTOR_FILESYSTEMS_ROOT = f"{COORDINATOR_ROOT}/agent_filesystems"
OUTSIDE_ACTOR_ROOT = "[outside actor root]"

_ACTOR_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_ROOT_PREFIX_RE = r"(?<![^\s'\"(<\[{,:;=])"
_ROOT_TERMINATOR_CHARS = r"\s'\"),:;\]}!?>"
_ROOT_SUFFIX_RE = (
    r"(?:/|(?=$)|(?=["
    + _ROOT_TERMINATOR_CHARS
    + r"])|(?=\.(?:$|["
    + _ROOT_TERMINATOR_CHARS
    + r"])))"
)
_current_actor_id: ContextVar[str | None] = ContextVar("mcp_actor_current_actor_id", default=None)


class ActorPathError(ValueError):
    pass


class ActorIdError(ValueError):
    pass


def extract_bearer_actor_id(headers: Mapping[str, str] | None) -> str | None:
    if not headers:
        return None
    raw = None
    for name, value in headers.items():
        if name.lower() == AUTHORIZATION_HEADER:
            raw = value
            break
    if not raw or not raw.lower().startswith(BEARER_PREFIX):
        return None
    actor_id = raw[len(BEARER_PREFIX) :].strip()
    return actor_id or None


def _request_headers() -> Mapping[str, str] | None:
    try:
        request = get_http_request()
    except RuntimeError:
        request = None
    if request is not None:
        return request.headers
    return None


def set_current_actor_id(actor_id: str | None) -> None:
    if actor_id is not None:
        actor_id = validate_actor_id(actor_id)
    _ = _current_actor_id.set(actor_id)


def get_current_actor_id(default: str = TARGET_AGENT_ACTOR_ID) -> str:
    actor_id: str | None = _current_actor_id.get()
    if actor_id:
        return actor_id
    actor_id = extract_bearer_actor_id(_request_headers())
    return actor_id or default


def validate_actor_id(actor_id: str) -> str:
    if actor_id in {TARGET_AGENT_ACTOR_ID, COORDINATOR_ACTOR_ID}:
        return actor_id
    if not _ACTOR_ID_RE.fullmatch(actor_id):
        raise ActorIdError("Invalid actor_id for filesystem tenancy")
    return actor_id


def actor_filesystem_root(actor_id: str) -> str:
    """Map the coordinator actor IDs to the physical public filesystem roots.

    The TA and coordinator share the visible `/filesystem` root. VCAs get the
    coordinator-managed per-actor filesystem under `agent_filesystems/<actor_id>`.
    """
    actor_id = validate_actor_id(actor_id)
    if actor_id in {TARGET_AGENT_ACTOR_ID, COORDINATOR_ACTOR_ID}:
        return TARGET_AGENT_FILESYSTEM_ROOT
    return str(Path(ACTOR_FILESYSTEMS_ROOT) / actor_id)


def active_filesystem_root() -> str:
    return actor_filesystem_root(get_current_actor_id())


def _dealias_root_prefix(raw_path: str, root_path: Path) -> str:
    """Rewrite an absolute path that ALIASES the root into its virtual form.

    The prefix loop below strips the root, but only recognizes two spellings of
    it: the root string and its ``.resolve()``. Delivered worlds expose the same
    directory under a THIRD name -- ``/app/files`` is a symlink to
    ``/filesystem`` (the ``APP_FS_ROOT``) -- and the task prompt names that
    alias, so an agent following the prompt sends ``/app/files/report.docx``.
    That shares no textual prefix with the root, so it fell through to
    ``lstrip("/")`` and was re-rooted to ``<root>/app/files/report.docx``: reads
    404, and writes silently created that directory and landed there, leaving
    the deliverable absent at grading with no error surfaced.

    Matching on ``realpath`` instead of on the string catches every alias
    (symlink, ``..`` chain, trailing slash) without enumerating them.

    Containment is unchanged. This only rewrites the INPUT, and only when the
    resolved path lands inside the resolved root; the caller still joins under
    the root and still enforces ``relative_to(resolved_root)`` below. A path
    resolving anywhere else is returned untouched and rejected exactly as
    before. It deliberately does NOT fall back to the caller's path when the
    rooted one is missing -- that would turn the sandbox into a passthrough.
    """
    # Today's interpretation wins whenever it resolves to something real, so a
    # tree that genuinely contains an ``app/files/`` subdirectory is unaffected
    # and the common case costs one stat and no realpath.
    naive = Path(os.path.normpath(root_path / raw_path.lstrip("/")))
    if naive.exists():
        return raw_path

    resolved_root = os.path.realpath(root_path)
    # realpath() resolves the existing prefix of a not-yet-created path, so a
    # write target under an aliased directory de-aliases too.
    real = os.path.realpath(raw_path)
    if real == resolved_root:
        return ""
    if real.startswith(resolved_root + os.sep):
        return real[len(resolved_root) + 1 :]
    return raw_path


def _requested_and_resolved(path: str | None, full_path: Path, root_path: Path) -> str:
    """Name the requested path and, when it differs, where it resolved to.

    The resolved form is the sandbox (virtual) path, so a VCA never sees its
    physical root. Only call this for paths already checked to be inside the
    root; an outside-root resolution must never be echoed.
    """
    requested = path or "/"
    try:
        relative = full_path.relative_to(root_path)
    except ValueError:
        return requested
    resolved = "/" if str(relative) == "." else "/" + relative.as_posix()
    if "/" + requested.strip("/") == resolved:
        return requested
    return f"{requested} (resolved to sandbox path {resolved})"


def resolve_virtual_path(
    path: str | None,
    *,
    root: str | PathLike[str] | None = None,
    check_exists: bool = False,
    must_be_file: bool = False,
    must_be_dir: bool = False,
    path_mode: Literal["physical", "virtual"] = "physical",
) -> str:
    """Resolve an agent-facing path into the active actor's physical root.

    Tool callers should see `/foo.docx` regardless of actor. This accepts those
    virtual paths, plus already-rooted physical paths, and rejects traversal out
    of the selected actor filesystem.

    The default "physical" mode preserves compatibility: absolute root prefixes
    and aliases name the mount; other paths are relative to the sandbox root.
    Use "virtual" for paths emitted by virtual_path_from_physical: every leading
    slash names the sandbox root, even when the first component is "filesystem".
    Choose the mode from the caller's path contract, including for writes and
    deletes, never from whether a candidate exists.
    """
    if path_mode not in {"physical", "virtual"}:
        raise ValueError(f"Invalid path mode: {path_mode!r}")
    root_path = Path(root or active_filesystem_root()).absolute()
    raw_path = "" if not path or path == "/" else path
    if path_mode == "virtual":
        raw_path = raw_path.lstrip("/")
    if os.path.isabs(raw_path):
        raw_path = _dealias_root_prefix(raw_path, root_path)
    if os.path.isabs(raw_path):
        for prefix in (str(root_path), str(root_path.resolve())):
            relative = os.path.relpath(raw_path, prefix)
            if relative == ".":
                raw_path = ""
                break
            if not relative.startswith(".." + os.sep) and relative != "..":
                raw_path = relative
                break
        else:
            raw_path = raw_path.lstrip("/")
    virtual_path = raw_path
    full_path = Path(os.path.normpath(root_path / virtual_path))
    resolved_root = root_path.resolve()
    resolved_path = full_path.resolve()

    try:
        _ = resolved_path.relative_to(resolved_root)
    except ValueError:
        raise ActorPathError(f"Path resolves outside actor filesystem root: {path!r}") from None

    if check_exists and not full_path.exists():
        raise FileNotFoundError(
            f"Path does not exist: {_requested_and_resolved(path, full_path, root_path)}"
        )
    if must_be_file and not full_path.is_file():
        raise ValueError(
            f"Path is not a file: {_requested_and_resolved(path, full_path, root_path)}"
        )
    if must_be_dir and not full_path.is_dir():
        raise ValueError(
            f"Path is not a directory: {_requested_and_resolved(path, full_path, root_path)}"
        )
    return str(full_path)


def is_path_within_active_root(
    path: str | PathLike[str],
    *,
    root: str | PathLike[str] | None = None,
) -> bool:
    root_path = Path(root or active_filesystem_root()).absolute()
    resolved_root = root_path.resolve()
    resolved_path = Path(path).resolve()
    try:
        _ = resolved_path.relative_to(resolved_root)
    except ValueError:
        return False
    return True


def virtual_path_from_physical(
    path: str | PathLike[str],
    *,
    root: str | PathLike[str] | None = None,
) -> str:
    """Convert a path inside the active physical root back to `/virtual` form.

    Paths outside the active root are intentionally hidden instead of returned
    verbatim, since they may reveal host or private app filesystem layout.
    """
    root_path = Path(root or active_filesystem_root()).absolute()
    resolved_root = root_path.resolve()
    resolved_path = Path(path).resolve()
    try:
        relative = resolved_path.relative_to(resolved_root)
    except ValueError:
        return OUTSIDE_ACTOR_ROOT
    if str(relative) == ".":
        return "/"
    return "/" + relative.as_posix()


def _root_is_public(root_path: str) -> bool:
    """Whether ``root_path`` is the TA/coordinator mount, which is not secret."""
    return root_path.rstrip("/") == str(Path(TARGET_AGENT_FILESYSTEM_ROOT).absolute()).rstrip("/")


def redact_physical_paths(
    value: str,
    *,
    root: str | PathLike[str] | None = None,
) -> str:
    """Scrub hidden physical paths from tool-facing strings.

    A hidden (VCA) root is rewritten to "/": for example, a VCA result like
    `created /.apps_data/.coordinator/agent_filesystems/alice/report.docx`
    becomes `created /report.docx`.

    The public TA/coordinator root ("/filesystem") is left alone. Tools already
    emit virtual paths, and "/filesystem/x" can be one: rewriting it named a
    file that does not exist (ENV-494).

    Do not scrub arbitrary "/.apps_data" tokens: that is also a valid virtual
    directory inside an actor root. Producers of physical paths must use
    virtual_path_from_physical to hide paths outside the actor root before
    mixing them into tool output.
    """
    root_path = str(Path(root or active_filesystem_root()).absolute())
    redacted = value
    if not _root_is_public(root_path):
        resolved_root = str(Path(root_path).resolve())
        for root_value in dict.fromkeys((root_path.rstrip("/"), resolved_root.rstrip("/"))):
            redacted = re.sub(
                _ROOT_PREFIX_RE + re.escape(root_value) + _ROOT_SUFFIX_RE,
                "/",
                redacted,
            )
    return redacted


def _redact_value(value: Any) -> Any:
    """Recursively redact strings inside structured tool output."""
    if isinstance(value, str):
        return redact_physical_paths(value)
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _redact_value(item) for key, item in value.items()}
    return value


def _redact_tool_result(result: Any) -> Any:
    """Redact hidden physical paths from string and FastMCP ToolResult outputs.

    For example, for a VCA `alice`, `ToolResult(content="read
    /.apps_data/.coordinator/agent_filesystems/alice/a.txt")` becomes a result
    whose text says `read /a.txt`. See :func:`redact_physical_paths`.
    """
    if isinstance(result, str):
        return redact_physical_paths(result)
    if not isinstance(result, ToolResult):
        return result

    content = []
    for item in result.content:
        text = getattr(item, "text", None)
        if isinstance(text, str):
            content.append(item.model_copy(update={"text": redact_physical_paths(text)}))
        else:
            content.append(item)

    # ToolResult may be a plain fastmcp class (no pydantic model_copy) on older
    # pins; mutate in place. Per-item model_copy above is fine (ContentBlocks).
    result.content = content
    result.structured_content = _redact_value(result.structured_content)
    result.meta = _redact_value(result.meta)
    return result


def _redact_exception(exc: Exception) -> Exception:
    exc.args = tuple(_redact_value(arg) for arg in exc.args)
    for attr in ("filename", "filename2", "strerror"):
        value = getattr(exc, attr, None)
        if isinstance(value, str):
            setattr(exc, attr, redact_physical_paths(value))
    notes = getattr(exc, "__notes__", None)
    if isinstance(notes, list):
        exc.__notes__ = [
            redact_physical_paths(note) if isinstance(note, str) else note for note in notes
        ]
    exc.__cause__ = None
    exc.__context__ = None
    exc.__suppress_context__ = True
    return exc


class ActorMiddleware(Middleware):
    async def on_call_tool(self, context: MiddlewareContext, call_next: CallNext):
        """Bind actor identity for one tool call and redact hidden paths afterward."""
        actor_id = validate_actor_id(
            extract_bearer_actor_id(_request_headers()) or TARGET_AGENT_ACTOR_ID
        )
        token = _current_actor_id.set(actor_id)
        try:
            return _redact_tool_result(await call_next(context))
        except Exception as exc:
            raise _redact_exception(exc) from None
        finally:
            _current_actor_id.reset(token)

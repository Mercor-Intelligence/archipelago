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
"""

import os
import re
from collections.abc import Mapping
from contextvars import ContextVar
from os import PathLike
from pathlib import Path
from typing import Any

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
COORDINATOR_ROOT = "/.apps_data/.coordinator"
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


def resolve_virtual_path(
    path: str | None,
    *,
    root: str | PathLike[str] | None = None,
    check_exists: bool = False,
    must_be_file: bool = False,
    must_be_dir: bool = False,
) -> str:
    """Resolve an agent-facing path into the active actor's physical root.

    Tool callers should see `/foo.docx` regardless of actor. This accepts those
    virtual paths, plus already-rooted physical paths, and rejects traversal out
    of the selected actor filesystem.
    """
    root_path = Path(root or active_filesystem_root()).absolute()
    raw_path = "" if not path or path == "/" else path
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
        raise FileNotFoundError(f"Path does not exist: {path}")
    if must_be_file and not full_path.is_file():
        raise ValueError(f"Path is not a file: {path}")
    if must_be_dir and not full_path.is_dir():
        raise ValueError(f"Path is not a directory: {path}")
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


def redact_physical_paths(
    value: str,
    *,
    root: str | PathLike[str] | None = None,
) -> str:
    """Scrub physical root prefixes from tool-facing strings.

    For example, a VCA result like
    `created /.apps_data/.coordinator/agent_filesystems/alice/report.docx`
    becomes `created /report.docx`.
    """
    root_path = str(Path(root or active_filesystem_root()).absolute())
    resolved_root = str(Path(root_path).resolve())
    redacted = value
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
    """Redact physical roots from string and FastMCP ToolResult outputs.

    For example, `ToolResult(content="read /filesystem/a.txt",
    structured_content={"path": "/filesystem/a.txt"})` becomes a result whose
    text and structured path both say `/a.txt`.
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

    return result.model_copy(
        update={
            "content": content,
            "structured_content": _redact_value(result.structured_content),
            "meta": _redact_value(result.meta),
        }
    )


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
        """Bind actor identity for one tool call and redact physical roots afterward."""
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

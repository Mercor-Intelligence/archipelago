import json
import os
import pwd
import time
from datetime import UTC, datetime
from pathlib import Path

from fastmcp import Client as FastMCPClient
from fastmcp.client.client import CallToolResult
from fastmcp.tools import ToolResult
from pydantic import JsonValue


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


# A summary is one line of mcp_calls.jsonl per tool call per actor, so structured
# content rides along only while it stays small. Anything larger keeps the old
# boolean-only shape rather than growing the log without bound.
MAX_STRUCTURED_CONTENT_BYTES = 8192
STRUCTURED_CONTENT_SUMMARY_KEY = "structured_content"
HAS_STRUCTURED_CONTENT_SUMMARY_KEY = "has_structured_content"


def _small_enough_to_keep(structured_content: object) -> bool:
    try:
        encoded = json.dumps(structured_content)
    except (TypeError, ValueError):
        return False
    return len(encoded.encode()) <= MAX_STRUCTURED_CONTENT_BYTES


def summarize_tool_result(
    result: ToolResult | CallToolResult,
) -> dict[str, JsonValue]:
    structured_content = result.structured_content
    content = result.content

    text_parts: list[str] = []
    if isinstance(content, list):
        for item in content[:3]:
            text = getattr(item, "text", None)
            if text is None and isinstance(item, dict):
                text = item.get("text")
            if isinstance(text, str):
                text_parts.append(text[:500])

    summary: dict[str, JsonValue] = {"content_items": len(content or [])}
    if text_parts:
        summary["text"] = "\n".join(text_parts)
    if structured_content is not None:
        summary[HAS_STRUCTURED_CONTENT_SUMMARY_KEY] = True
        if _small_enough_to_keep(structured_content):
            summary[STRUCTURED_CONTENT_SUMMARY_KEY] = structured_content
    return summary


def get_mcp_gateway_url() -> str:
    port = os.environ.get("PORT", "8080")
    return f"http://127.0.0.1:{port}/mcp/"


async def validate_mcp_gateway_url(mcp_gateway_url: str) -> None:
    schema = {
        "mcpServers": {
            "gateway": {
                "transport": "streamable-http",
                "url": mcp_gateway_url,
            }
        }
    }
    try:
        async with FastMCPClient(schema) as client:
            await client.list_tools()
    except Exception as e:
        raise RuntimeError(
            f"Environment Coordinator could not reach MCP gateway at {mcp_gateway_url}: {e}"
        ) from e


def get_archipelago_agents_cwd() -> str:
    """
    VCAs use Archipelago Agents as their process harness, so the Environment
    Coordinator needs to find the bundled Archipelago Agents folder before
    spawning a VCA run.
    """
    coordinator_path = Path(__file__).resolve()
    for parent in coordinator_path.parents:
        candidate = parent / "agents"
        if (candidate / "pyproject.toml").is_file() and (
            candidate / "runner" / "main.py"
        ).is_file():
            return str(candidate)
    raise RuntimeError(
        f"Could not locate Archipelago agents runner near Environment Coordinator at {coordinator_path}"
    )


def user_home(user: str) -> str:
    """Return ``user``'s home dir (so a confined VCA gets a writable HOME, not the Coordinator's)."""
    return pwd.getpwnam(user).pw_dir


def chown_tree(path: Path, user: str) -> None:
    """Recursively give ``path`` (a VCA's run/filesystem dir) to ``user`` and strip
    group/other permissions, so a sibling confined user cannot read another VCA's
    artifacts; requires a privileged Coordinator.
    Uses ``lchown``/``rglob`` (never follows symlinks) so a VCA-planted symlink can't
    chown/chmod Coordinator state out of confinement."""
    pw = pwd.getpwnam(user)

    def _hand_over(p: Path) -> None:
        os.lchown(p, pw.pw_uid, pw.pw_gid)
        if not p.is_symlink():
            # Owner-only (dirs keep x, files keep their owner bits).
            os.chmod(p, p.stat().st_mode & 0o700)

    _hand_over(path)
    for child in path.rglob("*"):
        _hand_over(child)


def write_json(path: Path, data: dict[str, JsonValue]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{time.monotonic_ns()}.tmp")
    tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)

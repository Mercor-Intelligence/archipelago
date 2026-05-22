import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from fastmcp import Client as FastMCPClient
from fastmcp.client.client import CallToolResult
from fastmcp.tools import ToolResult
from pydantic import JsonValue


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


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
        summary["has_structured_content"] = True
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


def write_json(path: Path, data: dict[str, JsonValue]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{time.monotonic_ns()}.tmp")
    tmp_path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp_path.replace(path)

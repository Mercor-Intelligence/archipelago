"""MCP client helpers for agents using LiteLLM."""

from typing import Any

from loguru import logger
from mcp.types import ContentBlock, ImageContent, TextContent

from runner.agents.models import LitellmInputMessage


def build_mcp_gateway_schema(
    mcp_gateway_url: str,
    mcp_gateway_auth_token: str | None,
) -> dict[str, dict[str, dict[str, Any]]]:
    """
    Build the MCP client config schema for connecting to the environment's MCP gateway.

    The gateway is a single HTTP endpoint that proxies to all configured MCP servers
    in the environment sandbox.

    Args:
        mcp_gateway_url: URL of the MCP gateway (e.g. "http://localhost:8000/mcp/")
        mcp_gateway_auth_token: Bearer token for authentication (None for local/unauthenticated)

    Returns:
        The standard schema expected by the MCP client.
    """
    gateway_config: dict[str, Any] = {
        "transport": "streamable-http",
        "url": mcp_gateway_url,
    }

    # Only add Authorization header if token is provided
    if mcp_gateway_auth_token:
        gateway_config["headers"] = {
            "Authorization": f"Bearer {mcp_gateway_auth_token}"
        }

    return {
        "mcpServers": {
            "gateway": gateway_config,
        }
    }


def content_blocks_to_messages(
    content_blocks: list[ContentBlock],
    tool_call_id: str,
    name: str,
    model: str,
    deferred_image_messages: list[LitellmInputMessage],
) -> list[LitellmInputMessage]:
    """
    Convert MCP content blocks to a single LiteLLM tool message.

    Each tool_use must have exactly one tool_result. This function combines all
    content blocks into a single tool message to satisfy API requirements for
    Anthropic, OpenAI, and other providers.

    For non-Anthropic models, images cannot be embedded in tool results, so they
    are appended to deferred_image_messages as user messages. The caller is
    responsible for adding them to self.messages after all tool responses.
    This list is mutated in place.

    Args:
        content_blocks: MCP content blocks from tool result
        tool_call_id: The tool call ID to associate with the result
        name: The tool name
        model: The model being used
        deferred_image_messages: Mutable list that image user messages are
            appended to (mutated in place). Callers should extend self.messages
            with this list after all tool responses are added.

    Returns:
        List containing exactly one tool message.
    """
    # Anthropic supports images directly in tool results
    supports_image_tool_results = model.startswith("anthropic/")

    text_contents: list[str] = []
    image_data_uris: list[str] = []

    for content_block in content_blocks:
        match content_block:
            case TextContent():
                block = TextContent.model_validate(content_block)
                text_contents.append(block.text)

            case ImageContent():
                block = ImageContent.model_validate(content_block)
                data_uri = f"data:{block.mimeType};base64,{block.data}"
                image_data_uris.append(data_uri)

            case _:
                logger.warning(f"Content block type {content_block.type} not supported")
                text_contents.append("Unable to parse tool call response")

    messages: list[LitellmInputMessage] = []

    if supports_image_tool_results:
        content: list[dict[str, Any]] = []
        for text in text_contents:
            content.append({"type": "text", "text": text})
        for data_uri in image_data_uris:
            content.append({"type": "image_url", "image_url": {"url": data_uri}})

        tool_message: LitellmInputMessage = {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": name,
            "content": content if content else [{"type": "text", "text": ""}],
        }  # pyright: ignore[reportAssignmentType]
        messages.append(tool_message)
    else:
        content = [{"type": "text", "text": text} for text in text_contents]

        if image_data_uris and not content:
            content.append(
                {"type": "text", "text": f"Image(s) returned by {name} tool"}
            )

        tool_message = {
            "role": "tool",
            "tool_call_id": tool_call_id,
            "name": name,
            "content": content if content else [{"type": "text", "text": ""}],
        }  # pyright: ignore[reportAssignmentType]
        messages.append(tool_message)

        # Image workaround: non-Anthropic models don't support images in tool results,
        # so we append them to deferred_image_messages for the caller to add after all tool responses.
        for data_uri in image_data_uris:
            deferred_image_messages.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": data_uri}},
                    ],
                }
            )

    return messages

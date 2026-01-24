"""Utility functions for smoke tests."""


def get_content_text(result) -> str:
    """Extract text content from MCP call result.

    Args:
        result: The result from client.call_tool()

    Returns:
        Text content as string
    """
    if not result.content:
        return ""
    content = result.content[0]
    if hasattr(content, "text"):
        return content.text
    return str(content)

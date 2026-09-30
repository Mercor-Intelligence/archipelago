"""
XML formatting for Stirrup agent tool results

Matches Stirrup's exact XML output format for tool results
"""

import html

MAX_LENGTH_SHELL_STDOUT = 20_000
MAX_LENGTH_SHELL_STDERR = 20_000
MAX_LENGTH_WEB_FETCH_HTML = 40_000
MAX_LENGTH_WEB_SEARCH_RESULTS = 40_000


def truncate_msg(msg: str, max_length: int) -> str:
    """Truncate a message to max_length characters"""
    if len(msg) <= max_length:
        return msg
    return msg[:max_length] + "\n... [truncated]"


def format_shell_result(
    exit_code: int,
    stdout: str,
    stderr: str,
    error_kind: str | None = None,
    advice: str | None = None,
) -> str:
    """Format shell execution result as XML

    Matches Stirrup's format_result() in code_backends/base.py

    Args:
        exit_code: Command exit code
        stdout: Standard output
        stderr: Standard error
        error_kind: Error type if an error occurred (e.g., 'timeout', 'invalid_argument')
        advice: Optional advice message for errors

    Returns:
        XML formatted result string
    """
    if error_kind:
        content = (
            f"<shell_results>"
            f"\n<error_kind>{error_kind}</error_kind>"
            f"\n<details>{truncate_msg(stderr, MAX_LENGTH_SHELL_STDERR)}</details>"
        )
        if advice:
            content += f"\n<advice>{advice}</advice>"
        content += "\n</shell_results>"
        return content

    return (
        f"<shell_results>"
        f"\n<exit_code>{exit_code}</exit_code>"
        f"\n<stdout>{truncate_msg(stdout, MAX_LENGTH_SHELL_STDOUT)}</stdout>"
        f"\n<stderr>{truncate_msg(stderr, MAX_LENGTH_SHELL_STDERR)}</stderr>"
        f"\n</shell_results>"
    )


def format_web_search_results(results: list[dict[str, str]]) -> str:
    """Format web search results as XML"""
    items = "\n".join(
        (
            "<result>"
            f"\n<title>{html.escape(result.get('title', '') or '')}</title>"
            f"\n<url>{html.escape(result.get('url', '') or '')}</url>"
            f"\n<description>{html.escape(result.get('description', '') or '')}</description>"
            "\n</result>"
        )
        for result in results
    )
    content = f"<results>\n{items}\n</results>"
    return truncate_msg(content, MAX_LENGTH_WEB_SEARCH_RESULTS)


def format_web_fetch_result(
    url: str, body: str | None = None, error: str | None = None
) -> str:
    """Format web fetch result as XML"""
    if error:
        return (
            f"<web_fetch>"
            f"<url>{url}</url>"
            f"<error>{truncate_msg(error, MAX_LENGTH_WEB_FETCH_HTML)}</error>"
            f"</web_fetch>"
        )
    return (
        f"<web_fetch>"
        f"<url>{url}</url>"
        f"<body>{truncate_msg(body or '', MAX_LENGTH_WEB_FETCH_HTML)}</body>"
        f"</web_fetch>"
    )


def parse_mcp_shell_result(content: str) -> tuple[int, str, str]:
    """Parse MCP code execution result to extract exit code, stdout, stderr"""
    import json

    try:
        data = json.loads(content)
        if isinstance(data, dict):
            return (
                int(data.get("exit_code", data.get("exitCode", 0))),
                str(data.get("stdout", data.get("output", ""))),
                str(data.get("stderr", data.get("error", ""))),
            )
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    return (0, content, "")


def parse_mcp_search_result(content: str) -> list[dict[str, str]]:
    """Parse MCP web search result to extract results list"""
    import json
    import re

    try:
        data = json.loads(content)
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            if "results" in data and isinstance(data["results"], list):
                return data["results"]
            return []
    except (json.JSONDecodeError, TypeError):
        pass

    results: list[dict[str, str]] = []

    result_blocks = re.split(r"Result \d+:\s*\n-+\n", content)

    for block in result_blocks[1:]:
        if not block.strip():
            continue

        result: dict[str, str] = {}

        title_match = re.search(r"^Title:\s*(.+?)$", block, re.MULTILINE)
        if title_match:
            result["title"] = title_match.group(1).strip()

        url_match = re.search(r"^URL:\s*(.+?)$", block, re.MULTILINE)
        if url_match:
            result["url"] = url_match.group(1).strip()

        snippet_match = re.search(r"^Snippet:\s*(.+?)$", block, re.MULTILINE)
        if snippet_match:
            result["description"] = snippet_match.group(1).strip()

        if result.get("title") or result.get("url"):
            results.append(result)

    return results

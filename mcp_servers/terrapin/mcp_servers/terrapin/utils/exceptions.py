"""Custom exceptions for the Terrapin MCP server."""


class NonRetryableError(Exception):
    """Raised for deterministic errors that should not be retried.

    Used instead of ValueError in retry-decorated functions so that transient
    ValueError subclasses (e.g. json.JSONDecodeError from truncated network
    responses) are still eligible for retry.
    """

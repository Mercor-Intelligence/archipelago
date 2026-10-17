"""EndpointResponse — lets an implementation signal a custom HTTP status code.

Import from the package root::

    from mcp_unified_endpoint import EndpointResponse
"""

from __future__ import annotations

import dataclasses
from typing import Any


@dataclasses.dataclass(frozen=True)
class EndpointResponse:
    """Wrapper that lets an implementation function control the HTTP status code.

    When an endpoint function returns an ``EndpointResponse`` instead of a
    plain ``dict``, the REST handler uses ``status`` as the HTTP response
    status code rather than the spec's ``success_status``.

    The MCP tool path automatically unwraps ``EndpointResponse`` to its
    ``body`` — status codes are an HTTP-only concept with no equivalent in
    the MCP protocol.  The MCP caller receives ``body`` directly.

    Usage::

        from typing import Any
        from mcp_unified_endpoint import EndpointResponse, endpoint

        @endpoint("POST /crm/v8/{module}/actions/mass_update")
        async def mass_update(
            module: str,
            body: dict[str, Any],
        ) -> dict[str, Any] | EndpointResponse:
            result = await _apply_updates(module, body)
            if result.scheduled:
                # Accepted for async processing → 202 Accepted.
                return EndpointResponse(body={"jobId": result.job_id}, status=202)
            # Completed synchronously → 200 OK (spec's success_status).
            return result.data

    A plain ``dict`` return still behaves as before: the spec's
    ``success_status`` is used.
    """

    body: dict[str, Any]
    """The response payload. Returned as-is by the MCP tool."""

    status: int
    """HTTP status code for the REST response. Ignored by the MCP tool path."""


__all__ = ["EndpointResponse"]

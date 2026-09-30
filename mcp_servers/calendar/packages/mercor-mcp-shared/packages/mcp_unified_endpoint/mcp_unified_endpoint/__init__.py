"""mcp_unified_endpoint — one decorator, three registrations.

The OpenAPI document is the source of truth for the wire shape; the
endpoint function only declares the parameters its implementation uses.
``@endpoint`` captures the wiring at decoration time and
``register_all`` wires everything to the MCP server + the Starlette
router at startup.

Typical use — **direct-decoration** of a service function. The handler
opens / closes the DB session via :class:`Depends`, and the per-endpoint
``on_error`` map translates the service's domain exceptions into the
configured Zoho V8 envelope shape::

    from typing import Annotated, Any

    from sqlalchemy.orm import Session

    from mcp_unified_endpoint import Depends, endpoint
    from db.session import SessionLocal


    def _bad_note_id(exc: KeyError, kw: dict[str, Any]) -> dict[str, Any]:
        return {
            "code": "INVALID_DATA",
            "message": "the id given seems to be invalid",
            "details": {"id": kw["note_id"]},
            "status": "error",
        }


    @endpoint(
        "GET /crm/v9/Notes/{note_id}",
        title="Get a single note by id",
        tool_name="get-ZohoCRM-note",
        on_error={KeyError: _bad_note_id},
    )
    def get_note(
        note_id: str,
        db: Annotated[Session, Depends(SessionLocal)],
    ) -> dict[str, Any]:
        ...

Adapter mode still works for endpoints whose wire shape diverges from the
service signature — wrap the call, decorate the wrapper, same machinery.
"""

from .decorator import endpoint
from .dependencies import Depends
from .errors import (
    ErrorBuilder,
    ErrorSpec,
    build_envelope,
    register_default_errors,
    resolve_error,
    resolve_status,
)
from .layer_switch import is_layer_enabled
from .openapi_lookup import (
    OpenAPILookupError,
    OperationSpec,
    ParamSpec,
    coerce,
    lookup,
)
from .registry import (
    EndpointDecl,
    MCPLike,
    RegistrationReport,
    get_declarations,
    register_all,
)
from .response import EndpointResponse
from .rest import build_handler

__all__ = [
    "Depends",
    "EndpointDecl",
    "EndpointResponse",
    "ErrorBuilder",
    "ErrorSpec",
    "MCPLike",
    "OpenAPILookupError",
    "OperationSpec",
    "ParamSpec",
    "RegistrationReport",
    "build_envelope",
    "build_handler",
    "coerce",
    "endpoint",
    "get_declarations",
    "is_layer_enabled",
    "lookup",
    "register_all",
    "register_default_errors",
    "resolve_error",
    "resolve_status",
]

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

from starlette.requests import Request

from .backfill import BackfillConfig, snake_operation_id
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
    ExpandOverride,
    MCPLike,
    NameMutator,
    RegistrationReport,
    ToolBinding,
    clear_name_mutator,
    get_declarations,
    register_all,
    register_name_mutator,
)
from .response import EndpointResponse
from .rest import (
    BodyErrorHook,
    BodyParseError,
    MissingParamError,
    MissingParamHook,
    TypeCoercionError,
    TypeCoercionHook,
    build_handler,
)

__all__ = [
    "BackfillConfig",
    "BodyErrorHook",
    "BodyParseError",
    "Depends",
    "EndpointDecl",
    "EndpointResponse",
    "ErrorBuilder",
    "ErrorSpec",
    "ExpandOverride",
    "MCPLike",
    "MissingParamError",
    "MissingParamHook",
    "NameMutator",
    "OpenAPILookupError",
    "OperationSpec",
    "ParamSpec",
    "RegistrationReport",
    "Request",
    "ToolBinding",
    "TypeCoercionError",
    "TypeCoercionHook",
    "build_envelope",
    "build_handler",
    "clear_name_mutator",
    "coerce",
    "endpoint",
    "get_declarations",
    "is_layer_enabled",
    "lookup",
    "register_all",
    "register_default_errors",
    "register_name_mutator",
    "resolve_error",
    "resolve_status",
    "snake_operation_id",
]

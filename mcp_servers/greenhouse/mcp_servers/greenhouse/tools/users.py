"""User tool wiring for the Greenhouse MCP server."""

from auth.permissions import Permission as Perm
from fastmcp import FastMCP
from fastmcp.exceptions import NotFoundError, ToolError
from mcp_auth import require_scopes
from schemas import (
    CreateUserInput,
    GetUserInput,
    ListUsersInput,
    ListUsersOutput,
    PaginationMeta,
    UserOutput,
)
from services.clean_provider import (
    CleanProvider,
    DepartmentNotFoundError,
    DuplicateUserEmailError,
    OfficeNotFoundError,
    UserNotFoundError,
)
from services.pagination import build_pagination_links


@require_scopes(Perm.USER_READ.value)
async def greenhouse_users_list(params: ListUsersInput) -> ListUsersOutput:
    """List users with optional filters and pagination metadata."""

    provider = CleanProvider()
    users = await provider.list_users(
        page=params.page,
        per_page=params.per_page,
        email=params.email,
        employee_id=params.employee_id,
        created_before=params.created_before,
        created_after=params.created_after,
        updated_before=params.updated_before,
        updated_after=params.updated_after,
    )

    total = None
    if not params.skip_count:
        total = await provider.count_users(
            email=params.email,
            employee_id=params.employee_id,
            created_before=params.created_before,
            created_after=params.created_after,
            updated_before=params.updated_before,
            updated_after=params.updated_after,
        )

    links = build_pagination_links("/users", params.page, params.per_page, total)
    meta = PaginationMeta(per_page=params.per_page, page=params.page, total=total, links=links)
    return ListUsersOutput(users=users, meta=meta)


@require_scopes(Perm.USER_READ.value)
async def greenhouse_users_get(params: GetUserInput) -> UserOutput:
    """Retrieve a single user by ID."""

    provider = CleanProvider()
    try:
        return await provider.get_user(params.user_id)
    except UserNotFoundError as exc:
        raise NotFoundError(str(exc)) from exc


@require_scopes(Perm.USER_CREATE.value)
async def greenhouse_users_create(params: CreateUserInput) -> UserOutput:
    """Create a new user in Greenhouse with Basic permissions."""
    provider = CleanProvider()

    try:
        return await provider.create_user(
            first_name=params.first_name,
            last_name=params.last_name,
            email=params.email,
            employee_id=params.employee_id,
            department_ids=params.department_ids,
            office_ids=params.office_ids,
        )
    except DuplicateUserEmailError as exc:
        raise ToolError(str(exc)) from exc
    except DepartmentNotFoundError as exc:
        raise ToolError(str(exc)) from exc
    except OfficeNotFoundError as exc:
        raise ToolError(str(exc)) from exc


def register_user_tools(mcp: FastMCP) -> None:
    """Register the user tools with the MCP server."""
    mcp.tool()(greenhouse_users_list)
    mcp.tool()(greenhouse_users_get)
    mcp.tool()(greenhouse_users_create)

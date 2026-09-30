"""Testing utilities for mcp_auth.

Provides context managers and helpers for mocking authentication
in tests without requiring full middleware setup.

Example:
    from mcp_auth.testing import mock_auth_user

    async def test_protected_tool():
        with mock_auth_user(roles=["recruiter"], scopes=["candidate:read"]):
            result = await my_protected_tool(params)
            assert result.success

    # Or with the class for more control:
    async def test_with_specific_user():
        with mock_auth_user(
            username="test_user",
            roles=["admin"],
            scopes=["read", "write", "delete"],
            user_id="user-123"
        ) as user:
            result = await my_tool(params)
            assert user["username"] == "test_user"
"""

import inspect
from collections.abc import Callable, Generator
from contextlib import contextmanager
from functools import wraps
from typing import Any

from .context import current_user, get_current_user
from .errors import AuthorizationError


@contextmanager
def mock_auth_user(
    username: str = "test_user",
    roles: list[str] | None = None,
    scopes: list[str] | None = None,
    user_id: str | None = None,
    **extra_fields: Any,
) -> Generator[dict, None, None]:
    """Context manager to mock an authenticated user for testing.

    Sets up the current_user context variable with the specified user info,
    and automatically cleans up after the test.

    Args:
        username: Username for the mock user (default: "test_user")
        roles: List of role names (e.g., ["recruiter", "admin"])
        scopes: List of permission scopes (e.g., ["candidate:read", "job:write"])
        user_id: Optional user ID
        **extra_fields: Additional fields to include in the user dict

    Yields:
        The user dict that was set in context

    Example:
        # Basic usage with custom scopes
        with mock_auth_user(roles=["recruiter"], scopes=["candidate:read"]):
            result = await protected_tool(params)

        # With specific username and extra fields
        with mock_auth_user(
            username="jane@example.com",
            roles=["hr_admin"],
            scopes=["candidate:read", "candidate:write", "job:read"],
            employee_id="EMP-123"
        ) as user:
            # Access the mock user if needed
            print(user["employee_id"])
    """
    user = {
        "username": username,
        "roles": roles or [],
        "scopes": scopes or [],
        **extra_fields,
    }
    if user_id:
        user["userId"] = user_id

    token = current_user.set(user)
    try:
        yield user
    finally:
        current_user.reset(token)


def with_scope_enforcement(func: Callable) -> Callable:
    """Wrap a tool function to enforce scope checking for testing.

    Use this to test authorization behavior without middleware.
    The wrapper reads scope requirements from the function's `_required_scopes`
    attribute (set by @require_scopes decorator) and `_required_any_scopes`
    attribute (set by @require_any_scopes decorator) and checks them against
    the current user's scopes.

    - `_required_scopes`: AND-logic (user must have ALL scopes)
    - `_required_any_scopes`: OR-logic (user must have at least ONE scope)

    Args:
        func: A tool function decorated with @require_scopes or @require_any_scopes

    Returns:
        Wrapped function that checks scopes before calling the original

    Raises:
        AuthorizationError: If user lacks required scopes

    Example:
        fn = with_scope_enforcement(_get_tool_fn("greenhouse_candidates_update"))

        with mock_hiring_manager():  # Has limited scopes
            with pytest.raises(AuthorizationError):
                await fn(UpdateCandidateInput(...))

        with mock_recruiter():  # Has candidate:update scope
            result = await fn(UpdateCandidateInput(...))  # Works
    """
    required_scopes = getattr(func, "_required_scopes", set())
    required_any_scopes = getattr(func, "_required_any_scopes", set())

    def _check_scopes():
        """Check scope requirements and raise AuthorizationError if not met."""
        user = get_current_user()

        # Check AND-logic scopes (all must be present)
        if required_scopes:
            if not user:
                raise AuthorizationError("No authenticated user")

            user_scopes = set(user.get("scopes", []))
            if not required_scopes.issubset(user_scopes):
                missing = required_scopes - user_scopes
                raise AuthorizationError(f"Missing scope(s): {', '.join(sorted(missing))}")

        # Check OR-logic scopes (at least one must be present)
        if required_any_scopes:
            if not user:
                raise AuthorizationError("No authenticated user")

            user_scopes = set(user.get("scopes", []))
            if not required_any_scopes.intersection(user_scopes):
                raise AuthorizationError(
                    f"Requires one of: {', '.join(sorted(required_any_scopes))}"
                )

    if inspect.iscoroutinefunction(func):

        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            _check_scopes()
            return await func(*args, **kwargs)

        # Preserve metadata
        async_wrapper._required_scopes = required_scopes
        async_wrapper._required_any_scopes = required_any_scopes
        return async_wrapper
    else:

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            _check_scopes()
            return func(*args, **kwargs)

        # Preserve metadata
        sync_wrapper._required_scopes = required_scopes
        sync_wrapper._required_any_scopes = required_any_scopes
        return sync_wrapper


__all__ = [
    "mock_auth_user",
    "with_scope_enforcement",
]

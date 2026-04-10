"""Authentication and authorization error classes for MCP servers.

These errors map to HTTP status codes:
- AuthenticationError: 401 Unauthorized (missing/invalid token)
- AuthorizationError: 403 Forbidden (insufficient permissions)
"""


class AuthenticationError(Exception):
    """Raised when authentication fails (401 Unauthorized).

    This error indicates:
    - Missing authentication token
    - Invalid token format
    - Token not recognized or expired

    Example:
        >>> raise AuthenticationError("Missing Authorization header")
    """

    def __init__(self, message: str = "Authentication required"):
        self.message = message
        self.status_code = 401
        super().__init__(self.message)


class AuthorizationError(Exception):
    """Raised when authorization fails (403 Forbidden).

    This error indicates:
    - User lacks required scope for the operation
    - User lacks required role
    - Resource-level access denied

    Example:
        >>> raise AuthorizationError("Missing scope: candidate:update")
    """

    def __init__(self, message: str = "Access denied"):
        self.message = message
        self.status_code = 403
        super().__init__(self.message)


__all__ = ["AuthenticationError", "AuthorizationError"]

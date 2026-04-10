"""Repository exceptions for Greenhouse MCP Server.

Defines the exception hierarchy for repository layer errors.
These exceptions map to HTTP status codes used by Greenhouse API.
"""


class RepositoryError(Exception):
    """Base exception for all repository errors.

    All repository exceptions inherit from this class to enable
    catching any repository-related error.
    """

    status_code: int = 500
    message: str = "An unexpected error occurred"

    def __init__(self, message: str | None = None):
        self.message = message or self.__class__.message
        super().__init__(self.message)


class NotFoundError(RepositoryError):
    """Resource not found (HTTP 404).

    Raised when a requested resource does not exist in the database.

    Example:
        >>> raise NotFoundError("User with id 123 does not exist")
    """

    status_code = 404
    message = "Resource not found"


class ValidationError(RepositoryError):
    """Invalid input data (HTTP 400/422).

    Raised when input data fails validation or business rules.

    Attributes:
        field: The field that failed validation (optional)
        errors: List of validation error details (optional)

    Example:
        >>> raise ValidationError("Invalid email format", field="email")
    """

    status_code = 422
    message = "Validation error"

    def __init__(
        self,
        message: str | None = None,
        field: str | None = None,
        errors: list[dict] | None = None,
    ):
        super().__init__(message)
        self.field = field
        self.errors = errors or []
        if field and not errors:
            self.errors = [{"field": field, "message": self.message}]


class AccessDeniedError(RepositoryError):
    """Access denied (HTTP 403).

    Raised when the current persona lacks permission to access a resource.

    Example:
        >>> raise AccessDeniedError("Hiring managers cannot create candidates")
    """

    status_code = 403
    message = "Access denied"


class ConflictError(RepositoryError):
    """Resource conflict (HTTP 409).

    Raised when an operation would create a conflicting state,
    such as duplicate unique values or invalid state transitions.

    Example:
        >>> raise ConflictError("Candidate with this email already exists")
    """

    status_code = 409
    message = "Resource conflict"


class BadRequestError(RepositoryError):
    """Malformed request (HTTP 400).

    Raised for malformed requests or missing required fields.

    Example:
        >>> raise BadRequestError("Missing required field: first_name")
    """

    status_code = 400
    message = "Bad request"

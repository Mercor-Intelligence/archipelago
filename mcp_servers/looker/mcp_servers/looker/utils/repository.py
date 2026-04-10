"""Repository classes for loading and querying data from various sources."""

import json
import os
import re
import tomllib
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, TypeVar
from urllib.parse import quote, urlparse

if TYPE_CHECKING:
    from auth import LookerAuthService

import httpx
from http_client import get_http_client
from loguru import logger
from pydantic import BaseModel, ValidationError
from starlette.responses import JSONResponse

T = TypeVar("T", bound=BaseModel)


def _get_version() -> str:
    """Get version from pyproject.toml.

    Returns:
        Version string, or '0.0.0' if unable to read
    """
    try:
        # Find pyproject.toml - go up from this file's directory
        repo_root = Path(__file__).parent.parent
        pyproject_path = repo_root / "pyproject.toml"

        if pyproject_path.exists():
            with open(pyproject_path, "rb") as f:
                data = tomllib.load(f)
                return data.get("project", {}).get("version", "0.0.0")
    except Exception:
        pass
    return "0.0.0"


class APIConfigurable(Protocol):
    """Protocol for input models that provide API configuration."""

    @staticmethod
    def get_api_config() -> dict:
        """Get API configuration including URL template and method.

        Returns:
            Dictionary with keys:
                - url_template: URL template with {field} placeholders
                - method: HTTP method (GET, POST, etc.)
                - body_template: Optional POST body template
                - endpoint: Optional endpoint name (e.g., "lookml_models")
        """
        ...

    def matches(self, lookup_key: dict[str, Any]) -> bool:
        """Check if this input model matches the given lookup key.

        Default implementation: matches if all fields in lookup_key equal
        the corresponding fields in this model. Empty lookup_key matches everything.

        Override this method if you need custom matching logic.

        Args:
            lookup_key: Dictionary of field names to values to match against

        Returns:
            True if this model matches the lookup criteria, False otherwise
        """
        # Empty lookup key matches everything (for list-all endpoints)
        if not lookup_key:
            return True

        # Check all fields in lookup_key match this model's fields
        for key, value in lookup_key.items():
            if not hasattr(self, key) or getattr(self, key) != value:
                return False

        return True

    def to_template_values(self) -> dict[str, str]:
        """Convert to template values for URL/body substitution.

        Returns:
            Dictionary mapping field names to values
        """
        ...

    @staticmethod
    def tool_name() -> str:
        """Get the name for the tool function.

        This method is optional. If not provided, a default name will be generated
        from the class name (e.g., 'LookMLModelRequest' -> 'list_lookml_models').

        Returns:
            The tool function name
        """
        ...

    @staticmethod
    def create_repository(response_class: type["BaseModel"]) -> "Repository":
        """Create a repository instance for this request type.

        This method is optional. If not provided, the default factory
        logic will be used (based on offline_mode setting).

        Override this to provide custom repository implementations
        for specific request types.

        Args:
            response_class: The response model class

        Returns:
            Repository instance
        """
        ...


InputT = TypeVar("InputT", bound=APIConfigurable)


def _generate_default_tool_name(class_name: str) -> str:
    """Generate a default tool name from a request class name.

    Converts class names like 'LookMLModelRequest' to tool names like
    'list_lookml_models'.

    Args:
        class_name: The name of the request class

    Returns:
        Generated tool function name

    Examples:
        >>> _generate_default_tool_name('LookMLModelRequest')
        'list_lookml_models'
        >>> _generate_default_tool_name('ExploreRequest')
        'get_explore'
        >>> _generate_default_tool_name('SearchContentRequest')
        'search_content'
        >>> _generate_default_tool_name('RunQueryByIdRequest')
        'run_query_by_id'
    """
    # Remove 'Request' suffix if present
    if class_name.endswith("Request"):
        class_name = class_name[:-7]  # Remove 'Request'

    # Convert CamelCase to snake_case
    import re

    snake_case = re.sub(r"(?<!^)(?=[A-Z])", "_", class_name).lower()

    # Determine prefix based on common patterns
    # Preserve verb prefixes: list_*, search_*, run_*, create_*
    if snake_case.startswith(("list_", "search_", "run_", "create_")):
        return snake_case
    # Everything else -> get_*
    else:
        return f"get_{snake_case}"


class Repository[T: BaseModel, InputT: APIConfigurable](ABC):
    """Abstract base class for data repositories.

    This base class defines the interface for repositories that store and query
    data based on input models. Concrete implementations can use different
    data sources (local files, REST APIs, databases, etc.).

    Type Parameters:
        T: The Pydantic model type for responses
        InputT: The input model type (must implement APIConfigurable)

    Args:
        endpoint: The name of the endpoint (e.g., "rates")
        model_class: The Pydantic model class to use for responses
        env_prefix: Prefix for environment variables (default: "LOOKER")
    """

    def __init__(
        self,
        endpoint: str,
        model_class: type[T],
        input_class: type[InputT] | None = None,
        env_prefix: str = "LOOKER",
        http_method: str | None = None,
    ) -> None:
        """Initialize the repository with an endpoint name and model class.

        Args:
            endpoint: The endpoint name
            model_class: The Pydantic model class for responses
            input_class: The input model class (optional, for HTTP endpoint registration)
            env_prefix: Prefix for environment variables (default: "LOOKER")
            http_method: HTTP method to use (GET, POST, etc.). If None, uses model's default
        """
        self.endpoint = endpoint
        self.model_class = model_class
        self.input_class = input_class
        self._env_prefix = env_prefix
        self.http_method = http_method
        self._synthetic_data_path_env_var: str | None = None
        self._api_key_env_var: str | None = None
        self._base_url_env_var: str | None = None

    @property
    def synthetic_data_path_env_var(self) -> str:
        """Get the environment variable name for synthetic data path.

        Computed once and cached.

        Returns:
            Environment variable name (e.g., "LOOKER_SYNTHETIC_DATA_PATH")
        """
        if self._synthetic_data_path_env_var is None:
            self._synthetic_data_path_env_var = f"{self._env_prefix}_SYNTHETIC_DATA_PATH"
        return self._synthetic_data_path_env_var

    @property
    def api_key_env_var(self) -> str:
        """Get the environment variable name for API key.

        Computed once and cached.

        Returns:
            Environment variable name (e.g., "LOOKER_API_KEY")
        """
        if self._api_key_env_var is None:
            self._api_key_env_var = f"{self._env_prefix}_API_KEY"
        return self._api_key_env_var

    @property
    def base_url_env_var(self) -> str:
        """Get the environment variable name for base URL.

        Computed once and cached.

        Returns:
            Environment variable name (e.g., "LOOKER_BASE_URL")
        """
        if self._base_url_env_var is None:
            self._base_url_env_var = f"{self._env_prefix}_BASE_URL"
        return self._base_url_env_var

    @abstractmethod
    async def get(self, input_model: InputT) -> T | None:
        """Get the response for the given input model.

        Args:
            input_model: An input model implementing APIConfigurable protocol

        Returns:
            The response model instance if found, None otherwise
        """
        pass

    @abstractmethod
    def get_all(self) -> list[dict[str, Any]]:
        """Get all raw entries from the repository.

        Returns:
            A list of all raw data entries
        """
        pass

    async def create(self, input_model: InputT, response_data: dict[str, Any] | None = None) -> T:
        """Create a new entry in the repository.

        This is an optional method that some repositories implement. The default
        implementation delegates to get() since many APIs handle creation through
        the same endpoint.

        Args:
            input_model: An input model implementing APIConfigurable protocol
            response_data: Optional response data (used by CreateDataRepository)

        Returns:
            The response model instance

        Raises:
            ValueError: If creation fails
        """
        # Default implementation delegates to get() for APIs that handle POST
        result = await self.get(input_model)
        if result is None:
            raise ValueError(f"Failed to create entry for {input_model}")
        return result

    def register_tool(self, mcp) -> None:
        """Register repository tool method with FastMCP.

        Automatically generates a tool function based on the input class.
        The tool name is determined by the input class's tool_name() method
        if available, otherwise a default name is generated from the class name.

        Args:
            mcp: FastMCP instance to register tools on

        Example:
            >>> from fastmcp import FastMCP
            >>> from repository_factory import create_repository
            >>> from models import LookMLModelRequest, LookMLModelResponse
            >>>
            >>> mcp = FastMCP("Looker")
            >>> repo = create_repository(LookMLModelRequest, LookMLModelResponse)
            >>> repo.register_tool(mcp)
        """
        if self.input_class is None:
            return  # No input class configured, skip registration

        input_class = self.input_class

        # Get tool name (custom or auto-generated)
        if hasattr(input_class, "tool_name") and callable(input_class.tool_name):
            try:
                tool_func_name = input_class.tool_name()
            except (NotImplementedError, AttributeError):
                # Protocol method not implemented, use default
                tool_func_name = _generate_default_tool_name(input_class.__name__)
        else:
            tool_func_name = _generate_default_tool_name(input_class.__name__)

        # Get the response class for proper typing
        response_class = self.model_class

        # Create the generic tool function
        async def tool_function(request: input_class) -> response_class:
            """Auto-generated tool function."""
            logger.info(f"Processing {tool_func_name} request")
            response = await self.get(request)

            # Handle None response based on response type
            if response is None:
                # Check if this is a list-type response (e.g., OrderListResponse)
                # vs a single-item response (e.g., OrderResponse)
                try:
                    # Inspect the response class to find list fields
                    if hasattr(response_class, "model_fields"):
                        # Pydantic v2
                        fields = response_class.model_fields
                    else:
                        # Pydantic v1
                        fields = response_class.__fields__

                    # Check if all required fields are lists (list response pattern)
                    required_fields = []
                    list_fields = []
                    for field_name, field_info in fields.items():
                        # Check if it's required
                        is_required = False
                        if hasattr(field_info, "is_required"):
                            is_required = field_info.is_required()
                        elif hasattr(field_info, "default"):
                            is_required = field_info.default is None or not hasattr(
                                field_info.default, "__class__"
                            )

                        if is_required:
                            required_fields.append(field_name)

                        # Check if it's a list field
                        if hasattr(field_info, "annotation"):
                            field_type = str(field_info.annotation)
                        else:
                            field_type = str(field_info.type_)

                        if "list[" in field_type.lower() or "list |" in field_type.lower():
                            list_fields.append(field_name)

                    # If there are required fields that aren't lists, this is a single-item
                    # response and None means "not found" - raise an error
                    non_list_required = [f for f in required_fields if f not in list_fields]
                    if non_list_required:
                        raise ValueError("Resource not found")

                    # For list responses, create empty response with empty lists
                    empty_values = {field: [] for field in list_fields}
                    return response_class(**empty_values)
                except ValueError:
                    # Re-raise ValueError (resource not found)
                    raise
                except Exception as e:
                    # If we can't determine the pattern, assume it's a single-item response
                    # and raise a not found error
                    logger.debug(f"Could not determine response pattern: {e}")
                    raise ValueError("Resource not found")

            return response

        # Set the function name and docstring
        tool_function.__name__ = tool_func_name

        # Try to get docstring from the input class
        if input_class.__doc__:
            tool_function.__doc__ = input_class.__doc__

        # Register with FastMCP
        mcp.tool(tool_function)

    def register_endpoint(self, mcp, base_path: str = "/v2") -> None:
        """Register this repository as an HTTP endpoint.

        Creates a REST endpoint that accepts the input model as JSON in the request body
        and returns the repository response. The endpoint path, HTTP method, and input
        validation are all derived from the repository's configuration.

        Args:
            mcp: FastMCP instance to register the endpoint on
            base_path: Base path prefix for the endpoint (default: "/v2")

        Raises:
            ValueError: If input_class is not configured

        Example:
            >>> from models import LookMLModelRequest, LookMLModelResponse
            >>> from utils.repository import DataRepository
            >>> from fastmcp import FastMCP
            >>>
            >>> mcp = FastMCP("My API")
            >>> repo = DataRepository(
            ...     "lookml_models", LookMLModelResponse, input_class=LookMLModelRequest
            ... )
            >>> repo.register_endpoint(mcp)
            >>> # Now you can access /v2/lookml_models endpoint
        """
        if self.input_class is None:
            raise ValueError(
                f"Repository for endpoint '{self.endpoint}' must have input_class configured "
                "to be exposed as an HTTP endpoint"
            )

        input_class = self.input_class

        # Get HTTP method from repository config or input model's API config
        # Use repository's http_method if specified, otherwise use model's method
        api_config = input_class.get_api_config()
        method = self.http_method or api_config.get("method", "POST")
        url_template = api_config.get("url_template", f"/{self.endpoint}")

        # Parse URL template to extract path (without query string)
        # e.g., "/rates/{zip}?city={city}&state={state}" -> "/rates/{zip}"
        parsed_url = urlparse(url_template)
        template_path = parsed_url.path

        # Build the full endpoint path with path parameters
        endpoint_path = f"{base_path}{template_path}"

        async def endpoint_handler(request) -> dict[str, Any]:
            """Handle repository endpoint requests.

            Args:
                request: FastMCP/Starlette request object

            Returns:
                Repository response as dictionary

            Raises:
                ValueError: If input validation fails or repository lookup fails
            """
            try:
                # Extract request data based on method
                if method.upper() == "GET":
                    # For GET requests, combine query parameters and path parameters
                    # Path parameters take precedence over query parameters
                    request_data = dict(request.query_params)  # e.g., {'city': 'New York', ...}
                    request_data.update(
                        dict(request.path_params)
                    )  # e.g., {'zip': '10001'} - overwrites any query param with same name
                else:
                    # For POST/other requests, parse JSON body and merge with path parameters
                    request_data = await request.json()
                    # Merge path parameters - they take precedence over body parameters
                    request_data.update(dict(request.path_params))

                # Parse and validate input
                input_model = input_class.model_validate(request_data)

                logger.info(f"Repository endpoint {endpoint_path}: processing request")

                # Query repository
                response = await self.get(input_model)

                if response is None:
                    logger.warning(f"Repository endpoint {endpoint_path}: no data found")
                    return JSONResponse(
                        content={"error": "No data found for the given parameters"}, status_code=404
                    )

                logger.info(f"Repository endpoint {endpoint_path}: returning response")

                # Return response as dictionary (FastMCP will handle Response wrapping)
                return JSONResponse(content=response.model_dump())

            except ValidationError as e:
                logger.error(f"Repository endpoint {endpoint_path}: validation error - {e}")
                return JSONResponse(content={"error": f"Invalid input: {str(e)}"}, status_code=400)

        # Register the endpoint with FastMCP
        endpoint_path_normalized = (
            endpoint_path if endpoint_path.startswith("/") else f"/{endpoint_path}"
        )
        methods = [method.upper()]

        logger.info(
            f"Registering repository endpoint: {methods[0]} {endpoint_path_normalized} "
            f"-> {self.endpoint}"
        )

        mcp.custom_route(endpoint_path_normalized, methods=methods)(endpoint_handler)


class DataRepository[T, InputT: APIConfigurable](Repository[T, InputT]):
    """Unified repository class for data access from files or memory.

    This class can load data either from JSON files or directly from Python objects.
    It provides a get method to query the data by matching lookup keys against
    input models. Responses are automatically parsed into Pydantic models.

    Type Parameters:
        T: The Pydantic model type for responses
        InputT: The input model type (must implement APIConfigurable)

    Args:
        endpoint: The name of the endpoint (e.g., "rates", "lookml_models")
        model_class: The Pydantic model class to use for parsing responses
        data: Optional in-memory data (dict or list). If None, will load from file.
        data_file: Optional path to JSON file. If None and data is None, will use default path.
        input_class: The input model class (optional, for HTTP endpoint registration)
        env_prefix: Prefix for environment variables (default: "LOOKER")

    Examples:
        # In-memory data (Looker-style)
        >>> from models import LookMLModelRequest, LookMLModelResponse
        >>> from stores import MODELS
        >>> repo = DataRepository(
        ...     "lookml_models",
        ...     LookMLModelResponse,
        ...     data={"models": [model.model_dump() for model in MODELS]},
        ...     input_class=LookMLModelRequest
        ... )

        # File-based data with custom env prefix
        >>> from models import ExploreRequest, ExploreResponse
        >>> repo = DataRepository("explores", ExploreResponse, input_class=ExploreRequest,
        ...                       env_prefix="MYAPI")
        >>> # Will auto-load from data/synthetic/explores.json
    """

    def __init__(
        self,
        endpoint: str,
        model_class: type[T],
        data: list[dict[str, Any]] | dict[str, Any] | None = None,
        data_file: Path | str | None = None,
        input_class: type[InputT] | None = None,
        env_prefix: str = "LOOKER",
    ) -> None:
        """Initialize the repository with endpoint, model class, and data source.

        Args:
            endpoint: The endpoint name (e.g., "rates", "lookml_models")
            model_class: The Pydantic model class for parsing responses
            data: Optional in-memory data. If provided, data_file is ignored.
            data_file: Optional path to JSON file. Used only if data is None.
            input_class: The input model class (optional, for HTTP endpoint registration)
            env_prefix: Prefix for environment variables (default: "LOOKER")
        """
        super().__init__(endpoint, model_class, input_class, env_prefix)
        self._data: list[dict[str, Any]] | None = None
        self._data_file: Path | None = Path(data_file) if data_file else None
        self._in_memory_data = data  # Store the initial data if provided
        self._user_file_mtime: float | None = None  # Track user file modification time

        # If in-memory data is provided, normalize and set it immediately
        if data is not None:
            if isinstance(data, list):
                self._data = data
            else:
                # Simple dict - wrap it for list-all queries (empty lookup_key matches all)
                self._data = [{"lookup_key": {}, "response": data}]

    def _get_user_data_file_path(self) -> Path:
        """Get the path to the user data file.

        The user data path is determined by:
        1. {env_prefix}_USER_DATA_PATH environment variable if set
        2. Otherwise, if {env_prefix}_SYNTHETIC_DATA_PATH is set, uses "../user" relative to that
        3. Otherwise, defaults to "data/user" relative to project root

        Returns:
            Path to the user data JSON file
        """
        # Get user data directory from environment variable or use default
        user_data_dir = os.getenv(f"{self._env_prefix}_USER_DATA_PATH")

        if user_data_dir:
            return Path(user_data_dir) / f"{self.endpoint}.json"

        # Check if synthetic data path is set - derive user path from it
        synthetic_data_dir = os.getenv(self.synthetic_data_path_env_var)
        if synthetic_data_dir:
            # Use "../user" relative to synthetic data path
            return Path(synthetic_data_dir).parent / "user" / f"{self.endpoint}.json"

        # Find the data directory (relative to project root)
        current_path = Path(__file__).resolve()
        project_root = current_path.parent.parent
        return project_root / "data" / "user" / f"{self.endpoint}.json"

    def _load_user_data(self) -> list[dict[str, Any]]:
        """Load user-created data if it exists.

        Also updates the cached modification time of the user data file.

        Returns:
            List of user data entries, or empty list if file doesn't exist
        """
        user_data_file = self._get_user_data_file_path()

        if not user_data_file.exists():
            logger.debug(f"No user data file found at {user_data_file}")
            self._user_file_mtime = None
            return []

        try:
            # Get the modification time before loading
            self._user_file_mtime = user_data_file.stat().st_mtime

            with open(user_data_file) as f:
                user_data = json.load(f)
            logger.info(f"Loaded {len(user_data)} user entries from {user_data_file}")
            return user_data
        except (OSError, json.JSONDecodeError) as e:
            logger.warning(f"Could not load user data from {user_data_file}: {e}")
            self._user_file_mtime = None
            return []

    def _should_reload_user_data(self) -> bool:
        """Check if the user data file has changed since last load.

        Returns:
            True if the user data file has been modified, False otherwise
        """
        # If we're using in-memory data only, never reload
        if self._in_memory_data is not None:
            return False

        user_data_file = self._get_user_data_file_path()

        # If file doesn't exist now but we had data before, reload
        if not user_data_file.exists():
            return self._user_file_mtime is not None

        try:
            current_mtime = user_data_file.stat().st_mtime

            # If we've never loaded the user data file but it exists now,
            # we need to reload to pick up the new file
            if self._user_file_mtime is None:
                logger.info(
                    f"User data file {user_data_file} now exists "
                    f"(mtime: {current_mtime}), reloading"
                )
                return True

            # Check if modification time has changed
            if current_mtime != self._user_file_mtime:
                logger.info(
                    f"User data file {user_data_file} has changed "
                    f"(mtime: {self._user_file_mtime} -> {current_mtime}), reloading"
                )
                return True

            return False
        except OSError:
            # If we can't stat the file, don't reload
            return False

    @abstractmethod
    def _load_base_data(self) -> list[dict[str, Any]]:
        """Load the base/system data for this repository.

        Subclasses must implement this method to load data from their specific
        source (e.g., synthetic data files, API responses, etc.).

        Returns:
            List of data entries from the base data source
        """
        ...

    def _load_data(self) -> None:
        """Load and merge base data with user data.

        Calls _load_base_data() to get the implementation-specific base data,
        then merges it with user data. User data entries override base data
        entries with matching keys.

        Raises:
            FileNotFoundError: If required data files don't exist
            json.JSONDecodeError: If JSON files are malformed
        """
        if self._data is not None:
            return  # Already loaded (either from memory or previous file load)

        # Load base data from implementation-specific source
        base_data = self._load_base_data()

        # Load user data
        user_data = self._load_user_data()

        # Merge: user data overrides base data for matching keys
        if user_data:
            # Build lookup of user data by params/lookup_key for deduplication
            user_keys = set()
            for entry in user_data:
                key = self._get_entry_key(entry)
                if key:
                    user_keys.add(key)

            # Filter out base entries that have matching user entries
            filtered_base = [
                entry for entry in base_data if self._get_entry_key(entry) not in user_keys
            ]

            self._data = filtered_base + user_data
            logger.info(
                f"Loaded {len(base_data)} base + {len(user_data)} user entries "
                f"({len(base_data) - len(filtered_base)} overridden) "
                f"= {len(self._data)} total for {self.endpoint}"
            )
        else:
            self._data = base_data
            if base_data:
                logger.info(f"Loaded {len(base_data)} base entries for {self.endpoint}")

    def _get_entry_key(self, entry: dict[str, Any]) -> tuple | None:
        """Get a hashable key for an entry for deduplication purposes.

        Args:
            entry: A data entry dict

        Returns:
            A tuple key based on params or lookup_key, or None if no key found
        """
        # Try lookup_key first (newer format)
        if "lookup_key" in entry and entry["lookup_key"]:
            return tuple(sorted(entry["lookup_key"].items()))

        # Fall back to params (legacy format)
        if "params" in entry and entry["params"]:
            return tuple(entry["params"])

        return None

    async def get(self, input_model: APIConfigurable) -> T | None:
        """Get the response for the given input model.

        This method searches through the loaded JSON data to find an entry
        where the lookup_key or params array matches the input model's parameters.
        When a match is found, the corresponding response is parsed into a Pydantic
        model and returned.

        If the entry contains an error instead of a response, this method will
        raise a ValueError with the error information, simulating the original
        API error.

        Automatically reloads data if the user data file has been modified since
        the last load.

        Args:
            input_model: An input model implementing APIConfigurable protocol

        Returns:
            The response model instance if a match is found, None otherwise

        Raises:
            ValueError: If the matched entry contains an error

        Example:
            >>> from models import ExploreRequest, ExploreResponse
            >>> repo = DataRepository(
            ...     "explores", ExploreResponse, input_class=ExploreRequest
            ... )
            >>> input_model = ExploreRequest(model="ecommerce", explore="order_items")
            >>> response = await repo.get(input_model)
            >>> print(response.name)
            'order_items'
        """
        # Check if user data file has changed and reload if necessary
        if self._should_reload_user_data():
            self._data = None  # Force reload

        # Lazy load the data on first get call
        if self._data is None:
            self._load_data()

        # Get input model parameters for backward compatibility with params format
        if hasattr(input_model, "to_params"):
            input_params = input_model.to_params()
        else:
            input_params = None

        # Search for matching entry
        for entry in self._data:
            # Try new lookup_key format first
            lookup_key = entry.get("lookup_key")

            if lookup_key is not None:
                # New format: use matches() method
                if hasattr(input_model, "matches") and callable(input_model.matches):
                    matched = input_model.matches(lookup_key)
                else:
                    # Fallback: check if all fields in lookup_key match
                    matched = all(getattr(input_model, k, None) == v for k, v in lookup_key.items())
            else:
                # Old format: use params array
                params = entry.get("params")
                if params is not None and input_params is not None:
                    matched = params == input_params
                else:
                    # Empty lookup matches everything
                    matched = True

            if matched:
                # Check if this entry has an error
                error_data = entry.get("error")
                if error_data is not None:
                    # Rethrow the error as if it came from the API
                    error_msg = error_data.get("message", "Unknown error")
                    status_code = error_data.get("status_code")
                    if status_code:
                        raise ValueError(f"API error {status_code}: {error_msg}")
                    else:
                        error_type = error_data.get("error_type", "HTTPError")
                        raise ValueError(f"{error_type}: {error_msg}")

                # Otherwise, return the response
                response_data = entry.get("response")
                if response_data is not None:
                    return self.model_class.model_validate(response_data)
                return None

        return None

    def get_all(self) -> list[dict[str, Any]]:
        """Get all entries from the repository.

        Automatically reloads data if the user data file has been modified since
        the last load.

        Returns:
            A list of all data entries

        Example:
            >>> repo = DataRepository("rates", RateResponse)
            >>> all_data = repo.get_all()
            >>> len(all_data)
            5
        """
        # Check if user data file has changed and reload if necessary
        if self._should_reload_user_data():
            self._data = None  # Force reload

        if self._data is None:
            self._load_data()

        return self._data

    def reload(self) -> None:
        """Force reload the data.

        For file-based repositories, this reloads from the JSON file and merges with user data.
        For in-memory repositories, this reloads from the initial data.

        This can be useful if the underlying data source has been modified
        and you want to refresh the data without creating a new repository instance.
        """
        if self._in_memory_data is not None:
            # Reload from in-memory data
            if isinstance(self._in_memory_data, list):
                self._data = self._in_memory_data
            else:
                self._data = [{"lookup_key": {}, "response": self._in_memory_data}]
        else:
            # Reload from file (will merge synthetic + user data)
            self._data = None
            self._load_data()


class InMemoryDataRepository[T, InputT: APIConfigurable](DataRepository[T, InputT]):
    """Repository class for in-memory data access.

    This class is designed for repositories that store data in memory (passed via
    the `data` parameter during initialization) rather than loading from files.
    It's the proper implementation of DataRepository for in-memory use cases.

    Type Parameters:
        T: The Pydantic model type for responses
        InputT: The input model type (must implement APIConfigurable)

    Args:
        endpoint: The name of the endpoint (e.g., "lookml_models")
        model_class: The Pydantic model class to use for parsing responses
        data: In-memory data (dict or list). This is required for InMemoryDataRepository.
        input_class: The input model class (optional, for HTTP endpoint registration)
        env_prefix: Prefix for environment variables (default: "LOOKER")

    Example:
        >>> from models import LookMLModelRequest, LookMLModelResponse
        >>> from stores import MODELS
        >>> repo = InMemoryDataRepository(
        ...     "lookml_models",
        ...     LookMLModelResponse,
        ...     data={"models": [model.model_dump() for model in MODELS]},
        ...     input_class=LookMLModelRequest
        ... )
    """

    def __init__(
        self,
        endpoint: str,
        model_class: type[T],
        data: list[dict[str, Any]] | dict[str, Any],
        input_class: type[InputT] | None = None,
        env_prefix: str = "LOOKER",
    ) -> None:
        """Initialize the repository with in-memory data.

        Args:
            endpoint: The endpoint name (e.g., "lookml_models")
            model_class: The Pydantic model class for parsing responses
            data: In-memory data (required)
            input_class: The input model class (optional, for HTTP endpoint registration)
            env_prefix: Prefix for environment variables (default: "LOOKER")
        """
        if data is None:
            raise ValueError("InMemoryDataRepository requires data to be provided")
        super().__init__(endpoint, model_class, data, None, input_class, env_prefix)

    def _load_base_data(self) -> list[dict[str, Any]]:
        """Load base data from in-memory storage.

        Returns:
            Empty list since in-memory data is already loaded in __init__
        """
        # In-memory data is already loaded in the parent's __init__
        # This method should never be called for in-memory repositories
        # because _in_memory_data is set, so _load_data() returns early
        return []


class LiveDataRepository[T, InputT: APIConfigurable](Repository[T, InputT]):
    """Repository class for making live REST API calls.

    This class makes real-time API calls instead of loading from local JSON files.
    It requires a base URL and API token, which can be provided via constructor
    parameters or environment variables.

    For Looker API, you can provide an auth_service to handle OAuth2 token management.

    Type Parameters:
        T: The Pydantic model type for responses
        InputT: The input model type (must implement APIConfigurable)

    Args:
        endpoint: The API endpoint name (e.g., "rates")
        model_class: The Pydantic model class to use for parsing responses
        input_class: The input model class (optional, for HTTP endpoint registration)
        base_url: The base URL for the API (defaults to {env_prefix}_BASE_URL env var)
        api_token: The API token (defaults to {env_prefix}_API_KEY env var)
        auth_service: Optional auth service for dynamic token retrieval (Looker OAuth2)
        env_prefix: Prefix for environment variables (default: "LOOKER")
        user_agent: Custom User-Agent header
            (defaults to "{env_prefix.lower()}-mcp-server/{version}")

    Environment Variables:
        {env_prefix}_BASE_URL: Base URL for the API (e.g., "LOOKER_BASE_URL")
        {env_prefix}_API_KEY: API authentication token (e.g., "LOOKER_API_KEY")

    Example:
        >>> # Using environment variables
        >>> import os
        >>> os.environ["LOOKER_BASE_URL"] = "https://example.looker.com"
        >>> os.environ["LOOKER_API_KEY"] = "your_token_here"
        >>> repo = LiveDataRepository("lookml_models", LookMLModelResponse)
        >>>
        >>> # Or passing directly
        >>> repo = LiveDataRepository(
        ...     "lookml_models",
        ...     LookMLModelResponse,
        ...     base_url="https://example.looker.com",
        ...     api_token="your_token_here"
        ... )
    """

    def __init__(
        self,
        endpoint: str,
        model_class: type[T],
        input_class: type[InputT] | None = None,
        base_url: str | None = None,
        api_token: str | None = None,
        auth_service: "LookerAuthService | None" = None,
        env_prefix: str = "LOOKER",
        user_agent: str | None = None,
        api_version: str = "4.0",
    ) -> None:
        """Initialize the repository with endpoint, model class, and API config.

        Args:
            endpoint: The API endpoint name (e.g., "rates")
            model_class: The Pydantic model class for parsing responses
            input_class: The input model class (optional, for HTTP endpoint registration)
            base_url: The base URL for the API (defaults to {env_prefix}_BASE_URL env var)
            api_token: The API token (defaults to {env_prefix}_API_KEY env var)
            auth_service: Optional auth service for dynamic token retrieval (Looker OAuth2)
            env_prefix: Prefix for environment variables (default: "LOOKER")
            user_agent: Custom User-Agent header (defaults to "{appname}-mcp-server/{version}")
            api_version: API version for URL prefix (default: "4.0" for Looker API)
        """
        super().__init__(endpoint, model_class, input_class, env_prefix)
        # Get base_url from parameter or environment variable
        resolved_base_url = base_url or os.getenv(self.base_url_env_var)
        if not resolved_base_url:
            raise ValueError(
                f"Base URL required for live API requests. "
                f"Set the {self.base_url_env_var} environment variable "
                f"or pass base_url to constructor."
            )
        self.base_url = resolved_base_url.rstrip("/")
        self.api_version = api_version
        self.auth_service = auth_service
        self.api_token = api_token or os.getenv(self.api_key_env_var)

        # If no auth service and no token, raise error
        if not self.auth_service and not self.api_token:
            raise ValueError(
                f"API token or auth_service required for live API requests. "
                f"Set the {self.api_key_env_var} environment variable, "
                f"pass api_token to constructor, or provide an auth_service."
            )
        self.user_agent = user_agent or f"{env_prefix.lower()}-mcp-server/{_get_version()}"

    def _get_client(self) -> httpx.AsyncClient:
        """Get the shared HTTP client.

        Returns:
            The shared AsyncClient instance for making HTTP requests.
            This client is managed globally and should not be closed by repositories.
        """
        return get_http_client()

    async def close(self) -> None:
        """No-op for backwards compatibility.

        The HTTP client is now shared globally and managed by the server lifecycle.
        Individual repositories should not close it.
        """
        pass

    async def __aenter__(self):
        """Async context manager entry."""
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit."""
        # No cleanup needed - shared client is managed globally
        pass

    async def get(self, input_model: APIConfigurable) -> T | None:
        """Get the response by making a live API call.

        Args:
            input_model: An input model implementing APIConfigurable protocol

        Returns:
            The response model instance if successful, None if not found

        Raises:
            ValueError: If API call fails
        """
        return await self._make_request_from_config(input_model)

    async def _make_request_from_config(self, input_model: APIConfigurable) -> T:
        """Make an API request using the input model's configuration.

        Args:
            input_model: An input model implementing APIConfigurable protocol

        Returns:
            The parsed response model

        Raises:
            ValueError: If API request fails with any HTTP error
        """
        # Get API configuration from the input model
        api_config = input_model.get_api_config()
        url_template = api_config["url_template"]
        method = api_config.get("method", "GET").upper()
        body_template = api_config.get("body_template")

        # Get template values from the input model
        template_values = input_model.to_template_values()

        # Determine which fields go where:
        # 1. Path parameters: fields in url_template
        # 2. Body parameters (POST only): fields in body_template, or all
        #    non-path fields if no body_template
        # 3. Query parameters: remaining fields not in path or body

        # Extract field names used in URL path template (e.g., {model}, {explore})
        url_path_fields = set(re.findall(r"\{(\w+)\}", url_template))

        # Extract field names used in body template (if any)
        body_fields_set = set()
        if body_template:
            body_fields_set = set(re.findall(r"\{(\w+)\}", body_template))

        # Get all model fields
        model_dict = input_model.model_dump()

        # Separate fields by destination
        path_params = {k: v for k, v in template_values.items() if k in url_path_fields}

        if method == "POST" and not body_template:
            # POST without body_template: all non-path fields go in body
            body_params = {k: v for k, v in model_dict.items() if k not in url_path_fields}
            query_params = {}
        elif body_template:
            # Explicit body_template: fields in template go in body
            body_params = {k: v for k, v in template_values.items() if k in body_fields_set}
            # Remaining fields (not in path, not in body) go in query string
            query_params = {
                k: v
                for k, v in template_values.items()
                if k not in url_path_fields and k not in body_fields_set
            }
        else:
            # GET or POST with body_template: non-path fields go in query string
            body_params = {}
            query_params = {k: v for k, v in template_values.items() if k not in url_path_fields}

        # Build the URL path with path parameters
        # Use safe="" to encode ALL special characters including slashes
        # This prevents "Q3/2024" from becoming /content/Q3/2024 (malformed path)
        encoded_path_params = {
            key: quote(str(value), safe="") for key, value in path_params.items()
        }
        url_path = url_template.format(**encoded_path_params)

        # Build the full URL with API version prefix
        full_url = f"{self.base_url}/api/{self.api_version}{url_path}"
        if query_params:
            # URL-encode query parameters
            from urllib.parse import urlencode

            query_string = urlencode(query_params)
            full_url = f"{full_url}?{query_string}"

        # Get token (either from auth service or static token)
        if self.auth_service:
            access_token = await self.auth_service.get_access_token()
        else:
            access_token = self.api_token

        # Prepare headers
        headers = {
            "Authorization": f"Bearer {access_token}",
            "User-Agent": self.user_agent,
        }

        # Prepare request body if needed
        body = None
        if body_template:
            # Use explicit body template if provided
            body = body_template.format(**template_values)
            headers["Content-Type"] = "application/json"
        elif hasattr(input_model, "to_api_body"):
            # Use custom API body conversion if available
            # This handles cases where Pydantic serialization doesn't match API format
            # (e.g., Looker expects filters as dict, not list)
            body = json.dumps(input_model.to_api_body())
            headers["Content-Type"] = "application/json"
        elif body_params:
            # Auto-serialize body parameters as JSON
            body = json.dumps(body_params)
            headers["Content-Type"] = "application/json"

        # Make the API call using pooled client
        try:
            client = self._get_client()
            if method == "GET":
                response = await client.get(full_url, headers=headers, timeout=10.0)
            elif method == "POST":
                response = await client.post(full_url, headers=headers, content=body, timeout=10.0)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")

            response.raise_for_status()
            data = response.json()

            # If response_key is set, wrap the data in that key
            response_key = api_config.get("response_key")
            if response_key:
                if isinstance(data, list):
                    # Wrap list responses
                    wrapped = {response_key: data}
                    # Add total_count if the model expects it
                    if "total_count" in self.model_class.model_fields:
                        wrapped["total_count"] = len(data)
                    data = wrapped
                elif isinstance(data, dict) and response_key not in data:
                    # Wrap dict responses if response_key not already present
                    data = {response_key: data}

            # Mark online mode as verified on first successful call
            from config import _online_mode_verified, mark_online_verified

            if _online_mode_verified is None:
                mark_online_verified(True)

            return self.model_class.model_validate(data)
        except httpx.HTTPStatusError as e:
            error_msg = f"API error: {e.response.status_code}"
            self._handle_online_failure(error_msg)
            raise ValueError(f"{error_msg} - {e.response.text}") from e
        except httpx.RequestError as e:
            error_msg = f"Request failed: {str(e)}"
            self._handle_online_failure(error_msg)
            raise ValueError(error_msg) from e

    def _handle_online_failure(self, error: str) -> None:
        """Handle online mode failure - mark for fallback to offline."""
        from config import _online_mode_verified, mark_online_verified

        if _online_mode_verified is None:
            mark_online_verified(False, error)

    def get_all(self) -> list[dict[str, Any]]:
        """Get all entries is not supported for live API calls.

        Raises:
            NotImplementedError: This operation is not supported for live API
        """
        raise NotImplementedError("get_all() is not supported for LiveDataRepository")


class FunctionalRepository[T: BaseModel, InputT: BaseModel](Repository[T, InputT]):
    """Repository that wraps a service function for custom logic.

    This repository delegates to a service function that implements custom
    filtering, searching, sorting, or other business logic that doesn't fit
    the standard lookup_key pattern.

    Useful for offline mode where you need custom behavior like:
    - Complex filtering/searching
    - Dynamic sorting
    - Pagination
    - Data transformations

    Type Parameters:
        T: The Pydantic model type for responses
        InputT: The input model type

    Args:
        endpoint: Endpoint name for this repository
        model_class: Response model class
        input_class: Input/request model class
        func: Async function that implements the logic
        env_prefix: Prefix for environment variables (default: "LOOKER")

    Examples:
        >>> async def custom_logic(request: MyInput) -> MyResponse:
        ...     # Custom logic here
        ...     return MyResponse(...)
        >>> repo = FunctionalRepository("endpoint", MyResponse, MyInput, custom_logic)
    """

    def __init__(
        self,
        endpoint: str,
        model_class: type[T],
        input_class: type[InputT],
        func: Any,  # Async callable
        env_prefix: str = "LOOKER",
    ):
        """Initialize functional repository.

        Args:
            endpoint: Endpoint name for this repository
            model_class: Response model class
            input_class: Input/request model class
            func: Async function that implements the logic
            env_prefix: Prefix for environment variables (default: "LOOKER")
        """
        super().__init__(endpoint, model_class, input_class, env_prefix)
        self.func = func

    async def get(self, input_model: InputT) -> T | None:
        """Get data by calling the wrapped service function.

        Args:
            input_model: Input model with request parameters

        Returns:
            Response model instance or None if not found
        """
        return await self.func(input_model)

    def get_all(self) -> list[dict[str, Any]]:
        """Get all entries is not supported for FunctionalRepository.

        Raises:
            NotImplementedError: This operation is not supported
        """
        raise NotImplementedError("get_all() is not supported for FunctionalRepository")


class SyntheticDataRepository[T, InputT: APIConfigurable](DataRepository[T, InputT]):
    """Repository class for loading and querying synthetic JSON data.

    This class loads JSON files from the data/synthetic directory based on
    an endpoint name and provides a get method to query the data by matching
    parameter arrays. Responses are automatically parsed into Pydantic models.
    The data directory can be customized using the {env_prefix}_SYNTHETIC_DATA_PATH
    environment variable. If not set, it defaults to data/synthetic/ relative
    to the project root.

    This class uses the legacy to_params() matching method for backward compatibility.
    For new code, consider using DataRepository which supports both params and lookup_key.

    Type Parameters:
        T: The Pydantic model type for responses
        InputT: The input model type (must implement APIConfigurable)

    Args:
        endpoint: The name of the endpoint (e.g., "rates"). This will be used
                 to load the corresponding JSON file from data/synthetic/{endpoint}.json
        model_class: The Pydantic model class to use for parsing responses
        input_class: The input model class (optional, for HTTP endpoint registration)
        env_prefix: Prefix for environment variables (default: "LOOKER")

    Example:
        >>> from models import LookMLModelRequest, LookMLModelResponse
        >>> repo = SyntheticDataRepository("lookml_models", LookMLModelResponse, LookMLModelRequest)
        >>> input_model = LookMLModelRequest()
        >>> response = await repo.get(input_model)
    """

    def __init__(
        self,
        endpoint: str,
        model_class: type[T],
        input_class: type[InputT] | None = None,
        env_prefix: str = "LOOKER",
        data_path: Path | str | None = None,
    ) -> None:
        """Initialize the repository with an endpoint name and model class.

        Args:
            endpoint: The endpoint name (e.g., "rates")
            model_class: The Pydantic model class for parsing responses
            input_class: The input model class (optional, for HTTP endpoint registration)
            env_prefix: Prefix for environment variables (default: "LOOKER")
            data_path: Optional custom directory path for data files. If provided,
                      will load from {data_path}/{endpoint}.json instead of the default location.
        """
        super().__init__(endpoint, model_class, None, None, input_class, env_prefix)
        self._custom_data_path = Path(data_path) if data_path else None

    def _load_base_data(self) -> list[dict[str, Any]]:
        """Load synthetic data from the configured data directory.

        Returns:
            List of synthetic data entries (empty list if file/folder doesn't exist)
        """
        # Determine data file path based on priority:
        # 1. Custom data_path parameter (highest priority)
        # 2. Environment variable
        # 3. Default location (lowest priority)

        if self._custom_data_path:
            # Use custom path provided in constructor
            data_file = self._custom_data_path / f"{self.endpoint}.json"
        else:
            # Get synthetic data directory from environment variable or use default
            synthetic_data_dir = os.getenv(self.synthetic_data_path_env_var)

            if synthetic_data_dir:
                # Use custom path from environment variable
                data_file = Path(synthetic_data_dir) / f"{self.endpoint}.json"
            else:
                # Find the data directory (relative to project root)
                current_path = Path(__file__).resolve()
                project_root = current_path.parent.parent
                data_file = project_root / "data" / "synthetic" / f"{self.endpoint}.json"

        if not data_file.exists():
            logger.debug(f"Synthetic data file not found: {data_file}, returning empty data")
            return []

        with open(data_file) as f:
            data = json.load(f)

        self._data_file = data_file
        logger.info(f"Loaded {len(data)} synthetic entries from {data_file}")
        return data

    # Note: SyntheticDataRepository uses the parent DataRepository.get() method
    # which handles both "params" format (legacy synthetic data) and
    # "lookup_key" format (user data created by CreateDataRepository).
    # The parent's get() method also uses input_model.matches() when available,
    # which is necessary for flexible matching (e.g., OrderGetInput matching
    # orders created with OrderInput).

    def get_all(self) -> list[dict[str, Any]]:
        """Get all entries from the repository.

        Automatically reloads data if the user data file has been modified since
        the last load.

        Returns:
            A list of all data entries

        Example:
            >>> repo = SyntheticDataRepository("rates", RateResponse)
            >>> all_data = repo.get_all()
            >>> len(all_data)
            5
        """
        # Check if user data file has changed and reload if necessary
        if self._should_reload_user_data():
            self._data = None  # Force reload

        if self._data is None:
            self._load_data()

        return self._data

    def reload(self) -> None:
        """Force reload of data from the JSON file.

        This clears the cached data and immediately reloads it from disk.
        Useful for testing or when the underlying data file has changed.

        Example:
            >>> repo = SyntheticDataRepository("rates", RateResponse)
            >>> repo.reload()  # Force reload from disk
        """
        self._data = None
        self._data_file = None
        self._load_data()

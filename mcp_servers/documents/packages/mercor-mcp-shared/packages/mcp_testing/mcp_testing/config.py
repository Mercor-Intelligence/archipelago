"""Configuration for flexible acceptance testing.

Allows overriding auto-generated test cases for APIs with non-standard patterns.
"""

from pydantic import BaseModel, Field


class TestCaseConfig(BaseModel):
    """Configuration for which test cases to generate."""

    # Success case variations
    generate_empty_results: bool = Field(
        True, description="Try to generate empty result cases with filters"
    )
    generate_pagination: bool = Field(True, description="Generate pagination test cases")
    generate_sorting: bool = Field(True, description="Generate sorting test cases")

    # Error case variations
    generate_400_errors: bool = Field(True, description="Generate 400 Bad Request cases")
    generate_401_errors: bool = Field(True, description="Generate 401 Unauthorized cases")
    generate_403_errors: bool = Field(True, description="Generate 403 Forbidden cases")
    generate_404_errors: bool = Field(True, description="Generate 404 Not Found cases")
    generate_422_errors: bool = Field(True, description="Generate 422 Validation cases")

    # Edge cases
    generate_edge_cases: bool = Field(True, description="Generate edge case tests")

    # API-specific behavior
    supports_query_params: bool = Field(
        True, description="API supports query parameters (filter, limit, etc.)"
    )
    supports_pagination: bool = Field(True, description="API supports limit/offset pagination")
    supports_sorting: bool = Field(True, description="API supports sorting")

    # Custom parameter names (for non-standard APIs)
    limit_param_name: str = Field("limit", description="Name of limit parameter")
    offset_param_name: str = Field("offset", description="Name of offset parameter")
    sort_param_name: str = Field("sort", description="Name of sort parameter")
    filter_param_name: str = Field("filter", description="Name of filter parameter")


class EndpointConfig(BaseModel):
    """Configuration for a specific endpoint."""

    path: str = Field(..., description="Endpoint path (e.g., /users)")
    methods: list[str] = Field(["GET"], description="HTTP methods to test")

    # Override test case generation for this endpoint
    test_config: TestCaseConfig | None = Field(
        None, description="Override default test case generation"
    )

    # Required parameters (API won't work without these)
    required_params: dict[str, str] = Field({}, description="Parameters required for all requests")

    # Custom test cases (in addition to auto-generated)
    custom_success_cases: list[dict] = Field([], description="Additional success test cases")
    custom_error_cases: list[dict] = Field([], description="Additional error test cases")

    # Skip auto-generation and only use custom cases
    skip_auto_generation: bool = Field(
        False, description="Skip auto-generated tests, only use custom"
    )


class APIConfig(BaseModel):
    """Configuration for API-specific behavior."""

    # API characteristics
    is_rest: bool = Field(True, description="Is this a REST API?")
    is_graphql: bool = Field(False, description="Is this a GraphQL API?")

    # Authentication
    auth_type: str = Field("bearer", description="Auth type: bearer, api_key, oauth, basic, custom")
    auth_header_name: str = Field("Authorization", description="Auth header name")

    # Rate limiting
    has_rate_limiting: bool = Field(True, description="API has rate limiting")
    requests_per_second: int | None = Field(None, description="Max requests per second")

    # Error handling
    error_format: str = Field("standard", description="Error format: standard, custom, graphql")


class AcceptanceTestConfig(BaseModel):
    """Complete configuration for acceptance testing."""

    api: APIConfig = Field(default_factory=APIConfig)
    default_test_config: TestCaseConfig = Field(default_factory=TestCaseConfig)
    endpoints: list[EndpointConfig] = Field([], description="Endpoint configurations")

    # Fallback behavior
    continue_on_error: bool = Field(True, description="Continue testing even if some cases fail")
    max_retries: int = Field(3, description="Max retries for failed requests")
    retry_delay: float = Field(1.0, description="Delay between retries (seconds)")


def load_config(config_file: str = "mcp_testing.yaml") -> AcceptanceTestConfig:
    """Load configuration from YAML file with fallbacks."""
    from pathlib import Path

    config_path = Path(config_file)

    if not config_path.exists():
        # Return default config if file doesn't exist
        return AcceptanceTestConfig()

    try:
        import yaml

        with open(config_path) as f:
            data = yaml.safe_load(f)
            return AcceptanceTestConfig.model_validate(data)
    except Exception as e:
        print(f"[WARN] Could not load config from {config_file}: {e}")
        print("[WARN] Using default configuration")
        return AcceptanceTestConfig()

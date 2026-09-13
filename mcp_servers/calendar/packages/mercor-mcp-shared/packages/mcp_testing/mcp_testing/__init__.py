"""MCP Testing Framework - Core testing utilities.

This package provides core testing infrastructure for validating MCP servers,
including:
- Comparison models and logic (APIComparator, ComparisonResult)
- Fixture models and validation (FixtureLoader, FixtureValidator, FixtureGenerator)
- Live API testing (HTTPClient, LiveAPIComparator)
- Mock testing (MockAPIClient, MockResponse)
- MCP client (MCPClient, MCPValidator)
"""

from .core.comparator import APIComparator
from .core.models import ComparisonResult, ValidationResult
from .fixtures.generator import FixtureGenerator
from .fixtures.loader import FixtureLoader
from .fixtures.validator import FixtureValidator
from .live_api.client import HTTPClient
from .live_api.comparator import LiveAPIComparator
from .mcp_client import MCPClient
from .mcp_validator import MCPValidator
from .mocking.mock_client import MockAPIClient, MockResponse

__version__ = "0.3.0"

__all__ = [
    # Core comparison
    "APIComparator",
    "ComparisonResult",
    "ValidationResult",
    # Fixtures
    "FixtureGenerator",
    "FixtureLoader",
    "FixtureValidator",
    # Live API testing
    "HTTPClient",
    "LiveAPIComparator",
    # MCP client
    "MCPClient",
    "MCPValidator",
    # Mocking
    "MockAPIClient",
    "MockResponse",
]

"""Test-Driven Development for __SNAKE_NAME__.

TDD WORKFLOW:
1. Run tests: uv run pytest tests/test___SNAKE_NAME__.py -v (RED - will fail)
2. Implement: Edit mcp_servers/__SNAKE_NAME__/tools/__SNAKE_NAME__.py (GREEN - pass)
3. Refactor: Improve code while keeping tests passing

TIPS:
- Update models.py to define your API spec
- Tests validate the spec automatically
- Pydantic ensures type safety
"""

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

# Add the server to path
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp_servers" / "__SNAKE_NAME__"))

from models import __PASCAL_NAME__Request, __PASCAL_NAME__Response
from tools.__SNAKE_NAME__ import __SNAKE_NAME__


class Test__PASCAL_NAME__Tool:
    """Test suite for __SNAKE_NAME__ with Pydantic validation."""

    @pytest.mark.asyncio
    async def test_basic_functionality(self):
        """Test that tool returns valid response for valid input."""
        # Arrange: Create a valid request
        request = __PASCAL_NAME__Request(input_param="test")

        # Act: Call the tool
        response = await __SNAKE_NAME__(request)

        # Assert: Response matches schema
        assert isinstance(response, __PASCAL_NAME__Response)
        assert response.result is not None

    @pytest.mark.asyncio
    async def test_validates_request_schema(self):
        """Test that invalid requests are rejected by Pydantic."""
        # Pydantic should reject invalid data
        with pytest.raises(ValidationError):
            __PASCAL_NAME__Request(input_param=123)  # Wrong type

    @pytest.mark.asyncio
    async def test_response_matches_schema(self):
        """Test that response conforms to Response schema."""
        request = __PASCAL_NAME__Request(input_param="test")
        response = await __SNAKE_NAME__(request)

        # Validate response can be serialized/deserialized
        json_data = response.model_dump()
        validated = __PASCAL_NAME__Response.model_validate(json_data)
        assert validated == response


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

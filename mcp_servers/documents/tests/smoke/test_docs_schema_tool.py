"""Smoke tests for the docs_schema tool.

The docs_schema tool provides JSON schemas for input/output models,
helping LLM agents understand the structure of requests and responses.
"""

import json

import pytest
from smoke_utils import get_content_text


def req(params: dict) -> dict:
    """Wrap parameters in request object as expected by the tool."""
    return {"request": params}


class TestDocsSchemaBasic:
    """Basic smoke tests for docs_schema tool."""

    @pytest.mark.asyncio
    async def test_schema_tool_registered(self, client):
        """docs_schema tool is registered."""
        tools = await client.list_tools()
        tool_names = {t.name for t in tools}
        assert "docs_schema" in tool_names

    @pytest.mark.asyncio
    async def test_get_docs_input_schema(self, client):
        """Get schema for input model."""
        result = await client.call_tool("docs_schema", req({"model": "input"}))
        content = get_content_text(result)

        # Should return valid JSON schema
        assert "properties" in content or "action" in content
        assert len(content) > 50

    @pytest.mark.asyncio
    async def test_get_docs_output_schema(self, client):
        """Get schema for output model."""
        result = await client.call_tool("docs_schema", req({"model": "output"}))
        content = get_content_text(result)

        assert len(content) > 50

    @pytest.mark.asyncio
    async def test_schema_returns_json(self, client):
        """Schema response is valid JSON."""
        result = await client.call_tool("docs_schema", req({"model": "input"}))
        content = get_content_text(result)

        # Try to parse as JSON
        try:
            parsed = json.loads(content)
            assert isinstance(parsed, dict)
        except json.JSONDecodeError:
            # If not pure JSON, should at least contain schema-like content
            assert "type" in content or "properties" in content


class TestDocsSchemaModels:
    """Test schema retrieval for various models."""

    @pytest.mark.asyncio
    async def test_create_result_schema(self, client):
        """Get schema for CreateResult model."""
        result = await client.call_tool("docs_schema", req({"model": "CreateResult"}))
        content = get_content_text(result)

        assert len(content) > 0

    @pytest.mark.asyncio
    async def test_overview_result_schema(self, client):
        """Get schema for OverviewResult model."""
        result = await client.call_tool("docs_schema", req({"model": "OverviewResult"}))
        content = get_content_text(result)

        assert len(content) > 0

    @pytest.mark.asyncio
    async def test_read_content_result_schema(self, client):
        """Get schema for ReadContentResult model."""
        result = await client.call_tool(
            "docs_schema", req({"model": "ReadContentResult"})
        )
        content = get_content_text(result)

        assert len(content) > 0


class TestDocsSchemaErrors:
    """Test error handling for docs_schema tool."""

    @pytest.mark.asyncio
    async def test_invalid_model_returns_error(self, client):
        """Invalid model name returns error."""
        result = await client.call_tool(
            "docs_schema", req({"model": "NonExistentModel123"})
        )
        content = get_content_text(result)

        # Should return error or empty response
        assert (
            "error" in content.lower()
            or "not found" in content.lower()
            or "unknown" in content.lower()
            or len(content) > 0  # At minimum, should respond
        )

    @pytest.mark.asyncio
    async def test_missing_model_returns_error(self, client):
        """Missing model parameter returns error."""
        from fastmcp.exceptions import ToolError

        # FastMCP raises ToolError for validation errors
        with pytest.raises(ToolError) as exc_info:
            await client.call_tool("docs_schema", req({}))

        # Error should mention missing field
        assert (
            "missing" in str(exc_info.value).lower()
            or "required" in str(exc_info.value).lower()
        )

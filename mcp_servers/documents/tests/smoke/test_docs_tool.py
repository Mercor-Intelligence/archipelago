"""Smoke tests for the docs meta-tool.

These tests verify that each action of the docs tool works correctly
when called through the MCP protocol using FastMCP's in-process client.
"""

import pytest
from fastmcp.exceptions import ToolError
from smoke_utils import get_content_text


def req(params: dict) -> dict:
    """Wrap parameters in request object as expected by the tool."""
    return {"request": params}


class TestServerHealth:
    """Verify MCP server is healthy and tools are registered."""

    @pytest.mark.asyncio
    async def test_server_connects(self, client):
        """Server accepts connections."""
        assert client.is_connected()

    @pytest.mark.asyncio
    async def test_list_tools_returns_docs(self, client):
        """docs tool is registered."""
        tools = await client.list_tools()
        tool_names = {t.name for t in tools}
        assert "docs" in tool_names

    @pytest.mark.asyncio
    async def test_list_tools_returns_docs_schema(self, client):
        """docs_schema tool is registered."""
        tools = await client.list_tools()
        tool_names = {t.name for t in tools}
        assert "docs_schema" in tool_names

    @pytest.mark.asyncio
    async def test_tools_have_valid_schemas(self, client):
        """Each tool has name, description, and input schema."""
        tools = await client.list_tools()
        for tool in tools:
            assert tool.name, "Tool must have a name"
            assert tool.description, f"Tool {tool.name} must have description"
            assert tool.inputSchema, f"Tool {tool.name} must have input schema"


class TestDocsHelp:
    """Smoke tests for docs help action."""

    @pytest.mark.asyncio
    async def test_help_returns_action_list(self, client):
        """Help action returns list of available actions."""
        result = await client.call_tool("docs", req({"action": "help"}))
        content = get_content_text(result)

        expected_actions = [
            "create",
            "delete",
            "overview",
            "read_content",
            "add_text",
            "edit_text",
            "delete_text",
            "format",
            "add_image",
            "modify_image",
            "read_image",
        ]
        for action in expected_actions:
            assert action in content.lower(), f"Action '{action}' not in help"


class TestDocsCreate:
    """Smoke tests for docs create action."""

    @pytest.mark.asyncio
    async def test_create_with_content(self, client, smoke_test_root):
        """Create document with content."""
        await client.call_tool(
            "docs",
            req(
                {
                    "action": "create",
                    "directory": "/",
                    "file_name": "new_doc.docx",
                    "content": [{"type": "paragraph", "text": "Hello World"}],
                }
            ),
        )
        assert (smoke_test_root / "new_doc.docx").exists()

    @pytest.mark.asyncio
    async def test_create_with_heading(self, client, smoke_test_root):
        """Create document with heading."""
        await client.call_tool(
            "docs",
            req(
                {
                    "action": "create",
                    "directory": "/",
                    "file_name": "heading.docx",
                    "content": [{"type": "heading", "text": "Title", "level": 1}],
                }
            ),
        )
        assert (smoke_test_root / "heading.docx").exists()

    @pytest.mark.asyncio
    async def test_create_with_table(self, client, smoke_test_root):
        """Create document with table."""
        await client.call_tool(
            "docs",
            req(
                {
                    "action": "create",
                    "directory": "/",
                    "file_name": "table.docx",
                    "content": [
                        {
                            "type": "table",
                            "rows": [["A", "B"], ["1", "2"]],
                            "header": True,
                        }
                    ],
                }
            ),
        )
        assert (smoke_test_root / "table.docx").exists()

    @pytest.mark.asyncio
    async def test_create_missing_content_returns_error(self, client):
        """Create without content returns error."""
        result = await client.call_tool(
            "docs",
            req({"action": "create", "directory": "/", "file_name": "test.docx"}),
        )
        content = get_content_text(result)
        assert "error" in content.lower() or "required" in content.lower()


class TestDocsDelete:
    """Smoke tests for docs delete action."""

    @pytest.mark.asyncio
    async def test_delete_existing_document(
        self, client, sample_document, smoke_test_root
    ):
        """Delete existing document succeeds."""
        assert (smoke_test_root / "sample.docx").exists()
        result = await client.call_tool(
            "docs", req({"action": "delete", "file_path": sample_document})
        )
        content = get_content_text(result)
        assert "success" in content.lower() or "deleted" in content.lower()

    @pytest.mark.asyncio
    async def test_delete_nonexistent_returns_error(self, client):
        """Delete nonexistent document returns error."""
        result = await client.call_tool(
            "docs", req({"action": "delete", "file_path": "/does_not_exist.docx"})
        )
        content = get_content_text(result)
        assert "error" in content.lower() or "not found" in content.lower()


class TestDocsOverview:
    """Smoke tests for docs overview action."""

    @pytest.mark.asyncio
    async def test_overview_returns_structure(self, client, sample_document):
        """Overview returns document structure."""
        result = await client.call_tool(
            "docs", req({"action": "overview", "file_path": sample_document})
        )
        content = get_content_text(result)
        assert len(content) > 50


class TestDocsReadContent:
    """Smoke tests for docs read_content action."""

    @pytest.mark.asyncio
    async def test_read_full_document(self, client, sample_document):
        """Read full document content."""
        result = await client.call_tool(
            "docs", req({"action": "read_content", "file_path": sample_document})
        )
        content = get_content_text(result)
        assert len(content) > 100

    @pytest.mark.asyncio
    async def test_read_specific_identifier(self, client, sample_document):
        """Read content at specific identifier."""
        result = await client.call_tool(
            "docs",
            req(
                {
                    "action": "read_content",
                    "file_path": sample_document,
                    "identifier": "body.p.0",
                }
            ),
        )
        content = get_content_text(result)
        assert len(content) > 0


class TestDocsAddText:
    """Smoke tests for docs add_text action."""

    @pytest.mark.asyncio
    async def test_add_text_after_paragraph(self, client, sample_document):
        """Add text after existing paragraph."""
        result = await client.call_tool(
            "docs",
            req(
                {
                    "action": "add_text",
                    "file_path": sample_document,
                    "identifier": "body.p.0",
                    "position": "after",
                    "text": "Added via smoke test.",
                }
            ),
        )
        content = get_content_text(result)
        assert "success" in content.lower() or "added" in content.lower()


class TestDocsEditText:
    """Smoke tests for docs edit_text action."""

    @pytest.mark.asyncio
    async def test_edit_text_replace(self, client, sample_document):
        """Edit/replace text in document."""
        result = await client.call_tool(
            "docs",
            req(
                {
                    "action": "edit_text",
                    "file_path": sample_document,
                    "identifier": "body.p.0",
                    "new_text": "Modified text content",
                }
            ),
        )
        content = get_content_text(result)
        assert len(content) > 0


class TestDocsDeleteText:
    """Smoke tests for docs delete_text action."""

    @pytest.mark.asyncio
    async def test_delete_paragraph(self, client, sample_document):
        """Delete paragraph from document."""
        result = await client.call_tool(
            "docs",
            req(
                {
                    "action": "delete_text",
                    "file_path": sample_document,
                    "identifier": "body.p.2",
                }
            ),
        )
        content = get_content_text(result)
        assert len(content) > 0


class TestDocsFormat:
    """Smoke tests for docs format action."""

    @pytest.mark.asyncio
    async def test_apply_bold(self, client, sample_document):
        """Apply bold formatting."""
        result = await client.call_tool(
            "docs",
            req(
                {
                    "action": "format",
                    "file_path": sample_document,
                    "identifier": "body.p.0",
                    "bold": True,
                }
            ),
        )
        content = get_content_text(result)
        assert len(content) > 0

    @pytest.mark.asyncio
    async def test_apply_font_size(self, client, sample_document):
        """Apply font size."""
        result = await client.call_tool(
            "docs",
            req(
                {
                    "action": "format",
                    "file_path": sample_document,
                    "identifier": "body.p.0",
                    "font_size": 14,
                }
            ),
        )
        content = get_content_text(result)
        assert len(content) > 0


class TestDocsAddImage:
    """Smoke tests for docs add_image action."""

    @pytest.mark.asyncio
    async def test_add_image_to_document(self, client, sample_document, sample_image):
        """Add image to document."""
        result = await client.call_tool(
            "docs",
            req(
                {
                    "action": "add_image",
                    "file_path": sample_document,
                    "identifier": "body.p.0",
                    "position": "after",
                    "image_path": sample_image,
                    "width": 3.0,
                    "height": 2.0,
                }
            ),
        )
        content = get_content_text(result)
        assert (
            "image" in content.lower() or "added" in content.lower() or len(content) > 0
        )


class TestDocsModifyImage:
    """Smoke tests for docs modify_image action."""

    @pytest.mark.asyncio
    async def test_rotate_image(self, client, document_with_images):
        """Rotate image in document."""
        result = await client.call_tool(
            "docs",
            req(
                {
                    "action": "modify_image",
                    "file_path": document_with_images,
                    "identifier": "body",
                    "image_index": 0,
                    "operation": "rotate",
                    "rotation": 90,
                }
            ),
        )
        content = get_content_text(result)
        assert len(content) > 0

    @pytest.mark.asyncio
    async def test_flip_image(self, client, document_with_images):
        """Flip image horizontally."""
        result = await client.call_tool(
            "docs",
            req(
                {
                    "action": "modify_image",
                    "file_path": document_with_images,
                    "identifier": "body",
                    "image_index": 0,
                    "operation": "flip",
                    "flip": "horizontal",
                }
            ),
        )
        content = get_content_text(result)
        assert len(content) > 0

    @pytest.mark.asyncio
    async def test_adjust_brightness(self, client, document_with_images):
        """Adjust image brightness."""
        result = await client.call_tool(
            "docs",
            req(
                {
                    "action": "modify_image",
                    "file_path": document_with_images,
                    "identifier": "body",
                    "image_index": 0,
                    "operation": "brightness",
                    "brightness": 1.2,
                }
            ),
        )
        content = get_content_text(result)
        assert len(content) > 0


class TestDocsReadImage:
    """Smoke tests for docs read_image action."""

    @pytest.mark.asyncio
    async def test_read_image_from_document(self, client, document_with_images):
        """Read image from document."""
        result = await client.call_tool(
            "docs",
            req(
                {
                    "action": "read_image",
                    "file_path": document_with_images,
                    "annotation": "body.img.0",
                }
            ),
        )
        content = get_content_text(result)
        assert len(content) > 0


class TestDocsInvalidAction:
    """Test invalid action handling."""

    @pytest.mark.asyncio
    async def test_invalid_action_returns_error(self, client):
        """Invalid action returns error."""
        with pytest.raises(ToolError):
            await client.call_tool("docs", req({"action": "invalid_xyz"}))

    @pytest.mark.asyncio
    async def test_missing_action_returns_error(self, client):
        """Missing action returns error."""
        with pytest.raises(ToolError):
            await client.call_tool("docs", req({}))

"""Unit tests for delete_document tool."""

import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(
    0, str(Path(__file__).parent.parent.parent / "mcp_servers" / "docs_server")
)

from tools.create_document import create_document
from tools.delete_document import delete_document

from .helpers.assertions import (
    assert_document_created,
    assert_document_deleted,
    assert_error,
)


class TestDeleteDocument:
    """Test document deletion functionality."""

    @pytest.mark.asyncio
    async def test_delete_existing_document(self, mock_docs_root, sample_basic_content):
        """Test deleting an existing document."""
        create_result = await create_document(
            directory="/", file_name="delete_me.docx", content=sample_basic_content
        )
        assert_document_created(create_result, "delete_me.docx")

        result = await delete_document("/delete_me.docx")
        assert_document_deleted(result, "/delete_me.docx")

    @pytest.mark.asyncio
    async def test_delete_nonexistent_document(self, mock_docs_root):
        """Test deleting non-existent document."""
        result = await delete_document("/nonexistent.docx")
        assert_error(result, "not found")

    @pytest.mark.asyncio
    async def test_delete_invalid_path(self, mock_docs_root):
        """Test deleting with invalid path."""
        result = await delete_document("no_slash.docx")
        assert_error(result, "must start with")

    @pytest.mark.asyncio
    async def test_delete_wrong_extension(self, mock_docs_root):
        """Test deleting file with wrong extension."""
        result = await delete_document("/test.txt")
        assert_error(result, "must end with")

    @pytest.mark.asyncio
    async def test_delete_empty_file_path(self, mock_docs_root):
        """Test deleting with empty file path."""
        result = await delete_document("")
        assert_error(result, "required")

    @pytest.mark.asyncio
    async def test_delete_not_a_file(self, mock_docs_root):
        """Test deleting when path is a directory."""
        import os

        dir_path = os.path.join(mock_docs_root, "test_dir.docx")
        os.makedirs(dir_path, exist_ok=True)

        result = await delete_document("/test_dir.docx")
        assert_error(result, "Not a file")

    @pytest.mark.asyncio
    async def test_delete_multiple_documents(
        self, mock_docs_root, sample_basic_content
    ):
        """Test deleting multiple documents."""
        # Create multiple documents
        for i in range(3):
            create_result = await create_document(
                directory="/",
                file_name=f"delete_{i}.docx",
                content=sample_basic_content,
            )
            assert_document_created(create_result, f"delete_{i}.docx")

        # Delete all of them
        for i in range(3):
            result = await delete_document(f"/delete_{i}.docx")
            assert_document_deleted(result, f"/delete_{i}.docx")

    @pytest.mark.asyncio
    async def test_delete_in_subdirectory(self, mock_docs_root, sample_basic_content):
        """Test deleting document in subdirectory."""
        import os

        subdir = os.path.join(mock_docs_root, "subdir")
        os.makedirs(subdir, exist_ok=True)

        create_result = await create_document(
            directory="/subdir", file_name="nested.docx", content=sample_basic_content
        )
        assert_document_created(create_result, "nested.docx")

        result = await delete_document("/subdir/nested.docx")
        assert_document_deleted(result, "/subdir/nested.docx")

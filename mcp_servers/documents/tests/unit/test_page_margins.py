"""Tests for page_margins tool."""

from pathlib import Path

import pytest
from docx import Document
from tools.page_margins import page_margins


def _create_doc(mock_docs_root: str, filename: str) -> str:
    full_path = Path(mock_docs_root) / filename
    doc = Document()
    doc.add_paragraph("Body text")
    doc.save(str(full_path))
    return f"/{filename}"


@pytest.mark.asyncio
async def test_page_margins_read(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "margins_read.docx")

    result = await page_margins(file_path, action="read")
    assert "Status: success" in result
    assert "Top:" in result
    assert "Bottom:" in result
    assert "Left:" in result
    assert "Right:" in result


@pytest.mark.asyncio
async def test_page_margins_set_all(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "margins_set_all.docx")

    result = await page_margins(
        file_path, action="set", top=1.0, bottom=1.0, left=1.0, right=1.0
    )
    assert "Status: success" in result


@pytest.mark.asyncio
async def test_page_margins_set_top_only(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "margins_set_top.docx")

    result = await page_margins(file_path, action="set", top=2.0)
    assert "Status: success" in result


@pytest.mark.asyncio
async def test_page_margins_set_bottom_only(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "margins_set_bottom.docx")

    result = await page_margins(file_path, action="set", bottom=1.5)
    assert "Status: success" in result


@pytest.mark.asyncio
async def test_page_margins_set_left_only(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "margins_set_left.docx")

    result = await page_margins(file_path, action="set", left=0.75)
    assert "Status: success" in result


@pytest.mark.asyncio
async def test_page_margins_set_right_only(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "margins_set_right.docx")

    result = await page_margins(file_path, action="set", right=0.5)
    assert "Status: success" in result


@pytest.mark.asyncio
async def test_page_margins_validation_empty_path(mock_docs_root):
    result = await page_margins("", action="read")
    assert "required" in result.lower()


@pytest.mark.asyncio
async def test_page_margins_validation_no_slash(mock_docs_root):
    result = await page_margins("test.docx", action="read")
    assert "must start with /" in result


@pytest.mark.asyncio
async def test_page_margins_validation_wrong_extension(mock_docs_root):
    result = await page_margins("/test.txt", action="read")
    assert "must end with .docx" in result


@pytest.mark.asyncio
async def test_page_margins_validation_invalid_action(mock_docs_root):
    _create_doc(mock_docs_root, "margins_action.docx")
    result = await page_margins("/margins_action.docx", action="invalid")
    assert "Invalid action" in result


@pytest.mark.asyncio
async def test_page_margins_validation_set_no_margins(mock_docs_root):
    _create_doc(mock_docs_root, "margins_no_margins.docx")
    result = await page_margins("/margins_no_margins.docx", action="set")
    assert "At least one margin" in result


@pytest.mark.asyncio
async def test_page_margins_file_not_found(mock_docs_root):
    result = await page_margins("/nonexistent.docx", action="read")
    assert "File not found" in result


@pytest.mark.asyncio
async def test_page_margins_invalid_section(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "margins_section.docx")
    result = await page_margins(file_path, action="read", section_index=99)
    assert "Invalid section_index" in result


@pytest.mark.asyncio
async def test_page_margins_not_a_file(mock_docs_root):
    # Create a directory instead of a file
    dir_path = Path(mock_docs_root) / "not_a_file.docx"
    dir_path.mkdir()
    result = await page_margins("/not_a_file.docx", action="read")
    assert "Not a file" in result


@pytest.mark.asyncio
async def test_page_margins_negative_margin_rejected(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "margins_negative.docx")
    result = await page_margins(file_path, action="set", top=-1.0)
    assert "cannot be negative" in result
    assert "top" in result


@pytest.mark.asyncio
async def test_page_margins_excessive_margin_rejected(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "margins_excessive.docx")
    result = await page_margins(file_path, action="set", left=25.0)
    assert "exceeds maximum" in result
    assert "left" in result


@pytest.mark.asyncio
async def test_page_margins_zero_margin_allowed(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "margins_zero.docx")
    result = await page_margins(file_path, action="set", top=0.0)
    assert "Status: success" in result

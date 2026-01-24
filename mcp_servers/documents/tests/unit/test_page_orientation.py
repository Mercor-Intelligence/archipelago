"""Tests for page_orientation tool."""

from pathlib import Path

import pytest
from docx import Document
from tools.page_orientation import page_orientation


def _create_doc(mock_docs_root: str, filename: str) -> str:
    full_path = Path(mock_docs_root) / filename
    doc = Document()
    doc.add_paragraph("Body text")
    doc.save(str(full_path))
    return f"/{filename}"


@pytest.mark.asyncio
async def test_page_orientation_read(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "orientation_read.docx")

    result = await page_orientation(file_path, action="read")
    assert "Status: success" in result
    assert "Orientation:" in result


@pytest.mark.asyncio
async def test_page_orientation_set_landscape(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "orientation_landscape.docx")

    result = await page_orientation(file_path, action="set", orientation="landscape")
    assert "Status: success" in result
    assert "landscape" in result


@pytest.mark.asyncio
async def test_page_orientation_set_portrait(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "orientation_portrait.docx")

    # First set to landscape, then back to portrait
    await page_orientation(file_path, action="set", orientation="landscape")
    result = await page_orientation(file_path, action="set", orientation="portrait")
    assert "Status: success" in result
    assert "portrait" in result


@pytest.mark.asyncio
async def test_page_orientation_set_uppercase(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "orientation_upper.docx")

    result = await page_orientation(file_path, action="set", orientation="LANDSCAPE")
    assert "Status: success" in result


@pytest.mark.asyncio
async def test_page_orientation_validation_empty_path(mock_docs_root):
    result = await page_orientation("", action="read")
    assert "required" in result.lower()


@pytest.mark.asyncio
async def test_page_orientation_validation_no_slash(mock_docs_root):
    result = await page_orientation("test.docx", action="read")
    assert "must start with /" in result


@pytest.mark.asyncio
async def test_page_orientation_validation_wrong_extension(mock_docs_root):
    result = await page_orientation("/test.txt", action="read")
    assert "must end with .docx" in result


@pytest.mark.asyncio
async def test_page_orientation_validation_invalid_action(mock_docs_root):
    _create_doc(mock_docs_root, "orientation_action.docx")
    result = await page_orientation("/orientation_action.docx", action="invalid")
    assert "Invalid action" in result


@pytest.mark.asyncio
async def test_page_orientation_validation_set_no_orientation(mock_docs_root):
    _create_doc(mock_docs_root, "orientation_no_orient.docx")
    result = await page_orientation("/orientation_no_orient.docx", action="set")
    assert "required" in result.lower()


@pytest.mark.asyncio
async def test_page_orientation_validation_invalid_orientation(mock_docs_root):
    _create_doc(mock_docs_root, "orientation_invalid.docx")
    result = await page_orientation(
        "/orientation_invalid.docx", action="set", orientation="diagonal"
    )
    assert "Invalid orientation" in result


@pytest.mark.asyncio
async def test_page_orientation_file_not_found(mock_docs_root):
    result = await page_orientation("/nonexistent.docx", action="read")
    assert "File not found" in result


@pytest.mark.asyncio
async def test_page_orientation_invalid_section(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "orientation_section.docx")
    result = await page_orientation(file_path, action="read", section_index=99)
    assert "Invalid section_index" in result


@pytest.mark.asyncio
async def test_page_orientation_not_a_file(mock_docs_root):
    dir_path = Path(mock_docs_root) / "not_a_file.docx"
    dir_path.mkdir()
    result = await page_orientation("/not_a_file.docx", action="read")
    assert "Not a file" in result

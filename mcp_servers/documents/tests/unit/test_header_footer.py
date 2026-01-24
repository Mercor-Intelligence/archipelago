"""Tests for header_footer tool."""

from pathlib import Path

import pytest
from docx import Document
from tools.header_footer import header_footer


def _create_doc(mock_docs_root: str, filename: str) -> str:
    full_path = Path(mock_docs_root) / filename
    doc = Document()
    doc.add_paragraph("Body text")
    doc.save(str(full_path))
    return f"/{filename}"


@pytest.mark.asyncio
async def test_header_footer_read_header(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "hf_read_header.docx")

    result = await header_footer(file_path, action="read", area="header")
    assert "Status: success" in result


@pytest.mark.asyncio
async def test_header_footer_read_footer(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "hf_read_footer.docx")

    result = await header_footer(file_path, action="read", area="footer")
    assert "Status: success" in result


@pytest.mark.asyncio
async def test_header_footer_set_header_paragraph(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "hf_set_para.docx")
    content = [{"type": "paragraph", "text": "Header text"}]

    result = await header_footer(
        file_path, action="set", area="header", content=content
    )
    assert "Status: success" in result
    assert "Blocks Added: 1" in result


@pytest.mark.asyncio
async def test_header_footer_set_footer_paragraph(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "hf_set_footer.docx")
    content = [{"type": "paragraph", "text": "Footer text"}]

    result = await header_footer(
        file_path, action="set", area="footer", content=content
    )
    assert "Status: success" in result


@pytest.mark.asyncio
async def test_header_footer_set_heading(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "hf_set_heading.docx")
    content = [{"type": "heading", "text": "Header Title", "level": 1}]

    result = await header_footer(
        file_path, action="set", area="header", content=content
    )
    assert "Status: success" in result


@pytest.mark.asyncio
async def test_header_footer_set_bullet_list(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "hf_set_bullet.docx")
    content = [{"type": "bullet_list", "items": ["Item 1", "Item 2"]}]

    result = await header_footer(
        file_path, action="set", area="header", content=content
    )
    assert "Status: success" in result


@pytest.mark.asyncio
async def test_header_footer_set_numbered_list(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "hf_set_numbered.docx")
    content = [{"type": "numbered_list", "items": ["First", "Second"]}]

    result = await header_footer(
        file_path, action="set", area="header", content=content
    )
    assert "Status: success" in result


@pytest.mark.asyncio
async def test_header_footer_set_table(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "hf_set_table.docx")
    # Tables in headers/footers work best with width specified
    content = [{"type": "table", "rows": [["A", "B"], ["1", "2"]], "width": 6.0}]

    result = await header_footer(
        file_path, action="set", area="header", content=content
    )
    assert "Status: success" in result


@pytest.mark.asyncio
async def test_header_footer_set_multiple_blocks(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "hf_set_multi.docx")
    content = [
        {"type": "paragraph", "text": "Line 1"},
        {"type": "paragraph", "text": "Line 2"},
    ]

    result = await header_footer(
        file_path, action="set", area="header", content=content
    )
    assert "Status: success" in result
    assert "Blocks Added: 2" in result


@pytest.mark.asyncio
async def test_header_footer_clear_header(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "hf_clear.docx")

    result = await header_footer(file_path, action="clear", area="header")
    assert "Status: success" in result


@pytest.mark.asyncio
async def test_header_footer_clear_footer(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "hf_clear_footer.docx")

    result = await header_footer(file_path, action="clear", area="footer")
    assert "Status: success" in result


@pytest.mark.asyncio
async def test_header_footer_link_unlink(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "hf_link.docx")

    result = await header_footer(
        file_path, action="link", area="footer", link_to_previous=False
    )
    assert "Status: success" in result


@pytest.mark.asyncio
async def test_header_footer_link_true(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "hf_link_true.docx")

    result = await header_footer(
        file_path, action="link", area="header", link_to_previous=True
    )
    # Section 0 cannot link to previous
    assert "Cannot link section 0" in result


@pytest.mark.asyncio
async def test_header_footer_validation_empty_path(mock_docs_root):
    result = await header_footer("", action="read", area="header")
    assert "required" in result.lower()


@pytest.mark.asyncio
async def test_header_footer_validation_no_slash(mock_docs_root):
    result = await header_footer("test.docx", action="read", area="header")
    assert "must start with /" in result


@pytest.mark.asyncio
async def test_header_footer_validation_wrong_extension(mock_docs_root):
    result = await header_footer("/test.txt", action="read", area="header")
    assert "must end with .docx" in result


@pytest.mark.asyncio
async def test_header_footer_validation_invalid_action(mock_docs_root):
    _create_doc(mock_docs_root, "hf_action.docx")
    result = await header_footer("/hf_action.docx", action="invalid", area="header")
    assert "Invalid action" in result


@pytest.mark.asyncio
async def test_header_footer_validation_invalid_area(mock_docs_root):
    _create_doc(mock_docs_root, "hf_area.docx")
    result = await header_footer("/hf_area.docx", action="read", area="invalid")
    assert "Invalid area" in result


@pytest.mark.asyncio
async def test_header_footer_validation_set_no_content(mock_docs_root):
    _create_doc(mock_docs_root, "hf_no_content.docx")
    result = await header_footer("/hf_no_content.docx", action="set", area="header")
    assert "Content is required" in result


@pytest.mark.asyncio
async def test_header_footer_validation_set_content_not_list(mock_docs_root):
    _create_doc(mock_docs_root, "hf_content_not_list.docx")
    result = await header_footer(
        "/hf_content_not_list.docx", action="set", area="header", content="not a list"
    )
    assert "must be a list" in result


@pytest.mark.asyncio
async def test_header_footer_validation_link_no_flag(mock_docs_root):
    _create_doc(mock_docs_root, "hf_link_no_flag.docx")
    result = await header_footer("/hf_link_no_flag.docx", action="link", area="header")
    assert "link_to_previous is required" in result


@pytest.mark.asyncio
async def test_header_footer_file_not_found(mock_docs_root):
    result = await header_footer("/nonexistent.docx", action="read", area="header")
    assert "File not found" in result


@pytest.mark.asyncio
async def test_header_footer_invalid_section(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "hf_section.docx")
    result = await header_footer(
        file_path, action="read", area="header", section_index=99
    )
    assert "Invalid section_index" in result


@pytest.mark.asyncio
async def test_header_footer_not_a_file(mock_docs_root):
    dir_path = Path(mock_docs_root) / "not_a_file.docx"
    dir_path.mkdir()
    result = await header_footer("/not_a_file.docx", action="read", area="header")
    assert "Not a file" in result


@pytest.mark.asyncio
async def test_header_footer_set_invalid_block_type(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "hf_invalid_block.docx")
    content = [{"type": "invalid_type", "text": "test"}]
    result = await header_footer(
        file_path, action="set", area="header", content=content
    )
    assert "Unknown block type" in result


@pytest.mark.asyncio
async def test_header_footer_set_block_no_type(mock_docs_root):
    file_path = _create_doc(mock_docs_root, "hf_no_type.docx")
    content = [{"text": "test"}]
    result = await header_footer(
        file_path, action="set", area="header", content=content
    )
    assert "'type'" in result


def _create_doc_with_header_content(mock_docs_root: str, filename: str) -> str:
    """Create a doc with content in the header including formatting."""
    full_path = Path(mock_docs_root) / filename
    doc = Document()
    doc.add_paragraph("Body text")

    # Access header and add content
    section = doc.sections[0]
    header = section.header
    p = header.add_paragraph()
    run = p.add_run("Bold text")
    run.bold = True
    run2 = p.add_run(" and italic")
    run2.italic = True

    doc.save(str(full_path))
    return f"/{filename}"


def _create_doc_with_header_table(mock_docs_root: str, filename: str) -> str:
    """Create a doc with a table in the header."""
    from docx.shared import Inches

    full_path = Path(mock_docs_root) / filename
    doc = Document()
    doc.add_paragraph("Body text")

    # Access header and add table (width required in headers)
    section = doc.sections[0]
    header = section.header
    table = header.add_table(rows=2, cols=2, width=Inches(6))
    table.cell(0, 0).text = "A"
    table.cell(0, 1).text = "B"
    table.cell(1, 0).text = "1"
    table.cell(1, 1).text = "2"

    doc.save(str(full_path))
    return f"/{filename}"


@pytest.mark.asyncio
async def test_header_footer_read_with_formatted_content(mock_docs_root):
    """Test reading header with formatted text to cover _serialize_run."""
    file_path = _create_doc_with_header_content(mock_docs_root, "hf_formatted.docx")

    result = await header_footer(file_path, action="read", area="header")
    assert "Status: success" in result


@pytest.mark.asyncio
async def test_header_footer_read_with_table(mock_docs_root):
    """Test reading header with table to cover _serialize_table."""
    file_path = _create_doc_with_header_table(mock_docs_root, "hf_table_read.docx")

    result = await header_footer(file_path, action="read", area="header")
    assert "Status: success" in result


@pytest.mark.asyncio
async def test_header_footer_set_paragraph_with_bold(mock_docs_root):
    """Test setting paragraph with bold formatting."""
    file_path = _create_doc(mock_docs_root, "hf_bold.docx")
    content = [{"type": "paragraph", "text": "Bold text", "bold": True}]

    result = await header_footer(
        file_path, action="set", area="header", content=content
    )
    assert "Status: success" in result


@pytest.mark.asyncio
async def test_header_footer_set_paragraph_with_italic(mock_docs_root):
    """Test setting paragraph with italic formatting."""
    file_path = _create_doc(mock_docs_root, "hf_italic.docx")
    content = [{"type": "paragraph", "text": "Italic text", "italic": True}]

    result = await header_footer(
        file_path, action="set", area="header", content=content
    )
    assert "Status: success" in result


@pytest.mark.asyncio
async def test_header_footer_set_paragraph_invalid_style(mock_docs_root):
    """Test setting paragraph with invalid style."""
    file_path = _create_doc(mock_docs_root, "hf_invalid_style.docx")
    content = [{"type": "paragraph", "text": "Text", "style": "NonExistentStyleXYZ123"}]

    result = await header_footer(
        file_path, action="set", area="header", content=content
    )
    assert "not defined" in result


@pytest.mark.asyncio
async def test_header_footer_set_table_empty_rows(mock_docs_root):
    """Test setting table with no rows."""
    file_path = _create_doc(mock_docs_root, "hf_empty_table.docx")
    content = [{"type": "table", "rows": []}]

    result = await header_footer(
        file_path, action="set", area="header", content=content
    )
    assert "at least one row" in result


@pytest.mark.asyncio
async def test_header_footer_set_table_empty_row(mock_docs_root):
    """Test setting table with an empty row."""
    file_path = _create_doc(mock_docs_root, "hf_empty_row.docx")
    content = [{"type": "table", "rows": [["A", "B"], []], "width": 6.0}]

    result = await header_footer(
        file_path, action="set", area="header", content=content
    )
    assert "at least one cell" in result


@pytest.mark.asyncio
async def test_header_footer_set_table_inconsistent_cols(mock_docs_root):
    """Test setting table with inconsistent column count."""
    file_path = _create_doc(mock_docs_root, "hf_inconsistent.docx")
    content = [{"type": "table", "rows": [["A", "B"], ["1"]], "width": 6.0}]

    result = await header_footer(
        file_path, action="set", area="header", content=content
    )
    assert "same number of cells" in result


@pytest.mark.asyncio
async def test_header_footer_set_then_clear(mock_docs_root):
    """Test setting content then clearing it."""
    file_path = _create_doc(mock_docs_root, "hf_set_clear.docx")

    # First set some content
    content = [{"type": "paragraph", "text": "To be cleared"}]
    result = await header_footer(
        file_path, action="set", area="header", content=content
    )
    assert "Status: success" in result

    # Now clear it
    result = await header_footer(file_path, action="clear", area="header")
    assert "Status: success" in result


@pytest.mark.asyncio
async def test_header_footer_set_table_without_width(mock_docs_root):
    """Test setting table without width - should fail with helpful message."""
    file_path = _create_doc(mock_docs_root, "hf_table_no_width.docx")
    content = [{"type": "table", "rows": [["A", "B"], ["1", "2"]]}]

    result = await header_footer(
        file_path, action="set", area="header", content=content
    )
    assert "width" in result.lower()
    assert "required" in result.lower()


@pytest.mark.asyncio
async def test_header_footer_set_table_negative_width(mock_docs_root):
    """Test setting table with negative width - should fail."""
    file_path = _create_doc(mock_docs_root, "hf_table_neg_width.docx")
    content = [{"type": "table", "rows": [["A", "B"], ["1", "2"]], "width": -1.0}]

    result = await header_footer(
        file_path, action="set", area="header", content=content
    )
    assert "must be positive" in result


@pytest.mark.asyncio
async def test_header_footer_set_table_with_style(mock_docs_root):
    """Test setting table with style and no header bolding."""
    file_path = _create_doc(mock_docs_root, "hf_table_style.docx")
    content = [
        {
            "type": "table",
            "rows": [["Col1", "Col2"], ["Data1", "Data2"]],
            "width": 5.0,
            "header": False,
        }
    ]

    result = await header_footer(
        file_path, action="set", area="header", content=content
    )
    assert "Status: success" in result


@pytest.mark.asyncio
async def test_header_footer_set_table_rows_not_list(mock_docs_root):
    """Test setting table with rows as string instead of list."""
    file_path = _create_doc(mock_docs_root, "hf_table_rows_str.docx")
    content = [{"type": "table", "rows": "not a list", "width": 6.0}]

    result = await header_footer(
        file_path, action="set", area="header", content=content
    )
    assert "must be a list" in result


@pytest.mark.asyncio
async def test_header_footer_set_table_row_not_list(mock_docs_root):
    """Test setting table with row as string instead of list."""
    file_path = _create_doc(mock_docs_root, "hf_table_row_str.docx")
    content = [{"type": "table", "rows": ["not a list", ["A", "B"]], "width": 6.0}]

    result = await header_footer(
        file_path, action="set", area="header", content=content
    )
    assert "must be" in result and "list" in result


@pytest.mark.asyncio
async def test_header_footer_set_bullet_list_items_not_list(mock_docs_root):
    """Test setting bullet list with items as string instead of list."""
    file_path = _create_doc(mock_docs_root, "hf_bullet_items_str.docx")
    content = [{"type": "bullet_list", "items": "not a list"}]

    result = await header_footer(
        file_path, action="set", area="header", content=content
    )
    assert "must be a list" in result


@pytest.mark.asyncio
async def test_header_footer_set_numbered_list_items_not_list(mock_docs_root):
    """Test setting numbered list with items as string instead of list."""
    file_path = _create_doc(mock_docs_root, "hf_numbered_items_str.docx")
    content = [{"type": "numbered_list", "items": "not a list"}]

    result = await header_footer(
        file_path, action="set", area="header", content=content
    )
    assert "must be a list" in result

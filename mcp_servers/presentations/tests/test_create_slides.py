import os
import tempfile
from pathlib import Path

import pytest
from pptx import Presentation


@pytest.fixture
def temp_slides_root():
    """Create a temporary directory for slide storage."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Set the environment variable to use the temp directory
        original_slides_root = os.environ.get("APP_SLIDES_ROOT")
        original_fs_root = os.environ.get("APP_FS_ROOT")

        os.environ["APP_SLIDES_ROOT"] = tmpdir

        yield tmpdir

        # Restore original environment variables
        if original_slides_root:
            os.environ["APP_SLIDES_ROOT"] = original_slides_root
        elif "APP_SLIDES_ROOT" in os.environ:
            del os.environ["APP_SLIDES_ROOT"]

        if original_fs_root:
            os.environ["APP_FS_ROOT"] = original_fs_root
        elif "APP_FS_ROOT" in os.environ:
            del os.environ["APP_FS_ROOT"]


@pytest.fixture
def create_deck_function(temp_slides_root):
    """Import and return the create_deck function with fresh module import."""
    import importlib
    import sys

    slides_server_path = Path(__file__).parent.parent / "mcp_servers" / "slides_server"
    if str(slides_server_path) not in sys.path:
        sys.path.insert(0, str(slides_server_path))

    # Force reload to pick up the new environment variable
    if "tools.create_slides" in sys.modules:
        importlib.reload(sys.modules["tools.create_slides"])

    from tools.create_slides import create_deck

    return create_deck


async def test_create_table_slide(temp_slides_root, create_deck_function):
    """Test creating a presentation with a table slide."""
    directory = "/"
    file_name = "table_test.pptx"
    slides = [
        {
            "layout": "title_and_content",
            "title": "Test Table",
            "table": {
                "rows": [["Name", "Value"], ["A", "100"], ["B", "200"]],
                "header": True,
            },
        }
    ]

    result = await create_deck_function(
        directory=directory, file_name=file_name, slides=slides, metadata=None
    )

    # Check structured response via model_dump()
    data = result.model_dump()
    assert data["success"] is True
    assert data["file_name"] == file_name
    assert data["error"] is None

    # Verify the file was created
    expected_path = Path(temp_slides_root) / file_name
    assert expected_path.exists(), f"File should exist at {expected_path}"

    # Verify the presentation content
    presentation = Presentation(str(expected_path))
    assert len(presentation.slides) == 1, "Should have exactly 1 slide"

    slide = presentation.slides[0]
    assert slide.shapes.title.text == "Test Table", "Slide title should match"

    tables = [shape for shape in slide.shapes if hasattr(shape, "table")]
    assert len(tables) == 1, "Should have exactly 1 table"

    table = tables[0].table
    assert len(table.rows) == 3, "Table should have 3 rows"
    assert len(table.columns) == 2, "Table should have 2 columns"
    assert table.cell(0, 0).text == "Name"
    assert table.cell(0, 1).text == "Value"
    assert table.cell(1, 0).text == "A"
    assert table.cell(1, 1).text == "100"
    assert table.cell(2, 0).text == "B"
    assert table.cell(2, 1).text == "200"

    # Check that header row is bold
    header_cell = table.cell(0, 0)
    is_bold = any(
        run.font.bold
        for paragraph in header_cell.text_frame.paragraphs
        for run in paragraph.runs
    )
    assert is_bold, "Header row should be bold"


async def test_create_table_without_header(temp_slides_root, create_deck_function):
    """Test creating a table with header=False."""
    directory = "/"
    file_name = "table_no_header.pptx"
    slides = [
        {
            "layout": "title_and_content",
            "title": "Table Without Header",
            "table": {
                "rows": [["Data1", "Data2"], ["Data3", "Data4"]],
                "header": False,
            },
        }
    ]

    result = await create_deck_function(
        directory=directory, file_name=file_name, slides=slides
    )

    data = result.model_dump()
    assert data["success"] is True
    assert data["file_name"] == file_name
    assert data["error"] is None

    # Load and verify
    expected_path = Path(temp_slides_root) / file_name
    presentation = Presentation(str(expected_path))
    slide = presentation.slides[0]

    tables = [shape for shape in slide.shapes if hasattr(shape, "table")]
    table = tables[0].table

    # Check that first row is NOT bold
    first_cell = table.cell(0, 0)
    has_bold = any(
        run.font.bold
        for paragraph in first_cell.text_frame.paragraphs
        for run in paragraph.runs
        if run.font.bold is not None
    )
    assert not has_bold or first_cell.text == "", (
        "First row should not be bold when header=False"
    )


async def test_create_multiple_slides_with_table(
    temp_slides_root, create_deck_function
):
    """Test creating multiple slides including a table slide."""
    directory = "/"
    file_name = "multi_slide_with_table.pptx"
    slides = [
        {
            "layout": "title",
            "title": "Title Slide",
            "subtitle": "A presentation with tables",
        },
        {
            "layout": "title_and_content",
            "title": "Test Table",
            "table": {
                "rows": [
                    ["Column 1", "Column 2", "Column 3"],
                    ["A", "B", "C"],
                    ["D", "E", "F"],
                ],
                "header": True,
            },
        },
        {
            "layout": "title_and_content",
            "title": "Summary",
            "bullets": {"items": ["Point 1", "Point 2"]},
        },
    ]

    result = await create_deck_function(
        directory=directory, file_name=file_name, slides=slides
    )

    data = result.model_dump()
    assert data["success"] is True
    assert data["file_name"] == file_name
    assert data["error"] is None

    # Verify the presentation
    expected_path = Path(temp_slides_root) / file_name
    presentation = Presentation(str(expected_path))

    assert len(presentation.slides) == 3, "Should have 3 slides"

    # Check the table slide (slide 1, 0-indexed)
    table_slide = presentation.slides[1]
    assert table_slide.shapes.title.text == "Test Table"

    tables = [shape for shape in table_slide.shapes if hasattr(shape, "table")]
    assert len(tables) == 1

    table = tables[0].table
    assert len(table.rows) == 3
    assert len(table.columns) == 3
    assert table.cell(0, 0).text == "Column 1"


async def test_invalid_table_rows(temp_slides_root, create_deck_function):
    """Test that validation catches invalid table rows (non-rectangular)."""
    directory = "/"
    file_name = "invalid_table.pptx"
    slides = [
        {
            "layout": "title_and_content",
            "title": "Invalid Table",
            "table": {
                "rows": [
                    ["A", "B"],
                    ["C"],  # This row has fewer columns
                ],
                "header": True,
            },
        }
    ]

    result = await create_deck_function(
        directory=directory, file_name=file_name, slides=slides
    )

    data = result.model_dump()
    assert data["success"] is False
    assert data["error"] is not None
    assert (
        "Invalid slides payload" in data["error"]
        or "must have the same number of cells" in data["error"]
    )


async def test_table_with_metadata(temp_slides_root, create_deck_function):
    """Test creating a table presentation with metadata."""
    directory = "/"
    file_name = "table_with_metadata.pptx"
    slides = [
        {
            "layout": "title_and_content",
            "title": "Test Table",
            "table": {"rows": [["Name", "Value"], ["A", "100"]], "header": True},
        }
    ]
    metadata = {
        "title": "Test Presentation",
        "author": "Test Author",
        "subject": "Testing Tables",
        "comments": "This is a test presentation with a table",
    }

    result = await create_deck_function(
        directory=directory, file_name=file_name, slides=slides, metadata=metadata
    )

    data = result.model_dump()
    assert data["success"] is True
    assert data["file_name"] == file_name
    assert data["error"] is None

    # Verify metadata
    expected_path = Path(temp_slides_root) / file_name
    presentation = Presentation(str(expected_path))

    core = presentation.core_properties
    assert core.title == "Test Presentation"
    assert core.author == "Test Author"
    assert core.subject == "Testing Tables"
    assert core.comments == "This is a test presentation with a table"


async def test_table_in_subdirectory(temp_slides_root, create_deck_function):
    """Test creating a table presentation in a subdirectory."""
    directory = "/reports"
    file_name = "quarterly_table.pptx"
    slides = [
        {
            "layout": "title_and_content",
            "title": "Quarterly Report",
            "table": {
                "rows": [["Quarter", "Revenue"], ["Q1", "$100K"], ["Q2", "$120K"]],
                "header": True,
            },
        }
    ]

    result = await create_deck_function(
        directory=directory, file_name=file_name, slides=slides
    )

    data = result.model_dump()
    assert data["success"] is True
    assert data["file_name"] == file_name
    assert data["error"] is None

    # Verify the file was created in the subdirectory
    expected_path = Path(temp_slides_root) / "reports" / file_name
    assert expected_path.exists(), f"File should exist at {expected_path}"

    # Verify content
    presentation = Presentation(str(expected_path))
    assert len(presentation.slides) == 1

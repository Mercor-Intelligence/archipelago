"""Tests for individual slide tools - delete, add_slide, edit, insert_table."""

import os
import sys
import tempfile
from pathlib import Path

import pytest
from pptx import Presentation

# Add the slides_server to the path
slides_server_path = Path(__file__).parent.parent / "mcp_servers" / "slides_server"
if str(slides_server_path) not in sys.path:
    sys.path.insert(0, str(slides_server_path))


@pytest.fixture
def temp_slides_root():
    """Create a temporary directory for slide storage."""
    with tempfile.TemporaryDirectory() as tmpdir:
        original_slides_root = os.environ.get("APP_SLIDES_ROOT")
        os.environ["APP_SLIDES_ROOT"] = tmpdir
        yield tmpdir
        if original_slides_root:
            os.environ["APP_SLIDES_ROOT"] = original_slides_root
        elif "APP_SLIDES_ROOT" in os.environ:
            del os.environ["APP_SLIDES_ROOT"]


@pytest.fixture
def create_function(temp_slides_root):
    """Import create_deck function."""
    import importlib

    if "tools.create_slides" in sys.modules:
        importlib.reload(sys.modules["tools.create_slides"])

    from tools.create_slides import create_deck

    return create_deck


@pytest.fixture
def delete_function(temp_slides_root):
    """Import delete_deck function."""
    import importlib

    if "tools.delete_slides" in sys.modules:
        importlib.reload(sys.modules["tools.delete_slides"])

    from tools.delete_slides import delete_deck

    return delete_deck


@pytest.fixture
def add_slide_function(temp_slides_root):
    """Import add_slide function."""
    import importlib

    if "tools.add_slide" in sys.modules:
        importlib.reload(sys.modules["tools.add_slide"])

    from tools.add_slide import add_slide

    return add_slide


@pytest.fixture
def insert_table_function(temp_slides_root):
    """Import insert_table function."""
    import importlib

    if "tools.insert_table" in sys.modules:
        importlib.reload(sys.modules["tools.insert_table"])

    from tools.insert_table import insert_table

    return insert_table


@pytest.fixture
def sample_slides():
    """Sample slide definitions (reused from create_slides tests)."""
    return [
        {
            "layout": "title",
            "title": "Test Presentation",
            "subtitle": "Created by tests",
        },
        {
            "layout": "title_and_content",
            "title": "Content Slide",
            "bullets": {"items": ["Point 1", "Point 2"]},
        },
    ]


@pytest.fixture
async def test_presentation(temp_slides_root, create_function, sample_slides):
    """Create a test presentation and return its path."""
    result = await create_function(
        directory="/",
        file_name="test_tools.pptx",
        slides=sample_slides,
    )
    assert result.success
    return "/test_tools.pptx"


# ============ delete_deck TESTS ============


async def test_delete_deck_success(
    delete_function, create_function, temp_slides_root, sample_slides
):
    """Test successful presentation deletion."""
    # Create a presentation
    create_result = await create_function(
        directory="/",
        file_name="to_delete.pptx",
        slides=sample_slides,
    )
    assert create_result.success

    # Verify it exists
    file_path = Path(temp_slides_root) / "to_delete.pptx"
    assert file_path.exists()

    # Delete it
    delete_result = await delete_function(file_path="/to_delete.pptx")

    data = delete_result.model_dump()
    assert data["success"] is True
    assert data["error"] is None
    assert data["file_path"] == "/to_delete.pptx"

    # Verify it's gone
    assert not file_path.exists()


async def test_delete_deck_nonexistent(delete_function):
    """Test deleting a file that doesn't exist (should succeed silently)."""
    result = await delete_function(file_path="/nonexistent.pptx")

    # Deletion of non-existent file succeeds (idempotent)
    data = result.model_dump()
    assert data["success"] is True


async def test_delete_deck_invalid_path_no_slash(delete_function):
    """Test delete with path not starting with /."""
    result = await delete_function(file_path="test.pptx")

    data = result.model_dump()
    assert data["success"] is False
    assert "must start with /" in data["error"]


async def test_delete_deck_invalid_extension(delete_function):
    """Test delete with wrong file extension."""
    result = await delete_function(file_path="/test.docx")

    data = result.model_dump()
    assert data["success"] is False
    assert ".pptx" in data["error"]


async def test_delete_deck_empty_path(delete_function):
    """Test delete with empty path."""
    result = await delete_function(file_path="")

    data = result.model_dump()
    assert data["success"] is False
    assert "required" in data["error"].lower()


# ============ add_slide TESTS ============


async def test_add_slide_at_end(
    add_slide_function, test_presentation, temp_slides_root
):
    """Test adding a slide at the end of the presentation."""
    result = await add_slide_function(
        input_data={
            "file_path": test_presentation,
            "layout": "title_only",
            "index": 2,  # At the end (0, 1, -> 2)
            "title": "New Slide",
        }
    )

    data = result.model_dump()
    assert data["success"] is True
    assert data["index"] == 2
    assert data["file_path"] == test_presentation

    # Verify slide count increased
    pptx_path = Path(temp_slides_root) / "test_tools.pptx"
    presentation = Presentation(str(pptx_path))
    assert len(presentation.slides) == 3


async def test_add_slide_at_beginning(
    add_slide_function, test_presentation, temp_slides_root
):
    """Test adding a slide at the beginning."""
    result = await add_slide_function(
        input_data={
            "file_path": test_presentation,
            "layout": "blank",
            "index": 0,
            "title": "First Slide",
        }
    )

    data = result.model_dump()
    assert data["success"] is True
    assert data["index"] == 0


async def test_add_slide_in_middle(
    add_slide_function, test_presentation, temp_slides_root
):
    """Test adding a slide in the middle."""
    result = await add_slide_function(
        input_data={
            "file_path": test_presentation,
            "layout": "section_header",
            "index": 1,
            "title": "Middle Slide",
        }
    )

    data = result.model_dump()
    assert data["success"] is True
    assert data["index"] == 1


async def test_add_slide_with_bullets(
    add_slide_function, test_presentation, temp_slides_root
):
    """Test adding a slide with bullet points."""
    result = await add_slide_function(
        input_data={
            "file_path": test_presentation,
            "layout": "title_and_content",
            "index": 2,
            "title": "Bullets Slide",
            "bullets": ["Point A", "Point B", "Point C"],
        }
    )

    data = result.model_dump()
    assert data["success"] is True


async def test_add_slide_invalid_index(add_slide_function, test_presentation):
    """Test adding a slide with out-of-range index."""
    result = await add_slide_function(
        input_data={
            "file_path": test_presentation,
            "layout": "title",
            "index": 100,  # Way out of range
            "title": "Invalid",
        }
    )

    data = result.model_dump()
    assert data["success"] is False
    assert "out of range" in data["error"].lower()


async def test_add_slide_file_not_found(add_slide_function):
    """Test adding a slide to non-existent file."""
    result = await add_slide_function(
        input_data={
            "file_path": "/nonexistent.pptx",
            "layout": "title",
            "index": 0,
            "title": "Test",
        }
    )

    data = result.model_dump()
    assert data["success"] is False
    assert "not found" in data["error"].lower()


async def test_add_slide_invalid_input(add_slide_function):
    """Test add_slide with invalid input data."""
    result = await add_slide_function(
        input_data={
            "file_path": "/test.pptx",
            # Missing required fields
        }
    )

    data = result.model_dump()
    assert data["success"] is False
    assert "Invalid input" in data["error"]


# ============ insert_table TESTS ============


async def test_insert_table_success(
    insert_table_function, test_presentation, temp_slides_root
):
    """Test successful table insertion."""
    result = await insert_table_function(
        file_path=test_presentation,
        slide_index=0,
        rows=[
            ["Header1", "Header2", "Header3"],
            ["A", "B", "C"],
            ["D", "E", "F"],
        ],
        header=True,
    )

    data = result.model_dump()
    assert data["success"] is True
    assert data["error"] is None
    assert data["slide_index"] == 0
    assert data["rows"] == 3
    assert data["cols"] == 3


async def test_insert_table_without_header(insert_table_function, test_presentation):
    """Test table insertion without header styling."""
    result = await insert_table_function(
        file_path=test_presentation,
        slide_index=1,
        rows=[["A", "B"], ["C", "D"]],
        header=False,
    )

    data = result.model_dump()
    assert data["success"] is True
    assert data["rows"] == 2
    assert data["cols"] == 2


async def test_insert_table_custom_position(insert_table_function, test_presentation):
    """Test table insertion with custom position and size."""
    result = await insert_table_function(
        file_path=test_presentation,
        slide_index=0,
        rows=[["X", "Y"], ["1", "2"]],
        header=True,
        x=1.0,
        y=2.0,
        width=8.0,
        height=4.0,
    )

    data = result.model_dump()
    assert data["success"] is True


async def test_insert_table_invalid_slide_index(
    insert_table_function, test_presentation
):
    """Test table insertion with out-of-range slide index."""
    result = await insert_table_function(
        file_path=test_presentation,
        slide_index=99,
        rows=[["A", "B"]],
    )

    data = result.model_dump()
    assert data["success"] is False
    assert "out of range" in data["error"].lower() or "index" in data["error"].lower()


async def test_insert_table_file_not_found(insert_table_function):
    """Test table insertion into non-existent file."""
    result = await insert_table_function(
        file_path="/nonexistent.pptx",
        slide_index=0,
        rows=[["A", "B"]],
    )

    data = result.model_dump()
    assert data["success"] is False
    assert "not found" in data["error"].lower()


async def test_insert_table_empty_rows(insert_table_function, test_presentation):
    """Test table insertion with empty rows."""
    result = await insert_table_function(
        file_path=test_presentation,
        slide_index=0,
        rows=[],
    )

    data = result.model_dump()
    assert data["success"] is False


# ============ INTEGRATION TESTS ============


async def test_create_add_delete_workflow(
    create_function, add_slide_function, delete_function, temp_slides_root
):
    """Test a complete workflow: create -> add slides -> delete."""
    # Create
    create_result = await create_function(
        directory="/",
        file_name="workflow_test.pptx",
        slides=[{"layout": "title", "title": "Initial"}],
    )
    assert create_result.model_dump()["success"] is True

    # Add a slide
    add_result = await add_slide_function(
        input_data={
            "file_path": "/workflow_test.pptx",
            "layout": "blank",
            "index": 1,
        }
    )
    assert add_result.model_dump()["success"] is True

    # Verify 2 slides
    pptx_path = Path(temp_slides_root) / "workflow_test.pptx"
    presentation = Presentation(str(pptx_path))
    assert len(presentation.slides) == 2

    # Delete
    delete_result = await delete_function(file_path="/workflow_test.pptx")
    assert delete_result.model_dump()["success"] is True

    # Verify deleted
    assert not pptx_path.exists()

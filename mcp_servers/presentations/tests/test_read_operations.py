"""Tests for read operations - read_slides, read_completedeck, read_individualslide."""

import os
import sys
import tempfile
from pathlib import Path

import pytest

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
def read_functions(temp_slides_root):
    """Import read functions with fresh module imports."""
    import importlib

    # Force reload modules
    for mod in [
        "tools.read_slides",
        "tools.read_completedeck",
        "tools.read_individualslide",
    ]:
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])

    from tools.read_completedeck import read_completedeck
    from tools.read_individualslide import read_individualslide
    from tools.read_slides import read_slides

    return read_slides, read_completedeck, read_individualslide


@pytest.fixture
def create_function(temp_slides_root):
    """Import create_deck function."""
    import importlib

    if "tools.create_slides" in sys.modules:
        importlib.reload(sys.modules["tools.create_slides"])

    from tools.create_slides import create_deck

    return create_deck


@pytest.fixture
def sample_slides():
    """Sample slide definitions for testing."""
    return [
        {
            "layout": "title",
            "title": "Test Presentation",
            "subtitle": "Subtitle text here",
        },
        {
            "layout": "title_and_content",
            "title": "Content Slide",
            "bullets": {"items": ["First point", "Second point", "Third point"]},
        },
        {
            "layout": "title_and_content",
            "title": "Table Slide",
            "table": {
                "rows": [["Name", "Value"], ["A", "100"], ["B", "200"]],
                "header": True,
            },
        },
    ]


@pytest.fixture
async def test_presentation(temp_slides_root, create_function, sample_slides):
    """Create a test presentation and return its path."""
    result = await create_function(
        directory="/",
        file_name="test_read.pptx",
        slides=sample_slides,
    )
    assert result.success
    return "/test_read.pptx"


# ============ read_slides TESTS ============


async def test_read_slides_success(read_functions, test_presentation):
    """Test successful read_slides operation."""
    read_slides, _, _ = read_functions

    result = await read_slides(
        file_path=test_presentation,
        start=0,
        end=500,
    )

    data = result.model_dump()
    assert data["success"] is True
    assert data["error"] is None
    assert data["content"] is not None
    assert len(data["content"]) > 0
    assert data["total_length"] > 0


async def test_read_slides_default_range(read_functions, test_presentation):
    """Test read_slides with default range (None values)."""
    read_slides, _, _ = read_functions

    result = await read_slides(file_path=test_presentation)

    data = result.model_dump()
    assert data["success"] is True
    # Default range is 0-500
    assert data["start"] == 0
    assert data["end"] == 500


async def test_read_slides_custom_range(read_functions, test_presentation):
    """Test read_slides with custom character range."""
    read_slides, _, _ = read_functions

    result = await read_slides(
        file_path=test_presentation,
        start=10,
        end=50,
    )

    data = result.model_dump()
    assert data["success"] is True
    assert data["start"] == 10
    assert data["end"] == 50
    assert len(data["content"]) <= 40


async def test_read_slides_invalid_path_no_slash(read_functions):
    """Test read_slides with path not starting with /."""
    read_slides, _, _ = read_functions

    result = await read_slides(file_path="test.pptx")

    data = result.model_dump()
    assert data["success"] is False
    assert "must start with /" in data["error"]


async def test_read_slides_invalid_extension(read_functions):
    """Test read_slides with wrong file extension."""
    read_slides, _, _ = read_functions

    result = await read_slides(file_path="/test.docx")

    data = result.model_dump()
    assert data["success"] is False
    assert ".pptx" in data["error"]


async def test_read_slides_file_not_found(read_functions):
    """Test read_slides with non-existent file."""
    read_slides, _, _ = read_functions

    result = await read_slides(file_path="/nonexistent.pptx")

    data = result.model_dump()
    assert data["success"] is False
    assert "not found" in data["error"].lower()


async def test_read_slides_invalid_range_negative(read_functions, test_presentation):
    """Test read_slides with negative range values."""
    read_slides, _, _ = read_functions

    result = await read_slides(
        file_path=test_presentation,
        start=-10,
        end=50,
    )

    data = result.model_dump()
    assert data["success"] is False
    assert "non-negative" in data["error"]


async def test_read_slides_invalid_range_end_before_start(
    read_functions, test_presentation
):
    """Test read_slides with end <= start."""
    read_slides, _, _ = read_functions

    result = await read_slides(
        file_path=test_presentation,
        start=100,
        end=50,
    )

    data = result.model_dump()
    assert data["success"] is False
    assert "greater than start" in data["error"]


async def test_read_slides_range_too_large(read_functions, test_presentation):
    """Test read_slides with range exceeding 10,000 characters."""
    read_slides, _, _ = read_functions

    result = await read_slides(
        file_path=test_presentation,
        start=0,
        end=15000,
    )

    data = result.model_dump()
    assert data["success"] is False
    assert "10,000" in data["error"]


# ============ read_completedeck TESTS ============


async def test_read_completedeck_success(read_functions, test_presentation):
    """Test successful read_completedeck operation."""
    _, read_completedeck, _ = read_functions

    result = await read_completedeck(file_path=test_presentation)

    data = result.model_dump()
    assert data["success"] is True
    assert data["error"] is None
    assert data["total_slides"] == 3
    assert len(data["slides"]) == 3


async def test_read_completedeck_slide_content(read_functions, test_presentation):
    """Test that read_completedeck extracts slide titles and content."""
    _, read_completedeck, _ = read_functions

    result = await read_completedeck(file_path=test_presentation)

    data = result.model_dump()
    slides = data["slides"]

    # Check first slide (title slide)
    assert slides[0]["slide_index"] == 0
    assert "Test Presentation" in slides[0]["title"]

    # Check second slide (content slide)
    assert slides[1]["slide_index"] == 1
    assert "Content Slide" in slides[1]["title"]


async def test_read_completedeck_invalid_path(read_functions):
    """Test read_completedeck with invalid path."""
    _, read_completedeck, _ = read_functions

    result = await read_completedeck(file_path="no_leading_slash.pptx")

    data = result.model_dump()
    assert data["success"] is False
    assert "must start with /" in data["error"]


async def test_read_completedeck_empty_path(read_functions):
    """Test read_completedeck with empty path."""
    _, read_completedeck, _ = read_functions

    result = await read_completedeck(file_path="")

    data = result.model_dump()
    assert data["success"] is False
    assert "required" in data["error"].lower()


async def test_read_completedeck_file_not_found(read_functions):
    """Test read_completedeck with non-existent file."""
    _, read_completedeck, _ = read_functions

    result = await read_completedeck(file_path="/does_not_exist.pptx")

    data = result.model_dump()
    assert data["success"] is False
    assert "not found" in data["error"].lower()


# ============ read_individualslide TESTS ============


async def test_read_individualslide_success(read_functions, test_presentation):
    """Test successful read_individualslide operation."""
    _, _, read_individualslide = read_functions

    result = await read_individualslide(
        file_path=test_presentation,
        slide_index=0,
    )

    data = result.model_dump()
    assert data["success"] is True
    assert data["error"] is None
    assert data["slide_index"] == 0
    assert data["total_slides"] == 3
    assert data["layout"] is not None


async def test_read_individualslide_all_slides(read_functions, test_presentation):
    """Test reading each slide individually."""
    _, _, read_individualslide = read_functions

    for i in range(3):
        result = await read_individualslide(
            file_path=test_presentation,
            slide_index=i,
        )
        data = result.model_dump()
        assert data["success"] is True
        assert data["slide_index"] == i


async def test_read_individualslide_components(read_functions, test_presentation):
    """Test that read_individualslide extracts component information."""
    _, _, read_individualslide = read_functions

    result = await read_individualslide(
        file_path=test_presentation,
        slide_index=0,
    )

    data = result.model_dump()
    assert data["components"] is not None
    # Title slide should have shapes
    assert len(data["components"]) > 0


async def test_read_individualslide_table_slide(read_functions, test_presentation):
    """Test reading a slide with a table."""
    _, _, read_individualslide = read_functions

    result = await read_individualslide(
        file_path=test_presentation,
        slide_index=2,  # Table slide
    )

    data = result.model_dump()
    assert data["success"] is True

    # Check for table component
    table_components = [
        c for c in data["components"] if c.get("component_type") == "TABLE"
    ]
    assert len(table_components) > 0


async def test_read_individualslide_out_of_range(read_functions, test_presentation):
    """Test read_individualslide with out-of-range index."""
    _, _, read_individualslide = read_functions

    result = await read_individualslide(
        file_path=test_presentation,
        slide_index=99,
    )

    data = result.model_dump()
    assert data["success"] is False
    assert "out of range" in data["error"].lower()


async def test_read_individualslide_negative_index(read_functions, test_presentation):
    """Test read_individualslide with negative index."""
    _, _, read_individualslide = read_functions

    result = await read_individualslide(
        file_path=test_presentation,
        slide_index=-1,
    )

    data = result.model_dump()
    assert data["success"] is False
    assert "non-negative" in data["error"]


async def test_read_individualslide_invalid_path(read_functions):
    """Test read_individualslide with invalid file path."""
    _, _, read_individualslide = read_functions

    result = await read_individualslide(
        file_path="no_slash.pptx",
        slide_index=0,
    )

    data = result.model_dump()
    assert data["success"] is False
    assert "must start with /" in data["error"]


async def test_read_individualslide_empty_path(read_functions):
    """Test read_individualslide with empty path."""
    _, _, read_individualslide = read_functions

    result = await read_individualslide(
        file_path="",
        slide_index=0,
    )

    data = result.model_dump()
    assert data["success"] is False
    assert "required" in data["error"].lower()

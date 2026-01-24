"""Tests for the slides meta-tool - action routing, validation, and error propagation."""

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
def slides_function(temp_slides_root):
    """Import the slides meta-tool with fresh module import."""
    import importlib

    # Force reload ALL tool modules to pick up the new environment variable
    # Order matters - reload leaf modules first, then modules that import them
    tool_modules = [
        "tools.create_slides",
        "tools.delete_slides",
        "tools.add_slide",
        "tools.edit_slides",
        "tools.read_slides",
        "tools.read_completedeck",
        "tools.read_individualslide",
        "tools.read_image",
        "tools.add_image",
        "tools.modify_image",
        "tools.insert_chart",
        "tools.insert_table",
        "tools._meta_tools",
    ]
    for mod in tool_modules:
        if mod in sys.modules:
            importlib.reload(sys.modules[mod])

    from tools._meta_tools import SlidesInput, slides

    return slides, SlidesInput


@pytest.fixture
def schema_function(temp_slides_root):
    """Import the slides_schema meta-tool."""
    from tools._meta_tools import SlidesSchemaInput, slides_schema

    return slides_schema, SlidesSchemaInput


@pytest.fixture
def sample_slides():
    """Sample slide definitions for testing."""
    return [
        {
            "layout": "title",
            "title": "Test Presentation",
            "subtitle": "Created by tests",
        },
        {
            "layout": "title_and_content",
            "title": "Content Slide",
            "bullets": {"items": ["Point 1", "Point 2", "Point 3"]},
        },
    ]


# ============ CREATE ACTION TESTS ============


async def test_create_action_success(slides_function, temp_slides_root, sample_slides):
    """Test successful presentation creation via meta-tool."""
    slides, SlidesInput = slides_function

    request = SlidesInput(
        action="create",
        directory="/",
        file_name="test_meta.pptx",
        slides=sample_slides,
    )
    result = await slides(request)

    data = result.model_dump()
    assert data["action"] == "create"
    assert data["error"] is None
    assert data["create"]["file_name"] == "test_meta.pptx"

    # Verify file exists
    expected_path = Path(temp_slides_root) / "test_meta.pptx"
    assert expected_path.exists()


async def test_create_action_missing_directory(slides_function):
    """Test create action with missing directory."""
    slides, SlidesInput = slides_function

    request = SlidesInput(
        action="create",
        file_name="test.pptx",
        slides=[{"layout": "title", "title": "Test"}],
    )
    result = await slides(request)

    data = result.model_dump()
    assert data["action"] == "create"
    assert data["error"] == "Required: directory"
    assert data["create"] is None


async def test_create_action_missing_file_name(slides_function):
    """Test create action with missing file_name."""
    slides, SlidesInput = slides_function

    request = SlidesInput(
        action="create",
        directory="/",
        slides=[{"layout": "title", "title": "Test"}],
    )
    result = await slides(request)

    data = result.model_dump()
    assert data["error"] == "Required: file_name"


async def test_create_action_missing_slides(slides_function):
    """Test create action with missing slides."""
    slides, SlidesInput = slides_function

    request = SlidesInput(
        action="create",
        directory="/",
        file_name="test.pptx",
    )
    result = await slides(request)

    data = result.model_dump()
    assert data["error"] == "Required: slides"


# ============ DELETE ACTION TESTS ============


async def test_delete_action_success(slides_function, temp_slides_root, sample_slides):
    """Test successful presentation deletion via meta-tool."""
    slides, SlidesInput = slides_function

    # First create a presentation
    create_request = SlidesInput(
        action="create",
        directory="/",
        file_name="to_delete.pptx",
        slides=sample_slides,
    )
    await slides(create_request)

    # Then delete it
    delete_request = SlidesInput(
        action="delete",
        file_path="/to_delete.pptx",
    )
    result = await slides(delete_request)

    data = result.model_dump()
    assert data["action"] == "delete"
    assert data["error"] is None
    assert data["delete"]["file_path"] == "/to_delete.pptx"

    # Verify file is deleted
    expected_path = Path(temp_slides_root) / "to_delete.pptx"
    assert not expected_path.exists()


async def test_delete_action_missing_file_path(slides_function):
    """Test delete action with missing file_path."""
    slides, SlidesInput = slides_function

    request = SlidesInput(action="delete")
    result = await slides(request)

    data = result.model_dump()
    assert data["error"] == "Required: file_path"


# ============ READ_RANGE ACTION TESTS ============


async def test_read_range_action_success(
    slides_function, temp_slides_root, sample_slides
):
    """Test successful read_range action."""
    slides, SlidesInput = slides_function

    # Create a presentation first
    create_request = SlidesInput(
        action="create",
        directory="/",
        file_name="read_test.pptx",
        slides=sample_slides,
    )
    await slides(create_request)

    # Read content
    read_request = SlidesInput(
        action="read_range",
        file_path="/read_test.pptx",
        start=0,
        end=100,
    )
    result = await slides(read_request)

    data = result.model_dump()
    assert data["action"] == "read_range"
    assert data["error"] is None
    assert data["read_range"]["content"] is not None
    assert data["read_range"]["total_length"] > 0


async def test_read_range_action_missing_file_path(slides_function):
    """Test read_range action with missing file_path."""
    slides, SlidesInput = slides_function

    request = SlidesInput(action="read_range")
    result = await slides(request)

    data = result.model_dump()
    assert data["error"] == "Required: file_path"


# ============ READ_DECK ACTION TESTS ============


async def test_read_deck_action_success(
    slides_function, temp_slides_root, sample_slides
):
    """Test successful read_deck action."""
    slides, SlidesInput = slides_function

    # Create a presentation first
    create_request = SlidesInput(
        action="create",
        directory="/",
        file_name="deck_test.pptx",
        slides=sample_slides,
    )
    await slides(create_request)

    # Read deck
    read_request = SlidesInput(
        action="read_deck",
        file_path="/deck_test.pptx",
    )
    result = await slides(read_request)

    data = result.model_dump()
    assert data["action"] == "read_deck"
    assert data["error"] is None
    assert data["read_deck"]["total_slides"] == 2
    assert len(data["read_deck"]["slides"]) == 2


async def test_read_deck_action_file_not_found(slides_function):
    """Test read_deck action with non-existent file."""
    slides, SlidesInput = slides_function

    request = SlidesInput(
        action="read_deck",
        file_path="/nonexistent.pptx",
    )
    result = await slides(request)

    data = result.model_dump()
    assert data["error"] is not None
    assert "not found" in data["error"].lower() or "File not found" in data["error"]


# ============ READ_SLIDE ACTION TESTS ============


async def test_read_slide_action_success(
    slides_function, temp_slides_root, sample_slides
):
    """Test successful read_slide action."""
    slides, SlidesInput = slides_function

    # Create a presentation first
    create_request = SlidesInput(
        action="create",
        directory="/",
        file_name="slide_test.pptx",
        slides=sample_slides,
    )
    await slides(create_request)

    # Read individual slide
    read_request = SlidesInput(
        action="read_slide",
        file_path="/slide_test.pptx",
        slide_index=0,
    )
    result = await slides(read_request)

    data = result.model_dump()
    assert data["action"] == "read_slide"
    assert data["error"] is None
    assert data["read_slide"]["slide_index"] == 0
    assert data["read_slide"]["total_slides"] == 2


async def test_read_slide_action_missing_slide_index(
    slides_function, temp_slides_root, sample_slides
):
    """Test read_slide action with missing slide_index."""
    slides, SlidesInput = slides_function

    # Create a presentation first
    create_request = SlidesInput(
        action="create",
        directory="/",
        file_name="slide_idx_test.pptx",
        slides=sample_slides,
    )
    await slides(create_request)

    request = SlidesInput(
        action="read_slide",
        file_path="/slide_idx_test.pptx",
    )
    result = await slides(request)

    data = result.model_dump()
    assert data["error"] == "Required: slide_index"


# ============ EDIT ACTION TESTS ============


async def test_edit_action_missing_operations(
    slides_function, temp_slides_root, sample_slides
):
    """Test edit action with missing operations."""
    slides, SlidesInput = slides_function

    # Create first
    create_request = SlidesInput(
        action="create",
        directory="/",
        file_name="edit_test.pptx",
        slides=sample_slides,
    )
    await slides(create_request)

    request = SlidesInput(
        action="edit",
        file_path="/edit_test.pptx",
    )
    result = await slides(request)

    data = result.model_dump()
    assert data["error"] == "Required: operations"


# ============ ADD_IMAGE ACTION TESTS ============


async def test_add_image_action_missing_required(slides_function):
    """Test add_image action with missing required fields."""
    slides, SlidesInput = slides_function

    # Missing file_path
    request = SlidesInput(
        action="add_image",
        image_path="/image.png",
        slide_index=0,
    )
    result = await slides(request)
    assert result.model_dump()["error"] == "Required: file_path"

    # Missing image_path
    request = SlidesInput(
        action="add_image",
        file_path="/test.pptx",
        slide_index=0,
    )
    result = await slides(request)
    assert result.model_dump()["error"] == "Required: image_path"

    # Missing slide_index
    request = SlidesInput(
        action="add_image",
        file_path="/test.pptx",
        image_path="/image.png",
    )
    result = await slides(request)
    assert result.model_dump()["error"] == "Required: slide_index"


# ============ INSERT_CHART ACTION TESTS ============


async def test_insert_chart_action_missing_required(slides_function):
    """Test insert_chart action with missing required fields."""
    slides, SlidesInput = slides_function

    request = SlidesInput(
        action="insert_chart",
        file_path="/test.pptx",
        slide_index=0,
        # Missing spreadsheet_path, sheet_name, data_range
    )
    result = await slides(request)
    assert "Required" in result.model_dump()["error"]


# ============ INSERT_TABLE ACTION TESTS ============


async def test_insert_table_action_missing_required(slides_function):
    """Test insert_table action with missing required fields."""
    slides, SlidesInput = slides_function

    request = SlidesInput(
        action="insert_table",
        file_path="/test.pptx",
        # Missing slide_index and rows
    )
    result = await slides(request)
    assert "Required" in result.model_dump()["error"]


# ============ SCHEMA TOOL TESTS ============


async def test_schema_list_all(schema_function):
    """Test listing all available schemas."""
    slides_schema, SlidesSchemaInput = schema_function

    request = SlidesSchemaInput()
    result = await slides_schema(request)

    data = result.model_dump()
    assert data["schema_names"] is not None
    assert "SlidesInput" in data["schema_names"]
    assert "SlidesOutput" in data["schema_names"]
    assert "CreateResult" in data["schema_names"]


async def test_schema_get_specific(schema_function):
    """Test getting a specific schema."""
    slides_schema, SlidesSchemaInput = schema_function

    request = SlidesSchemaInput(schema_name="SlidesInput")
    result = await slides_schema(request)

    data = result.model_dump()
    assert data["json_schema"] is not None
    assert "properties" in data["json_schema"]
    assert "action" in data["json_schema"]["properties"]


async def test_schema_unknown(schema_function):
    """Test error for unknown schema."""
    slides_schema, SlidesSchemaInput = schema_function

    request = SlidesSchemaInput(schema_name="NonExistentSchema")
    result = await slides_schema(request)

    data = result.model_dump()
    assert data["error"] is not None
    assert "Unknown schema" in data["error"]


# ============ ERROR PROPAGATION TESTS ============


async def test_error_propagation_invalid_file(slides_function):
    """Test that errors from underlying tools propagate correctly."""
    slides, SlidesInput = slides_function

    request = SlidesInput(
        action="read_deck",
        file_path="/definitely_does_not_exist.pptx",
    )
    result = await slides(request)

    data = result.model_dump()
    assert data["error"] is not None
    assert data["read_deck"] is None

"""Tests for new slide tool features: cropping, chart types, hyperlinks, table formatting, shapes."""

import os
import sys
import tempfile
from pathlib import Path

import pytest
from openpyxl import Workbook
from PIL import Image as PILImage
from pptx import Presentation
from pptx.shapes.autoshape import Shape

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
def modify_image_function(temp_slides_root):
    """Import modify_image function."""
    import importlib

    if "tools.modify_image" in sys.modules:
        importlib.reload(sys.modules["tools.modify_image"])

    from tools.modify_image import modify_image

    return modify_image


@pytest.fixture
def insert_chart_function(temp_slides_root):
    """Import insert_chart function."""
    import importlib

    if "tools.insert_chart" in sys.modules:
        importlib.reload(sys.modules["tools.insert_chart"])

    from tools.insert_chart import insert_chart

    return insert_chart


@pytest.fixture
def edit_slides_function(temp_slides_root):
    """Import edit_slides function."""
    import importlib

    if "tools.edit_slides" in sys.modules:
        importlib.reload(sys.modules["tools.edit_slides"])

    from tools.edit_slides import edit_slides

    return edit_slides


@pytest.fixture
def add_shape_function(temp_slides_root):
    """Import add_shape function."""
    import importlib

    if "tools.add_shape" in sys.modules:
        importlib.reload(sys.modules["tools.add_shape"])

    from tools.add_shape import add_shape

    return add_shape


@pytest.fixture
def insert_table_function(temp_slides_root):
    """Import insert_table function."""
    import importlib

    if "tools.insert_table" in sys.modules:
        importlib.reload(sys.modules["tools.insert_table"])

    from tools.insert_table import insert_table

    return insert_table


@pytest.fixture
def add_image_function(temp_slides_root):
    """Import add_image function."""
    import importlib

    if "tools.add_image" in sys.modules:
        importlib.reload(sys.modules["tools.add_image"])

    from tools.add_image import add_image

    return add_image


@pytest.fixture
def sample_slides():
    """Sample slide definitions."""
    return [
        {
            "layout": "title",
            "title": "Test Presentation",
            "subtitle": "Testing new features",
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
        file_name="test_new_features.pptx",
        slides=sample_slides,
    )
    assert result.success
    return "/test_new_features.pptx"


@pytest.fixture
def test_image(temp_slides_root):
    """Create a test image and return its path."""
    img = PILImage.new("RGB", (200, 100), color="red")
    img_path = Path(temp_slides_root) / "test_image.png"
    img.save(str(img_path))
    return "/test_image.png"


@pytest.fixture
def test_spreadsheet(temp_slides_root):
    """Create a test spreadsheet with chart data (categorical X values)."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Data"

    # Write header row
    ws["A1"] = "Category"
    ws["B1"] = "Series1"
    ws["C1"] = "Series2"

    # Write data rows
    ws["A2"] = "Jan"
    ws["B2"] = 10
    ws["C2"] = 20

    ws["A3"] = "Feb"
    ws["B3"] = 15
    ws["C3"] = 25

    ws["A4"] = "Mar"
    ws["B4"] = 12
    ws["C4"] = 18

    spreadsheet_path = Path(temp_slides_root) / "test_data.xlsx"
    wb.save(str(spreadsheet_path))
    wb.close()
    return "/test_data.xlsx"


@pytest.fixture
def test_spreadsheet_numeric(temp_slides_root):
    """Create a test spreadsheet with numeric X values for scatter charts."""
    wb = Workbook()
    ws = wb.active
    ws.title = "Data"

    # Write header row
    ws["A1"] = "X"
    ws["B1"] = "Y1"
    ws["C1"] = "Y2"

    # Write data rows with numeric X values
    ws["A2"] = 1.0
    ws["B2"] = 10
    ws["C2"] = 20

    ws["A3"] = 2.0
    ws["B3"] = 15
    ws["C3"] = 25

    ws["A4"] = 3.0
    ws["B4"] = 12
    ws["C4"] = 18

    spreadsheet_path = Path(temp_slides_root) / "test_data_numeric.xlsx"
    wb.save(str(spreadsheet_path))
    wb.close()
    return "/test_data_numeric.xlsx"


# ============ IMAGE CROPPING TESTS ============


async def test_crop_image_success(
    modify_image_function,
    add_image_function,
    test_presentation,
    test_image,
    temp_slides_root,
):
    """Test successful image cropping."""
    # First add an image
    add_result = await add_image_function(
        file_path=test_presentation,
        image_path=test_image,
        slide_index=0,
        x=1.0,
        y=1.0,
    )
    assert add_result.success

    # Now crop the image
    result = await modify_image_function(
        file_path=test_presentation,
        slide_index=0,
        image_index=0,
        operation="crop",
        crop_left=10,
        crop_top=10,
        crop_right=150,
        crop_bottom=80,
    )

    data = result.model_dump()
    assert data["success"] is True
    assert data["operation"] == "crop"


async def test_crop_image_invalid_bounds(
    modify_image_function,
    add_image_function,
    test_presentation,
    test_image,
):
    """Test image cropping with invalid bounds."""
    # First add an image
    add_result = await add_image_function(
        file_path=test_presentation,
        image_path=test_image,
        slide_index=0,
        x=1.0,
        y=1.0,
    )
    assert add_result.success

    # Crop with left >= right should fail
    result = await modify_image_function(
        file_path=test_presentation,
        slide_index=0,
        image_index=0,
        operation="crop",
        crop_left=100,
        crop_top=10,
        crop_right=50,  # Less than left
        crop_bottom=80,
    )

    data = result.model_dump()
    assert data["success"] is False
    assert "crop_left must be less than crop_right" in data["error"]


async def test_crop_image_missing_params(
    modify_image_function,
    add_image_function,
    test_presentation,
    test_image,
):
    """Test image cropping with missing parameters."""
    # First add an image
    add_result = await add_image_function(
        file_path=test_presentation,
        image_path=test_image,
        slide_index=0,
        x=1.0,
        y=1.0,
    )
    assert add_result.success

    # Crop without all params should fail
    result = await modify_image_function(
        file_path=test_presentation,
        slide_index=0,
        image_index=0,
        operation="crop",
        crop_left=10,
        crop_top=10,
        # Missing crop_right and crop_bottom
    )

    data = result.model_dump()
    assert data["success"] is False
    assert "requires" in data["error"].lower()


# ============ NEW CHART TYPES TESTS ============


async def test_insert_area_chart(
    insert_chart_function, test_presentation, test_spreadsheet
):
    """Test inserting an area chart."""
    result = await insert_chart_function(
        presentation_path=test_presentation,
        slide_index=0,
        spreadsheet_path=test_spreadsheet,
        sheet_name="Data",
        data_range="A1:C4",
        chart_type="area",
        title="Area Chart Test",
    )

    data = result.model_dump()
    assert data["success"] is True
    assert data["chart_type"] == "area"


async def test_insert_scatter_chart(
    insert_chart_function, test_presentation, test_spreadsheet_numeric
):
    """Test inserting a scatter chart with numeric X values."""
    result = await insert_chart_function(
        presentation_path=test_presentation,
        slide_index=0,
        spreadsheet_path=test_spreadsheet_numeric,
        sheet_name="Data",
        data_range="A1:C4",
        chart_type="scatter",
        title="Scatter Chart Test",
    )

    data = result.model_dump()
    assert data["success"] is True
    assert data["chart_type"] == "scatter"


async def test_insert_scatter_chart_non_numeric_x_values(
    insert_chart_function, test_presentation, test_spreadsheet
):
    """Test that scatter chart fails with non-numeric X values."""
    result = await insert_chart_function(
        presentation_path=test_presentation,
        slide_index=0,
        spreadsheet_path=test_spreadsheet,
        sheet_name="Data",
        data_range="A1:C4",
        chart_type="scatter",
        title="Scatter Chart Test",
    )

    data = result.model_dump()
    assert data["success"] is False
    assert "numeric X values" in data["error"]


async def test_insert_doughnut_chart(
    insert_chart_function, test_presentation, test_spreadsheet
):
    """Test inserting a doughnut chart."""
    result = await insert_chart_function(
        presentation_path=test_presentation,
        slide_index=0,
        spreadsheet_path=test_spreadsheet,
        sheet_name="Data",
        data_range="A1:C4",
        chart_type="doughnut",
        title="Doughnut Chart Test",
    )

    data = result.model_dump()
    assert data["success"] is True
    assert data["chart_type"] == "doughnut"


async def test_insert_radar_chart(
    insert_chart_function, test_presentation, test_spreadsheet
):
    """Test inserting a radar chart."""
    result = await insert_chart_function(
        presentation_path=test_presentation,
        slide_index=0,
        spreadsheet_path=test_spreadsheet,
        sheet_name="Data",
        data_range="A1:C4",
        chart_type="radar",
        title="Radar Chart Test",
    )

    data = result.model_dump()
    assert data["success"] is True
    assert data["chart_type"] == "radar"


async def test_invalid_chart_type(
    insert_chart_function, test_presentation, test_spreadsheet
):
    """Test inserting a chart with invalid type."""
    result = await insert_chart_function(
        presentation_path=test_presentation,
        slide_index=0,
        spreadsheet_path=test_spreadsheet,
        sheet_name="Data",
        data_range="A1:C4",
        chart_type="invalid_type",
        title="Invalid Chart",
    )

    data = result.model_dump()
    assert data["success"] is False
    assert "must be one of" in data["error"]


# ============ HYPERLINK TESTS ============


async def test_add_hyperlink_success(edit_slides_function, test_presentation):
    """Test adding a hyperlink to text."""
    result = await edit_slides_function(
        file_path=test_presentation,
        operations=[
            {
                "type": "add_hyperlink",
                "index": 0,
                "placeholder": "title",
                "url": "https://example.com",
            }
        ],
    )

    data = result.model_dump()
    assert data["success"] is True
    assert data["operations_applied"] == 1


async def test_add_hyperlink_with_indices(edit_slides_function, test_presentation):
    """Test adding a hyperlink to specific paragraph and run."""
    result = await edit_slides_function(
        file_path=test_presentation,
        operations=[
            {
                "type": "add_hyperlink",
                "index": 1,
                "placeholder": "body",
                "url": "https://example.com/docs",
                "paragraph_index": 0,
                "run_index": 0,
            }
        ],
    )

    data = result.model_dump()
    assert data["success"] is True


async def test_add_hyperlink_invalid_run_index(edit_slides_function, test_presentation):
    """Test adding a hyperlink with invalid run index."""
    result = await edit_slides_function(
        file_path=test_presentation,
        operations=[
            {
                "type": "add_hyperlink",
                "index": 0,
                "placeholder": "title",
                "url": "https://example.com",
                "run_index": 99,  # Out of range
            }
        ],
    )

    data = result.model_dump()
    assert data["success"] is False
    assert "out of range" in data["error"].lower()


# ============ TABLE CELL FORMATTING TESTS ============


async def test_format_table_cell_bg_color(
    edit_slides_function, insert_table_function, test_presentation
):
    """Test formatting table cell with background color."""
    # First insert a table
    table_result = await insert_table_function(
        file_path=test_presentation,
        slide_index=0,
        rows=[["A", "B"], ["C", "D"]],
        header=True,
    )
    assert table_result.success

    # Format a cell
    result = await edit_slides_function(
        file_path=test_presentation,
        operations=[
            {
                "type": "format_table_cell",
                "index": 0,
                "table_idx": 0,
                "row": 0,
                "column": 0,
                "bg_color": "FF0000",  # Red background
            }
        ],
    )

    data = result.model_dump()
    assert data["success"] is True
    assert data["operations_applied"] == 1


async def test_format_table_cell_font_styling(
    edit_slides_function, insert_table_function, test_presentation
):
    """Test formatting table cell with font styling."""
    # First insert a table
    table_result = await insert_table_function(
        file_path=test_presentation,
        slide_index=0,
        rows=[["Header1", "Header2"], ["Data1", "Data2"]],
        header=True,
    )
    assert table_result.success

    # Format a cell with multiple styles
    result = await edit_slides_function(
        file_path=test_presentation,
        operations=[
            {
                "type": "format_table_cell",
                "index": 0,
                "table_idx": 0,
                "row": 1,
                "column": 1,
                "bold": True,
                "italic": True,
                "font_size": 14,
                "font_color": "0000FF",  # Blue text
            }
        ],
    )

    data = result.model_dump()
    assert data["success"] is True


async def test_format_table_cell_invalid_index(
    edit_slides_function, insert_table_function, test_presentation
):
    """Test formatting table cell with invalid row/column index."""
    # First insert a table
    table_result = await insert_table_function(
        file_path=test_presentation,
        slide_index=0,
        rows=[["A", "B"], ["C", "D"]],
    )
    assert table_result.success

    # Try to format non-existent cell
    result = await edit_slides_function(
        file_path=test_presentation,
        operations=[
            {
                "type": "format_table_cell",
                "index": 0,
                "table_idx": 0,
                "row": 10,  # Out of range
                "column": 0,
                "bg_color": "FF0000",
            }
        ],
    )

    data = result.model_dump()
    assert data["success"] is False
    assert "out of range" in data["error"].lower()


# ============ ADD SHAPE TESTS ============


async def test_add_rectangle_shape(
    add_shape_function, test_presentation, temp_slides_root
):
    """Test adding a rectangle shape."""
    result = await add_shape_function(
        file_path=test_presentation,
        slide_index=0,
        shape_type="rectangle",
        x=2.0,
        y=2.0,
        width=3.0,
        height=1.5,
    )

    data = result.model_dump()
    assert data["success"] is True
    assert data["shape_type"] == "rectangle"
    assert data["position"] == (2.0, 2.0)

    # Verify shape was added
    pptx_path = Path(temp_slides_root) / "test_new_features.pptx"
    presentation = Presentation(str(pptx_path))
    slide = presentation.slides[0]
    shapes = [s for s in slide.shapes if isinstance(s, Shape)]
    assert len(shapes) >= 1


async def test_add_shape_with_fill_color(add_shape_function, test_presentation):
    """Test adding a shape with fill color."""
    result = await add_shape_function(
        file_path=test_presentation,
        slide_index=0,
        shape_type="oval",
        x=1.0,
        y=1.0,
        width=2.0,
        height=2.0,
        fill_color="00FF00",  # Green
    )

    data = result.model_dump()
    assert data["success"] is True
    assert data["shape_type"] == "oval"


async def test_add_shape_with_text(add_shape_function, test_presentation):
    """Test adding a shape with text inside."""
    result = await add_shape_function(
        file_path=test_presentation,
        slide_index=0,
        shape_type="rounded_rectangle",
        x=1.0,
        y=1.0,
        width=4.0,
        height=1.0,
        text="Hello World",
        text_color="FFFFFF",  # White text
        fill_color="000000",  # Black background
        font_size=18,
    )

    data = result.model_dump()
    assert data["success"] is True


async def test_add_arrow_shape(add_shape_function, test_presentation):
    """Test adding an arrow shape."""
    result = await add_shape_function(
        file_path=test_presentation,
        slide_index=0,
        shape_type="right_arrow",
        x=1.0,
        y=3.0,
        width=2.0,
        height=1.0,
        fill_color="FF6600",  # Orange
    )

    data = result.model_dump()
    assert data["success"] is True
    assert data["shape_type"] == "right_arrow"


async def test_add_star_shape(add_shape_function, test_presentation):
    """Test adding a star shape."""
    result = await add_shape_function(
        file_path=test_presentation,
        slide_index=0,
        shape_type="star",
        x=5.0,
        y=3.0,
        width=2.0,
        height=2.0,
        fill_color="FFD700",  # Gold
        line_color="000000",
        line_width=2,
    )

    data = result.model_dump()
    assert data["success"] is True
    assert data["shape_type"] == "star"


async def test_add_shape_invalid_type(add_shape_function, test_presentation):
    """Test adding a shape with invalid type."""
    result = await add_shape_function(
        file_path=test_presentation,
        slide_index=0,
        shape_type="invalid_shape",
        x=1.0,
        y=1.0,
        width=2.0,
        height=2.0,
    )

    data = result.model_dump()
    assert data["success"] is False
    assert "must be one of" in data["error"]


async def test_add_shape_invalid_slide_index(add_shape_function, test_presentation):
    """Test adding a shape to non-existent slide."""
    result = await add_shape_function(
        file_path=test_presentation,
        slide_index=99,
        shape_type="rectangle",
        x=1.0,
        y=1.0,
        width=2.0,
        height=2.0,
    )

    data = result.model_dump()
    assert data["success"] is False
    assert "out of range" in data["error"].lower()


async def test_add_shape_invalid_dimensions(add_shape_function, test_presentation):
    """Test adding a shape with invalid dimensions."""
    result = await add_shape_function(
        file_path=test_presentation,
        slide_index=0,
        shape_type="rectangle",
        x=1.0,
        y=1.0,
        width=-2.0,  # Invalid
        height=2.0,
    )

    data = result.model_dump()
    assert data["success"] is False
    assert "positive" in data["error"].lower()


async def test_add_shape_file_not_found(add_shape_function):
    """Test adding a shape to non-existent file."""
    result = await add_shape_function(
        file_path="/nonexistent.pptx",
        slide_index=0,
        shape_type="rectangle",
        x=1.0,
        y=1.0,
        width=2.0,
        height=2.0,
    )

    data = result.model_dump()
    assert data["success"] is False
    assert "not found" in data["error"].lower()


# ============ INTEGRATION TESTS ============


async def test_create_presentation_with_all_new_features(
    create_function,
    add_shape_function,
    insert_chart_function,
    edit_slides_function,
    insert_table_function,
    test_spreadsheet,
    temp_slides_root,
):
    """Test creating a presentation using all new features."""
    # Create presentation
    create_result = await create_function(
        directory="/",
        file_name="integration_test.pptx",
        slides=[
            {
                "layout": "title",
                "title": "Integration Test",
                "subtitle": "All Features",
            },
            {
                "layout": "title_and_content",
                "title": "Chart Slide",
                "bullets": {"items": ["Test"]},
            },
            {"layout": "blank"},
        ],
    )
    assert create_result.success
    file_path = "/integration_test.pptx"

    # Add multiple shapes
    shape_result = await add_shape_function(
        file_path=file_path,
        slide_index=2,
        shape_type="rectangle",
        x=1.0,
        y=1.0,
        width=3.0,
        height=2.0,
        fill_color="3366FF",
        text="Hello",
    )
    assert shape_result.success

    # Add chart
    chart_result = await insert_chart_function(
        presentation_path=file_path,
        slide_index=1,
        spreadsheet_path=test_spreadsheet,
        sheet_name="Data",
        data_range="A1:C4",
        chart_type="doughnut",
        title="Test Doughnut",
    )
    assert chart_result.success

    # Add table and format it
    table_result = await insert_table_function(
        file_path=file_path,
        slide_index=2,
        rows=[["Name", "Value"], ["Item1", "100"], ["Item2", "200"]],
        header=True,
    )
    assert table_result.success

    # Format table cell
    format_result = await edit_slides_function(
        file_path=file_path,
        operations=[
            {
                "type": "format_table_cell",
                "index": 2,
                "table_idx": 0,
                "row": 0,
                "column": 0,
                "bg_color": "4472C4",
                "font_color": "FFFFFF",
                "bold": True,
            }
        ],
    )
    assert format_result.success

    # Add hyperlink
    hyperlink_result = await edit_slides_function(
        file_path=file_path,
        operations=[
            {
                "type": "add_hyperlink",
                "index": 0,
                "placeholder": "title",
                "url": "https://example.com",
            }
        ],
    )
    assert hyperlink_result.success

    # Verify file exists
    pptx_path = Path(temp_slides_root) / "integration_test.pptx"
    assert pptx_path.exists()

    # Open and verify contents
    presentation = Presentation(str(pptx_path))
    assert len(presentation.slides) == 3

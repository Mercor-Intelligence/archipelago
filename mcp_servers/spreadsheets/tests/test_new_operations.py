"""Tests for new spreadsheet operations."""

import os
import sys
import tempfile
from pathlib import Path

import pytest
from openpyxl import Workbook, load_workbook
from pydantic import ValidationError

# Add the sheets_server directory to the path for module imports
sys.path.insert(
    0,
    str(Path(__file__).parent.parent / "mcp_servers" / "sheets_server"),
)

from mcp_servers.sheets_server.models.sheet import (
    AddConditionalFormattingOperation,
    AddDataValidationOperation,
    AddImageOperation,
    AddNamedRangeOperation,
    DeleteNamedRangeOperation,
    FreezePanesOperation,
    MergeCellsOperation,
    SetAutoFilterOperation,
    SetColumnWidthOperation,
    SetNumberFormatOperation,
    SetRowHeightOperation,
    UnmergeCellsOperation,
)


class TestResponseModels:
    """Tests for response models."""

    def test_edit_spreadsheet_response_str(self):
        from mcp_servers.sheets_server.models.response import EditSpreadsheetResponse

        response = EditSpreadsheetResponse(
            status="success", file_path="/test.xlsx", operations_applied=5
        )
        s = str(response)
        assert "success" in s
        assert "/test.xlsx" in s
        assert "5" in s

    def test_create_spreadsheet_response_str(self):
        from mcp_servers.sheets_server.models.response import CreateSpreadsheetResponse

        response = CreateSpreadsheetResponse(
            status="success",
            file_name="test.xlsx",
            file_path="/test.xlsx",
            sheets_created=3,
        )
        s = str(response)
        assert "success" in s
        assert "test.xlsx" in s
        assert "3" in s

    def test_add_tab_response_str(self):
        from mcp_servers.sheets_server.models.response import AddTabResponse

        response = AddTabResponse(
            status="success", tab_name="Sheet2", file_path="/test.xlsx", rows_added=10
        )
        s = str(response)
        assert "success" in s
        assert "Sheet2" in s
        assert "10" in s

    def test_add_tab_response_without_rows(self):
        from mcp_servers.sheets_server.models.response import AddTabResponse

        response = AddTabResponse(
            status="success", tab_name="Sheet2", file_path="/test.xlsx"
        )
        s = str(response)
        assert "success" in s
        assert "rows_added" not in s

    def test_delete_tab_response_str(self):
        from mcp_servers.sheets_server.models.response import DeleteTabResponse

        response = DeleteTabResponse(
            status="success", tab_name="Sheet2", tab_index=1, file_path="/test.xlsx"
        )
        s = str(response)
        assert "success" in s
        assert "Sheet2" in s
        assert "1" in s

    def test_delete_spreadsheet_response_str(self):
        from mcp_servers.sheets_server.models.response import DeleteSpreadsheetResponse

        response = DeleteSpreadsheetResponse(status="success", file_path="/test.xlsx")
        s = str(response)
        assert "success" in s
        assert "/test.xlsx" in s

    def test_add_content_text_response_str(self):
        from mcp_servers.sheets_server.models.response import AddContentTextResponse

        response = AddContentTextResponse(
            status="success", cell="A1", tab_index=0, file_path="/test.xlsx"
        )
        s = str(response)
        assert "success" in s
        assert "A1" in s
        assert "0" in s

    def test_delete_content_cell_response_str(self):
        from mcp_servers.sheets_server.models.response import DeleteContentCellResponse

        response = DeleteContentCellResponse(
            status="success",
            cell="A1",
            tab_index=0,
            file_path="/test.xlsx",
            old_value="Hello",
        )
        s = str(response)
        assert "success" in s
        assert "Hello" in s

    def test_delete_content_cell_response_without_old_value(self):
        from mcp_servers.sheets_server.models.response import DeleteContentCellResponse

        response = DeleteContentCellResponse(
            status="success", cell="A1", tab_index=0, file_path="/test.xlsx"
        )
        s = str(response)
        assert "old_value" not in s

    def test_read_tab_single_cell_response_str(self):
        from mcp_servers.sheets_server.models.response import ReadTabSingleCellResponse

        response = ReadTabSingleCellResponse(
            cell="A1", value=123, formula="=SUM(B1:B10)"
        )
        s = str(response)
        assert "A1" in s
        assert "123" in s
        assert "=SUM(B1:B10)" in s

    def test_read_tab_single_cell_response_without_formula(self):
        from mcp_servers.sheets_server.models.response import ReadTabSingleCellResponse

        response = ReadTabSingleCellResponse(cell="A1", value="Hello")
        s = str(response)
        assert "A1" in s
        assert "Hello" in s
        assert "formula" not in s

    def test_list_tabs_response_str(self):
        from mcp_servers.sheets_server.models.response import (
            ListTabsResponse,
            WorksheetInfo,
        )

        response = ListTabsResponse(
            worksheets=[
                WorksheetInfo(name="Sheet1", index=0, row_count=10, column_count=5),
                WorksheetInfo(name="Sheet2", index=1, row_count=20, column_count=10),
            ]
        )
        s = str(response)
        assert "Sheet1" in s
        assert "Sheet2" in s
        assert "worksheets" in s


class TestResolveUnderRoot:
    """Tests for the _resolve_under_root function."""

    def test_resolve_path_without_leading_slash(self, monkeypatch):
        import mcp_servers.sheets_server.tools.edit_spreadsheet as edit_module

        monkeypatch.setattr(edit_module, "SHEETS_ROOT", "/test/root")
        from mcp_servers.sheets_server.tools.edit_spreadsheet import _resolve_under_root

        result = _resolve_under_root("path/to/file.xlsx")
        assert result == "/test/root/path/to/file.xlsx"

    def test_resolve_path_with_leading_slash(self, monkeypatch):
        import mcp_servers.sheets_server.tools.edit_spreadsheet as edit_module

        monkeypatch.setattr(edit_module, "SHEETS_ROOT", "/test/root")
        from mcp_servers.sheets_server.tools.edit_spreadsheet import _resolve_under_root

        result = _resolve_under_root("/path/to/file.xlsx")
        assert result == "/test/root/path/to/file.xlsx"


class TestAppendRowsFunction:
    """Tests for the _append_rows function."""

    def test_append_rows_success(self):
        from mcp_servers.sheets_server.tools.edit_spreadsheet import _append_rows

        wb = Workbook()
        ws = wb.active
        assert ws is not None
        # Add header row
        ws.append(["A", "B", "C"])

        result = _append_rows(ws, [[1, 2, 3], [4, 5, 6]], header_length=3)
        assert result is None
        assert ws.max_row == 3
        wb.close()

    def test_append_rows_length_mismatch(self):
        from mcp_servers.sheets_server.tools.edit_spreadsheet import _append_rows

        wb = Workbook()
        ws = wb.active
        assert ws is not None
        ws.append(["A", "B", "C"])

        result = _append_rows(ws, [[1, 2]], header_length=3)
        assert result is not None
        assert "must match header length" in result
        wb.close()

    def test_append_rows_no_header(self):
        from mcp_servers.sheets_server.tools.edit_spreadsheet import _append_rows

        wb = Workbook()
        ws = wb.active
        assert ws is not None

        result = _append_rows(ws, [[1, 2, 3], [4, 5, 6]], header_length=None)
        assert result is None
        assert ws.max_row == 2
        wb.close()


class TestMergeCellsOperation:
    """Tests for MergeCellsOperation model validation."""

    def test_valid_merge_cells(self):
        op = MergeCellsOperation(type="merge_cells", sheet="Sheet1", range="A1:D1")
        assert op.sheet == "Sheet1"
        assert op.range == "A1:D1"

    def test_merge_cells_range_normalized(self):
        op = MergeCellsOperation(type="merge_cells", sheet="Sheet1", range="a1:d1")
        assert op.range == "A1:D1"

    def test_merge_cells_invalid_range(self):
        with pytest.raises(ValidationError) as exc_info:
            MergeCellsOperation(type="merge_cells", sheet="Sheet1", range="A1")
        assert "Range must be like 'A1:D1'" in str(exc_info.value)

    def test_merge_cells_empty_sheet(self):
        with pytest.raises(ValidationError) as exc_info:
            MergeCellsOperation(type="merge_cells", sheet="", range="A1:D1")
        assert "Sheet name must not be empty" in str(exc_info.value)


class TestUnmergeCellsOperation:
    """Tests for UnmergeCellsOperation model validation."""

    def test_valid_unmerge_cells(self):
        op = UnmergeCellsOperation(type="unmerge_cells", sheet="Sheet1", range="A1:D1")
        assert op.sheet == "Sheet1"
        assert op.range == "A1:D1"

    def test_unmerge_cells_invalid_range(self):
        with pytest.raises(ValidationError) as exc_info:
            UnmergeCellsOperation(type="unmerge_cells", sheet="Sheet1", range="A:A")
        assert "Range must be like 'A1:D1'" in str(exc_info.value)


class TestSetColumnWidthOperation:
    """Tests for SetColumnWidthOperation model validation."""

    def test_valid_column_width(self):
        op = SetColumnWidthOperation(
            type="set_column_width", sheet="Sheet1", column="A", width=20.0
        )
        assert op.column == "A"
        assert op.width == 20.0

    def test_column_width_normalized(self):
        op = SetColumnWidthOperation(
            type="set_column_width", sheet="Sheet1", column="aa", width=15.5
        )
        assert op.column == "AA"

    def test_column_width_invalid_column(self):
        with pytest.raises(ValidationError) as exc_info:
            SetColumnWidthOperation(
                type="set_column_width", sheet="Sheet1", column="A1", width=20.0
            )
        assert "Column must be a letter" in str(exc_info.value)

    def test_column_width_negative(self):
        with pytest.raises(ValidationError) as exc_info:
            SetColumnWidthOperation(
                type="set_column_width", sheet="Sheet1", column="A", width=-5.0
            )
        assert "Width must be positive" in str(exc_info.value)

    def test_column_width_exceeds_max(self):
        with pytest.raises(ValidationError) as exc_info:
            SetColumnWidthOperation(
                type="set_column_width", sheet="Sheet1", column="A", width=300.0
            )
        assert "Width must not exceed 255" in str(exc_info.value)


class TestSetRowHeightOperation:
    """Tests for SetRowHeightOperation model validation."""

    def test_valid_row_height(self):
        op = SetRowHeightOperation(
            type="set_row_height", sheet="Sheet1", row=1, height=25.0
        )
        assert op.row == 1
        assert op.height == 25.0

    def test_row_height_invalid_row(self):
        with pytest.raises(ValidationError) as exc_info:
            SetRowHeightOperation(
                type="set_row_height", sheet="Sheet1", row=0, height=25.0
            )
        assert "Row must be at least 1" in str(exc_info.value)

    def test_row_height_negative(self):
        with pytest.raises(ValidationError) as exc_info:
            SetRowHeightOperation(
                type="set_row_height", sheet="Sheet1", row=1, height=-10.0
            )
        assert "Height must be positive" in str(exc_info.value)

    def test_row_height_exceeds_max(self):
        with pytest.raises(ValidationError) as exc_info:
            SetRowHeightOperation(
                type="set_row_height", sheet="Sheet1", row=1, height=500.0
            )
        assert "Height must not exceed 409" in str(exc_info.value)


class TestFreezePanesOperation:
    """Tests for FreezePanesOperation model validation."""

    def test_valid_freeze_panes(self):
        op = FreezePanesOperation(type="freeze_panes", sheet="Sheet1", cell="B2")
        assert op.cell == "B2"

    def test_freeze_panes_unfreeze(self):
        op = FreezePanesOperation(type="freeze_panes", sheet="Sheet1", cell=None)
        assert op.cell is None

    def test_freeze_panes_normalized(self):
        op = FreezePanesOperation(type="freeze_panes", sheet="Sheet1", cell="b2")
        assert op.cell == "B2"

    def test_freeze_panes_invalid_cell(self):
        with pytest.raises(ValidationError) as exc_info:
            FreezePanesOperation(type="freeze_panes", sheet="Sheet1", cell="B")
        assert "Cell must be like 'A1' or 'B2'" in str(exc_info.value)


class TestAddNamedRangeOperation:
    """Tests for AddNamedRangeOperation model validation."""

    def test_valid_named_range(self):
        op = AddNamedRangeOperation(
            type="add_named_range", name="MyRange", sheet="Sheet1", range="A1:B10"
        )
        assert op.name == "MyRange"
        assert op.range == "A1:B10"

    def test_named_range_with_underscore(self):
        op = AddNamedRangeOperation(
            type="add_named_range", name="_my_range", sheet="Sheet1", range="A1:C5"
        )
        assert op.name == "_my_range"

    def test_named_range_with_dots(self):
        op = AddNamedRangeOperation(
            type="add_named_range", name="Data.Range1", sheet="Sheet1", range="A1:C5"
        )
        assert op.name == "Data.Range1"

    def test_named_range_invalid_name_starts_with_digit(self):
        with pytest.raises(ValidationError) as exc_info:
            AddNamedRangeOperation(
                type="add_named_range", name="123Range", sheet="Sheet1", range="A1:B10"
            )
        assert "Name must start with a letter or underscore" in str(exc_info.value)

    def test_named_range_invalid_range(self):
        with pytest.raises(ValidationError) as exc_info:
            AddNamedRangeOperation(
                type="add_named_range", name="MyRange", sheet="Sheet1", range="A1"
            )
        assert "Range must be like 'A1:B10'" in str(exc_info.value)


class TestDeleteNamedRangeOperation:
    """Tests for DeleteNamedRangeOperation model validation."""

    def test_valid_delete_named_range(self):
        op = DeleteNamedRangeOperation(type="delete_named_range", name="MyRange")
        assert op.name == "MyRange"

    def test_delete_named_range_empty_name(self):
        with pytest.raises(ValidationError) as exc_info:
            DeleteNamedRangeOperation(type="delete_named_range", name="")
        assert "Name must not be empty" in str(exc_info.value)


class TestAddDataValidationOperation:
    """Tests for AddDataValidationOperation model validation."""

    def test_valid_list_validation(self):
        op = AddDataValidationOperation(
            type="add_data_validation",
            sheet="Sheet1",
            range="A1:A10",
            validation_type="list",
            formula1='"Option1,Option2,Option3"',
        )
        assert op.validation_type == "list"
        assert op.formula1 == '"Option1,Option2,Option3"'

    def test_valid_whole_number_validation(self):
        op = AddDataValidationOperation(
            type="add_data_validation",
            sheet="Sheet1",
            range="B1:B10",
            validation_type="whole",
            operator="between",
            formula1="1",
            formula2="100",
        )
        assert op.validation_type == "whole"
        assert op.operator == "between"

    def test_validation_type_normalized(self):
        op = AddDataValidationOperation(
            type="add_data_validation",
            sheet="Sheet1",
            range="A1:A10",
            validation_type="LIST",
        )
        assert op.validation_type == "list"

    def test_invalid_validation_type(self):
        with pytest.raises(ValidationError) as exc_info:
            AddDataValidationOperation(
                type="add_data_validation",
                sheet="Sheet1",
                range="A1:A10",
                validation_type="invalid",
            )
        assert "validation_type must be one of" in str(exc_info.value)

    def test_invalid_operator(self):
        with pytest.raises(ValidationError) as exc_info:
            AddDataValidationOperation(
                type="add_data_validation",
                sheet="Sheet1",
                range="A1:A10",
                validation_type="whole",
                operator="invalid",
            )
        assert "operator must be one of" in str(exc_info.value)


class TestAddConditionalFormattingOperation:
    """Tests for AddConditionalFormattingOperation model validation."""

    def test_valid_cell_is_rule(self):
        op = AddConditionalFormattingOperation(
            type="add_conditional_formatting",
            sheet="Sheet1",
            range="A1:A10",
            rule_type="cellIs",
            operator="greaterThan",
            formula="50",
            fill_color="FF0000",
        )
        assert op.rule_type == "cellIs"
        assert op.fill_color == "FF0000"

    def test_valid_color_scale(self):
        op = AddConditionalFormattingOperation(
            type="add_conditional_formatting",
            sheet="Sheet1",
            range="A1:A10",
            rule_type="colorScale",
            color_scale_colors=["FF0000", "00FF00"],
        )
        assert op.rule_type == "colorScale"
        assert op.color_scale_colors is not None
        assert len(op.color_scale_colors) == 2

    def test_valid_data_bar(self):
        op = AddConditionalFormattingOperation(
            type="add_conditional_formatting",
            sheet="Sheet1",
            range="A1:A10",
            rule_type="dataBar",
            data_bar_color="0000FF",
        )
        assert op.data_bar_color == "0000FF"

    def test_invalid_rule_type(self):
        with pytest.raises(ValidationError) as exc_info:
            AddConditionalFormattingOperation(
                type="add_conditional_formatting",
                sheet="Sheet1",
                range="A1:A10",
                rule_type="invalid",
            )
        assert "rule_type must be one of" in str(exc_info.value)

    def test_color_scale_too_few_colors(self):
        with pytest.raises(ValidationError) as exc_info:
            AddConditionalFormattingOperation(
                type="add_conditional_formatting",
                sheet="Sheet1",
                range="A1:A10",
                rule_type="colorScale",
                color_scale_colors=["FF0000"],
            )
        assert "color_scale_colors must have 2 or 3 colors" in str(exc_info.value)

    def test_color_scale_too_many_colors(self):
        with pytest.raises(ValidationError) as exc_info:
            AddConditionalFormattingOperation(
                type="add_conditional_formatting",
                sheet="Sheet1",
                range="A1:A10",
                rule_type="colorScale",
                color_scale_colors=["FF0000", "00FF00", "0000FF", "FFFF00"],
            )
        assert "color_scale_colors must have 2 or 3 colors" in str(exc_info.value)

    def test_invalid_color(self):
        with pytest.raises(ValidationError) as exc_info:
            AddConditionalFormattingOperation(
                type="add_conditional_formatting",
                sheet="Sheet1",
                range="A1:A10",
                rule_type="cellIs",
                fill_color="GGG",
            )
        assert "Color must be 6 or 8 hex digits" in str(exc_info.value)


class TestSetAutoFilterOperation:
    """Tests for SetAutoFilterOperation model validation."""

    def test_valid_auto_filter(self):
        op = SetAutoFilterOperation(
            type="set_auto_filter", sheet="Sheet1", range="A1:D10"
        )
        assert op.range == "A1:D10"

    def test_auto_filter_remove(self):
        op = SetAutoFilterOperation(type="set_auto_filter", sheet="Sheet1", range=None)
        assert op.range is None

    def test_auto_filter_invalid_range(self):
        with pytest.raises(ValidationError) as exc_info:
            SetAutoFilterOperation(type="set_auto_filter", sheet="Sheet1", range="A1")
        assert "Range must be like 'A1:D10'" in str(exc_info.value)


class TestSetNumberFormatOperation:
    """Tests for SetNumberFormatOperation model validation."""

    def test_valid_number_format(self):
        op = SetNumberFormatOperation(
            type="set_number_format", sheet="Sheet1", range="A1:A10", format="#,##0.00"
        )
        assert op.format == "#,##0.00"

    def test_valid_percentage_format(self):
        op = SetNumberFormatOperation(
            type="set_number_format", sheet="Sheet1", range="B1:B5", format="0%"
        )
        assert op.format == "0%"

    def test_valid_date_format(self):
        op = SetNumberFormatOperation(
            type="set_number_format", sheet="Sheet1", range="C1", format="yyyy-mm-dd"
        )
        assert op.format == "yyyy-mm-dd"

    def test_number_format_empty(self):
        with pytest.raises(ValidationError) as exc_info:
            SetNumberFormatOperation(
                type="set_number_format", sheet="Sheet1", range="A1", format=""
            )
        assert "Format must not be empty" in str(exc_info.value)


class TestAddImageOperation:
    """Tests for AddImageOperation model validation."""

    def test_valid_add_image(self):
        op = AddImageOperation(
            type="add_image", sheet="Sheet1", image_path="/images/logo.png", cell="A1"
        )
        assert op.image_path == "/images/logo.png"
        assert op.cell == "A1"

    def test_add_image_with_dimensions(self):
        op = AddImageOperation(
            type="add_image",
            sheet="Sheet1",
            image_path="/images/logo.png",
            cell="B5",
            width=200,
            height=100,
        )
        assert op.width == 200
        assert op.height == 100

    def test_add_image_invalid_cell(self):
        with pytest.raises(ValidationError) as exc_info:
            AddImageOperation(
                type="add_image",
                sheet="Sheet1",
                image_path="/images/logo.png",
                cell="A",
            )
        assert "Cell must be like 'A1'" in str(exc_info.value)

    def test_add_image_empty_path(self):
        with pytest.raises(ValidationError) as exc_info:
            AddImageOperation(
                type="add_image", sheet="Sheet1", image_path="", cell="A1"
            )
        assert "Image path must not be empty" in str(exc_info.value)

    def test_add_image_negative_dimension(self):
        with pytest.raises(ValidationError) as exc_info:
            AddImageOperation(
                type="add_image",
                sheet="Sheet1",
                image_path="/images/logo.png",
                cell="A1",
                width=-100,
            )
        assert "Dimension must be positive" in str(exc_info.value)


class TestEditSpreadsheetIntegration:
    """Integration tests for edit_spreadsheet with new operations."""

    @pytest.fixture
    def temp_workbook(self):
        """Create a temporary workbook for testing."""
        with tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as f:
            wb = Workbook()
            ws = wb.active
            assert ws is not None
            ws.title = "Sheet1"
            # Add some data
            for row in range(1, 11):
                for col in range(1, 5):
                    ws.cell(row=row, column=col, value=row * col)
            wb.save(f.name)
            wb.close()
            yield f.name
            os.unlink(f.name)

    @pytest.fixture
    def temp_image(self):
        """Create a temporary image for testing."""
        from PIL import Image as PILImage

        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            # Create a small red image using PIL
            img = PILImage.new("RGB", (10, 10), color="red")
            img.save(f.name)
            yield f.name
            os.unlink(f.name)

    def test_merge_cells_integration(self, temp_workbook):
        """Test merge cells operation actually merges cells."""
        wb = load_workbook(temp_workbook)
        ws = wb.active
        assert ws is not None
        ws.merge_cells("A1:D1")
        wb.save(temp_workbook)
        wb.close()

        wb = load_workbook(temp_workbook)
        ws = wb.active
        assert ws is not None
        assert "A1:D1" in [str(m) for m in ws.merged_cells.ranges]
        wb.close()

    def test_unmerge_cells_integration(self, temp_workbook):
        """Test unmerge cells operation actually unmerges cells."""
        wb = load_workbook(temp_workbook)
        ws = wb.active
        assert ws is not None
        ws.merge_cells("A1:D1")
        wb.save(temp_workbook)
        wb.close()

        wb = load_workbook(temp_workbook)
        ws = wb.active
        assert ws is not None
        ws.unmerge_cells("A1:D1")
        wb.save(temp_workbook)
        wb.close()

        wb = load_workbook(temp_workbook)
        ws = wb.active
        assert ws is not None
        assert "A1:D1" not in [str(m) for m in ws.merged_cells.ranges]
        wb.close()

    def test_column_width_integration(self, temp_workbook):
        """Test column width operation sets width correctly."""
        wb = load_workbook(temp_workbook)
        ws = wb.active
        assert ws is not None
        ws.column_dimensions["A"].width = 30
        wb.save(temp_workbook)
        wb.close()

        wb = load_workbook(temp_workbook)
        ws = wb.active
        assert ws is not None
        assert ws.column_dimensions["A"].width == 30
        wb.close()

    def test_row_height_integration(self, temp_workbook):
        """Test row height operation sets height correctly."""
        wb = load_workbook(temp_workbook)
        ws = wb.active
        assert ws is not None
        ws.row_dimensions[1].height = 40
        wb.save(temp_workbook)
        wb.close()

        wb = load_workbook(temp_workbook)
        ws = wb.active
        assert ws is not None
        assert ws.row_dimensions[1].height == 40
        wb.close()

    def test_freeze_panes_integration(self, temp_workbook):
        """Test freeze panes operation freezes correctly."""
        wb = load_workbook(temp_workbook)
        ws = wb.active
        assert ws is not None
        ws.freeze_panes = "B2"
        wb.save(temp_workbook)
        wb.close()

        wb = load_workbook(temp_workbook)
        ws = wb.active
        assert ws is not None
        assert ws.freeze_panes == "B2"
        wb.close()

    def test_named_range_integration(self, temp_workbook):
        """Test named range operations."""
        from openpyxl.workbook.defined_name import DefinedName

        wb = load_workbook(temp_workbook)
        defn = DefinedName("TestRange", attr_text="'Sheet1'!$A$1:$B$10")
        wb.defined_names.add(defn)
        wb.save(temp_workbook)
        wb.close()

        wb = load_workbook(temp_workbook)
        assert "TestRange" in wb.defined_names
        wb.close()

    def test_data_validation_integration(self, temp_workbook):
        """Test data validation operation."""
        from openpyxl.worksheet.datavalidation import DataValidation

        wb = load_workbook(temp_workbook)
        ws = wb.active
        assert ws is not None
        dv = DataValidation(type="list", formula1='"Yes,No,Maybe"', allow_blank=True)
        dv.add("E1:E10")
        ws.add_data_validation(dv)
        wb.save(temp_workbook)
        wb.close()

        wb = load_workbook(temp_workbook)
        ws = wb.active
        assert ws is not None
        assert len(ws.data_validations.dataValidation) > 0
        wb.close()

    def test_auto_filter_integration(self, temp_workbook):
        """Test auto filter operation."""
        wb = load_workbook(temp_workbook)
        ws = wb.active
        assert ws is not None
        ws.auto_filter.ref = "A1:D10"
        wb.save(temp_workbook)
        wb.close()

        wb = load_workbook(temp_workbook)
        ws = wb.active
        assert ws is not None
        assert ws.auto_filter.ref == "A1:D10"
        wb.close()

    def test_number_format_integration(self, temp_workbook):
        """Test number format operation."""
        wb = load_workbook(temp_workbook)
        ws = wb.active
        assert ws is not None
        ws["A1"].number_format = "#,##0.00"
        wb.save(temp_workbook)
        wb.close()

        wb = load_workbook(temp_workbook)
        ws = wb.active
        assert ws is not None
        assert ws["A1"].number_format == "#,##0.00"
        wb.close()

    def test_conditional_formatting_color_scale_integration(self, temp_workbook):
        """Test conditional formatting with color scale."""
        from openpyxl.formatting.rule import ColorScaleRule

        wb = load_workbook(temp_workbook)
        ws = wb.active
        assert ws is not None
        rule = ColorScaleRule(
            start_type="min",
            start_color="FF0000",
            end_type="max",
            end_color="00FF00",
        )
        ws.conditional_formatting.add("A1:A10", rule)
        wb.save(temp_workbook)
        wb.close()

        wb = load_workbook(temp_workbook)
        ws = wb.active
        assert ws is not None
        assert len(list(ws.conditional_formatting)) > 0
        wb.close()

    def test_conditional_formatting_data_bar_integration(self, temp_workbook):
        """Test conditional formatting with data bar."""
        from openpyxl.formatting.rule import DataBarRule

        wb = load_workbook(temp_workbook)
        ws = wb.active
        assert ws is not None
        rule = DataBarRule(
            start_type="min",
            end_type="max",
            color="638EC6",
        )
        ws.conditional_formatting.add("B1:B10", rule)
        wb.save(temp_workbook)
        wb.close()

        wb = load_workbook(temp_workbook)
        ws = wb.active
        assert ws is not None
        assert len(list(ws.conditional_formatting)) > 0
        wb.close()

    def test_add_image_integration(self, temp_workbook, temp_image):
        """Test adding image to worksheet."""
        from openpyxl.drawing.image import Image

        wb = load_workbook(temp_workbook)
        ws = wb.active
        assert ws is not None
        img = Image(temp_image)
        ws.add_image(img, "E1")
        wb.save(temp_workbook)
        wb.close()

        wb = load_workbook(temp_workbook)
        ws = wb.active
        assert ws is not None
        assert len(ws._images) > 0  # pyright: ignore[reportAttributeAccessIssue]
        wb.close()


class TestFormatCellFunction:
    """Tests for the _format_cell function."""

    def test_format_cell_font_properties(self):
        from mcp_servers.sheets_server.models.sheet import FormatCellsOperation
        from mcp_servers.sheets_server.tools.edit_spreadsheet import _format_cell

        wb = Workbook()
        ws = wb.active
        assert ws is not None
        cell = ws["A1"]
        cell.value = "Test"

        op = FormatCellsOperation(
            type="format_cells",
            sheet="Sheet",
            range="A1",
            font_name="Arial",
            font_size=14,
            font_bold=True,
            font_italic=True,
            font_underline=True,
            font_color="FF0000",
        )
        _format_cell(cell, op)

        assert cell.font.name == "Arial"
        assert cell.font.size == 14
        assert cell.font.bold is True
        assert cell.font.italic is True
        assert cell.font.underline == "single"
        wb.close()

    def test_format_cell_fill_properties(self):
        from mcp_servers.sheets_server.models.sheet import FormatCellsOperation
        from mcp_servers.sheets_server.tools.edit_spreadsheet import _format_cell

        wb = Workbook()
        ws = wb.active
        assert ws is not None
        cell = ws["A1"]
        cell.value = "Test"

        op = FormatCellsOperation(
            type="format_cells",
            sheet="Sheet",
            range="A1",
            fill_color="00FF00",
            fill_pattern="solid",
        )
        _format_cell(cell, op)

        assert cell.fill.start_color.rgb == "FF00FF00"
        assert cell.fill.fill_type == "solid"
        wb.close()

    def test_format_cell_alignment_properties(self):
        from mcp_servers.sheets_server.models.sheet import FormatCellsOperation
        from mcp_servers.sheets_server.tools.edit_spreadsheet import _format_cell

        wb = Workbook()
        ws = wb.active
        assert ws is not None
        cell = ws["A1"]
        cell.value = "Test"

        op = FormatCellsOperation(
            type="format_cells",
            sheet="Sheet",
            range="A1",
            horizontal_alignment="center",
            vertical_alignment="top",
            wrap_text=True,
        )
        _format_cell(cell, op)

        assert cell.alignment.horizontal == "center"
        assert cell.alignment.vertical == "top"
        assert cell.alignment.wrap_text is True
        wb.close()

    def test_format_cell_border_properties(self):
        from mcp_servers.sheets_server.models.sheet import FormatCellsOperation
        from mcp_servers.sheets_server.tools.edit_spreadsheet import _format_cell

        wb = Workbook()
        ws = wb.active
        assert ws is not None
        cell = ws["A1"]
        cell.value = "Test"

        op = FormatCellsOperation(
            type="format_cells",
            sheet="Sheet",
            range="A1",
            border_style="thin",
            border_color="0000FF",
            border_sides=["left", "right"],
        )
        _format_cell(cell, op)

        assert cell.border.left.style == "thin"
        assert cell.border.right.style == "thin"
        wb.close()

    def test_format_cell_font_underline_false(self):
        from mcp_servers.sheets_server.models.sheet import FormatCellsOperation
        from mcp_servers.sheets_server.tools.edit_spreadsheet import _format_cell

        wb = Workbook()
        ws = wb.active
        assert ws is not None
        cell = ws["A1"]
        cell.value = "Test"

        op = FormatCellsOperation(
            type="format_cells",
            sheet="Sheet",
            range="A1",
            font_underline=False,
        )
        _format_cell(cell, op)

        assert cell.font.underline is None
        wb.close()


class TestApplyFormattingFunction:
    """Tests for the _apply_formatting function."""

    def test_apply_formatting_single_cell(self):
        from mcp_servers.sheets_server.models.sheet import FormatCellsOperation
        from mcp_servers.sheets_server.tools.edit_spreadsheet import _apply_formatting

        wb = Workbook()
        ws = wb.active
        assert ws is not None
        ws["A1"] = "Test"

        op = FormatCellsOperation(
            type="format_cells",
            sheet="Sheet",
            range="A1",
            font_bold=True,
        )
        count = _apply_formatting(ws, op)

        assert count == 1
        assert ws["A1"].font.bold is True
        wb.close()

    def test_apply_formatting_range(self):
        from mcp_servers.sheets_server.models.sheet import FormatCellsOperation
        from mcp_servers.sheets_server.tools.edit_spreadsheet import _apply_formatting

        wb = Workbook()
        ws = wb.active
        assert ws is not None
        for row in range(1, 4):
            for col in range(1, 3):
                ws.cell(row=row, column=col, value="Test")

        op = FormatCellsOperation(
            type="format_cells",
            sheet="Sheet",
            range="A1:B3",
            fill_color="FFFF00",
        )
        count = _apply_formatting(ws, op)

        assert count == 6
        assert ws["A1"].fill.start_color.rgb == "FFFFFF00"
        assert ws["B3"].fill.start_color.rgb == "FFFFFF00"
        wb.close()


class TestHelperFunctions:
    """Tests for helper functions in edit_spreadsheet."""

    def test_parse_hex_color_6_digit(self):
        from mcp_servers.sheets_server.tools.edit_spreadsheet import _parse_hex_color

        result = _parse_hex_color("FF0000")
        assert result == "FFFF0000"

    def test_parse_hex_color_8_digit(self):
        from mcp_servers.sheets_server.tools.edit_spreadsheet import _parse_hex_color

        result = _parse_hex_color("80FF0000")
        assert result == "80FF0000"

    def test_parse_hex_color_with_hash(self):
        from mcp_servers.sheets_server.tools.edit_spreadsheet import _parse_hex_color

        result = _parse_hex_color("#FF0000")
        assert result == "FFFF0000"

    def test_parse_hex_color_none(self):
        from mcp_servers.sheets_server.tools.edit_spreadsheet import _parse_hex_color

        result = _parse_hex_color(None)
        assert result is None

    def test_parse_hex_color_invalid(self):
        from mcp_servers.sheets_server.tools.edit_spreadsheet import _parse_hex_color

        with pytest.raises(ValueError) as exc_info:
            _parse_hex_color("GGG")
        assert "Invalid color format" in str(exc_info.value)

    def test_get_or_create_sheet_existing(self):
        from mcp_servers.sheets_server.tools.edit_spreadsheet import (
            _get_or_create_sheet,
        )

        wb = Workbook()
        ws = _get_or_create_sheet(wb, "Sheet")
        assert ws.title == "Sheet"
        wb.close()

    def test_get_or_create_sheet_new(self):
        from mcp_servers.sheets_server.tools.edit_spreadsheet import (
            _get_or_create_sheet,
        )

        wb = Workbook()
        ws = _get_or_create_sheet(wb, "NewSheet")
        assert ws.title == "NewSheet"
        assert "NewSheet" in wb.sheetnames
        wb.close()

    def test_apply_number_format_single_cell(self):
        from mcp_servers.sheets_server.models.sheet import SetNumberFormatOperation
        from mcp_servers.sheets_server.tools.edit_spreadsheet import (
            _apply_number_format,
        )

        wb = Workbook()
        ws = wb.active
        assert ws is not None
        ws["A1"] = 1234.5678
        op = SetNumberFormatOperation(
            type="set_number_format", sheet="Sheet", range="A1", format="#,##0.00"
        )
        count = _apply_number_format(ws, op)
        assert count == 1
        assert ws["A1"].number_format == "#,##0.00"
        wb.close()

    def test_apply_number_format_range(self):
        from mcp_servers.sheets_server.models.sheet import SetNumberFormatOperation
        from mcp_servers.sheets_server.tools.edit_spreadsheet import (
            _apply_number_format,
        )

        wb = Workbook()
        ws = wb.active
        assert ws is not None
        for row in range(1, 4):
            for col in range(1, 3):
                ws.cell(row=row, column=col, value=row * col * 100)
        op = SetNumberFormatOperation(
            type="set_number_format", sheet="Sheet", range="A1:B3", format="0%"
        )
        count = _apply_number_format(ws, op)
        assert count == 6
        assert ws["A1"].number_format == "0%"
        assert ws["B3"].number_format == "0%"
        wb.close()


class TestConditionalFormattingRules:
    """Tests for conditional formatting rule creation."""

    def test_create_color_scale_2_colors(self):
        from mcp_servers.sheets_server.models.sheet import (
            AddConditionalFormattingOperation,
        )
        from mcp_servers.sheets_server.tools.edit_spreadsheet import (
            _create_conditional_formatting_rule,
        )

        op = AddConditionalFormattingOperation(
            type="add_conditional_formatting",
            sheet="Sheet1",
            range="A1:A10",
            rule_type="colorScale",
            color_scale_colors=["FF0000", "00FF00"],
        )
        rule = _create_conditional_formatting_rule(op)
        assert rule.type == "colorScale"

    def test_create_color_scale_3_colors(self):
        from mcp_servers.sheets_server.models.sheet import (
            AddConditionalFormattingOperation,
        )
        from mcp_servers.sheets_server.tools.edit_spreadsheet import (
            _create_conditional_formatting_rule,
        )

        op = AddConditionalFormattingOperation(
            type="add_conditional_formatting",
            sheet="Sheet1",
            range="A1:A10",
            rule_type="colorScale",
            color_scale_colors=["FF0000", "FFFF00", "00FF00"],
        )
        rule = _create_conditional_formatting_rule(op)
        assert rule.type == "colorScale"

    def test_create_data_bar_rule(self):
        from mcp_servers.sheets_server.models.sheet import (
            AddConditionalFormattingOperation,
        )
        from mcp_servers.sheets_server.tools.edit_spreadsheet import (
            _create_conditional_formatting_rule,
        )

        op = AddConditionalFormattingOperation(
            type="add_conditional_formatting",
            sheet="Sheet1",
            range="A1:A10",
            rule_type="dataBar",
            data_bar_color="0000FF",
        )
        rule = _create_conditional_formatting_rule(op)
        assert rule.type == "dataBar"

    def test_create_expression_rule(self):
        from mcp_servers.sheets_server.models.sheet import (
            AddConditionalFormattingOperation,
        )
        from mcp_servers.sheets_server.tools.edit_spreadsheet import (
            _create_conditional_formatting_rule,
        )

        op = AddConditionalFormattingOperation(
            type="add_conditional_formatting",
            sheet="Sheet1",
            range="A1:A10",
            rule_type="expression",
            formula="$A1>10",
            fill_color="FFFF00",
        )
        rule = _create_conditional_formatting_rule(op)
        assert rule.type == "expression"

    def test_create_top10_rule(self):
        from mcp_servers.sheets_server.models.sheet import (
            AddConditionalFormattingOperation,
        )
        from mcp_servers.sheets_server.tools.edit_spreadsheet import (
            _create_conditional_formatting_rule,
        )

        op = AddConditionalFormattingOperation(
            type="add_conditional_formatting",
            sheet="Sheet1",
            range="A1:A10",
            rule_type="top10",
            rank=5,
            percent=True,
            fill_color="00FF00",
        )
        rule = _create_conditional_formatting_rule(op)
        assert rule.type == "top10"

    def test_create_above_average_rule(self):
        from mcp_servers.sheets_server.models.sheet import (
            AddConditionalFormattingOperation,
        )
        from mcp_servers.sheets_server.tools.edit_spreadsheet import (
            _create_conditional_formatting_rule,
        )

        op = AddConditionalFormattingOperation(
            type="add_conditional_formatting",
            sheet="Sheet1",
            range="A1:A10",
            rule_type="aboveAverage",
            fill_color="00FF00",
        )
        rule = _create_conditional_formatting_rule(op)
        assert rule.type == "aboveAverage"

    def test_create_duplicate_values_rule(self):
        from mcp_servers.sheets_server.models.sheet import (
            AddConditionalFormattingOperation,
        )
        from mcp_servers.sheets_server.tools.edit_spreadsheet import (
            _create_conditional_formatting_rule,
        )

        op = AddConditionalFormattingOperation(
            type="add_conditional_formatting",
            sheet="Sheet1",
            range="A1:A10",
            rule_type="duplicateValues",
            fill_color="FF0000",
        )
        rule = _create_conditional_formatting_rule(op)
        assert rule.type == "duplicateValues"

    def test_create_unique_values_rule(self):
        from mcp_servers.sheets_server.models.sheet import (
            AddConditionalFormattingOperation,
        )
        from mcp_servers.sheets_server.tools.edit_spreadsheet import (
            _create_conditional_formatting_rule,
        )

        op = AddConditionalFormattingOperation(
            type="add_conditional_formatting",
            sheet="Sheet1",
            range="A1:A10",
            rule_type="uniqueValues",
            fill_color="00FF00",
        )
        rule = _create_conditional_formatting_rule(op)
        assert rule.type == "uniqueValues"

    def test_create_contains_text_rule(self):
        from mcp_servers.sheets_server.models.sheet import (
            AddConditionalFormattingOperation,
        )
        from mcp_servers.sheets_server.tools.edit_spreadsheet import (
            _create_conditional_formatting_rule,
        )

        op = AddConditionalFormattingOperation(
            type="add_conditional_formatting",
            sheet="Sheet1",
            range="A1:A10",
            rule_type="containsText",
            text="error",
            fill_color="FF0000",
        )
        rule = _create_conditional_formatting_rule(op)
        assert rule.type == "containsText"

    def test_create_not_contains_text_rule(self):
        from mcp_servers.sheets_server.models.sheet import (
            AddConditionalFormattingOperation,
        )
        from mcp_servers.sheets_server.tools.edit_spreadsheet import (
            _create_conditional_formatting_rule,
        )

        op = AddConditionalFormattingOperation(
            type="add_conditional_formatting",
            sheet="Sheet1",
            range="A1:A10",
            rule_type="notContainsText",
            text="error",
            fill_color="00FF00",
        )
        rule = _create_conditional_formatting_rule(op)
        assert rule.type == "notContainsText"

    def test_create_begins_with_rule(self):
        from mcp_servers.sheets_server.models.sheet import (
            AddConditionalFormattingOperation,
        )
        from mcp_servers.sheets_server.tools.edit_spreadsheet import (
            _create_conditional_formatting_rule,
        )

        op = AddConditionalFormattingOperation(
            type="add_conditional_formatting",
            sheet="Sheet1",
            range="A1:A10",
            rule_type="beginsWith",
            text="ABC",
            fill_color="0000FF",
        )
        rule = _create_conditional_formatting_rule(op)
        assert rule.type == "beginsWith"

    def test_create_ends_with_rule(self):
        from mcp_servers.sheets_server.models.sheet import (
            AddConditionalFormattingOperation,
        )
        from mcp_servers.sheets_server.tools.edit_spreadsheet import (
            _create_conditional_formatting_rule,
        )

        op = AddConditionalFormattingOperation(
            type="add_conditional_formatting",
            sheet="Sheet1",
            range="A1:A10",
            rule_type="endsWith",
            text="XYZ",
            fill_color="FF00FF",
        )
        rule = _create_conditional_formatting_rule(op)
        assert rule.type == "endsWith"

    def test_create_contains_blanks_rule(self):
        from mcp_servers.sheets_server.models.sheet import (
            AddConditionalFormattingOperation,
        )
        from mcp_servers.sheets_server.tools.edit_spreadsheet import (
            _create_conditional_formatting_rule,
        )

        op = AddConditionalFormattingOperation(
            type="add_conditional_formatting",
            sheet="Sheet1",
            range="A1:A10",
            rule_type="containsBlanks",
            fill_color="CCCCCC",
        )
        rule = _create_conditional_formatting_rule(op)
        assert rule.type == "containsBlanks"

    def test_create_not_contains_blanks_rule(self):
        from mcp_servers.sheets_server.models.sheet import (
            AddConditionalFormattingOperation,
        )
        from mcp_servers.sheets_server.tools.edit_spreadsheet import (
            _create_conditional_formatting_rule,
        )

        op = AddConditionalFormattingOperation(
            type="add_conditional_formatting",
            sheet="Sheet1",
            range="A1:A10",
            rule_type="notContainsBlanks",
            fill_color="00FF00",
        )
        rule = _create_conditional_formatting_rule(op)
        assert rule.type == "notContainsBlanks"

    def test_create_rule_with_font_formatting(self):
        from mcp_servers.sheets_server.models.sheet import (
            AddConditionalFormattingOperation,
        )
        from mcp_servers.sheets_server.tools.edit_spreadsheet import (
            _create_conditional_formatting_rule,
        )

        op = AddConditionalFormattingOperation(
            type="add_conditional_formatting",
            sheet="Sheet1",
            range="A1:A10",
            rule_type="expression",
            formula="$A1>10",
            font_color="FF0000",
            font_bold=True,
            font_italic=True,
        )
        rule = _create_conditional_formatting_rule(op)
        assert rule.type == "expression"

    def test_create_rule_unsupported_type(self):
        from mcp_servers.sheets_server.models.sheet import (
            AddConditionalFormattingOperation,
        )
        from mcp_servers.sheets_server.tools.edit_spreadsheet import (
            _create_conditional_formatting_rule,
        )

        # Create a mock operation with an unsupported rule type
        # We need to bypass validation for this test
        op = AddConditionalFormattingOperation.model_construct(
            type="add_conditional_formatting",
            sheet="Sheet1",
            range="A1:A10",
            rule_type="unsupported",
        )
        with pytest.raises(ValueError) as exc_info:
            _create_conditional_formatting_rule(op)
        assert "Unsupported rule type" in str(exc_info.value)


class TestEditSpreadsheetValidation:
    """Tests for edit_spreadsheet input validation."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    @pytest.fixture
    def setup_module(self, temp_dir, monkeypatch):
        """Set up the module with the temp directory."""
        import mcp_servers.sheets_server.tools.edit_spreadsheet as edit_module

        monkeypatch.setattr(edit_module, "SHEETS_ROOT", temp_dir)
        return temp_dir

    @pytest.mark.asyncio
    async def test_empty_file_path(self, setup_module):
        """Test with empty file path."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        result = await edit_spreadsheet(file_path="", operations=[])
        assert "File path is required" in result

    @pytest.mark.asyncio
    async def test_file_path_not_starting_with_slash(self, setup_module):
        """Test with file path not starting with /."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        result = await edit_spreadsheet(file_path="test.xlsx", operations=[])
        assert "File path must start with /" in result

    @pytest.mark.asyncio
    async def test_file_path_not_xlsx(self, setup_module):
        """Test with non-xlsx file path."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        result = await edit_spreadsheet(file_path="/test.csv", operations=[])
        assert "File path must end with .xlsx" in result

    @pytest.mark.asyncio
    async def test_empty_operations(self, setup_module):
        """Test with empty operations list."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        result = await edit_spreadsheet(file_path="/test.xlsx", operations=[])
        assert "Operations must be a non-empty list" in result

    @pytest.mark.asyncio
    async def test_file_not_found(self, setup_module):
        """Test with non-existent file."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        result = await edit_spreadsheet(
            file_path="/nonexistent.xlsx",
            operations=[{"type": "merge_cells", "sheet": "Sheet1", "range": "A1:B1"}],
        )
        assert "File not found" in result

    @pytest.mark.asyncio
    async def test_invalid_operation_type(self, setup_module, temp_dir):
        """Test with invalid operation type."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        # Create a workbook
        filepath = os.path.join(temp_dir, "test.xlsx")
        wb = Workbook()
        wb.save(filepath)
        wb.close()

        result = await edit_spreadsheet(
            file_path="/test.xlsx",
            operations=[{"type": "invalid_type", "sheet": "Sheet1"}],
        )
        assert "Invalid operations payload" in result

    @pytest.mark.asyncio
    async def test_format_cells_sheet_not_exists(self, setup_module, temp_dir):
        """Test format_cells with non-existent sheet."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        filepath = os.path.join(temp_dir, "test.xlsx")
        wb = Workbook()
        wb.save(filepath)
        wb.close()

        result = await edit_spreadsheet(
            file_path="/test.xlsx",
            operations=[
                {
                    "type": "format_cells",
                    "sheet": "NonExistent",
                    "range": "A1",
                    "font_bold": True,
                }
            ],
        )
        assert "Sheet 'NonExistent' does not exist" in result

    @pytest.mark.asyncio
    async def test_data_validation_with_all_options(self, setup_module, temp_dir):
        """Test data validation with all options."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        filepath = os.path.join(temp_dir, "test.xlsx")
        wb = Workbook()
        ws = wb.active
        assert ws is not None
        ws.title = "Sheet1"
        wb.save(filepath)
        wb.close()

        result = await edit_spreadsheet(
            file_path="/test.xlsx",
            operations=[
                {
                    "type": "add_data_validation",
                    "sheet": "Sheet1",
                    "range": "A1:A10",
                    "validation_type": "whole",
                    "operator": "between",
                    "formula1": "1",
                    "formula2": "100",
                    "allow_blank": False,
                    "show_error_message": True,
                    "error_title": "Invalid Input",
                    "error_message": "Please enter a number between 1 and 100",
                    "show_input_message": True,
                    "input_title": "Enter Number",
                    "input_message": "Please enter a number between 1 and 100",
                }
            ],
        )
        assert "success" in result

    @pytest.mark.asyncio
    async def test_add_image_file_not_found(self, setup_module, temp_dir):
        """Test add_image with non-existent image file."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        filepath = os.path.join(temp_dir, "test.xlsx")
        wb = Workbook()
        ws = wb.active
        assert ws is not None
        ws.title = "Sheet1"
        wb.save(filepath)
        wb.close()

        result = await edit_spreadsheet(
            file_path="/test.xlsx",
            operations=[
                {
                    "type": "add_image",
                    "sheet": "Sheet1",
                    "image_path": "/nonexistent.png",
                    "cell": "A1",
                }
            ],
        )
        assert "Image file not found" in result

    @pytest.mark.asyncio
    async def test_add_image_success(self, setup_module, temp_dir):
        """Test add_image successfully."""
        from PIL import Image as PILImage

        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        filepath = os.path.join(temp_dir, "test.xlsx")
        wb = Workbook()
        ws = wb.active
        assert ws is not None
        ws.title = "Sheet1"
        wb.save(filepath)
        wb.close()

        # Create image in the temp dir (which is our SHEETS_ROOT)
        img_path = os.path.join(temp_dir, "test.png")
        img = PILImage.new("RGB", (10, 10), color="blue")
        img.save(img_path)

        result = await edit_spreadsheet(
            file_path="/test.xlsx",
            operations=[
                {
                    "type": "add_image",
                    "sheet": "Sheet1",
                    "image_path": "/test.png",
                    "cell": "A1",
                    "width": 100,
                    "height": 50,
                }
            ],
        )
        assert "success" in result

    @pytest.mark.asyncio
    async def test_conditional_formatting_with_all_options(
        self, setup_module, temp_dir
    ):
        """Test conditional formatting with all style options."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        filepath = os.path.join(temp_dir, "test.xlsx")
        wb = Workbook()
        ws = wb.active
        assert ws is not None
        ws.title = "Sheet1"
        for row in range(1, 11):
            ws.cell(row=row, column=1, value=row)
        wb.save(filepath)
        wb.close()

        result = await edit_spreadsheet(
            file_path="/test.xlsx",
            operations=[
                {
                    "type": "add_conditional_formatting",
                    "sheet": "Sheet1",
                    "range": "A1:A10",
                    "rule_type": "top10",
                    "rank": 3,
                    "percent": False,
                    "font_color": "FF0000",
                    "fill_color": "FFFF00",
                    "font_bold": True,
                    "font_italic": True,
                }
            ],
        )
        assert "success" in result

    @pytest.mark.asyncio
    async def test_remove_auto_filter(self, setup_module, temp_dir):
        """Test removing auto filter by setting range to None."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        filepath = os.path.join(temp_dir, "test.xlsx")
        wb = Workbook()
        ws = wb.active
        assert ws is not None
        ws.title = "Sheet1"
        ws.auto_filter.ref = "A1:D10"
        wb.save(filepath)
        wb.close()

        result = await edit_spreadsheet(
            file_path="/test.xlsx",
            operations=[{"type": "set_auto_filter", "sheet": "Sheet1", "range": None}],
        )
        assert "success" in result


class TestMoreEditOperations:
    """Tests for more edit operations error paths."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    @pytest.fixture
    def setup_module(self, temp_dir, monkeypatch):
        """Set up the module with the temp directory."""
        import mcp_servers.sheets_server.tools.edit_spreadsheet as edit_module

        monkeypatch.setattr(edit_module, "SHEETS_ROOT", temp_dir)
        return temp_dir

    @pytest.mark.asyncio
    async def test_merge_cells_sheet_not_exists(self, setup_module, temp_dir):
        """Test merge_cells with non-existent sheet."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        filepath = os.path.join(temp_dir, "test.xlsx")
        wb = Workbook()
        wb.save(filepath)
        wb.close()

        result = await edit_spreadsheet(
            file_path="/test.xlsx",
            operations=[
                {"type": "merge_cells", "sheet": "NonExistent", "range": "A1:B1"}
            ],
        )
        assert "Sheet 'NonExistent' does not exist" in result

    @pytest.mark.asyncio
    async def test_unmerge_cells_sheet_not_exists(self, setup_module, temp_dir):
        """Test unmerge_cells with non-existent sheet."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        filepath = os.path.join(temp_dir, "test.xlsx")
        wb = Workbook()
        wb.save(filepath)
        wb.close()

        result = await edit_spreadsheet(
            file_path="/test.xlsx",
            operations=[
                {"type": "unmerge_cells", "sheet": "NonExistent", "range": "A1:B1"}
            ],
        )
        assert "Sheet 'NonExistent' does not exist" in result

    @pytest.mark.asyncio
    async def test_set_column_width_sheet_not_exists(self, setup_module, temp_dir):
        """Test set_column_width with non-existent sheet."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        filepath = os.path.join(temp_dir, "test.xlsx")
        wb = Workbook()
        wb.save(filepath)
        wb.close()

        result = await edit_spreadsheet(
            file_path="/test.xlsx",
            operations=[
                {
                    "type": "set_column_width",
                    "sheet": "NonExistent",
                    "column": "A",
                    "width": 20,
                }
            ],
        )
        assert "Sheet 'NonExistent' does not exist" in result

    @pytest.mark.asyncio
    async def test_set_row_height_sheet_not_exists(self, setup_module, temp_dir):
        """Test set_row_height with non-existent sheet."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        filepath = os.path.join(temp_dir, "test.xlsx")
        wb = Workbook()
        wb.save(filepath)
        wb.close()

        result = await edit_spreadsheet(
            file_path="/test.xlsx",
            operations=[
                {
                    "type": "set_row_height",
                    "sheet": "NonExistent",
                    "row": 1,
                    "height": 25,
                }
            ],
        )
        assert "Sheet 'NonExistent' does not exist" in result

    @pytest.mark.asyncio
    async def test_freeze_panes_sheet_not_exists(self, setup_module, temp_dir):
        """Test freeze_panes with non-existent sheet."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        filepath = os.path.join(temp_dir, "test.xlsx")
        wb = Workbook()
        wb.save(filepath)
        wb.close()

        result = await edit_spreadsheet(
            file_path="/test.xlsx",
            operations=[{"type": "freeze_panes", "sheet": "NonExistent", "cell": "B2"}],
        )
        assert "Sheet 'NonExistent' does not exist" in result

    @pytest.mark.asyncio
    async def test_add_named_range_sheet_not_exists(self, setup_module, temp_dir):
        """Test add_named_range with non-existent sheet."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        filepath = os.path.join(temp_dir, "test.xlsx")
        wb = Workbook()
        wb.save(filepath)
        wb.close()

        result = await edit_spreadsheet(
            file_path="/test.xlsx",
            operations=[
                {
                    "type": "add_named_range",
                    "name": "TestRange",
                    "sheet": "NonExistent",
                    "range": "A1:B10",
                }
            ],
        )
        assert "Sheet 'NonExistent' does not exist" in result

    @pytest.mark.asyncio
    async def test_data_validation_sheet_not_exists(self, setup_module, temp_dir):
        """Test add_data_validation with non-existent sheet."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        filepath = os.path.join(temp_dir, "test.xlsx")
        wb = Workbook()
        wb.save(filepath)
        wb.close()

        result = await edit_spreadsheet(
            file_path="/test.xlsx",
            operations=[
                {
                    "type": "add_data_validation",
                    "sheet": "NonExistent",
                    "range": "A1:A10",
                    "validation_type": "list",
                    "formula1": '"a,b,c"',
                }
            ],
        )
        assert "Sheet 'NonExistent' does not exist" in result

    @pytest.mark.asyncio
    async def test_conditional_formatting_sheet_not_exists(
        self, setup_module, temp_dir
    ):
        """Test add_conditional_formatting with non-existent sheet."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        filepath = os.path.join(temp_dir, "test.xlsx")
        wb = Workbook()
        wb.save(filepath)
        wb.close()

        result = await edit_spreadsheet(
            file_path="/test.xlsx",
            operations=[
                {
                    "type": "add_conditional_formatting",
                    "sheet": "NonExistent",
                    "range": "A1:A10",
                    "rule_type": "colorScale",
                }
            ],
        )
        assert "Sheet 'NonExistent' does not exist" in result

    @pytest.mark.asyncio
    async def test_auto_filter_sheet_not_exists(self, setup_module, temp_dir):
        """Test set_auto_filter with non-existent sheet."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        filepath = os.path.join(temp_dir, "test.xlsx")
        wb = Workbook()
        wb.save(filepath)
        wb.close()

        result = await edit_spreadsheet(
            file_path="/test.xlsx",
            operations=[
                {"type": "set_auto_filter", "sheet": "NonExistent", "range": "A1:D10"}
            ],
        )
        assert "Sheet 'NonExistent' does not exist" in result

    @pytest.mark.asyncio
    async def test_number_format_sheet_not_exists(self, setup_module, temp_dir):
        """Test set_number_format with non-existent sheet."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        filepath = os.path.join(temp_dir, "test.xlsx")
        wb = Workbook()
        wb.save(filepath)
        wb.close()

        result = await edit_spreadsheet(
            file_path="/test.xlsx",
            operations=[
                {
                    "type": "set_number_format",
                    "sheet": "NonExistent",
                    "range": "A1:A10",
                    "format": "#,##0.00",
                }
            ],
        )
        assert "Sheet 'NonExistent' does not exist" in result

    @pytest.mark.asyncio
    async def test_add_image_sheet_not_exists(self, setup_module, temp_dir):
        """Test add_image with non-existent sheet."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        filepath = os.path.join(temp_dir, "test.xlsx")
        wb = Workbook()
        wb.save(filepath)
        wb.close()

        result = await edit_spreadsheet(
            file_path="/test.xlsx",
            operations=[
                {
                    "type": "add_image",
                    "sheet": "NonExistent",
                    "image_path": "/test.png",
                    "cell": "A1",
                }
            ],
        )
        assert "Sheet 'NonExistent' does not exist" in result

    @pytest.mark.asyncio
    async def test_set_cell_operation(self, setup_module, temp_dir):
        """Test set_cell operation."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        filepath = os.path.join(temp_dir, "test.xlsx")
        wb = Workbook()
        ws = wb.active
        assert ws is not None
        ws.title = "Sheet1"
        wb.save(filepath)
        wb.close()

        result = await edit_spreadsheet(
            file_path="/test.xlsx",
            operations=[
                {"type": "set_cell", "sheet": "Sheet1", "cell": "A1", "value": "Hello"}
            ],
        )
        assert "success" in result

        wb = load_workbook(filepath)
        ws = wb.active
        assert ws is not None
        assert ws["A1"].value == "Hello"
        wb.close()

    @pytest.mark.asyncio
    async def test_append_rows_operation(self, setup_module, temp_dir):
        """Test append_rows operation."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        filepath = os.path.join(temp_dir, "test.xlsx")
        wb = Workbook()
        ws = wb.active
        assert ws is not None
        ws.title = "Sheet1"
        ws.append(["A", "B", "C"])
        wb.save(filepath)
        wb.close()

        result = await edit_spreadsheet(
            file_path="/test.xlsx",
            operations=[
                {
                    "type": "append_rows",
                    "sheet": "Sheet1",
                    "rows": [[1, 2, 3], [4, 5, 6]],
                }
            ],
        )
        assert "success" in result

        wb = load_workbook(filepath)
        ws = wb.active
        assert ws is not None
        assert ws.max_row == 3
        wb.close()

    @pytest.mark.asyncio
    async def test_rename_sheet_operation(self, setup_module, temp_dir):
        """Test rename_sheet operation."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        filepath = os.path.join(temp_dir, "test.xlsx")
        wb = Workbook()
        ws = wb.active
        assert ws is not None
        ws.title = "Sheet1"
        wb.save(filepath)
        wb.close()

        result = await edit_spreadsheet(
            file_path="/test.xlsx",
            operations=[
                {"type": "rename_sheet", "sheet": "Sheet1", "new_name": "RenamedSheet"}
            ],
        )
        assert "success" in result

        wb = load_workbook(filepath)
        assert "RenamedSheet" in wb.sheetnames
        wb.close()

    @pytest.mark.asyncio
    async def test_rename_sheet_not_exists(self, setup_module, temp_dir):
        """Test rename_sheet with non-existent sheet."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        filepath = os.path.join(temp_dir, "test.xlsx")
        wb = Workbook()
        wb.save(filepath)
        wb.close()

        result = await edit_spreadsheet(
            file_path="/test.xlsx",
            operations=[
                {"type": "rename_sheet", "sheet": "NonExistent", "new_name": "NewName"}
            ],
        )
        assert "Sheet 'NonExistent' does not exist" in result

    @pytest.mark.asyncio
    async def test_rename_sheet_target_exists(self, setup_module, temp_dir):
        """Test rename_sheet when target name already exists."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        filepath = os.path.join(temp_dir, "test.xlsx")
        wb = Workbook()
        ws = wb.active
        assert ws is not None
        ws.title = "Sheet1"
        wb.create_sheet("Sheet2")
        wb.save(filepath)
        wb.close()

        result = await edit_spreadsheet(
            file_path="/test.xlsx",
            operations=[
                {"type": "rename_sheet", "sheet": "Sheet1", "new_name": "Sheet2"}
            ],
        )
        assert "Sheet 'Sheet2' already exists" in result


class TestAdditionalConditionalFormattingRules:
    """Additional tests for conditional formatting edge cases."""

    def test_create_cell_is_without_fill(self):
        from mcp_servers.sheets_server.models.sheet import (
            AddConditionalFormattingOperation,
        )
        from mcp_servers.sheets_server.tools.edit_spreadsheet import (
            _create_conditional_formatting_rule,
        )

        op = AddConditionalFormattingOperation(
            type="add_conditional_formatting",
            sheet="Sheet1",
            range="A1:A10",
            rule_type="cellIs",
            operator="greaterThan",
            formula="50",
            font_bold=True,
        )
        rule = _create_conditional_formatting_rule(op)
        assert rule is not None

    def test_create_data_bar_default_color(self):
        from mcp_servers.sheets_server.models.sheet import (
            AddConditionalFormattingOperation,
        )
        from mcp_servers.sheets_server.tools.edit_spreadsheet import (
            _create_conditional_formatting_rule,
        )

        op = AddConditionalFormattingOperation(
            type="add_conditional_formatting",
            sheet="Sheet1",
            range="A1:A10",
            rule_type="dataBar",
        )
        rule = _create_conditional_formatting_rule(op)
        assert rule.type == "dataBar"

    def test_create_color_scale_default_colors(self):
        from mcp_servers.sheets_server.models.sheet import (
            AddConditionalFormattingOperation,
        )
        from mcp_servers.sheets_server.tools.edit_spreadsheet import (
            _create_conditional_formatting_rule,
        )

        op = AddConditionalFormattingOperation(
            type="add_conditional_formatting",
            sheet="Sheet1",
            range="A1:A10",
            rule_type="colorScale",
        )
        rule = _create_conditional_formatting_rule(op)
        assert rule.type == "colorScale"

    def test_create_top10_defaults(self):
        from mcp_servers.sheets_server.models.sheet import (
            AddConditionalFormattingOperation,
        )
        from mcp_servers.sheets_server.tools.edit_spreadsheet import (
            _create_conditional_formatting_rule,
        )

        op = AddConditionalFormattingOperation(
            type="add_conditional_formatting",
            sheet="Sheet1",
            range="A1:A10",
            rule_type="top10",
        )
        rule = _create_conditional_formatting_rule(op)
        assert rule.type == "top10"

    def test_create_contains_text_no_text(self):
        from mcp_servers.sheets_server.models.sheet import (
            AddConditionalFormattingOperation,
        )
        from mcp_servers.sheets_server.tools.edit_spreadsheet import (
            _create_conditional_formatting_rule,
        )

        op = AddConditionalFormattingOperation(
            type="add_conditional_formatting",
            sheet="Sheet1",
            range="A1:A10",
            rule_type="containsText",
        )
        rule = _create_conditional_formatting_rule(op)
        assert rule.type == "containsText"

    def test_create_not_contains_text_no_text(self):
        from mcp_servers.sheets_server.models.sheet import (
            AddConditionalFormattingOperation,
        )
        from mcp_servers.sheets_server.tools.edit_spreadsheet import (
            _create_conditional_formatting_rule,
        )

        op = AddConditionalFormattingOperation(
            type="add_conditional_formatting",
            sheet="Sheet1",
            range="A1:A10",
            rule_type="notContainsText",
        )
        rule = _create_conditional_formatting_rule(op)
        assert rule.type == "notContainsText"

    def test_create_begins_with_no_text(self):
        from mcp_servers.sheets_server.models.sheet import (
            AddConditionalFormattingOperation,
        )
        from mcp_servers.sheets_server.tools.edit_spreadsheet import (
            _create_conditional_formatting_rule,
        )

        op = AddConditionalFormattingOperation(
            type="add_conditional_formatting",
            sheet="Sheet1",
            range="A1:A10",
            rule_type="beginsWith",
        )
        rule = _create_conditional_formatting_rule(op)
        assert rule.type == "beginsWith"

    def test_create_ends_with_no_text(self):
        from mcp_servers.sheets_server.models.sheet import (
            AddConditionalFormattingOperation,
        )
        from mcp_servers.sheets_server.tools.edit_spreadsheet import (
            _create_conditional_formatting_rule,
        )

        op = AddConditionalFormattingOperation(
            type="add_conditional_formatting",
            sheet="Sheet1",
            range="A1:A10",
            rule_type="endsWith",
        )
        rule = _create_conditional_formatting_rule(op)
        assert rule.type == "endsWith"

    def test_create_expression_no_formula(self):
        from mcp_servers.sheets_server.models.sheet import (
            AddConditionalFormattingOperation,
        )
        from mcp_servers.sheets_server.tools.edit_spreadsheet import (
            _create_conditional_formatting_rule,
        )

        op = AddConditionalFormattingOperation(
            type="add_conditional_formatting",
            sheet="Sheet1",
            range="A1:A10",
            rule_type="expression",
        )
        rule = _create_conditional_formatting_rule(op)
        assert rule.type == "expression"


class TestEditSpreadsheetFunction:
    """Tests for the edit_spreadsheet function with new operations."""

    @pytest.fixture
    def temp_dir(self):
        """Create a temporary directory for testing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            yield tmpdir

    @pytest.fixture
    def temp_workbook_in_root(self, temp_dir, monkeypatch):
        """Create a temporary workbook in the SHEETS_ROOT."""
        import mcp_servers.sheets_server.tools.edit_spreadsheet as edit_module

        # Patch the SHEETS_ROOT variable directly in the module
        monkeypatch.setattr(edit_module, "SHEETS_ROOT", temp_dir)

        # Create a workbook
        filepath = os.path.join(temp_dir, "test.xlsx")
        wb = Workbook()
        ws = wb.active
        assert ws is not None
        ws.title = "Sheet1"
        for row in range(1, 11):
            for col in range(1, 5):
                ws.cell(row=row, column=col, value=row * col)
        wb.save(filepath)
        wb.close()

        yield "/test.xlsx"

    @pytest.mark.asyncio
    async def test_edit_merge_cells(self, temp_workbook_in_root, temp_dir):
        """Test edit_spreadsheet with merge_cells operation."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        result = await edit_spreadsheet(
            file_path=temp_workbook_in_root,
            operations=[{"type": "merge_cells", "sheet": "Sheet1", "range": "A1:D1"}],
        )
        assert "success" in result

        # Verify the cells were merged
        wb = load_workbook(os.path.join(temp_dir, "test.xlsx"))
        ws = wb.active
        assert ws is not None
        assert "A1:D1" in [str(m) for m in ws.merged_cells.ranges]
        wb.close()

    @pytest.mark.asyncio
    async def test_edit_unmerge_cells(self, temp_workbook_in_root, temp_dir):
        """Test edit_spreadsheet with unmerge_cells operation."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        # First merge the cells
        wb = load_workbook(os.path.join(temp_dir, "test.xlsx"))
        ws = wb.active
        assert ws is not None
        ws.merge_cells("A1:D1")
        wb.save(os.path.join(temp_dir, "test.xlsx"))
        wb.close()

        # Now unmerge
        result = await edit_spreadsheet(
            file_path=temp_workbook_in_root,
            operations=[{"type": "unmerge_cells", "sheet": "Sheet1", "range": "A1:D1"}],
        )
        assert "success" in result

        # Verify the cells were unmerged
        wb = load_workbook(os.path.join(temp_dir, "test.xlsx"))
        ws = wb.active
        assert ws is not None
        assert "A1:D1" not in [str(m) for m in ws.merged_cells.ranges]
        wb.close()

    @pytest.mark.asyncio
    async def test_edit_column_width(self, temp_workbook_in_root, temp_dir):
        """Test edit_spreadsheet with set_column_width operation."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        result = await edit_spreadsheet(
            file_path=temp_workbook_in_root,
            operations=[
                {
                    "type": "set_column_width",
                    "sheet": "Sheet1",
                    "column": "A",
                    "width": 25.0,
                }
            ],
        )
        assert "success" in result

        wb = load_workbook(os.path.join(temp_dir, "test.xlsx"))
        ws = wb.active
        assert ws is not None
        assert ws.column_dimensions["A"].width == 25.0
        wb.close()

    @pytest.mark.asyncio
    async def test_edit_row_height(self, temp_workbook_in_root, temp_dir):
        """Test edit_spreadsheet with set_row_height operation."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        result = await edit_spreadsheet(
            file_path=temp_workbook_in_root,
            operations=[
                {"type": "set_row_height", "sheet": "Sheet1", "row": 1, "height": 35.0}
            ],
        )
        assert "success" in result

        wb = load_workbook(os.path.join(temp_dir, "test.xlsx"))
        ws = wb.active
        assert ws is not None
        assert ws.row_dimensions[1].height == 35.0
        wb.close()

    @pytest.mark.asyncio
    async def test_edit_freeze_panes(self, temp_workbook_in_root, temp_dir):
        """Test edit_spreadsheet with freeze_panes operation."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        result = await edit_spreadsheet(
            file_path=temp_workbook_in_root,
            operations=[{"type": "freeze_panes", "sheet": "Sheet1", "cell": "B2"}],
        )
        assert "success" in result

        wb = load_workbook(os.path.join(temp_dir, "test.xlsx"))
        ws = wb.active
        assert ws is not None
        assert ws.freeze_panes == "B2"
        wb.close()

    @pytest.mark.asyncio
    async def test_edit_unfreeze_panes(self, temp_workbook_in_root, temp_dir):
        """Test edit_spreadsheet with freeze_panes operation to unfreeze."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        # First freeze
        await edit_spreadsheet(
            file_path=temp_workbook_in_root,
            operations=[{"type": "freeze_panes", "sheet": "Sheet1", "cell": "B2"}],
        )

        # Then unfreeze
        result = await edit_spreadsheet(
            file_path=temp_workbook_in_root,
            operations=[{"type": "freeze_panes", "sheet": "Sheet1", "cell": None}],
        )
        assert "success" in result

        wb = load_workbook(os.path.join(temp_dir, "test.xlsx"))
        ws = wb.active
        assert ws is not None
        assert ws.freeze_panes is None
        wb.close()

    @pytest.mark.asyncio
    async def test_edit_add_named_range(self, temp_workbook_in_root, temp_dir):
        """Test edit_spreadsheet with add_named_range operation."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        result = await edit_spreadsheet(
            file_path=temp_workbook_in_root,
            operations=[
                {
                    "type": "add_named_range",
                    "name": "TestRange",
                    "sheet": "Sheet1",
                    "range": "A1:B10",
                }
            ],
        )
        assert "success" in result

        wb = load_workbook(os.path.join(temp_dir, "test.xlsx"))
        assert "TestRange" in wb.defined_names
        wb.close()

    @pytest.mark.asyncio
    async def test_edit_delete_named_range(self, temp_workbook_in_root, temp_dir):
        """Test edit_spreadsheet with delete_named_range operation."""
        from openpyxl.workbook.defined_name import DefinedName

        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        # First add a named range
        wb = load_workbook(os.path.join(temp_dir, "test.xlsx"))
        defn = DefinedName("TestRange", attr_text="'Sheet1'!$A$1:$B$10")
        wb.defined_names.add(defn)
        wb.save(os.path.join(temp_dir, "test.xlsx"))
        wb.close()

        # Now delete it
        result = await edit_spreadsheet(
            file_path=temp_workbook_in_root,
            operations=[{"type": "delete_named_range", "name": "TestRange"}],
        )
        assert "success" in result

        wb = load_workbook(os.path.join(temp_dir, "test.xlsx"))
        assert "TestRange" not in wb.defined_names
        wb.close()

    @pytest.mark.asyncio
    async def test_edit_add_data_validation(self, temp_workbook_in_root, temp_dir):
        """Test edit_spreadsheet with add_data_validation operation."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        result = await edit_spreadsheet(
            file_path=temp_workbook_in_root,
            operations=[
                {
                    "type": "add_data_validation",
                    "sheet": "Sheet1",
                    "range": "E1:E10",
                    "validation_type": "list",
                    "formula1": '"Yes,No,Maybe"',
                }
            ],
        )
        assert "success" in result

        wb = load_workbook(os.path.join(temp_dir, "test.xlsx"))
        ws = wb.active
        assert ws is not None
        assert len(ws.data_validations.dataValidation) > 0
        wb.close()

    @pytest.mark.asyncio
    async def test_edit_set_auto_filter(self, temp_workbook_in_root, temp_dir):
        """Test edit_spreadsheet with set_auto_filter operation."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        result = await edit_spreadsheet(
            file_path=temp_workbook_in_root,
            operations=[
                {"type": "set_auto_filter", "sheet": "Sheet1", "range": "A1:D10"}
            ],
        )
        assert "success" in result

        wb = load_workbook(os.path.join(temp_dir, "test.xlsx"))
        ws = wb.active
        assert ws is not None
        assert ws.auto_filter.ref == "A1:D10"
        wb.close()

    @pytest.mark.asyncio
    async def test_edit_set_number_format(self, temp_workbook_in_root, temp_dir):
        """Test edit_spreadsheet with set_number_format operation."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        result = await edit_spreadsheet(
            file_path=temp_workbook_in_root,
            operations=[
                {
                    "type": "set_number_format",
                    "sheet": "Sheet1",
                    "range": "A1:A10",
                    "format": "#,##0.00",
                }
            ],
        )
        assert "success" in result

        wb = load_workbook(os.path.join(temp_dir, "test.xlsx"))
        ws = wb.active
        assert ws is not None
        assert ws["A1"].number_format == "#,##0.00"
        wb.close()

    @pytest.mark.asyncio
    async def test_edit_conditional_formatting_color_scale(
        self, temp_workbook_in_root, temp_dir
    ):
        """Test edit_spreadsheet with add_conditional_formatting operation (color scale)."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        result = await edit_spreadsheet(
            file_path=temp_workbook_in_root,
            operations=[
                {
                    "type": "add_conditional_formatting",
                    "sheet": "Sheet1",
                    "range": "A1:A10",
                    "rule_type": "colorScale",
                    "color_scale_colors": ["FF0000", "00FF00"],
                }
            ],
        )
        assert "success" in result

        wb = load_workbook(os.path.join(temp_dir, "test.xlsx"))
        ws = wb.active
        assert ws is not None
        assert len(list(ws.conditional_formatting)) > 0
        wb.close()

    @pytest.mark.asyncio
    async def test_edit_conditional_formatting_data_bar(
        self, temp_workbook_in_root, temp_dir
    ):
        """Test edit_spreadsheet with add_conditional_formatting operation (data bar)."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        result = await edit_spreadsheet(
            file_path=temp_workbook_in_root,
            operations=[
                {
                    "type": "add_conditional_formatting",
                    "sheet": "Sheet1",
                    "range": "B1:B10",
                    "rule_type": "dataBar",
                    "data_bar_color": "638EC6",
                }
            ],
        )
        assert "success" in result

        wb = load_workbook(os.path.join(temp_dir, "test.xlsx"))
        ws = wb.active
        assert ws is not None
        assert len(list(ws.conditional_formatting)) > 0
        wb.close()

    @pytest.mark.asyncio
    async def test_edit_conditional_formatting_cell_is(
        self, temp_workbook_in_root, temp_dir
    ):
        """Test edit_spreadsheet with add_conditional_formatting operation (cellIs)."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        result = await edit_spreadsheet(
            file_path=temp_workbook_in_root,
            operations=[
                {
                    "type": "add_conditional_formatting",
                    "sheet": "Sheet1",
                    "range": "C1:C10",
                    "rule_type": "cellIs",
                    "operator": "greaterThan",
                    "formula": "5",
                    "fill_color": "FFFF00",
                }
            ],
        )
        assert "success" in result

        wb = load_workbook(os.path.join(temp_dir, "test.xlsx"))
        ws = wb.active
        assert ws is not None
        assert len(list(ws.conditional_formatting)) > 0
        wb.close()

    @pytest.mark.asyncio
    async def test_edit_sheet_not_exists(self, temp_workbook_in_root, temp_dir):
        """Test edit_spreadsheet with non-existent sheet."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        result = await edit_spreadsheet(
            file_path=temp_workbook_in_root,
            operations=[
                {"type": "merge_cells", "sheet": "NonExistent", "range": "A1:D1"}
            ],
        )
        assert "Sheet 'NonExistent' does not exist" in result

    @pytest.mark.asyncio
    async def test_edit_named_range_already_exists(
        self, temp_workbook_in_root, temp_dir
    ):
        """Test edit_spreadsheet when named range already exists."""
        from openpyxl.workbook.defined_name import DefinedName

        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        # First add a named range
        wb = load_workbook(os.path.join(temp_dir, "test.xlsx"))
        defn = DefinedName("TestRange", attr_text="'Sheet1'!$A$1:$B$10")
        wb.defined_names.add(defn)
        wb.save(os.path.join(temp_dir, "test.xlsx"))
        wb.close()

        # Try to add the same named range
        result = await edit_spreadsheet(
            file_path=temp_workbook_in_root,
            operations=[
                {
                    "type": "add_named_range",
                    "name": "TestRange",
                    "sheet": "Sheet1",
                    "range": "C1:D10",
                }
            ],
        )
        assert "Named range 'TestRange' already exists" in result

    @pytest.mark.asyncio
    async def test_edit_delete_nonexistent_named_range(
        self, temp_workbook_in_root, temp_dir
    ):
        """Test edit_spreadsheet when deleting non-existent named range."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        result = await edit_spreadsheet(
            file_path=temp_workbook_in_root,
            operations=[{"type": "delete_named_range", "name": "NonExistent"}],
        )
        assert "Named range 'NonExistent' does not exist" in result

    @pytest.mark.asyncio
    async def test_edit_multiple_operations(self, temp_workbook_in_root, temp_dir):
        """Test edit_spreadsheet with multiple operations."""
        from mcp_servers.sheets_server.tools.edit_spreadsheet import edit_spreadsheet

        result = await edit_spreadsheet(
            file_path=temp_workbook_in_root,
            operations=[
                {"type": "merge_cells", "sheet": "Sheet1", "range": "A1:D1"},
                {
                    "type": "set_column_width",
                    "sheet": "Sheet1",
                    "column": "A",
                    "width": 20.0,
                },
                {"type": "freeze_panes", "sheet": "Sheet1", "cell": "A2"},
                {"type": "set_auto_filter", "sheet": "Sheet1", "range": "A1:D10"},
            ],
        )
        assert "success" in result
        assert "operations_applied': 4" in result

        wb = load_workbook(os.path.join(temp_dir, "test.xlsx"))
        ws = wb.active
        assert ws is not None
        assert "A1:D1" in [str(m) for m in ws.merged_cells.ranges]
        assert ws.column_dimensions["A"].width == 20.0
        assert ws.freeze_panes == "A2"
        assert ws.auto_filter.ref == "A1:D10"
        wb.close()

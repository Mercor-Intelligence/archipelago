# MCP Mock Product Specification: Sheets Server

---

## 0. App Classification

| Property | Value | Notes |
|-----------|--------|-------|
| **Stateful** | Yes | Maintains spreadsheets in filesystem |
| **Mutable** | Yes | Can create, modify, and delete spreadsheets |
| **Visual** | No | Does not require a GUI or Computer Use |
| **Private** | No | Does not require authentication or authorization |
| **Mock Type** | Full Mock | Operates on local filesystem; no external API dependencies |
| **Setup Flags** | `APP_SHEETS_ROOT` | Environment variable for root directory configuration |

---

## 1. Title

**Sheets Server MCP**

---

## 2. Overview

The Sheets Server provides comprehensive Spreadsheets spreadsheet (.xlsx) management capabilities including creating workbooks, reading data, editing cells, managing tabs, creating charts, and working with formulas. It enables AI agents to programmatically generate reports, analyze tabular data, and automate spreadsheet workflows within a sandboxed filesystem environment.

---

## 3. Approach

### Data Source

- **Filesystem-based**: All operations target .xlsx files within a configurable root directory (`APP_SHEETS_ROOT`)
- **No external APIs**: Self-contained processing using openpyxl library
- **CSV Support**: Can read CSV files and parse them into structured data

### Data Modes

| Mode | Description |
|------|-------------|
| **Clean Mode** | Empty filesystem; spreadsheets must be created before use |
| **Offline Mode** | Reactive-only; processes spreadsheets that exist at request time |

### Core Architecture

| Component | Technology | Purpose |
|-----------|------------|---------|
| Spreadsheet Engine | openpyxl | Create, read, and modify .xlsx files |
| Formula Engine | openpyxl | Support Spreadsheets formulas with auto-calculation |
| Chart Engine | openpyxl.chart | Create bar, line, and pie charts |
| CSV Parser | Python csv | Read and parse CSV files |

### Security

- **Path traversal protection**: All file paths are normalized and validated
- **Sandboxed operations**: Cannot access files outside `APP_SHEETS_ROOT`

---

## 4. Prioritized User Archetypes and Workflows

| User Type | Goal / Workflow | Primary Tools |
|-----------|-----------------|---------------|
| **Report Generator** | Create Spreadsheets reports with multiple sheets and formulas | `create_spreadsheet`, `edit_spreadsheet` |
| **Data Analyst** | Read and analyze spreadsheet data | `read_tab`, `read_csv` |
| **Dashboard Builder** | Create visualizations from spreadsheet data | `create_chart` |
| **Data Entry Automation** | Update specific cells or append rows | `edit_spreadsheet`, `add_content_text` |
| **Workbook Manager** | Organize multi-sheet workbooks | `add_tab`, `delete_tab`, `list_tabs_in_spreadsheet` |
| **Data Importer** | Parse CSV files for processing | `read_csv` |

---

## 5. Extended Functionality (Future Builds)

| Feature | Description | Priority |
|---------|-------------|----------|
| **Pivot Tables** | Create and manage pivot tables | High |
| **Conditional Formatting** | Apply conditional formatting rules | Medium |
| **Data Validation** | Add dropdown lists and validation rules | Medium |
| **Cell Styling** | Apply fonts, colors, borders, and backgrounds | Medium |
| **Named Ranges** | Create and reference named ranges | Low |
| **Merge Cells** | Merge and unmerge cell ranges | Low |
| **Export to CSV** | Export sheet data to CSV format | Medium |

---

## 6. Build Considerations

| Area | Description |
|------|-------------|
| **File Format** | Microsoft Spreadsheets .xlsx format (OpenXML) |
| **Path Validation** | All file paths must start with `/` and end with `.xlsx` (or `.csv` for read_csv) |
| **Tab Indexing** | Worksheet tabs are 0-indexed (first sheet is 0) |
| **Formula Support** | Standard Spreadsheets formulas with `=` prefix; auto-calculated on save |
| **Cross-Sheet References** | Supported via `Sheet2!A1` syntax |
| **Header Validation** | When headers exist, `append_rows` validates column count consistency |
| **Chart Types** | Bar, line, and pie charts supported |
| **Dependencies** | `openpyxl` |

---

## 7. Example MCP Commands

### Create Spreadsheet

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `create_spreadsheet` | Create a new .xlsx workbook | `directory`, `file_name`, `sheets[]` | Success message with file path |

```
create_spreadsheet(
  directory="/reports",
  file_name="quarterly.xlsx",
  sheets=[
    {
      "name": "Sales",
      "headers": ["Product", "Q1", "Q2", "Q3"],
      "rows": [
        ["Widgets", 100, 120, 90],
        ["Gadgets", 80, 95, 110]
      ]
    },
    {
      "name": "Summary",
      "headers": ["Metric", "Value"],
      "rows": [
        ["Total Products", 2],
        ["Total Revenue", "=SUM(Sales!B2:D3)"]
      ]
    }
  ]
)

→ CreateSpreadsheetResponse(status="success", file_path="/reports/quarterly.xlsx", sheets_created=2)
```

### Read Tab

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `read_tab` | Read data from a worksheet | `file_path`, `tab_index`, `cell_range` (optional) | Cell values and formulas |

```
# Read entire sheet
read_tab("/reports/quarterly.xlsx", tab_index=0)

→ ReadTabRangeResponse(
    range="all",
    values=[["Product", "Q1", "Q2", "Q3"], ["Widgets", 100, 120, 90], ["Gadgets", 80, 95, 110]],
    formulas=None
  )

# Read specific cell with formula
read_tab("/reports/quarterly.xlsx", tab_index=1, cell_range="B2")

→ ReadTabSingleCellResponse(cell="B2", value=495, formula="=SUM(Sales!B2:D3)")

# Read range
read_tab("/reports/quarterly.xlsx", tab_index=0, cell_range="A1:B3")

→ ReadTabRangeResponse(range="A1:B3", values=[["Product", "Q1"], ["Widgets", 100], ["Gadgets", 80]])
```

### Edit Spreadsheet

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `edit_spreadsheet` | Apply batch operations to spreadsheet | `file_path`, `operations[]` | Operations applied count |

```
edit_spreadsheet(
  file_path="/reports/quarterly.xlsx",
  operations=[
    {"type": "set_cell", "sheet": "Sales", "cell": "E2", "value": 130},
    {"type": "set_cell", "sheet": "Sales", "cell": "E1", "value": "Q4"},
    {"type": "append_rows", "sheet": "Sales", "rows": [["Gizmos", 50, 60, 70, 80]]},
    {"type": "rename_sheet", "sheet": "Summary", "new_name": "Dashboard"}
  ]
)

→ EditSpreadsheetResponse(status="success", file_path="/reports/quarterly.xlsx", operations_applied=4)
```

### Create Chart

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `create_chart` | Add chart to spreadsheet | `file_path`, `sheet`, `data_range`, `chart_type`, `title`, `position` | Success message |

```
create_chart(
  file_path="/reports/quarterly.xlsx",
  sheet="Sales",
  data_range="A1:D3",
  chart_type="bar",
  title="Quarterly Sales",
  position="F2"
)

→ Chart 'Quarterly Sales' created in Sales at position F2
```

### Read CSV

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `read_csv` | Parse CSV file into structured data | `file_path`, `delimiter`, `encoding`, `has_header`, `row_limit` | Headers and values |

```
read_csv("/data/import.csv", delimiter=",", has_header=True)

→ ReadCsvResponse(
    file_path="/data/import.csv",
    headers=["Name", "Age", "City"],
    values=[["Alice", 30, "NYC"], ["Bob", 25, "LA"]],
    row_count=2,
    column_count=3
  )
```

### Tab Management

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `list_tabs_in_spreadsheet` | List all worksheet tabs | `file_path` | Tab names and indices |
| `add_tab` | Add new worksheet | `file_path`, `tab_name`, `headers`, `rows` | Success message |
| `delete_tab` | Remove worksheet | `file_path`, `tab_index` | Success message |

```
list_tabs_in_spreadsheet("/reports/quarterly.xlsx")

→ [{"index": 0, "name": "Sales"}, {"index": 1, "name": "Dashboard"}]

add_tab(
  file_path="/reports/quarterly.xlsx",
  tab_name="Forecast",
  headers=["Month", "Projected"],
  rows=[["Jan", 150], ["Feb", 175]]
)

→ Tab 'Forecast' added to /reports/quarterly.xlsx
```

### Edit Operations Summary

| Operation Type | Required Fields | Optional Fields | Description |
|----------------|-----------------|-----------------|-------------|
| `set_cell` | `type`, `sheet`, `cell`, `value` | — | Set cell value or formula |
| `append_rows` | `type`, `sheet`, `rows` | — | Append rows to sheet |
| `rename_sheet` | `type`, `sheet`, `new_name` | — | Rename worksheet |

---

## 8. Notes and References

### Environment Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_SHEETS_ROOT` | `/filesystem` | Root directory for spreadsheet operations |
| `TOOLS` | All enabled | Comma-separated list to enable specific tools only |

### Key Behaviors

- **Formula Syntax**: Use standard Spreadsheets formulas with `=` prefix (e.g., `=SUM(A1:A10)`, `=AVERAGE(B:B)`)
- **Cross-Sheet References**: Use `SheetName!Cell` syntax (e.g., `=Sales!B2`)
- **Auto-Calculation**: Formulas recalculated automatically on save
- **Data Types**: Supports strings, numbers, booleans, formulas, and None/empty

### Chart Types

| Type | Description | Best For |
|------|-------------|----------|
| `bar` | Vertical bar chart | Comparing categories |
| `line` | Line chart | Trends over time |
| `pie` | Pie chart | Part-to-whole relationships |

### Dependencies

- [openpyxl](https://openpyxl.readthedocs.io/) — Spreadsheets file reading/writing

---



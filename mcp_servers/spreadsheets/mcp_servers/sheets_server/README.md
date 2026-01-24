# Rls Sheets MCP Server

Mercor RLS Sheets MCP

## Tools

### 1. `sheets`

Spreadsheet operations: create, read, edit, and manage .xlsx files.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'create', 'delete', 'read_tab', 'read_csv', 'list_tabs', 'add_tab', 'delete_tab', 'edit', 'add_content', 'delete_content', 'create_chart'] | Ellipsis | Action to perform |
| `file_path` | string? | null | Full file path (e.g., '/report.xlsx'). REQUIRED for all actions except 'create'. |
| `directory` | string? | null | Directory path. REQUIRED for 'create' action (e.g., '/'). Use with file_name. |
| `file_name` | string? | null | File name with .xlsx extension. REQUIRED for 'create' action (e.g., 'report.xlsx'). |
| `tab_index` | integer? | null | 0-based tab index. REQUIRED for read_tab, delete_tab, add_content, delete_content. Use 0 for firs... |
| `tab_name` | string? | null | Tab name for 'add_tab' action only. NOT used for read_tab (use tab_index instead). |
| `cell_range` | string? | null | Cell range for 'read_tab' (e.g., 'A1:C5') |
| `sheets` | array[object[string, Any]]? | null | Sheet definitions for 'create'. REQUIRED for create. Format: [{name: 'Sheet1', headers: ['A','B']... |
| `sheet_data` | object[string, Any]? | null | Data for 'add_tab': {headers?, rows} |
| `operations` | array[object[string, Any]]? | null | Operations for 'edit' action. Each operation needs 'type' field. |
| `cell` | string? | null | Cell reference for add_content/delete_content (e.g., 'A1') |
| `value` | Any? | null | Value to set or match. |
| `sheet` | string? | null | Target sheet name. |
| `data_range` | string? | null | Data range for chart (e.g., 'A1:C10') |
| `chart_type` | enum['bar', 'line', 'pie']? | null | Chart type |
| `title` | string? | null | Title for the entity. REQUIRED for create. |
| `position` | string? | null | Chart position (e.g., 'E2') |
| `categories_column` | integer? | null | Column index for X-axis categories |
| `include_header` | boolean? | null | Whether first row is header |
| `delimiter` | string? | null | CSV delimiter |
| `encoding` | string? | null | CSV encoding |
| `has_header` | boolean? | null | CSV has header row |
| `row_limit` | integer? | null | Max rows to read from CSV |

---

### 2. `sheets_schema`

Get JSON schema for sheets input/output models.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `model` | string | Ellipsis | Model name: 'input', 'output', or a result type like 'ReadTabResult' |

---

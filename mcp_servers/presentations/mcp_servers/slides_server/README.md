# Rls Slides MCP Server

Mercor RLS Slides MCP

## Tools

### 1. `slides_schema`

Get JSON schemas for slides tool input/output models.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `schema_name` | string? | null | Name of specific schema to retrieve. If not provided, returns all schema names. |

---

### 2. `slides`

Unified interface for all Presentations presentation operations.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['create', 'delete', 'add_slide', 'edit', 'add_image', 'modify_image', 'insert_chart', 'insert_table', 'read_range', 'read_deck', 'read_slide', 'read_image'] | Ellipsis | The action to perform |
| `file_path` | string? | null | Path to the .pptx file (required for most actions) |
| `directory` | string? | null | Directory path. REQUIRED for list/create operations. |
| `file_name` | string? | null | Filename with extension. REQUIRED for create/save. |
| `slides` | array[object[string, Any]]? | null | Slide definitions for create |
| `metadata` | object[string, Any]? | null | Presentation metadata (title, subject, author, comments) |
| `input_data` | object[string, Any]? | null | Input data for add_slide action |
| `operations` | array[object[string, Any]]? | null | Edit operations to apply |
| `image_path` | string? | null | Path to image file |
| `slide_index` | integer? | null | Slide index (0-based) |
| `x` | number? | null | X position in inches |
| `y` | number? | null | Y position in inches |
| `width` | number? | null | Width in pixels. Optional for export. |
| `height` | number? | null | Height in pixels. Optional for export. |
| `image_index` | integer? | null | Image index on slide (0-based) |
| `operation` | string? | null | Operation: rotate, flip, brightness, contrast |
| `rotation` | integer? | null | Rotation angle (0-360) |
| `flip` | string? | null | Flip direction: horizontal, vertical |
| `brightness` | number? | null | Brightness factor (0.0-2.0). 1.0=unchanged. |
| `contrast` | number? | null | Contrast factor (0.0-2.0). 1.0=unchanged. |
| `spreadsheet_path` | string? | null | Path to source spreadsheet |
| `sheet_name` | string? | null | Sheet name in spreadsheet |
| `data_range` | string? | null | Cell range (e.g., 'A1:D5') |
| `chart_type` | string? | null | Chart type filter. Optional. |
| `title` | string? | null | Title for the entity. REQUIRED for create. |
| `position` | string? | null | Position: body, left, right |
| `include_header` | boolean? | null | Whether first row is header |
| `rows` | array[array[Any]]? | null | Table rows data |
| `header` | boolean? | null | Bold first row as header |
| `start` | integer? | null | Start character position |
| `end` | integer? | null | End character position |
| `annotation` | string? | null | Image annotation key from cache |

---

# Rls Docs MCP Server

Mercor RLS Docs MCP

## Tools

### 1. `docs`

Document operations: create, read, edit, and manage .docx files.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'create', 'delete', 'overview', 'read_content', 'read_image', 'add_text', 'edit_text', 'delete_text', 'add_image', 'modify_image', 'format'] | Ellipsis | Action to perform |
| `file_path` | string? | null | Full file path. REQUIRED for file operations. |
| `directory` | string? | null | Directory for 'create' (e.g., '/') |
| `file_name` | string? | null | File name for 'create' (e.g., 'report.docx') |
| `content` | array[object[string, Any]]? | null | Content blocks for 'create': [{type, text, ...}] |
| `metadata` | object[string, Any]? | null | Document metadata for 'create': {title?, author?, ...} |
| `identifier` | string? | null | Stable identifier from read_content (e.g., 'body.p.0') |
| `text` | string? | null | Text content for add_text |
| `new_text` | string? | null | Replacement text for edit_text |
| `position` | string? | null | Position for add_text/add_image: 'start' or 'end' |
| `scope` | string? | null | Scope for delete_text: 'content' or 'element' |
| `collapse_whitespace` | boolean? | null | Collapse whitespace for delete_text in cells |
| `section_index` | integer? | null | Section index for read_content pagination |
| `annotation` | string? | null | Image annotation key for read_image |
| `image_path` | string? | null | Path to image file for add_image |
| `image_index` | integer? | null | 0-based image index for modify_image |
| `operation` | string? | null | Operation for modify_image: rotate, flip, brightness, contrast |
| `rotation` | integer? | null | Rotation angle (0-360) |
| `flip` | string? | null | Flip direction: 'horizontal' or 'vertical' |
| `brightness` | number? | null | Brightness factor (0.0-2.0). 1.0=unchanged. |
| `contrast` | number? | null | Contrast factor (0.0-2.0). 1.0=unchanged. |
| `width` | number? | null | Width in pixels. Optional for export. |
| `height` | number? | null | Height in pixels. Optional for export. |
| `bold` | boolean? | null | Apply bold formatting. |
| `italic` | boolean? | null | Apply italic formatting. |
| `underline` | boolean? | null | Underline formatting |
| `strikethrough` | boolean? | null | Strikethrough formatting |
| `font_size` | number? | null | Font size in points. |
| `font_color` | string? | null | Font color as hex (e.g., 'FF0000') |

---

### 2. `docs_schema`

Get JSON schema for docs input/output models.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `model` | string | Ellipsis | Model name: 'input', 'output', or a result type |

---

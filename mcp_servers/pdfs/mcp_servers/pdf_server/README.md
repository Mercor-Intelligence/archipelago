# Rls Pdf MCP Server

RLS PDF MCP

## Tools

### 1. `pdf`

PDF operations: create, read, search, and extract images from .pdf files.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'create', 'read_pages', 'read_image', 'page_as_image', 'search'] | Ellipsis | Action to perform |
| `file_path` | string? | null | Absolute path to PDF file. REQUIRED for read_pages, read_image, page_as_image, search. |
| `directory` | string? | null | Directory for 'create' action. Use '/' for root. REQUIRED for create. |
| `file_name` | string? | null | File name for 'create' action (e.g., 'report.pdf'). REQUIRED for create. |
| `content` | array[object[string, Any]]? | null | Content blocks for 'create': [{type, text, ...}] |
| `metadata` | object[string, Any]? | null | PDF metadata for 'create': {title?, author?, ...} |
| `page_size` | string? | null | Page size for 'create': 'letter' or 'a4' |
| `pages` | array[integer]? | null | Page numbers to read (1-indexed), None for all |
| `page_number` | integer? | null | Page number for 'page_as_image' (1-indexed) |
| `annotation` | string? | null | Image annotation key for 'read_image' |
| `query` | string? | null | Search text. Matches names, descriptions. Case-insensitive. |
| `case_sensitive` | boolean? | null | Case-sensitive search (default: False) |
| `whole_documents` | boolean? | null | Match whole words only (default: False) |
| `max_results` | integer? | null | Maximum results to return (default: 100) |
| `context_chars` | integer? | null | Context characters around match (default: 50) |

---

### 2. `pdf_schema`

Get JSON schema for pdf input/output models.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `model` | string | Ellipsis | Model name: 'input', 'output', or a result type |

---

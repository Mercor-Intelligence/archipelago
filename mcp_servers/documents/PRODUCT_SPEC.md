# MCP Mock Product Specification: Docs Server

---

## 0. App Classification

| Property | Value | Notes |
|-----------|--------|-------|
| **Stateful** | Yes | Maintains documents in filesystem and caches images |
| **Mutable** | Yes | Can create, modify, and delete documents |
| **Visual** | No | Does not require a GUI or Computer Use |
| **Private** | No | Does not require authentication or authorization |
| **Mock Type** | Full Mock | Operates on local filesystem; no external API dependencies |
| **Setup Flags** | `APP_DOCS_ROOT`, `MAX_IMAGE_WIDTH`, `MAX_IMAGE_HEIGHT`, `IMAGE_QUALITY` | Environment variables for configuration |

---

## 1. Title

**Docs Server MCP**

---

## 2. Overview

The Docs Server provides comprehensive Microsoft Documents (.docx) document management capabilities including creation, reading, editing, formatting, and image extraction. It enables AI agents to programmatically generate documents, make targeted edits using stable element identifiers, apply text formatting, and extract embedded images.

---

## 3. Approach

### Data Source

- **Filesystem-based**: All operations target .docx files within a configurable root directory (`APP_DOCS_ROOT`)
- **No external APIs**: Self-contained processing using python-docx library

### Data Modes

| Mode | Description |
|------|-------------|
| **Clean Mode** | Empty filesystem; documents must be created before use |
| **Offline Mode** | Reactive-only; processes documents that exist at request time |

### Core Architecture

| Component | Technology | Purpose |
|-----------|------------|---------|
| Document Engine | python-docx | Create, read, and modify .docx files |
| Element Identifiers | Custom | Stable IDs for precise editing (body.p.0, body.tbl.0.r.0.c.0) |
| Image Cache | In-memory dict | Store extracted images for retrieval |
| Image Processing | Pillow | Compress and convert images |

### Security

- **Path traversal protection**: All file paths are normalized and validated
- **Sandboxed operations**: Cannot access files outside `APP_DOCS_ROOT`

---

## 4. Prioritized User Archetypes and Workflows

| User Type | Goal / Workflow | Primary Tools |
|-----------|-----------------|---------------|
| **Document Creator** | Generate formatted Documents documents | `create_document` |
| **Content Editor** | Edit specific paragraphs or table cells | `edit_content_text`, `add_content_text`, `delete_content_text` |
| **Document Analyst** | Read and analyze document structure | `read_document_content`, `get_document_overview` |
| **Formatter** | Apply styling to text | `apply_formatting` |
| **Image Extractor** | Extract embedded images | `read_image` |
| **Content Manager** | Add images to documents | `add_image`, `modify_image` |

---

## 5. Extended Functionality (Future Builds)

| Feature | Description | Priority |
|---------|-------------|----------|
| **Headers/Footers** | Add and edit headers and footers | High |
| **Page Layout** | Control margins, orientation, page size | Medium |
| **Styles Management** | Create and apply custom styles | Medium |
| **Comments** | Add and read document comments | Medium |
| **Track Changes** | Support revision tracking | Low |
| **Mail Merge** | Template-based document generation | Medium |
| **Export to PDF** | Convert documents to PDF | High |

---

## 6. Build Considerations

| Area | Description |
|------|-------------|
| **File Format** | Microsoft Documents .docx format (OpenXML) |
| **Path Validation** | All file paths must start with `/` and end with `.docx` |
| **Stable Identifiers** | Every element has an ID for precise editing (e.g., `body.p.0`, `body.tbl.0.r.0.c.0`) |
| **Image Cache** | Images extracted and cached with `{file_path}::{annotation}` key |
| **Image Compression** | Auto-compressed to JPEG with configurable quality and dimensions |
| **Section Support** | Large documents can be paginated by section |
| **Dependencies** | `python-docx`, `Pillow` |

---

## 7. Example MCP Commands

### Create Document

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `create_document` | Create a new .docx document | `directory`, `file_name`, `content[]`, `metadata` | Success message |

```
create_document(
  directory="/reports",
  file_name="summary.docx",
  content=[
    {"type": "heading", "text": "Project Summary", "level": 1},
    {"type": "paragraph", "text": "This document contains the quarterly summary.", "bold": false},
    {"type": "heading", "text": "Key Achievements", "level": 2},
    {"type": "bullet_list", "items": ["Launched new product", "Expanded to 3 markets", "Grew team by 25%"]},
    {"type": "heading", "text": "Metrics", "level": 2},
    {"type": "table", "rows": [["Metric", "Value"], ["Revenue", "$1.2M"], ["Users", "50K"]], "header": true}
  ],
  metadata={"title": "Project Summary", "author": "John Doe"}
)

→ Document summary.docx created at /filesystem/reports/summary.docx
```

### Read Document

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `read_document_content` | Parse document into structured content | `file_path`, `section_index` | Structured elements with IDs |
| `get_document_overview` | Get heading hierarchy | `file_path` | Document structure outline |

```
read_document_content("/reports/summary.docx")

→ {
    "filepath": "/reports/summary.docx",
    "metadata": {
      "num_paragraphs": 8,
      "num_tables": 1,
      "num_sections": 1,
      "total_runs": 15,
      "num_images": 0
    },
    "body": {
      "paragraphs": [
        {
          "id": "body.p.0",
          "style": "Heading 1",
          "runs": [{"id": "body.p.0.r.0", "text": "Project Summary", "bold": true}]
        },
        {
          "id": "body.p.1",
          "style": "Normal",
          "runs": [{"id": "body.p.1.r.0", "text": "This document contains...", "bold": false}]
        }
      ],
      "tables": [
        {
          "id": "body.tbl.0",
          "rows": [...]
        }
      ]
    }
  }

get_document_overview("/reports/summary.docx")

→ {
    "structure": [
      {"type": "heading", "level": 1, "text": "Project Summary", "annotation": "body.p.0"},
      {"type": "heading", "level": 2, "text": "Key Achievements", "annotation": "body.p.2"},
      {"type": "heading", "level": 2, "text": "Metrics", "annotation": "body.p.4"}
    ]
  }
```

### Edit Content

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `edit_content_text` | Replace text at identifier | `file_path`, `identifier`, `new_text` | Change summary |
| `add_content_text` | Add text after identifier | `file_path`, `identifier`, `text` | New content ID |
| `delete_content_text` | Delete content at identifier | `file_path`, `identifier` | Success message |

```
edit_content_text(
  file_path="/reports/summary.docx",
  identifier="body.p.1",
  new_text="Updated: This document contains the annual summary."
)

→ EditContentTextResponse(
    status="success",
    identifier="body.p.1",
    old_text="This document contains the quarterly summary.",
    new_text="Updated: This document contains the annual summary."
  )

add_content_text(
  file_path="/reports/summary.docx",
  identifier="body.p.3",
  text="This is a new paragraph added after the bullet list."
)

→ Content added after body.p.3

delete_content_text("/reports/summary.docx", "body.p.5")

→ Content at body.p.5 deleted
```

### Apply Formatting

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `apply_formatting` | Apply text styling | `file_path`, `identifier`, `bold`, `italic`, `underline`, `color` | Success message |

```
apply_formatting(
  file_path="/reports/summary.docx",
  identifier="body.p.0.r.0",
  bold=true,
  italic=false,
  underline=true,
  color="blue"
)

→ Formatting applied to body.p.0.r.0: bold=True, underline=True, color=blue
```

### Image Operations

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `read_image` | Retrieve extracted image | `file_path`, `annotation` | JPEG image data |
| `add_image` | Insert image into document | `file_path`, `identifier`, `image_path` | Success message |
| `modify_image` | Resize image | `file_path`, `identifier`, `width`, `height` | Success message |

```
# After reading document that shows: @body_p_3_r1
read_image("/reports/summary.docx", "body_p_3_r1")

→ [Image: JPEG, format=jpeg]
```

### Content Block Types for `create_document`

| Block Type | Required Fields | Optional Fields | Description |
|------------|-----------------|-----------------|-------------|
| `paragraph` | `type`, `text` | `bold`, `italic`, `style` | Body text paragraph |
| `heading` | `type`, `text` | `level` (1-9), `style` | Section heading |
| `bullet_list` | `type`, `items[]` | `style` | Bulleted list |
| `numbered_list` | `type`, `items[]` | `style` | Numbered list |
| `table` | `type`, `rows[][]` | `header`, `style` | Data table |

### Stable Identifier Format

| Identifier Pattern | Example | Description |
|--------------------|---------|-------------|
| `body.p.{N}` | `body.p.0` | Paragraph N in document body |
| `body.p.{N}.r.{M}` | `body.p.0.r.1` | Run M in paragraph N |
| `body.tbl.{N}` | `body.tbl.0` | Table N in document |
| `body.tbl.{N}.r.{R}.c.{C}` | `body.tbl.0.r.1.c.2` | Cell at row R, column C in table N |
| `body.tbl.{N}.r.{R}.c.{C}.p.{P}` | `body.tbl.0.r.0.c.0.p.0` | Paragraph P in table cell |

---

## 8. Notes and References

### Environment Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_DOCS_ROOT` | `/filesystem` | Root directory for document operations |
| `MAX_IMAGE_WIDTH` | Configurable | Maximum width for extracted images |
| `MAX_IMAGE_HEIGHT` | Configurable | Maximum height for extracted images |
| `IMAGE_QUALITY` | Configurable | JPEG compression quality (1-100) |
| `TOOLS` | All enabled | Comma-separated list to enable specific tools only |

### Key Behaviors

- **Stable Identifiers**: Every paragraph, run, table, and cell has a unique ID that persists across reads
- **Image Annotation Format**: `body_p_{N}_r{M}` based on element location
- **Cache Key Format**: `{file_path}::{annotation}`
- **Section Pagination**: Large documents can be read by section index

### Dependencies

- [python-docx](https://python-docx.readthedocs.io/) — Documents document reading/writing
- [Pillow](https://pillow.readthedocs.io/) — Image processing and compression

---



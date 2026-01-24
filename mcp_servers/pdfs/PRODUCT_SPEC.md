# MCP Mock Product Specification: PDF Server

---

## 0. App Classification

| Property | Value | Notes |
|-----------|--------|-------|
| **Stateful** | Yes | Caches extracted images in memory for retrieval via annotation keys |
| **Mutable** | Yes | Can create new PDF documents from structured content |
| **Visual** | No | Does not require a GUI or Computer Use |
| **Private** | No | Does not require authentication or authorization |
| **Mock Type** | Full Mock | Operates on local filesystem; no external API dependencies |
| **Setup Flags** | `APP_PDF_ROOT`, `MAX_IMAGE_WIDTH`, `MAX_IMAGE_HEIGHT`, `IMAGE_QUALITY` | Environment variables for configuration |

---

## 1. Title

**PDF Server MCP**

---

## 2. Overview

The PDF Server provides comprehensive PDF document processing capabilities including text extraction, image extraction, text search, page rendering, and document creation. It enables AI agents to read, analyze, search, and create PDF documents within a sandboxed filesystem environment, supporting workflows such as document analysis, content extraction, report generation, and document search.

---

## 3. Approach

### Data Source

- **Filesystem-based**: All operations target PDF files within a configurable root directory (`APP_PDF_ROOT`)
- **No external APIs**: Self-contained processing using local PDF libraries (pypdf, PyMuPDF, ReportLab, Pillow)

### Data Modes

| Mode | Description |
|------|-------------|
| **Clean Mode** | Empty filesystem; PDFs must be created or placed in the root directory before use |
| **Offline Mode** | Reactive-only; processes PDFs that exist at request time |

### Core Architecture

| Component | Technology | Purpose |
|-----------|------------|---------|
| Text Extraction | pypdf | Extract text content from PDF pages |
| Image Extraction | pypdf + Pillow | Extract and compress embedded images |
| Visual Analysis | PyMuPDF (fitz) | Detect visual strikethrough, render pages as images |
| PDF Creation | ReportLab | Generate new PDF documents from structured blocks |
| Image Cache | In-memory dict | Store extracted images for retrieval by annotation key |

### Security

- **Path traversal protection**: All file paths are normalized and validated to prevent escaping the PDF root
- **Sandboxed operations**: Cannot access files outside `APP_PDF_ROOT`

---

## 4. Prioritized User Archetypes and Workflows

| User Type | Goal / Workflow | Primary Tools |
|-----------|-----------------|---------------|
| **Document Analyst** | Extract and analyze text content from PDF reports | `read_pdf_pages`, `search_pdf` |
| **Data Extractor** | Pull embedded images from PDF documents for processing | `read_pdf_pages` → `read_image` |
| **Report Generator** | Create structured PDF reports from data | `create_pdf` |
| **Legal/Compliance Reviewer** | Search for specific terms across PDF documents | `search_pdf` |
| **Accessibility Specialist** | Identify strikethrough text and document annotations | `read_pdf_pages` |
| **Scanned Document Handler** | Render PDF pages as images for OCR or visual inspection | `read_page_as_image` |

---

## 5. Extended Functionality (Future Builds)

| Feature | Description | Priority |
|---------|-------------|----------|
| **PDF Merging** | Combine multiple PDFs into one document | Medium |
| **Page Extraction** | Extract specific pages to a new PDF | Medium |
| **PDF Encryption** | Add password protection to created PDFs | Low |
| **Form Filling** | Populate PDF form fields | Medium |
| **Annotation Support** | Add highlights, comments, or annotations | Low |
| **OCR Integration** | Extract text from scanned/image-based PDFs | High |
| **Batch Processing** | Process multiple PDFs in a single operation | Medium |

---

## 6. Build Considerations

| Area | Description |
|------|-------------|
| **Path Validation** | All file paths must start with `/` and end with `.pdf`; path traversal is blocked |
| **Image Compression** | Extracted images auto-compressed to JPEG; configurable quality and max dimensions |
| **Memory Management** | Image cache is in-memory; large PDFs with many images may consume significant RAM |
| **Page Indexing** | All page numbers are 1-indexed (first page = 1) |
| **Color Space Handling** | Supports RGB, Grayscale, CMYK; all converted to RGB/JPEG for output |
| **Error Tolerance** | Invalid pages skipped with warnings; processing continues for valid pages |
| **Strikethrough Detection** | Two methods: annotation-based (PDF markup) and visual (line detection over text) |
| **Dependencies** | `pypdf`, `PyMuPDF (fitz)`, `Pillow`, `reportlab` |

---

## 7. Example MCP Commands

### Read PDF Pages

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `read_pdf_pages` | Extract text, images, and annotations from PDF pages | `file_path: "/docs/report.pdf"`, `pages: [1, 2, 3]` | Text content per page, image annotations, strikethrough info, metadata |

```
read_pdf_pages("/docs/annual_report.pdf", pages=[1, 2])

→ [pdf: pages=50, read=2]
  [images: count=3]
  [page 1]
  Annual Report 2024...
  [image: page=1, index=0, ref=@page1_img0, size=800x600]
  [page 2]
  Executive Summary...
```

### Retrieve Extracted Image

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `read_image` | Get cached image from previous extraction | `file_path: "/docs/report.pdf"`, `annotation: "page1_img0"` | JPEG image data |

```
read_image("/docs/annual_report.pdf", "page1_img0")

→ [Image: JPEG, format=jpeg]
```

### Search PDF

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `search_pdf` | Find text occurrences (Ctrl+F equivalent) | `file_path`, `query`, `case_sensitive`, `whole_documents`, `max_results` | Matches with page, line, position, and context |

```
search_pdf("/docs/contract.pdf", "termination", case_sensitive=False, whole_documents=True)

→ Found 3 match(es) for "termination":
  1. [Page 5, Line 12, Chars 45-56]: ...early termination of this agreement...
  2. [Page 8, Line 3, Chars 0-11]: Termination clause applies...
  3. [Page 15, Line 22, Chars 18-29]: ...grounds for termination include...
```

### Render Page as Image

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `read_page_as_image` | Render PDF page as high-quality image | `file_path`, `page_number` | JPEG image of rendered page |

```
read_page_as_image("/docs/scanned_form.pdf", page_number=1)

→ [Image: JPEG, 1700x2200, format=jpeg]
```

### Create PDF

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `create_pdf` | Generate new PDF from structured content | `directory`, `file_name`, `content[]`, `metadata`, `page_size` | Success message with file path |

```
create_pdf(
  directory="/reports",
  file_name="summary.pdf",
  content=[
    {"type": "heading", "text": "Q4 Summary", "level": 1},
    {"type": "paragraph", "text": "This quarter showed strong growth...", "bold": False},
    {"type": "bullet_list", "items": ["Revenue up 15%", "New markets entered", "Team expanded"]},
    {"type": "table", "rows": [["Metric", "Value"], ["Revenue", "$1.2M"], ["Users", "50K"]], "header": True}
  ],
  metadata={"title": "Q4 Summary Report", "author": "Finance Team"},
  page_size="letter"
)

→ PDF summary.pdf created at /reports/summary.pdf
```

### Content Block Types for `create_pdf`

| Block Type | Required Fields | Optional Fields | Description |
|------------|-----------------|-----------------|-------------|
| `paragraph` | `type`, `text` | `bold`, `italic` | Body text paragraph |
| `heading` | `type`, `text` | `level` (1-4) | Section heading |
| `bullet_list` | `type`, `items[]` | — | Bulleted list |
| `numbered_list` | `type`, `items[]` | — | Numbered list |
| `table` | `type`, `rows[][]` | `header` (bool) | Data table with rows/columns |
| `page_break` | `type` | — | Force new page |
| `spacer` | `type` | `height` (points) | Vertical whitespace |

---

## 8. Notes and References

### Environment Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_PDF_ROOT` | `/filesystem` | Root directory for all PDF operations |
| `MAX_IMAGE_WIDTH` | Configurable | Maximum width for extracted images |
| `MAX_IMAGE_HEIGHT` | Configurable | Maximum height for extracted images |
| `IMAGE_QUALITY` | Configurable | JPEG compression quality (1-100) |
| `TOOLS` | All enabled | Comma-separated list to enable specific tools only |

### Key Behaviors

- **Image Annotation Format**: `page{N}_img{M}` where N=page number (1-indexed), M=image index on page (0-indexed)
- **Cache Key Format**: `{file_path}::{annotation}` (e.g., `/docs/report.pdf::page1_img0`)
- **Strikethrough**: Detected via both PDF annotations (`/StrikeOut`) and visual analysis (lines drawn through text)
- **Error Handling**: Graceful degradation — errors on individual pages don't fail the entire operation

### Dependencies

- [pypdf](https://pypdf.readthedocs.io/) — PDF reading and text extraction
- [PyMuPDF (fitz)](https://pymupdf.readthedocs.io/) — Visual analysis, page rendering
- [Pillow](https://pillow.readthedocs.io/) — Image processing and compression
- [ReportLab](https://docs.reportlab.com/) — PDF creation

---



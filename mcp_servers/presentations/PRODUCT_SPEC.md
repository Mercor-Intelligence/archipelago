# MCP Mock Product Specification: Slides Server

---

## 0. App Classification

| Property | Value | Notes |
|-----------|--------|-------|
| **Stateful** | Yes | Maintains presentations in filesystem and caches images |
| **Mutable** | Yes | Can create, modify, and delete presentations |
| **Visual** | No | Does not require a GUI or Computer Use |
| **Private** | No | Does not require authentication or authorization |
| **Mock Type** | Full Mock | Operates on local filesystem; no external API dependencies |
| **Setup Flags** | `APP_SLIDES_ROOT` | Environment variable for root directory configuration |

---

## 1. Title

**Slides Server MCP**

---

## 2. Overview

The Slides Server provides comprehensive Microsoft Presentations (.pptx) presentation management capabilities including creating decks, reading content, editing slides, managing layouts, inserting charts and tables, and extracting images. It enables AI agents to programmatically generate presentations, update existing decks, and automate slide creation workflows.

---

## 3. Approach

### Data Source

- **Filesystem-based**: All operations target .pptx files within a configurable root directory (`APP_SLIDES_ROOT`)
- **No external APIs**: Self-contained processing using python-pptx library

### Data Modes

| Mode | Description |
|------|-------------|
| **Clean Mode** | Empty filesystem; presentations must be created before use |
| **Offline Mode** | Reactive-only; processes presentations that exist at request time |

### Core Architecture

| Component | Technology | Purpose |
|-----------|------------|---------|
| Presentation Engine | python-pptx | Create, read, and modify .pptx files |
| Layout System | python-pptx | Support 6 standard Presentations layouts |
| Image Cache | In-memory dict | Store extracted images for retrieval |
| Chart Engine | python-pptx.chart | Insert charts into slides |

### Security

- **Path traversal protection**: All file paths are normalized and validated
- **Sandboxed operations**: Cannot access files outside `APP_SLIDES_ROOT`

---

## 4. Prioritized User Archetypes and Workflows

| User Type | Goal / Workflow | Primary Tools |
|-----------|-----------------|---------------|
| **Presentation Creator** | Generate complete presentation decks | `create_deck`, `add_slide` |
| **Content Editor** | Update existing presentations | `edit_slides`, `read_individualslide` |
| **Report Generator** | Create data-driven presentations with charts | `insert_chart`, `insert_table` |
| **Content Reviewer** | Read and analyze presentation content | `read_slides`, `read_completedeck` |
| **Image Extractor** | Extract images from presentations | `read_image` |
| **Bulk Editor** | Find and replace text across slides | `edit_slides` (replace_text operation) |

---

## 5. Extended Functionality (Future Builds)

| Feature | Description | Priority |
|---------|-------------|----------|
| **Custom Themes** | Apply custom color themes and fonts | Medium |
| **Master Slides** | Edit slide master templates | Medium |
| **Animations** | Add entrance/exit animations | Low |
| **Transitions** | Add slide transitions | Low |
| **Audio/Video** | Insert audio and video files | Low |
| **Export to PDF** | Export presentation as PDF | High |
| **Slide Reordering** | Move slides to different positions | Medium |

---

## 6. Build Considerations

| Area | Description |
|------|-------------|
| **File Format** | Microsoft Presentations .pptx format (OpenXML) |
| **Path Validation** | All file paths must start with `/` and end with `.pptx` |
| **Slide Indexing** | Slides are 0-indexed (first slide is 0) |
| **Layout Support** | 6 standard layouts: title, title_and_content, section_header, two_content, title_only, blank |
| **Placeholders** | Content placed in placeholders: title, body, left, right |
| **Image Cache** | Images extracted and cached in memory with `{file_path}::{annotation}` key |
| **Notes Support** | Speaker notes supported on all slides |
| **Dependencies** | `python-pptx`, `Pillow` |

---

## 7. Example MCP Commands

### Create Deck

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `create_deck` | Create a new .pptx presentation | `directory`, `file_name`, `slides[]`, `metadata` | Success message |

```
create_deck(
  directory="/presentations",
  file_name="quarterly_review.pptx",
  slides=[
    {
      "layout": "title",
      "title": "Q4 2025 Review",
      "subtitle": "Financial Performance"
    },
    {
      "layout": "title_and_content",
      "title": "Key Metrics",
      "bullets": {"items": ["Revenue up 25%", "Customers increased 40%", "Profit margin at 35%"]},
      "notes": "Emphasize the growth trajectory"
    },
    {
      "layout": "title_and_content",
      "title": "Data Summary",
      "table": {
        "rows": [["Metric", "Q3", "Q4"], ["Revenue", "$1M", "$1.25M"], ["Customers", "1000", "1400"]],
        "header": true
      }
    },
    {
      "layout": "two_content",
      "title": "Comparison",
      "columns": {
        "left": {"items": ["Strengths", "Growth", "Innovation"]},
        "right": {"items": ["Challenges", "Competition", "Costs"]}
      }
    }
  ],
  metadata={"title": "Quarterly Review", "author": "Finance Team"}
)

→ Slides quarterly_review.pptx created
```

### Read Slides

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `read_slides` | Read content with pagination | `file_path`, `start`, `end` | Content chunk with range info |
| `read_completedeck` | Read entire presentation | `file_path` | Full text content |
| `read_individualslide` | Read single slide | `file_path`, `slide_index` | Slide text content |

```
read_slides("/presentations/quarterly_review.pptx", start=0, end=500)

→ SlideContentRange(content="Q4 2025 Review\nFinancial Performance\n...", range=(0, 500), total_file_length=2500)

read_individualslide("/presentations/quarterly_review.pptx", slide_index=1)

→ "Key Metrics\n• Revenue up 25%\n• Customers increased 40%\n• Profit margin at 35%"
```

### Edit Slides

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `edit_slides` | Apply batch operations to presentation | `file_path`, `operations[]`, `metadata` | Success message |

```
edit_slides(
  file_path="/presentations/quarterly_review.pptx",
  operations=[
    {"type": "update_slide_title", "index": 0, "title": "Q4 2025 Final Review"},
    {"type": "append_bullets", "index": 1, "placeholder": "body", "items": ["New achievement: 50% growth"]},
    {"type": "replace_text", "search": "Q3", "replace": "Q4", "match_case": false},
    {"type": "set_notes", "index": 2, "notes": "Emphasize the customer growth"},
    {"type": "duplicate_slide", "index": 1, "position": "after"}
  ],
  metadata={"author": "Updated by Finance Team"}
)

→ Slides at /presentations/quarterly_review.pptx updated
```

### Add Slide

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `add_slide` | Add new slide to existing deck | `file_path`, `slide_definition`, `position` | Success message |

```
add_slide(
  file_path="/presentations/quarterly_review.pptx",
  slide_definition={
    "layout": "title_and_content",
    "title": "Future Plans",
    "bullets": {"items": ["Goal 1", "Goal 2", "Goal 3"]}
  },
  position=3
)

→ Slide added at position 3
```

### Insert Chart / Table

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `insert_chart` | Add chart to slide | `file_path`, `slide_index`, `chart_type`, `data`, `title` | Success message |
| `insert_table` | Add table to slide | `file_path`, `slide_index`, `rows`, `header` | Success message |

```
insert_chart(
  file_path="/presentations/quarterly_review.pptx",
  slide_index=2,
  chart_type="bar",
  data=[["Category", "Value"], ["Q1", 100], ["Q2", 150], ["Q3", 200]],
  title="Quarterly Growth"
)

→ Chart inserted on slide 3
```

### Image Operations

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `read_image` | Retrieve extracted image | `file_path`, `annotation` | JPEG image data |
| `add_image` | Add image to slide | `file_path`, `slide_index`, `image_path` | Success message |
| `modify_image` | Resize/reposition image | `file_path`, `slide_index`, `image_index`, `width`, `height` | Success message |

```
read_image("/presentations/quarterly_review.pptx", "slide2_img0")

→ [Image: JPEG, format=jpeg]
```

### Edit Operations Summary

| Operation Type | Required Fields | Optional Fields | Description |
|----------------|-----------------|-----------------|-------------|
| `update_slide_title` | `type`, `index`, `title` | — | Update slide title |
| `set_bullets` | `type`, `index`, `placeholder`, `items` | — | Replace bullets |
| `append_bullets` | `type`, `index`, `placeholder`, `items` | — | Add bullets |
| `append_table` | `type`, `index`, `placeholder`, `rows` | `header` | Add table |
| `update_table_cell` | `type`, `index`, `table_idx`, `row`, `column`, `text` | — | Edit table cell |
| `replace_text` | `type`, `search`, `replace` | `match_case` | Find/replace all |
| `delete_slide` | `type`, `index` | — | Remove slide |
| `duplicate_slide` | `type`, `index` | `position` | Copy slide |
| `set_notes` | `type`, `index`, `notes` | — | Set speaker notes |
| `clear_placeholder` | `type`, `index`, `placeholder` | — | Clear placeholder |

### Slide Layouts

| Layout | Index | Content Areas | Description |
|--------|-------|---------------|-------------|
| `title` | 0 | title, subtitle | Title slide |
| `title_and_content` | 1 | title, body | Title with bullets/table |
| `section_header` | 2 | title, subtitle | Section divider |
| `two_content` | 3 | title, left, right | Two-column layout |
| `title_only` | 5 | title | Title with blank body |
| `blank` | 6 | none | Completely blank |

---

## 8. Notes and References

### Environment Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_SLIDES_ROOT` | `/filesystem` | Root directory for presentation operations |
| `TOOLS` | All enabled | Comma-separated list to enable specific tools only |

### Key Behaviors

- **Image Annotation Format**: `slide{N}_img{M}` where N=slide index, M=image index
- **Cache Key Format**: `{file_path}::{annotation}`
- **Pagination**: Large presentations can be read in chunks (max 10,000 chars per read)
- **Notes**: Speaker notes accessible via creation or `set_notes` operation

### Dependencies

- [python-pptx](https://python-pptx.readthedocs.io/) — Presentations file reading/writing
- [Pillow](https://pillow.readthedocs.io/) — Image processing

---



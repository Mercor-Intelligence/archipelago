import base64
import io
import os
import re

from docx import Document
from PIL import Image
from utils.decorators import make_async_background
from utils.image_cache import (
    IMAGE_CACHE,
    IMAGE_QUALITY,
    MAX_IMAGE_HEIGHT,
    MAX_IMAGE_WIDTH,
)
from utils.models import (
    Cell,
    DocumentBody,
    ImageRun,
    Paragraph,
    ReadDocumentContentMetadata,
    ReadDocumentContentResponse,
    Run,
    Table,
    TableRow,
)

DOCS_ROOT = os.getenv("APP_DOCS_ROOT") or os.getenv("APP_FS_ROOT", "/filesystem")


def _resolve_under_root(path: str) -> str:
    """Map path to the docs root."""
    path = path.lstrip("/")
    full_path = os.path.join(DOCS_ROOT, path)
    return os.path.normpath(full_path)


def _sanitize_filename(name: str) -> str:
    """Sanitize a filename to be filesystem-safe."""
    # Remove path separators and other problematic characters
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    # Remove leading/trailing spaces and dots
    name = name.strip(". ")
    return name if name else "unnamed"


def _compress_image_to_base64(image_bytes: bytes) -> str:
    """Compress and convert image to base64 using same technique as read_image tool.

    Args:
        image_bytes: Raw image bytes from docx

    Returns:
        Base64 encoded string of compressed JPEG image
    """
    buffer = io.BytesIO(image_bytes)

    with Image.open(buffer) as img:
        # Convert to RGB (handle RGBA, P, LA modes)
        if img.mode in ("RGBA", "P", "LA"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            if img.mode in ("RGBA", "LA"):
                background.paste(img, mask=img.split()[-1])
                img = background
            else:
                img = img.convert("RGB")
        elif img.mode != "RGB":
            img = img.convert("RGB")

        if img.width > MAX_IMAGE_WIDTH or img.height > MAX_IMAGE_HEIGHT:
            img.thumbnail((MAX_IMAGE_WIDTH, MAX_IMAGE_HEIGHT), Image.Resampling.LANCZOS)

        output_buffer = io.BytesIO()
        img.save(output_buffer, format="JPEG", quality=IMAGE_QUALITY, optimize=True)
        compressed_bytes = output_buffer.getvalue()

    return base64.b64encode(compressed_bytes).decode("utf-8")


def _extract_image_from_run(
    run, file_path: str, paragraph_id: str, run_idx: int
) -> ImageRun | None:
    """Extract image from a run and store in memory cache.

    Images are compressed once and stored as base64 in memory dictionary.
    They can be retrieved using the read_image tool with file_path and annotation.

    Args:
        run: The docx run object
        file_path: Full file path of the document (used as cache key prefix)
        paragraph_id: Paragraph identifier for unique naming
        run_idx: Run index

    Returns:
        ImageRun object if image was found and extracted, None otherwise
    """
    try:
        inline_shapes = run._element.xpath(".//pic:pic")
        if not inline_shapes:
            return None

        inline = run._element.xpath(".//a:blip/@r:embed")
        if not inline:
            return None

        image_rId = inline[0]
        image_part = run.part.related_parts.get(image_rId)
        if not image_part:
            return None

        image_bytes = image_part.blob

        base64_data = _compress_image_to_base64(image_bytes)

        safe_para_id = paragraph_id.replace(".", "_")
        annotation_key = f"{safe_para_id}_r{run_idx}"

        cache_key = f"{file_path}::{annotation_key}"
        IMAGE_CACHE[cache_key] = base64_data

        # Try to get dimensions
        width = None
        height = None
        try:
            extent_elements = run._element.xpath(".//wp:extent")
            if extent_elements:
                extent = extent_elements[0]
                width = int(extent.get("cx", 0))
                height = int(extent.get("cy", 0))
        except Exception:
            pass

        # Try to get alt text
        alt_text = None
        try:
            desc_elements = run._element.xpath(".//pic:cNvPr/@descr")
            if desc_elements:
                alt_text = desc_elements[0]
        except Exception:
            pass

        run_id = f"{paragraph_id}.r.{run_idx}"
        return ImageRun(
            id=run_id,
            type="image",
            annotation=annotation_key,
            width=width,
            height=height,
            alt_text=alt_text if alt_text else None,
        )
    except Exception:
        return None


def _serialize_run(
    paragraph_id: str, run, r_idx: int, file_path: str
) -> Run | ImageRun:
    """Serialize a run with its formatting, detecting images."""
    image_run = _extract_image_from_run(run, file_path, paragraph_id, r_idx)
    if image_run:
        return image_run

    run_id = f"{paragraph_id}.r.{r_idx}"
    font = run.font
    style_obj = getattr(run, "style", None)
    style_name = style_obj.name if style_obj else None

    bold = bool(getattr(run, "bold", False) or getattr(font, "bold", False)) or None
    italic = (
        bool(getattr(run, "italic", False) or getattr(font, "italic", False)) or None
    )
    underline = (
        bool(getattr(run, "underline", False) or getattr(font, "underline", False))
        or None
    )
    strikethrough = bool(getattr(font, "strike", False)) or None

    return Run(
        id=run_id,
        text=run.text,
        bold=bold if bold else None,
        italic=italic if italic else None,
        underline=underline if underline else None,
        strikethrough=strikethrough if strikethrough else None,
        style=style_name,
    )


def _serialize_paragraph(
    prefix: str, paragraph, p_idx: int, file_path: str
) -> Paragraph:
    """Serialize a paragraph with its runs."""
    paragraph_id = f"{prefix}.p.{p_idx}"
    runs = [
        _serialize_run(paragraph_id, r, i, file_path)
        for i, r in enumerate(paragraph.runs)
    ]
    alignment = (
        paragraph.alignment.name if getattr(paragraph, "alignment", None) else None
    )
    style_name = paragraph.style.name if getattr(paragraph, "style", None) else None
    return Paragraph(
        id=paragraph_id,
        style=style_name,
        alignment=alignment,
        runs=runs,
    )


def _serialize_table(prefix: str, table, t_idx: int, file_path: str) -> Table:
    """Serialize a table with its rows and cells."""
    table_id = f"{prefix}.tbl.{t_idx}"
    rows_out: list[TableRow] = []
    for r_idx, row in enumerate(table.rows):
        cells_out: list[Cell] = []
        for c_idx, cell in enumerate(row.cells):
            cell_prefix = f"{table_id}.r.{r_idx}.c.{c_idx}"
            cell_paragraphs = [
                _serialize_paragraph(cell_prefix, p, i, file_path)
                for i, p in enumerate(cell.paragraphs)
            ]
            cells_out.append(Cell(id=f"{cell_prefix}", paragraphs=cell_paragraphs))
        rows_out.append(TableRow(cells=cells_out))
    return Table(id=table_id, rows=rows_out)


def _is_heading(paragraph) -> bool:
    """Check if a paragraph is a heading."""
    style = getattr(paragraph, "style", None)
    if not style:
        return False
    style_name = style.name.lower() if style.name else ""
    return "heading" in style_name or "title" in style_name


def _find_heading_boundaries(doc) -> list[int]:
    """Find paragraph indices where headings occur."""
    heading_indices = []
    for p_idx, paragraph in enumerate(doc.paragraphs):
        if _is_heading(paragraph):
            heading_indices.append(p_idx)
    return heading_indices


@make_async_background
def read_document_content(file_path: str, section_index: int | None = None) -> str:
    """Parse a .docx document into structured content with stable identifiers."""

    if not isinstance(file_path, str) or not file_path:
        return "File path is required"
    if not file_path.startswith("/"):
        return "File path must start with /"
    if not file_path.lower().endswith(".docx"):
        return "File path must end with .docx"

    target_path = _resolve_under_root(file_path)

    try:
        if not os.path.exists(target_path):
            return f"File not found: {file_path}"
        if not os.path.isfile(target_path):
            return f"Not a file: {file_path}"

        doc = Document(target_path)
    except Exception as exc:
        return f"Failed to read document: {repr(exc)}"

    # Determine paragraph range based on section_index
    all_paragraphs = list(doc.paragraphs)
    start_idx = 0
    end_idx = len(all_paragraphs)
    section_range_str = None

    if section_index is not None:
        heading_indices = _find_heading_boundaries(doc)

        if not heading_indices:
            return "No headings found in document. Cannot paginate by section."

        if section_index < 0 or section_index >= len(heading_indices):
            return f"Invalid section_index: {section_index}. Document has {len(heading_indices)} sections."

        start_idx = heading_indices[section_index]
        # End at the next heading or end of document
        if section_index + 1 < len(heading_indices):
            end_idx = heading_indices[section_index + 1]
        else:
            end_idx = len(all_paragraphs)

        section_range_str = f"paragraphs {start_idx} to {end_idx - 1}"

    # Extract paragraphs in range
    body_prefix = "body"
    selected_paragraphs = all_paragraphs[start_idx:end_idx]
    body_paragraphs = [
        _serialize_paragraph(body_prefix, p, i, file_path)
        for i, p in enumerate(selected_paragraphs, start=start_idx)
    ]

    # Tables: For simplicity, include all tables when paginating
    # (determining table location within sections is complex)
    body_tables = [
        _serialize_table(body_prefix, t, i, file_path) for i, t in enumerate(doc.tables)
    ]

    # Calculate total runs and images
    total_runs = 0
    num_images = 0
    for para in body_paragraphs:
        total_runs += len(para.runs)
        for run in para.runs:
            if isinstance(run, ImageRun):
                num_images += 1
    for tbl in body_tables:
        for row in tbl.rows:
            for cell in row.cells:
                for para in cell.paragraphs:
                    total_runs += len(para.runs)
                    for run in para.runs:
                        if isinstance(run, ImageRun):
                            num_images += 1

    _, ext = os.path.splitext(file_path)
    extension = ext[1:].lower() if ext.startswith(".") else ext.lower()

    result = ReadDocumentContentResponse(
        filepath=file_path,
        extension=extension,
        status="success",
        metadata=ReadDocumentContentMetadata(
            num_paragraphs=len(body_paragraphs),
            num_tables=len(body_tables),
            num_sections=len(doc.sections),
            total_runs=total_runs,
            num_images=num_images,
            section_index=section_index,
            section_range=section_range_str,
        ),
        body=DocumentBody(paragraphs=body_paragraphs, tables=body_tables),
    )

    return str(result)

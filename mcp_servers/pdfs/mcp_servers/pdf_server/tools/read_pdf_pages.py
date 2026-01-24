import base64
import io
import os
from typing import Any, cast

import fitz  # PyMuPDF
import pypdf
from models.pdf_read import ImageInfo, PdfPagesRead, StrikethroughInfo
from PIL import Image
from pypdf.generic import TextStringObject
from utils.decorators import make_async_background
from utils.image_cache import (
    IMAGE_CACHE,
    IMAGE_QUALITY,
    MAX_IMAGE_HEIGHT,
    MAX_IMAGE_WIDTH,
)

PDF_ROOT = os.getenv("APP_PDF_ROOT") or os.getenv("APP_FS_ROOT", "/filesystem")


def _resolve_under_root(path: str) -> str:
    """Map path to the PDF root."""
    path = path.lstrip("/")
    full_path = os.path.join(PDF_ROOT, path)
    return os.path.normpath(full_path)


def _compress_image_to_base64(
    image_bytes: bytes, width: int, height: int, color_space: str
) -> str:
    """Compress and convert image to base64.

    Args:
        image_bytes: Raw image bytes from PDF (already decompressed by pypdf)
        width: Image width in pixels
        height: Image height in pixels
        color_space: PDF color space (e.g., /DeviceRGB, /DeviceGray, /DeviceCMYK)

    Returns:
        Base64 encoded string of compressed JPEG image
    """
    try:
        # Determine PIL mode from PDF color space
        if color_space == "/DeviceRGB":
            mode = "RGB"
        elif color_space == "/DeviceGray":
            mode = "L"
        elif color_space == "/DeviceCMYK":
            mode = "CMYK"
        else:
            # Try to decode as RGB by default
            mode = "RGB"

        # Create PIL Image from raw bytes
        try:
            img = Image.frombytes(mode, (width, height), image_bytes)
        except ValueError:
            buffer = io.BytesIO(image_bytes)
            img = Image.open(buffer)

        if img.mode in ("RGBA", "LA"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1])
            img = background
        elif img.mode == "P":
            img = img.convert("RGBA")
            background = Image.new("RGB", img.size, (255, 255, 255))
            background.paste(img, mask=img.split()[-1])
            img = background
        elif img.mode == "CMYK":
            img = img.convert("RGB")
        elif img.mode != "RGB" and img.mode != "L":
            img = img.convert("RGB")

        if img.width > MAX_IMAGE_WIDTH or img.height > MAX_IMAGE_HEIGHT:
            img.thumbnail((MAX_IMAGE_WIDTH, MAX_IMAGE_HEIGHT), Image.Resampling.LANCZOS)

        output_buffer = io.BytesIO()
        img.save(output_buffer, format="JPEG", quality=IMAGE_QUALITY, optimize=True)
        compressed_bytes = output_buffer.getvalue()

        return base64.b64encode(compressed_bytes).decode("utf-8")

    except Exception:
        raise


def _extract_images_from_page(
    page: pypdf.PageObject,
    page_num: int,
    file_path: str,
    errors: list[str] | None = None,
) -> list[ImageInfo]:
    """Extract images from a PDF page and store in memory cache.

    Args:
        page: PyPDF page object
        page_num: Page number (1-indexed)
        file_path: Full file path of the PDF (used as cache key prefix)
        errors: Optional list to append error messages to

    Returns:
        List of ImageInfo objects for images found on the page
    """
    images = []

    try:
        resources = page.get("/Resources")
        if resources is None:
            return images

        xobjects = resources.get("/XObject")
        if xobjects is None:
            return images

        image_count = 0
        for _xobj_idx, (obj_name, obj_ref) in enumerate(xobjects.items()):
            try:
                obj = obj_ref.get_object()

                if obj.get("/Subtype") != "/Image":
                    continue

                width = int(obj.get("/Width", 0))
                height = int(obj.get("/Height", 0))

                color_space = str(obj.get("/ColorSpace", "/DeviceRGB"))

                image_bytes = obj.get_data()

                base64_data = _compress_image_to_base64(
                    image_bytes, width, height, color_space
                )

                annotation_key = f"page{page_num}_img{image_count}"

                cache_key = f"{file_path}::{annotation_key}"
                IMAGE_CACHE[cache_key] = base64_data

                image_info = ImageInfo(
                    annotation=annotation_key,
                    page_number=page_num,
                    image_index=image_count,
                    width=width if width > 0 else None,
                    height=height if height > 0 else None,
                )
                images.append(image_info)
                image_count += 1

            except Exception as exc:
                if errors is not None:
                    errors.append(f"Page {page_num} image {obj_name}: {repr(exc)}")
                continue

    except Exception as exc:
        if errors is not None:
            errors.append(f"Page {page_num} image extraction: {repr(exc)}")

    return images


def _extract_strikethrough_from_pypdf_page(
    page: pypdf.PageObject, page_num: int, errors: list[str] | None = None
) -> list[StrikethroughInfo]:
    """Extract StrikeOut annotations from a PDF page using pypdf.

    Args:
        page: PyPDF page object
        page_num: Page number (1-indexed)
        errors: Optional list to append error messages to

    Returns:
        List of StrikethroughInfo objects for StrikeOut annotations found on the page
    """
    strikethrough_items = []

    try:
        annotations = page.get("/Annots")
        if annotations is None:
            return strikethrough_items

        for annot_idx, annot_ref in enumerate(annotations):
            try:
                annot = annot_ref.get_object()
                subtype = annot.get("/Subtype")

                if subtype == "/StrikeOut":
                    contents = annot.get("/Contents")
                    if contents is not None:
                        if isinstance(contents, TextStringObject):
                            contents = str(contents)
                        elif isinstance(contents, bytes):
                            contents = contents.decode("utf-8", errors="replace")
                        else:
                            contents = str(contents) if contents else None
                    else:
                        contents = None

                    rect = annot.get("/Rect")
                    rect_coords = None
                    if rect:
                        try:
                            rect_coords = [float(x) for x in rect]
                        except (TypeError, ValueError):
                            rect_coords = None

                    strikethrough_info = StrikethroughInfo(
                        page_number=page_num,
                        contents=contents,
                        rect=rect_coords,
                    )
                    strikethrough_items.append(strikethrough_info)

            except Exception as exc:
                if errors is not None:
                    errors.append(
                        f"Page {page_num} annotation {annot_idx}: {repr(exc)}"
                    )
                continue

    except Exception as exc:
        if errors is not None:
            errors.append(f"Page {page_num} strikethrough extraction: {repr(exc)}")

    return strikethrough_items


def _extract_visual_strikethrough_from_page(
    fitz_page: fitz.Page, page_num: int, errors: list[str] | None = None
) -> list[StrikethroughInfo]:
    """Detect visual strikethrough (lines drawn through text) using PyMuPDF.

    Args:
        fitz_page: PyMuPDF page object
        page_num: Page number (1-indexed)
        errors: Optional list to append error messages to

    Returns:
        List of StrikethroughInfo objects for visual strikethrough found on the page
    """
    results = []
    try:
        lines = [
            ((min(i[1].x, i[2].x), max(i[1].x, i[2].x)), (i[1].y + i[2].y) / 2)
            for d in fitz_page.get_drawings()
            for i in d.get("items", [])
            if i[0] == "l" and abs(i[1].y - i[2].y) < 3
        ]
        if not lines:
            return results

        text_dict = cast(dict[str, Any], fitz_page.get_text("dict"))
        for block in text_dict.get("blocks", []):
            if block.get("type") != 0:
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    text, bbox = span.get("text", "").strip(), span.get("bbox")
                    if not text or not bbox:
                        continue
                    x0, y0, x1, y1 = bbox
                    text_height = y1 - y0
                    center_y = (y0 + y1) / 2
                    tolerance = text_height * 0.2

                    if any(
                        lx[0] <= x1 and lx[1] >= x0 and abs(ly - center_y) <= tolerance
                        for lx, ly in lines
                    ):
                        results.append(
                            StrikethroughInfo(
                                page_number=page_num, contents=text, rect=list(bbox)
                            )
                        )
    except Exception as exc:
        if errors is not None:
            errors.append(f"Page {page_num} visual strikethrough: {repr(exc)}")
    return results


@make_async_background
def read_pdf_pages(file_path: str, pages: list[int] | None = None) -> str:
    """Extract text, images, and strikethrough annotations from PDF pages."""

    if not file_path.startswith("/"):
        return "File path must start with /"
    if not file_path.lower().endswith(".pdf"):
        return "File path must end with .pdf"

    target_path = _resolve_under_root(file_path)

    try:
        if not os.path.exists(target_path):
            return f"File not found: {file_path}"
        if not os.path.isfile(target_path):
            return f"Not a file: {file_path}"

        content = {}
        all_images = []
        all_strikethrough = []
        errors = []

        with open(target_path, "rb") as f:
            pdf_reader = pypdf.PdfReader(f)
            total_pages = len(pdf_reader.pages)

            # Determine which pages to read
            if pages is None or not pages:
                pages_to_read = list(range(1, total_pages + 1))
            else:
                # Filter valid pages (1-indexed)
                pages_to_read = [p for p in pages if 1 <= p <= total_pages]

            for page_num in pages_to_read:
                try:
                    # Convert to 0-indexed for pypdf
                    page = pdf_reader.pages[page_num - 1]

                    text = page.extract_text(extraction_mode="layout")
                    content[page_num] = text

                    # Extract images
                    page_images = _extract_images_from_page(
                        page, page_num, file_path, errors
                    )
                    all_images.extend(page_images)

                    page_strikethrough = _extract_strikethrough_from_pypdf_page(
                        page, page_num, errors
                    )
                    all_strikethrough.extend(page_strikethrough)

                except Exception as exc:
                    errors.append(f"Page {page_num}: {repr(exc)}")

        try:
            fitz_doc = fitz.open(target_path)
            try:
                for page_num in pages_to_read:
                    try:
                        fitz_page = fitz_doc[page_num - 1]  # 0-indexed
                        visual_strikethrough = _extract_visual_strikethrough_from_page(
                            fitz_page, page_num, errors
                        )
                        all_strikethrough.extend(visual_strikethrough)
                    except Exception as exc:
                        errors.append(
                            f"Visual strikethrough page {page_num}: {repr(exc)}"
                        )
            finally:
                fitz_doc.close()
        except Exception as exc:
            errors.append(f"PyMuPDF processing: {repr(exc)}")

    except Exception as exc:
        return f"Failed to process PDF: {repr(exc)}"

    try:
        result = PdfPagesRead(
            content=content,
            total_pages=total_pages,
            requested_pages=pages_to_read,
            images=all_images,
            strikethrough=all_strikethrough,
            errors=errors if errors else None,
        )
        return str(result)
    except Exception as exc:
        return f"Failed to create result: {repr(exc)}"

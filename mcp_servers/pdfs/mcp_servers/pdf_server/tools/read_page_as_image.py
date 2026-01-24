import io
import os

import fitz  # PyMuPDF
from fastmcp.utilities.types import Image as FastMCPImage
from PIL import Image
from utils.decorators import make_async_background

PDF_ROOT = os.getenv("APP_PDF_ROOT") or os.getenv("APP_FS_ROOT", "/filesystem")


def _resolve_under_root(path: str) -> str:
    """Map path to the PDF root."""
    path = path.lstrip("/")
    full_path = os.path.join(PDF_ROOT, path)
    return os.path.normpath(full_path)


@make_async_background
def read_page_as_image(file_path: str, page_number: int) -> FastMCPImage | str:
    """Render a PDF page as an image."""
    if not file_path.startswith("/"):
        return "File path must start with /"
    if not file_path.lower().endswith(".pdf"):
        return "File path must end with .pdf"

    target_path = _resolve_under_root(file_path)

    if not os.path.exists(target_path):
        return f"File not found: {file_path}"

    try:
        doc = fitz.open(target_path)
        try:
            total_pages = len(doc)

            if page_number < 1 or page_number > total_pages:
                return f"Page {page_number} is out of range (PDF has {total_pages} page(s))"

            page = doc[page_number - 1]
            mat = fitz.Matrix(2, 2)  # 2x zoom = 144 DPI
            pix = page.get_pixmap(matrix=mat)

            img_bytes = pix.tobytes("png")
            img = Image.open(io.BytesIO(img_bytes))

            output_buffer = io.BytesIO()
            if img.mode in ("RGBA", "LA", "P"):
                background = Image.new("RGB", img.size, (255, 255, 255))
                if img.mode == "P":
                    img = img.convert("RGBA")
                background.paste(
                    img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None
                )
                img = background

            img.save(output_buffer, format="JPEG", quality=85, optimize=True)

            return FastMCPImage(
                data=output_buffer.getvalue(),
                format="jpeg",
            )

        finally:
            doc.close()

    except Exception as exc:
        return f"Failed to render page as image: {repr(exc)}"

import io
import os
from typing import Annotated

import fitz  # PyMuPDF
from fastmcp.utilities.types import Image as FastMCPImage
from PIL import Image
from pydantic import Field
from utils.decorators import make_async_background
from utils.path_utils import PathTraversalError, resolve_under_root
from utils.pdf_protection import (
    PdfPasswordIncorrectError,
    PdfPasswordRequiredError,
    password_error_message,
    unlock_pdf_bytes,
)


@make_async_background
def read_page_as_image(
    file_path: Annotated[
        str,
        Field(
            description="Absolute path to the PDF file. Must start with '/' and end with '.pdf'."
        ),
    ],
    page_number: Annotated[
        int,
        Field(
            description="The page to render, 1-indexed (first page is 1). Must be between 1 and the total page count."
        ),
    ],
    password: Annotated[
        str | None,
        Field(
            description="Password for a password-protected (encrypted) PDF. Omit it for "
            "ordinary PDFs and for PDFs that open with an empty password. A wrong "
            "password returns a distinct 'password ... is incorrect' error."
        ),
    ] = None,
) -> FastMCPImage | str:
    """Render a single PDF page as a JPEG image at 144 DPI.

    Converts the specified page of a PDF into an image suitable for OCR
    input, visual inspection, or thumbnail generation. Returns the
    rendered image, or an error string if the file is not found, the page
    number is out of range, or the document is password-protected and
    ``password`` is missing or wrong.
    """
    if not file_path.startswith("/"):
        return "File path must start with /"
    if not file_path.lower().endswith(".pdf"):
        return "File path must end with .pdf"

    try:
        target_path = resolve_under_root(file_path)
    except PathTraversalError:
        return f"Invalid path: {file_path}"

    if not os.path.exists(target_path):
        return f"File not found: {file_path}"

    try:
        doc = fitz.open(target_path)
        if doc.needs_pass:
            # Decrypt with pypdf and render the plain bytes. PyMuPDF's own
            # authenticate() reports success on some real documents while
            # leaving every page unreadable.
            doc.close()
            with open(target_path, "rb") as f:
                encrypted = f.read()
            try:
                plain, _ = unlock_pdf_bytes(encrypted, password)
            except (PdfPasswordRequiredError, PdfPasswordIncorrectError) as exc:
                return password_error_message(file_path, exc)
            doc = fitz.open(stream=plain, filetype="pdf")
        try:
            if doc.needs_pass:
                return password_error_message(file_path, PdfPasswordRequiredError())

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

"""Optional OCR support for image-only/scanned PDF pages.

Uses pytesseract, which shells out to the `tesseract` binary. When either is
missing, `ocr_unavailable_reason()` says which so callers can report it.
"""

from __future__ import annotations

import shutil

_OCR_AVAILABLE = False
_pytesseract = None

try:
    import pytesseract

    _pytesseract = pytesseract
    _OCR_AVAILABLE = True
except ImportError:
    pass


def ocr_unavailable_reason() -> str | None:
    """Return why OCR cannot run, or None when pytesseract and tesseract are both present."""
    if not _OCR_AVAILABLE or _pytesseract is None:
        return "OCR unavailable: pytesseract is not installed"
    cmd = getattr(_pytesseract.pytesseract, "tesseract_cmd", "tesseract")
    if shutil.which(cmd) is None:
        return f"OCR unavailable: tesseract binary '{cmd}' not found"
    return None


def ocr_available() -> bool:
    """Return True if OCR can run (pytesseract importable and tesseract binary on PATH)."""
    return ocr_unavailable_reason() is None


def ocr_page_image(image_bytes: bytes, *, format: str = "PNG") -> str | None:
    """Run OCR on an image and return extracted text.

    Args:
        image_bytes: Raw image bytes (PNG, JPEG, etc.)
        format: Image format hint for PIL (default "PNG")

    Returns:
        Extracted text string, or None if OCR failed or is unavailable.
    """
    if not ocr_available() or _pytesseract is None:
        return None

    try:
        import io

        from PIL import Image

        img = Image.open(io.BytesIO(image_bytes))
        text = _pytesseract.image_to_string(img)
        return text.strip() if text else None
    except Exception:
        return None

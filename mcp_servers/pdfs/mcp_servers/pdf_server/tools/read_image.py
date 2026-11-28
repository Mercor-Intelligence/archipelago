import base64
import re
from typing import Annotated

from fastmcp.utilities.types import Image
from loguru import logger
from pydantic import Field
from tools.read_pdf_pages import warm_page_image_cache
from utils.decorators import make_async_background
from utils.image_cache import IMAGE_CACHE
from utils.path_utils import PathTraversalError, resolve_under_root
from utils.pdf_protection import (
    PdfPasswordIncorrectError,
    PdfPasswordRequiredError,
    image_cache_prefix,
)

# Annotation keys are minted by read_pdf_pages as 'page{N}_img{M}', which is
# enough to locate the image in the document itself when it is not cached.
_ANNOTATION_PATTERN = re.compile(r"^page(\d+)_img(\d+)$")

# What the caller is told when the document cannot be parsed at all. The reason
# has to be short, stable, and free of library internals: the parser's own
# exception repr names the pypdf error class and changes with the pypdf version,
# and it tells the agent nothing it can act on. The repr stays in the log line
# for the operator.
UNREADABLE_DOCUMENT_NOTE = "the file is not a readable PDF"


def _cached_image(
    physical_path: str, annotation: str, password: str | None
) -> str | None:
    """Look the image up under every cache key this call is entitled to.

    The plain key only ever holds images from documents that open without a
    password. The password-scoped key is consulted only when the caller supplied
    a password, so an image unlocked by an earlier call is never served to a
    call that does not present the same password.
    """
    keys = [f"{image_cache_prefix(physical_path, None)}::{annotation}"]
    if password:
        keys.append(f"{image_cache_prefix(physical_path, password)}::{annotation}")
    for key in keys:
        data = IMAGE_CACHE.get(key)
        if data is not None:
            return data
    return None


def _extract_on_demand(
    file_path: str, physical_path: str, annotation: str, password: str | None
) -> str:
    """Re-extract a not-yet-cached image from the document and return its base64 JPEG.

    Raises ValueError naming the exact required call when the annotation cannot
    be resolved against the document.
    """
    match = _ANNOTATION_PATTERN.match(annotation)
    if match is None:
        logger.warning(
            f"read_image: cache miss for '{file_path}' with unrecognized annotation "
            f"'{annotation}'; cannot self-initialize"
        )
        raise ValueError(
            f"Image not found in cache for file '{file_path}' with annotation "
            f"'{annotation}', and the annotation is not in the expected "
            "'page{N}_img{M}' format (e.g. 'page1_img0'). Required call: "
            f"read_pdf_pages(file_path='{file_path}') returns the available image "
            "annotation keys."
        )

    page_num = int(match.group(1))
    logger.info(
        f"read_image: image cache miss for '{file_path}' annotation '{annotation}'; "
        f"extracting page {page_num} from the document on demand"
    )

    try:
        image_count = warm_page_image_cache(physical_path, page_num, password)
    except (PdfPasswordRequiredError, PdfPasswordIncorrectError) as exc:
        logger.warning(
            f"read_image: on-demand extraction failed for '{file_path}' "
            f"annotation '{annotation}': document password missing or incorrect"
        )
        raise ValueError(
            f"Image not found in cache for file '{file_path}' with annotation "
            f"'{annotation}', and it could not be extracted from the document: "
            f"{exc}."
        ) from None
    except Exception as exc:
        # warm_page_image_cache raises ValueError for every condition it names
        # itself ("the document does not exist", "page N is out of range", ...);
        # those messages are written for the caller and pass through verbatim.
        # Anything else is the parser failing, and only the log gets its repr.
        reason = str(exc) if isinstance(exc, ValueError) else UNREADABLE_DOCUMENT_NOTE
        logger.warning(
            f"read_image: on-demand extraction failed for '{file_path}' "
            f"annotation '{annotation}': {repr(exc)}"
        )
        raise ValueError(
            f"Image not found in cache for file '{file_path}' with annotation "
            f"'{annotation}', and it could not be extracted from the document: "
            f"{reason}."
        ) from exc

    base64_data = _cached_image(physical_path, annotation, password)
    if base64_data is None:
        logger.warning(
            f"read_image: page {page_num} of '{file_path}' holds {image_count} "
            f"image(s); annotation '{annotation}' is not one of them"
        )
        raise ValueError(
            f"Image not found in cache for file '{file_path}' with annotation "
            f"'{annotation}': the document was re-read on demand and page "
            f"{page_num} contains {image_count} embedded image(s). Required call: "
            f"read_pdf_pages(file_path='{file_path}', pages=[{page_num}]) returns "
            "the available image annotation keys."
        )

    logger.info(
        f"read_image: recovered annotation '{annotation}' for '{file_path}' "
        "by re-extracting it from the document"
    )
    return base64_data


@make_async_background
def read_image(
    file_path: Annotated[
        str,
        Field(
            description="Absolute path to the PDF that holds the image. Must start with '/' and end with '.pdf'."
        ),
    ],
    annotation: Annotated[
        str,
        Field(
            description="Image annotation key in the format 'page{N}_img{M}' (e.g., 'page1_img0') "
            "as returned in the read_pdf_pages output. A leading '@' prefix is stripped "
            "automatically. No prior call is required: an uncached image is extracted from "
            "the document on demand."
        ),
    ],
    password: Annotated[
        str | None,
        Field(
            description="Password for a password-protected (encrypted) PDF. Omit it for "
            "ordinary PDFs and for PDFs that open with an empty password. Pass the same "
            "password given to read_pdf_pages."
        ),
    ] = None,
) -> Image:
    """Retrieve an embedded image from a PDF by its annotation key.

    Returns the JPEG image data for vision analysis or downstream storage.
    Serves the image from the in-memory cache when a prior read_pdf_pages call
    already extracted it, and otherwise extracts it from the document on demand,
    so the call also succeeds on a fresh server, after cache eviction, and on
    retries. The image is addressed by (document path, annotation key); no
    hidden process state has to be seeded first. For a password-protected PDF,
    pass its password in ``password``.
    """
    if not isinstance(file_path, str) or not file_path:
        raise ValueError("File path is required and must be a string")

    if not isinstance(annotation, str) or not annotation:
        raise ValueError("Annotation is required and must be a string")

    # Normalize path to match read_pdf_pages behavior (must start with /)
    if not file_path.startswith("/"):
        file_path = "/" + file_path

    # The suffix the schema documents, enforced the way read_pdf_pages,
    # read_page_as_image, search_pdf and create_pdf all enforce it. Without it a
    # non-PDF path reached pypdf and came back as a failed document read, which
    # describes neither what was wrong nor what the caller should do instead.
    if not file_path.lower().endswith(".pdf"):
        raise ValueError("File path must end with .pdf")

    # Strip leading @ if present (the @ is a display prefix in read_pdf_pages output)
    clean_annotation = annotation.lstrip("@")

    # Validate annotation is not empty after stripping
    if not clean_annotation:
        raise ValueError("Annotation cannot be empty or contain only '@' characters")

    # Cache keys use the resolved physical path so entries stay scoped to the
    # active actor's filesystem (read_pdf_pages writes them the same way).
    try:
        physical_path = resolve_under_root(file_path)
    except PathTraversalError as exc:
        raise ValueError(f"Invalid path: {file_path}") from exc

    try:
        base64_data = _cached_image(physical_path, clean_annotation, password)

        if base64_data is None:
            base64_data = _extract_on_demand(
                file_path, physical_path, clean_annotation, password
            )

        if len(base64_data) == 0:
            raise ValueError("Image data is empty")

        image_bytes = base64.b64decode(base64_data, validate=True)
        return Image(data=image_bytes, format="jpeg")

    except ValueError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Failed to read image from cache: {repr(exc)}") from exc

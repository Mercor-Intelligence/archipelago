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

# Annotation keys are minted by read_pdf_pages as 'page{N}_img{M}', which is
# enough to locate the image in the document itself when it is not cached.
_ANNOTATION_PATTERN = re.compile(r"^page(\d+)_img(\d+)$")


def _extract_on_demand(
    file_path: str, physical_path: str, annotation: str, cache_key: str
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
        image_count = warm_page_image_cache(physical_path, page_num)
    except Exception as exc:
        reason = str(exc) if isinstance(exc, ValueError) else repr(exc)
        logger.warning(
            f"read_image: on-demand extraction failed for '{file_path}' "
            f"annotation '{annotation}': {reason}"
        )
        raise ValueError(
            f"Image not found in cache for file '{file_path}' with annotation "
            f"'{annotation}', and it could not be extracted from the document: "
            f"{reason}."
        ) from exc

    base64_data = IMAGE_CACHE.get(cache_key)
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
) -> Image:
    """Retrieve an embedded image from a PDF by its annotation key.

    Returns the JPEG image data for vision analysis or downstream storage.
    Serves the image from the in-memory cache when a prior read_pdf_pages call
    already extracted it, and otherwise extracts it from the document on demand,
    so the call also succeeds on a fresh server, after cache eviction, and on
    retries. The image is addressed by (document path, annotation key); no
    hidden process state has to be seeded first.
    """
    if not isinstance(file_path, str) or not file_path:
        raise ValueError("File path is required and must be a string")

    if not isinstance(annotation, str) or not annotation:
        raise ValueError("Annotation is required and must be a string")

    # Normalize path to match read_pdf_pages behavior (must start with /)
    if not file_path.startswith("/"):
        file_path = "/" + file_path

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

    cache_key = f"{physical_path}::{clean_annotation}"

    try:
        # Use get() atomically to avoid race condition between check and get
        base64_data = IMAGE_CACHE.get(cache_key)

        if base64_data is None:
            base64_data = _extract_on_demand(
                file_path, physical_path, clean_annotation, cache_key
            )

        if len(base64_data) == 0:
            raise ValueError("Image data is empty")

        image_bytes = base64.b64decode(base64_data, validate=True)
        return Image(data=image_bytes, format="jpeg")

    except ValueError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Failed to read image from cache: {repr(exc)}") from exc

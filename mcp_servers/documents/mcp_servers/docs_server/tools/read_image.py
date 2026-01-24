import base64

from fastmcp.utilities.types import Image
from utils.decorators import make_async_background
from utils.image_cache import IMAGE_CACHE


@make_async_background
def read_image(file_path: str, annotation: str) -> Image:
    """Read an image from document using file path and annotation key."""
    if not isinstance(file_path, str) or not file_path:
        raise ValueError("File path is required and must be a string")

    if not isinstance(annotation, str) or not annotation:
        raise ValueError("Annotation is required and must be a string")

    cache_key = f"{file_path}::{annotation}"

    if cache_key not in IMAGE_CACHE:
        raise ValueError(
            f"Image not found in cache for file '{file_path}' with annotation '{annotation}'. "
            "Make sure you've called read_document_content first to extract images."
        )

    try:
        base64_data = IMAGE_CACHE[cache_key]

        if not base64_data or len(base64_data) == 0:
            raise ValueError("Image data is empty")

        image_bytes = base64.b64decode(base64_data)
        return Image(data=image_bytes, format="jpeg")

    except ValueError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Failed to read image from cache: {repr(exc)}") from exc

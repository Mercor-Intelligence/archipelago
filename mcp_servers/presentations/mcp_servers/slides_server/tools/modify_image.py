import os
import tempfile
from io import BytesIO

from models.response import ModifyImageResponse
from PIL import Image as PILImage
from PIL import ImageEnhance
from pptx import Presentation
from pptx.shapes.picture import Picture
from utils.decorators import make_async_background

SLIDES_ROOT = os.getenv("APP_SLIDES_ROOT") or os.getenv("APP_FS_ROOT", "/filesystem")


def _resolve_under_root(path: str) -> str:
    """Map path to the slides root."""
    path = path.lstrip("/")
    full_path = os.path.join(SLIDES_ROOT, path)
    return os.path.normpath(full_path)


@make_async_background
def modify_image(
    file_path: str,
    slide_index: int,
    image_index: int,
    operation: str,
    rotation: int | None = None,
    flip: str | None = None,
    brightness: float | None = None,
    contrast: float | None = None,
    crop_left: int | None = None,
    crop_top: int | None = None,
    crop_right: int | None = None,
    crop_bottom: int | None = None,
) -> ModifyImageResponse:
    """Modify an existing image in a slide (rotate, flip, brightness, contrast, crop)."""

    def error(msg: str) -> ModifyImageResponse:
        return ModifyImageResponse(success=False, error=msg)

    if not isinstance(file_path, str) or not file_path:
        return error("File path is required")
    if not file_path.startswith("/"):
        return error("File path must start with /")
    if not file_path.lower().endswith(".pptx"):
        return error("File path must end with .pptx")

    valid_operations = ("rotate", "flip", "brightness", "contrast", "crop")
    if operation not in valid_operations:
        return error(
            f"Invalid operation: {operation}. Valid operations: {', '.join(valid_operations)}"
        )

    if operation == "rotate":
        if rotation is None:
            return error("Rotation angle is required for rotate operation")
        if not isinstance(rotation, int | float) or rotation < 0 or rotation > 360:
            return error("Rotation must be between 0 and 360 degrees")
    elif operation == "flip":
        if flip is None:
            return error("Flip direction is required for flip operation")
        if flip not in ("horizontal", "vertical"):
            return error("Flip must be 'horizontal' or 'vertical'")
    elif operation == "brightness":
        if brightness is None:
            return error("Brightness factor is required for brightness operation")
        if not isinstance(brightness, int | float) or brightness <= 0:
            return error("Brightness must be a positive number")
    elif operation == "contrast":
        if contrast is None:
            return error("Contrast factor is required for contrast operation")
        if not isinstance(contrast, int | float) or contrast <= 0:
            return error("Contrast must be a positive number")
    elif operation == "crop":
        if (
            crop_left is None
            or crop_top is None
            or crop_right is None
            or crop_bottom is None
        ):
            return error(
                "Crop operation requires crop_left, crop_top, crop_right, and crop_bottom"
            )
        if not all(
            isinstance(v, int) and v >= 0
            for v in [crop_left, crop_top, crop_right, crop_bottom]
        ):
            return error("Crop values must be non-negative integers")
        if crop_left >= crop_right:
            return error("crop_left must be less than crop_right")
        if crop_top >= crop_bottom:
            return error("crop_top must be less than crop_bottom")

    target_path = _resolve_under_root(file_path)

    if not os.path.exists(target_path):
        return error(f"File not found: {file_path}")

    try:
        with open(target_path, "rb") as f:
            presentation = Presentation(BytesIO(f.read()))
    except Exception as exc:
        return error(f"Failed to open presentation: {repr(exc)}")

    if slide_index < 0 or slide_index >= len(presentation.slides):
        if len(presentation.slides) == 0:
            return error(
                f"Slide index {slide_index} is invalid: presentation has no slides"
            )
        return error(
            f"Slide index {slide_index} is out of range (0-{len(presentation.slides) - 1})"
        )

    slide = presentation.slides[slide_index]

    images = [shape for shape in slide.shapes if isinstance(shape, Picture)]

    if image_index < 0 or image_index >= len(images):
        if len(images) == 0:
            return error(
                f"Image index {image_index} is invalid: no images found on slide {slide_index}"
            )
        return error(
            f"Image index {image_index} is out of range (0-{len(images) - 1}). "
            f"Found {len(images)} image(s) on slide {slide_index}"
        )

    picture_shape = images[image_index]

    try:
        image_part = picture_shape.image
        image_bytes = image_part.blob

        pil_image = PILImage.open(BytesIO(image_bytes))
        image_format = pil_image.format or "PNG"

        original_pil_width, original_pil_height = pil_image.size

        if operation == "rotate":
            pil_image = pil_image.rotate(-rotation, expand=True)  # type: ignore[arg-type]
        elif operation == "flip":
            if flip == "horizontal":
                pil_image = pil_image.transpose(PILImage.Transpose.FLIP_LEFT_RIGHT)  # type: ignore[attr-defined]
            else:
                pil_image = pil_image.transpose(PILImage.Transpose.FLIP_TOP_BOTTOM)  # type: ignore[attr-defined]
        elif operation == "brightness":
            enhancer = ImageEnhance.Brightness(pil_image)
            pil_image = enhancer.enhance(brightness)  # type: ignore[arg-type]
        elif operation == "contrast":
            enhancer = ImageEnhance.Contrast(pil_image)
            pil_image = enhancer.enhance(contrast)  # type: ignore[arg-type]
        elif operation == "crop":
            img_width, img_height = pil_image.size
            if crop_right > img_width or crop_bottom > img_height:  # type: ignore[operator]
                return error(
                    f"Crop bounds exceed image dimensions ({img_width}x{img_height})"
                )
            pil_image = pil_image.crop((crop_left, crop_top, crop_right, crop_bottom))  # type: ignore[arg-type]

        output_buffer = BytesIO()
        pil_image.save(output_buffer, format=image_format)
        modified_image_bytes = output_buffer.getvalue()

        left = picture_shape.left
        top = picture_shape.top
        width = picture_shape.width
        height = picture_shape.height

        if operation == "rotate" and rotation is not None:
            rotated_width, rotated_height = pil_image.size

            width_scale = rotated_width / original_pil_width
            height_scale = rotated_height / original_pil_height

            width = int(width * width_scale)
            height = int(height * height_scale)
        elif operation == "crop":
            cropped_width, cropped_height = pil_image.size

            width_scale = cropped_width / original_pil_width
            height_scale = cropped_height / original_pil_height

            width = int(width * width_scale)
            height = int(height * height_scale)

        sp = picture_shape._element
        sp.getparent().remove(sp)

        with tempfile.NamedTemporaryFile(
            delete=False, suffix=f".{image_format.lower()}"
        ) as tmp_file:
            tmp_path = tmp_file.name
            tmp_file.write(modified_image_bytes)

        try:
            slide.shapes.add_picture(tmp_path, left, top, width, height)
        finally:
            os.unlink(tmp_path)

    except Exception as exc:
        return error(f"Failed to modify image: {repr(exc)}")

    try:
        presentation.save(target_path)
    except Exception as exc:
        return error(f"Failed to save presentation: {repr(exc)}")

    return ModifyImageResponse(
        success=True,
        image_index=image_index,
        slide_index=slide_index,
        operation=operation,
    )

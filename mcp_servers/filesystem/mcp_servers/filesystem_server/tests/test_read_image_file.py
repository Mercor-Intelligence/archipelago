"""Tests for ``tools.read_image_file`` — media type comes from content.

Regression coverage for issue #205: the tool used to derive the returned
media type from the filename extension, so JPEG bytes named ``.png`` (as
shipped in ``examples/simple_task/original_snapshot.zip``) were sent to
providers as ``image/png``. Anthropic rejects that mismatch; the agent then
retried and the task failed ungraded. The format must be sniffed from the
image magic bytes, with the extension only as a fallback.
"""

import io
import zipfile
from pathlib import Path

import pytest
from PIL import Image as PILImage
from tools.read_image_file import _sniff_image_format, read_image_file

REPO_ROOT = Path(__file__).resolve().parents[5]


def _make_image_bytes(fmt: str) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", (8, 8), color="red").save(buf, format=fmt)
    return buf.getvalue()


@pytest.mark.asyncio
async def test_jpeg_named_png_returns_jpeg(sandbox):
    """The #205 regression: JPEG content named .png must be typed jpeg."""
    (sandbox / "photo.png").write_bytes(_make_image_bytes("JPEG"))

    result = await read_image_file("/photo.png")

    assert result._format == "jpeg"
    assert result.to_image_content().mimeType == "image/jpeg"


@pytest.mark.asyncio
async def test_png_named_jpg_returns_png(sandbox):
    """Sniffing must win in both directions: PNG content named .jpg is png."""
    (sandbox / "photo.jpg").write_bytes(_make_image_bytes("PNG"))

    result = await read_image_file("/photo.jpg")

    assert result._format == "png"
    assert result.to_image_content().mimeType == "image/png"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fixture_path",
    [
        "filesystem/animals/xk92m/qz7fw.png",
        "filesystem/animals/jh21q/pl4nc.png",
        "filesystem/animals/ab47z/rt3ky.png",
        "filesystem/animals/mn83p/jw9vb.png",
    ],
)
async def test_shipped_simple_task_fixtures_are_real_pngs(sandbox, fixture_path):
    """All four shipped fixtures were JPEG bytes named .png (#205); they are
    now re-encoded as genuine PNGs, so each must be typed png."""
    with zipfile.ZipFile(
        REPO_ROOT / "examples/simple_task/original_snapshot.zip"
    ) as zf:
        png_bytes = zf.read(fixture_path)
    assert png_bytes[:8] == b"\x89PNG\r\n\x1a\n"
    filename = Path(fixture_path).name
    (sandbox / filename).write_bytes(png_bytes)

    result = await read_image_file(f"/{filename}")

    assert result._format == "png"
    assert result.to_image_content().mimeType == "image/png"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "pil_format", "expected_format", "expected_mime"),
    [
        ("a.png", "PNG", "png", "image/png"),
        ("a.jpg", "JPEG", "jpeg", "image/jpeg"),
        ("a.jpeg", "JPEG", "jpeg", "image/jpeg"),
        ("a.gif", "GIF", "gif", "image/gif"),
        ("a.webp", "WEBP", "webp", "image/webp"),
    ],
)
async def test_matching_content_and_extension(
    sandbox, filename, pil_format, expected_format, expected_mime
):
    (sandbox / filename).write_bytes(_make_image_bytes(pil_format))

    result = await read_image_file(f"/{filename}")

    assert result._format == expected_format
    assert result.to_image_content().mimeType == expected_mime


@pytest.mark.asyncio
async def test_unknown_bytes_fall_back_to_extension(sandbox):
    """Bytes with no recognizable signature keep the old extension behavior."""
    (sandbox / "mystery.png").write_bytes(b"not an image at all")

    result = await read_image_file("/mystery.png")

    assert result._format == "png"


@pytest.mark.asyncio
async def test_unsupported_extension_still_rejected(sandbox):
    (sandbox / "photo.bmp").write_bytes(_make_image_bytes("BMP"))

    with pytest.raises(ValueError, match="Unsupported image format"):
        await read_image_file("/photo.bmp")


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 8, "png"),
        (b"\xff\xd8\xff\xe0" + b"\x00" * 8, "jpeg"),
        (b"GIF87a" + b"\x00" * 8, "gif"),
        (b"GIF89a" + b"\x00" * 8, "gif"),
        (b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 8, "webp"),
        (b"RIFF\x00\x00\x00\x00AVI " + b"\x00" * 8, None),  # RIFF, not WEBP
        (b"", None),
        (b"hello", None),
    ],
)
def test_sniff_image_format(data, expected):
    assert _sniff_image_format(data) == expected

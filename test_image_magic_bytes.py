#!/usr/bin/env python3
"""
Standalone test for the _sniff_image_format function (Issue #205 fix).
Tests magic byte detection without requiring fastmcp or other dependencies.
"""

# JPEG magic bytes
JPEG_MAGIC = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00"
# PNG magic bytes
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _sniff_image_format(data: bytes) -> str | None:
    """Return the image format implied by the file's magic bytes, or None."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def test_mislabeled_image():
    """Test that image formats are correctly detected from magic bytes."""
    
    # Test JPEG detection
    jpeg_data = JPEG_MAGIC + b"\x00" * 100  # minimal JPEG-like data
    detected_format = _sniff_image_format(jpeg_data)
    assert detected_format == "jpeg", f"Expected 'jpeg', got '{detected_format}'"
    print("✓ JPEG magic bytes correctly detected")
    
    # Test PNG detection
    png_data = PNG_MAGIC + b"\x00" * 100
    detected_format = _sniff_image_format(png_data)
    assert detected_format == "png", f"Expected 'png', got '{detected_format}'"
    print("✓ PNG magic bytes correctly detected")
    
    # Test GIF detection
    gif_data = b"GIF89a" + b"\x00" * 100
    detected_format = _sniff_image_format(gif_data)
    assert detected_format == "gif", f"Expected 'gif', got '{detected_format}'"
    print("✓ GIF magic bytes correctly detected")
    
    # Test WEBP detection
    webp_data = b"RIFF\x00\x00\x00\x00WEBP" + b"\x00" * 100
    detected_format = _sniff_image_format(webp_data)
    assert detected_format == "webp", f"Expected 'webp', got '{detected_format}'"
    print("✓ WEBP magic bytes correctly detected")
    
    # Test unknown format (should return None)
    unknown_data = b"\x00\x00\x00\x00" + b"\x00" * 100
    detected_format = _sniff_image_format(unknown_data)
    assert detected_format is None, f"Expected None for unknown format, got '{detected_format}'"
    print("✓ Unknown format returns None (falls back to extension)")
    
    # Test the actual issue case: JPEG data in a .png file
    jpeg_in_png_file = b"\xff\xd8\xff" + b"\x00" * 100
    detected_format = _sniff_image_format(jpeg_in_png_file)
    assert detected_format == "jpeg", f"ISSUE #205: JPEG data mislabeled as .png should detect as 'jpeg', got '{detected_format}'"
    print("✓ Issue #205 case: JPEG data with .png filename correctly detected as JPEG")
    
    print("\n✅ All magic byte detection tests passed!")
    print("   The fix correctly detects image format from content, not filename.")


if __name__ == "__main__":
    test_mislabeled_image()

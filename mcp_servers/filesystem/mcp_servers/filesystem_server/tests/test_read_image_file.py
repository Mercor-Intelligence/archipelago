"""Comprehensive tests for read_image_file tool.

This module tests all aspects of the read_image_file functionality:
- Input validation
- Path resolution and security
- Image format support (PNG, JPG, JPEG, GIF, WEBP)
- Error handling
- Edge cases
- Security (sandboxing, path traversal prevention)

Test Coverage Areas:
1. Basic Functionality: Reading various image formats
2. Input Validation: Path validation, extension checks
3. Error Handling: File not found, not a file, invalid formats
4. Image Data Verification: Correct format, data integrity
5. Security: Directory traversal prevention, sandboxing
6. Edge Cases: Large images, small images, various dimensions
"""

from pathlib import Path
from unittest.mock import patch

import pytest
from fastmcp.utilities.types import Image
from tools.read_image_file import _resolve_under_root, read_image_file

try:
    from PIL import Image as PILImage
except ImportError:
    PILImage = None

# ============================================================================
# IMAGE GENERATION FIXTURES
# ============================================================================


@pytest.fixture
def sample_png_image(temp_workspace: Path) -> Path:
    """Create a sample PNG image file.

    Returns:
        Path: Path to the created PNG file
    """
    if PILImage is None:
        pytest.skip("PIL/Pillow not available")

    img_path = temp_workspace / "test.png"
    img = PILImage.new("RGB", (100, 100), color="red")
    img.save(img_path, "PNG")
    return img_path


@pytest.fixture
def sample_jpg_image(temp_workspace: Path) -> Path:
    """Create a sample JPG image file.

    Returns:
        Path: Path to the created JPG file
    """
    if PILImage is None:
        pytest.skip("PIL/Pillow not available")

    img_path = temp_workspace / "test.jpg"
    img = PILImage.new("RGB", (100, 100), color="blue")
    img.save(img_path, "JPEG")
    return img_path


@pytest.fixture
def sample_jpeg_image(temp_workspace: Path) -> Path:
    """Create a sample JPEG image file (with .jpeg extension).

    Returns:
        Path: Path to the created JPEG file
    """
    if PILImage is None:
        pytest.skip("PIL/Pillow not available")

    img_path = temp_workspace / "test.jpeg"
    img = PILImage.new("RGB", (100, 100), color="green")
    img.save(img_path, "JPEG")
    return img_path


@pytest.fixture
def sample_gif_image(temp_workspace: Path) -> Path:
    """Create a sample GIF image file.

    Returns:
        Path: Path to the created GIF file
    """
    if PILImage is None:
        pytest.skip("PIL/Pillow not available")

    img_path = temp_workspace / "test.gif"
    img = PILImage.new("RGB", (100, 100), color="yellow")
    img.save(img_path, "GIF")
    return img_path


@pytest.fixture
def sample_webp_image(temp_workspace: Path) -> Path:
    """Create a sample WEBP image file.

    Returns:
        Path: Path to the created WEBP file
    """
    if PILImage is None:
        pytest.skip("PIL/Pillow not available")

    img_path = temp_workspace / "test.webp"
    img = PILImage.new("RGB", (100, 100), color="purple")
    try:
        img.save(img_path, "WEBP")
    except Exception:
        pytest.skip("WEBP support not available in PIL")
    return img_path


@pytest.fixture
def images_directory(temp_workspace: Path) -> Path:
    """Create a directory with various image files.

    Returns:
        Path: Path to the directory containing test images
    """
    if PILImage is None:
        pytest.skip("PIL/Pillow not available")

    img_dir = temp_workspace / "images"
    img_dir.mkdir()

    # Create various image files
    PILImage.new("RGB", (50, 50), "red").save(img_dir / "red.png", "PNG")
    PILImage.new("RGB", (60, 60), "blue").save(img_dir / "blue.jpg", "JPEG")
    PILImage.new("RGB", (70, 70), "green").save(img_dir / "green.jpeg", "JPEG")
    PILImage.new("RGB", (80, 80), "yellow").save(img_dir / "yellow.gif", "GIF")

    return img_dir


@pytest.fixture
def large_image(temp_workspace: Path) -> Path:
    """Create a large image file for performance testing.

    Returns:
        Path: Path to the large image
    """
    if PILImage is None:
        pytest.skip("PIL/Pillow not available")

    img_path = temp_workspace / "large.png"
    # Create a 2000x2000 image
    img = PILImage.new("RGB", (2000, 2000), color="cyan")
    img.save(img_path, "PNG")
    return img_path


@pytest.fixture
def tiny_image(temp_workspace: Path) -> Path:
    """Create a very small image (1x1 pixel).

    Returns:
        Path: Path to the tiny image
    """
    if PILImage is None:
        pytest.skip("PIL/Pillow not available")

    img_path = temp_workspace / "tiny.png"
    img = PILImage.new("RGB", (1, 1), color="black")
    img.save(img_path, "PNG")
    return img_path


# ============================================================================
# BASIC FUNCTIONALITY TESTS
# ============================================================================


class TestReadImageFileBasic:
    """Test core functionality with standard use cases."""

    @pytest.mark.asyncio
    async def test_read_png_image(self, sample_png_image: Path, temp_workspace: Path):
        """Test reading a PNG image file."""
        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            result = await read_image_file("/test.png")

            assert isinstance(result, Image)
            assert result._format == "png"
            assert result.data is not None
            assert len(result.data) > 0

    @pytest.mark.asyncio
    async def test_read_jpg_image(self, sample_jpg_image: Path, temp_workspace: Path):
        """Test reading a JPG image file."""
        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            result = await read_image_file("/test.jpg")

            assert isinstance(result, Image)
            assert result._format == "jpeg"
            assert result.data is not None
            assert len(result.data) > 0

    @pytest.mark.asyncio
    async def test_read_jpeg_image(self, sample_jpeg_image: Path, temp_workspace: Path):
        """Test reading a JPEG image file (with .jpeg extension)."""
        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            result = await read_image_file("/test.jpeg")

            assert isinstance(result, Image)
            assert result._format == "jpeg"
            assert result.data is not None
            assert len(result.data) > 0

    @pytest.mark.asyncio
    async def test_read_gif_image(self, sample_gif_image: Path, temp_workspace: Path):
        """Test reading a GIF image file."""
        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            result = await read_image_file("/test.gif")

            assert isinstance(result, Image)
            assert result._format == "gif"
            assert result.data is not None
            assert len(result.data) > 0

    @pytest.mark.asyncio
    async def test_read_webp_image(self, sample_webp_image: Path, temp_workspace: Path):
        """Test reading a WEBP image file."""
        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            result = await read_image_file("/test.webp")

            assert isinstance(result, Image)
            assert result._format == "webp"
            assert result.data is not None
            assert len(result.data) > 0

    @pytest.mark.asyncio
    async def test_read_image_from_subdirectory(
        self, images_directory: Path, temp_workspace: Path
    ):
        """Test reading image from subdirectory."""
        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            result = await read_image_file("/images/red.png")

            assert isinstance(result, Image)
            assert result._format == "png"
            assert result.data is not None
            assert len(result.data) > 0

    @pytest.mark.asyncio
    async def test_read_multiple_images_sequentially(
        self, images_directory: Path, temp_workspace: Path
    ):
        """Test reading multiple images one after another."""
        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            # Read PNG
            result1 = await read_image_file("/images/red.png")
            assert isinstance(result1, Image)
            assert result1._format == "png"

            # Read JPG
            result2 = await read_image_file("/images/blue.jpg")
            assert isinstance(result2, Image)
            assert result2._format == "jpeg"

            # Read GIF
            result3 = await read_image_file("/images/yellow.gif")
            assert isinstance(result3, Image)
            assert result3._format == "gif"


# ============================================================================
# INPUT VALIDATION TESTS
# ============================================================================


class TestReadImageFileValidation:
    """Test input validation and parameter checking."""

    @pytest.mark.asyncio
    async def test_file_path_required(self, temp_workspace: Path):
        """Test that file_path is required."""
        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            with pytest.raises(ValueError, match="File path is required"):
                await read_image_file("")

    @pytest.mark.asyncio
    async def test_file_path_must_be_string(self, temp_workspace: Path):
        """Test that file_path must be a string."""
        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            with pytest.raises(ValueError, match="File path is required"):
                await read_image_file(None)  # type: ignore

    @pytest.mark.asyncio
    async def test_file_path_must_start_with_slash(
        self, sample_png_image: Path, temp_workspace: Path
    ):
        """Test that file_path must start with '/'."""
        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            with pytest.raises(ValueError, match="File path must start with /"):
                await read_image_file("test.png")

    @pytest.mark.asyncio
    async def test_unsupported_image_format_bmp(self, temp_workspace: Path):
        """Test that unsupported formats (BMP) raise error."""
        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            with pytest.raises(ValueError, match="Unsupported image format: bmp"):
                await read_image_file("/test.bmp")

    @pytest.mark.asyncio
    async def test_unsupported_image_format_tiff(self, temp_workspace: Path):
        """Test that unsupported formats (TIFF) raise error."""
        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            with pytest.raises(ValueError, match="Unsupported image format: tiff"):
                await read_image_file("/test.tiff")

    @pytest.mark.asyncio
    async def test_unsupported_image_format_svg(self, temp_workspace: Path):
        """Test that unsupported formats (SVG) raise error."""
        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            with pytest.raises(ValueError, match="Unsupported image format: svg"):
                await read_image_file("/test.svg")

    @pytest.mark.asyncio
    async def test_non_image_file_format(self, temp_workspace: Path):
        """Test that non-image files raise error."""
        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            with pytest.raises(ValueError, match="Unsupported image format"):
                await read_image_file("/document.pdf")

    @pytest.mark.asyncio
    async def test_no_extension(self, temp_workspace: Path):
        """Test file without extension raises error."""
        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            with pytest.raises(ValueError, match="Unsupported image format"):
                await read_image_file("/image")

    @pytest.mark.asyncio
    async def test_uppercase_extension_png(self, temp_workspace: Path):
        """Test that uppercase extensions are handled correctly."""
        if PILImage is None:
            pytest.skip("PIL/Pillow not available")

        # Create image with uppercase extension
        img_path = temp_workspace / "TEST.PNG"
        PILImage.new("RGB", (50, 50), "red").save(img_path, "PNG")

        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            result = await read_image_file("/TEST.PNG")

            assert isinstance(result, Image)
            assert result._format == "png"

    @pytest.mark.asyncio
    async def test_mixed_case_extension(self, temp_workspace: Path):
        """Test mixed case extension (e.g., .JpG)."""
        if PILImage is None:
            pytest.skip("PIL/Pillow not available")

        img_path = temp_workspace / "test.JpG"
        PILImage.new("RGB", (50, 50), "blue").save(img_path, "JPEG")

        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            result = await read_image_file("/test.JpG")

            assert isinstance(result, Image)
            assert result._format == "jpeg"


# ============================================================================
# ERROR HANDLING TESTS
# ============================================================================


class TestReadImageFileErrors:
    """Test error scenarios and recovery."""

    @pytest.mark.asyncio
    async def test_file_not_found(self, temp_workspace: Path):
        """Test error when file doesn't exist."""
        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            with pytest.raises(
                FileNotFoundError, match="File not found: /nonexistent.png"
            ):
                await read_image_file("/nonexistent.png")

    @pytest.mark.asyncio
    async def test_path_is_directory_not_file(self, temp_workspace: Path):
        """Test error when path points to a directory."""
        img_dir = temp_workspace / "images.png"
        img_dir.mkdir()

        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            with pytest.raises(ValueError, match="Not a file: /images.png"):
                await read_image_file("/images.png")

    @pytest.mark.asyncio
    async def test_corrupted_image_file(self, temp_workspace: Path):
        """Test handling of corrupted image file."""
        # Create a file with .png extension but invalid content
        corrupted = temp_workspace / "corrupted.png"
        corrupted.write_text("This is not a valid PNG file")

        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            # Should read the file bytes even if corrupted
            # The tool just reads raw bytes, validation happens elsewhere
            result = await read_image_file("/corrupted.png")

            assert isinstance(result, Image)
            assert result._format == "png"
            # Data should still be there, just not valid PNG
            assert result.data is not None
            assert len(result.data) > 0

    @pytest.mark.asyncio
    async def test_empty_image_file(self, temp_workspace: Path):
        """Test handling of empty image file."""
        empty_img = temp_workspace / "empty.png"
        empty_img.write_bytes(b"")

        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            result = await read_image_file("/empty.png")

            # Should return empty data
            assert isinstance(result, Image)
            assert result.data is not None
            assert len(result.data) == 0

    @pytest.mark.asyncio
    async def test_nested_path_not_found(self, temp_workspace: Path):
        """Test error for nested path that doesn't exist."""
        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            with pytest.raises(FileNotFoundError):
                await read_image_file("/folder/subfolder/image.png")

    @pytest.mark.asyncio
    async def test_read_exception_wrapped(self, temp_workspace: Path):
        """Test that exceptions during read are wrapped in RuntimeError."""
        if PILImage is None:
            pytest.skip("PIL/Pillow not available")

        # Create a valid image
        img_path = temp_workspace / "test_exc.png"
        PILImage.new("RGB", (50, 50), "red").save(img_path, "PNG")

        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            # Mock open to raise an exception during read
            import builtins

            original_open = builtins.open

            def mock_open_raise(*args, **kwargs):
                if "test_exc.png" in str(args[0]):
                    raise OSError("Simulated read error")
                return original_open(*args, **kwargs)

            with patch("builtins.open", mock_open_raise):
                with pytest.raises(RuntimeError, match="Failed to read image file"):
                    await read_image_file("/test_exc.png")


# ============================================================================
# IMAGE DATA VERIFICATION TESTS
# ============================================================================


class TestReadImageFileDataIntegrity:
    """Test that image data is read correctly."""

    @pytest.mark.asyncio
    async def test_image_data_not_empty(
        self, sample_png_image: Path, temp_workspace: Path
    ):
        """Test that image data is not empty."""
        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            result = await read_image_file("/test.png")

            assert result.data is not None
            assert len(result.data) > 0

    @pytest.mark.asyncio
    async def test_image_data_matches_file_size(
        self, sample_png_image: Path, temp_workspace: Path
    ):
        """Test that read data matches file size."""
        file_size = sample_png_image.stat().st_size

        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            result = await read_image_file("/test.png")

            assert result.data is not None
            assert len(result.data) == file_size

    @pytest.mark.asyncio
    async def test_image_format_correct_for_png(
        self, sample_png_image: Path, temp_workspace: Path
    ):
        """Test that PNG format is correctly identified."""
        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            result = await read_image_file("/test.png")

            assert result._format == "png"

    @pytest.mark.asyncio
    async def test_image_format_correct_for_jpeg(
        self, sample_jpg_image: Path, temp_workspace: Path
    ):
        """Test that JPEG format is correctly identified."""
        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            result = await read_image_file("/test.jpg")

            assert result._format == "jpeg"

    @pytest.mark.asyncio
    async def test_reading_same_image_twice_consistent(
        self, sample_png_image: Path, temp_workspace: Path
    ):
        """Test that reading same image twice returns consistent data."""
        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            result1 = await read_image_file("/test.png")
            result2 = await read_image_file("/test.png")

            assert result1.data == result2.data
            assert result1._format == result2._format


# ============================================================================
# EDGE CASES TESTS
# ============================================================================


class TestReadImageFileEdgeCases:
    """Test boundary conditions and edge cases."""

    @pytest.mark.asyncio
    async def test_large_image_file(self, large_image: Path, temp_workspace: Path):
        """Test reading a large image file."""
        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            result = await read_image_file("/large.png")

            assert isinstance(result, Image)
            assert result._format == "png"
            # Large image should have substantial data
            assert result.data is not None
            assert len(result.data) > 10000

    @pytest.mark.asyncio
    async def test_tiny_image_file(self, tiny_image: Path, temp_workspace: Path):
        """Test reading a 1x1 pixel image."""
        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            result = await read_image_file("/tiny.png")

            assert isinstance(result, Image)
            assert result._format == "png"
            assert result.data is not None
            assert len(result.data) > 0

    @pytest.mark.asyncio
    async def test_image_with_unicode_filename(self, temp_workspace: Path):
        """Test reading image with Unicode filename."""
        if PILImage is None:
            pytest.skip("PIL/Pillow not available")

        unicode_name = temp_workspace / "图片.png"
        PILImage.new("RGB", (50, 50), "red").save(unicode_name, "PNG")

        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            result = await read_image_file("/图片.png")

            assert isinstance(result, Image)
            assert result._format == "png"

    @pytest.mark.asyncio
    async def test_image_with_spaces_in_filename(self, temp_workspace: Path):
        """Test reading image with spaces in filename."""
        if PILImage is None:
            pytest.skip("PIL/Pillow not available")

        img_with_spaces = temp_workspace / "my image file.png"
        PILImage.new("RGB", (50, 50), "blue").save(img_with_spaces, "PNG")

        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            result = await read_image_file("/my image file.png")

            assert isinstance(result, Image)
            assert result._format == "png"

    @pytest.mark.asyncio
    async def test_image_with_special_chars_filename(self, temp_workspace: Path):
        """Test reading image with special characters in filename."""
        if PILImage is None:
            pytest.skip("PIL/Pillow not available")

        special_name = temp_workspace / "image(test)-[1].png"
        PILImage.new("RGB", (50, 50), "green").save(special_name, "PNG")

        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            result = await read_image_file("/image(test)-[1].png")

            assert isinstance(result, Image)
            assert result._format == "png"

    @pytest.mark.asyncio
    async def test_deeply_nested_image(self, temp_workspace: Path):
        """Test reading image from deeply nested directory."""
        if PILImage is None:
            pytest.skip("PIL/Pillow not available")

        # Create deeply nested structure
        nested_dir = temp_workspace / "level1" / "level2" / "level3" / "level4"
        nested_dir.mkdir(parents=True)
        img_path = nested_dir / "deep.png"
        PILImage.new("RGB", (50, 50), "red").save(img_path, "PNG")

        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            result = await read_image_file("/level1/level2/level3/level4/deep.png")

            assert isinstance(result, Image)
            assert result._format == "png"


# ============================================================================
# SECURITY TESTS
# ============================================================================


class TestReadImageFileSecurity:
    """Test security-related functionality."""

    @pytest.mark.asyncio
    async def test_cannot_escape_sandbox_with_traversal(self, temp_workspace: Path):
        """Test that directory traversal attempts stay in sandbox."""
        if PILImage is None:
            pytest.skip("PIL/Pillow not available")

        # Create an image in sandbox
        img_path = temp_workspace / "safe.png"
        PILImage.new("RGB", (50, 50), "red").save(img_path, "PNG")

        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            # Try to traverse up - should resolve within sandbox
            with pytest.raises(FileNotFoundError):
                await read_image_file("/../../../etc/passwd.png")

    @pytest.mark.asyncio
    async def test_parent_directory_resolution(self, temp_workspace: Path):
        """Test that parent directory references are normalized."""
        if PILImage is None:
            pytest.skip("PIL/Pillow not available")

        # Create directory structure
        dir1 = temp_workspace / "dir1"
        dir2 = temp_workspace / "dir2"
        dir1.mkdir()
        dir2.mkdir()

        img_path = dir2 / "image.png"
        PILImage.new("RGB", (50, 50), "blue").save(img_path, "PNG")

        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            # Access via parent traversal: /dir1/../dir2/image.png
            result = await read_image_file("/dir1/../dir2/image.png")

            assert isinstance(result, Image)
            assert result._format == "png"

    @pytest.mark.asyncio
    async def test_absolute_path_stays_in_sandbox(self, temp_workspace: Path):
        """Test that absolute paths are resolved relative to sandbox."""
        if PILImage is None:
            pytest.skip("PIL/Pillow not available")

        img_path = temp_workspace / "test.png"
        PILImage.new("RGB", (50, 50), "red").save(img_path, "PNG")

        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            # Absolute path should resolve to FS_ROOT/test.png
            result = await read_image_file("/test.png")

            assert isinstance(result, Image)
            assert result._format == "png"

    def test_resolve_under_root_function(self, temp_workspace: Path):
        """Test the _resolve_under_root helper function."""
        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            # Regular path
            result = _resolve_under_root("/images/test.png")
            assert result.startswith(str(temp_workspace))
            assert "images" in result
            assert "test.png" in result

            # Traversal should stay in root
            result = _resolve_under_root("/../../etc/image.png")
            assert result.startswith(str(temp_workspace))

    def test_resolve_under_root_empty_path(self, temp_workspace: Path):
        """Test that empty path returns FS_ROOT."""
        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            result = _resolve_under_root("")
            assert result == str(temp_workspace)

    def test_resolve_under_root_root_path(self, temp_workspace: Path):
        """Test that '/' returns FS_ROOT."""
        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            result = _resolve_under_root("/")
            assert result == str(temp_workspace)


# ============================================================================
# CONCURRENCY TESTS
# ============================================================================


class TestReadImageFileConcurrency:
    """Test concurrent access scenarios."""

    @pytest.mark.asyncio
    async def test_multiple_concurrent_reads_same_image(
        self, sample_png_image: Path, temp_workspace: Path
    ):
        """Test multiple simultaneous reads of the same image."""
        import asyncio

        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            # Run 5 concurrent reads
            tasks = [read_image_file("/test.png") for _ in range(5)]
            results = await asyncio.gather(*tasks)

            # All should succeed with same data
            assert len(results) == 5
            for result in results:
                assert isinstance(result, Image)
                assert result._format == "png"
                assert result.data == results[0].data

    @pytest.mark.asyncio
    async def test_multiple_concurrent_reads_different_images(
        self, images_directory: Path, temp_workspace: Path
    ):
        """Test concurrent reads of different images."""
        import asyncio

        with patch("tools.read_image_file.FS_ROOT", str(temp_workspace)):
            # Read different images concurrently
            tasks = [
                read_image_file("/images/red.png"),
                read_image_file("/images/blue.jpg"),
                read_image_file("/images/yellow.gif"),
            ]
            results = await asyncio.gather(*tasks)

            # All should succeed
            assert len(results) == 3
            assert results[0]._format == "png"
            assert results[1]._format == "jpeg"
            assert results[2]._format == "gif"

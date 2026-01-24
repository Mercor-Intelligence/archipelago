"""Unit tests for modify_image tool."""

import sys
from pathlib import Path

import pytest
from PIL import Image as PILImage

# Add parent directory to path
sys.path.insert(
    0, str(Path(__file__).parent.parent.parent / "mcp_servers" / "docs_server")
)

from tools.add_image import add_image
from tools.create_document import create_document
from tools.modify_image import modify_image

from .helpers.assertions import (
    assert_document_created,
    assert_error,
    assert_success,
)


@pytest.fixture
def test_image_large(mock_docs_root):
    """Create a larger test image file within mock_docs_root.

    Returns:
        str: Path to the test image (as docx path)
    """
    # Create image in mock_docs_root
    img_path = Path(mock_docs_root) / "test_image_large.png"
    # Create a 200x200 blue image
    img = PILImage.new("RGB", (200, 200), color="blue")
    img.save(str(img_path))
    return "/test_image_large.png"  # Return as docx-style path


class TestModifyImage:
    """Test image modification functionality."""

    @pytest.mark.asyncio
    async def test_rotate_image(
        self, mock_docs_root, sample_basic_content, test_image_large
    ):
        """Test rotating an image."""
        # Create document and add image
        create_result = await create_document(
            directory="/", file_name="rotate_test.docx", content=sample_basic_content
        )
        assert_document_created(create_result, "rotate_test.docx")

        add_result = await add_image("/rotate_test.docx", test_image_large, "body.p.1")
        assert_success(add_result, "added")

        # Rotate image 90 degrees
        result = await modify_image(
            file_path="/rotate_test.docx",
            image_index=0,
            operation="rotate",
            rotation=90,
        )
        assert_success(result, "rotated")

    @pytest.mark.asyncio
    async def test_flip_image_horizontal(
        self, mock_docs_root, sample_basic_content, test_image_large
    ):
        """Test flipping an image horizontally."""
        # Create document and add image
        create_result = await create_document(
            directory="/", file_name="flip_h_test.docx", content=sample_basic_content
        )
        assert_document_created(create_result, "flip_h_test.docx")

        add_result = await add_image("/flip_h_test.docx", test_image_large, "body.p.1")
        assert_success(add_result, "added")

        # Flip horizontally
        result = await modify_image(
            file_path="/flip_h_test.docx",
            image_index=0,
            operation="flip",
            flip="horizontal",
        )
        assert_success(result, "flipped")

    @pytest.mark.asyncio
    async def test_flip_image_vertical(
        self, mock_docs_root, sample_basic_content, test_image_large
    ):
        """Test flipping an image vertically."""
        # Create document and add image
        create_result = await create_document(
            directory="/", file_name="flip_v_test.docx", content=sample_basic_content
        )
        assert_document_created(create_result, "flip_v_test.docx")

        add_result = await add_image("/flip_v_test.docx", test_image_large, "body.p.1")
        assert_success(add_result, "added")

        # Flip vertically
        result = await modify_image(
            file_path="/flip_v_test.docx",
            image_index=0,
            operation="flip",
            flip="vertical",
        )
        assert_success(result, "flipped")

    @pytest.mark.asyncio
    async def test_adjust_brightness(
        self, mock_docs_root, sample_basic_content, test_image_large
    ):
        """Test adjusting image brightness."""
        # Create document and add image
        create_result = await create_document(
            directory="/",
            file_name="brightness_test.docx",
            content=sample_basic_content,
        )
        assert_document_created(create_result, "brightness_test.docx")

        add_result = await add_image(
            "/brightness_test.docx", test_image_large, "body.p.1"
        )
        assert_success(add_result, "added")

        # Increase brightness
        result = await modify_image(
            file_path="/brightness_test.docx",
            image_index=0,
            operation="brightness",
            brightness=1.5,
        )
        assert_success(result, "brightness")

    @pytest.mark.asyncio
    async def test_adjust_contrast(
        self, mock_docs_root, sample_basic_content, test_image_large
    ):
        """Test adjusting image contrast."""
        # Create document and add image
        create_result = await create_document(
            directory="/", file_name="contrast_test.docx", content=sample_basic_content
        )
        assert_document_created(create_result, "contrast_test.docx")

        add_result = await add_image(
            "/contrast_test.docx", test_image_large, "body.p.1"
        )
        assert_success(add_result, "added")

        # Increase contrast
        result = await modify_image(
            file_path="/contrast_test.docx",
            image_index=0,
            operation="contrast",
            contrast=1.3,
        )
        assert_success(result, "contrast")

    @pytest.mark.asyncio
    async def test_resize_image(
        self, mock_docs_root, sample_basic_content, test_image_large
    ):
        """Test resizing an image."""
        # Create document and add image
        create_result = await create_document(
            directory="/", file_name="resize_test.docx", content=sample_basic_content
        )
        assert_document_created(create_result, "resize_test.docx")

        add_result = await add_image("/resize_test.docx", test_image_large, "body.p.1")
        assert_success(add_result, "added")

        # Resize image (test with rotation operation)
        result = await modify_image(
            file_path="/resize_test.docx",
            image_index=0,
            operation="rotate",
            rotation=0,  # No rotation, just testing it works
        )
        assert_success(result, "rotated")

    @pytest.mark.asyncio
    async def test_modify_nonexistent_document(self, mock_docs_root):
        """Test modifying image in non-existent document."""
        result = await modify_image(
            file_path="/nonexistent.docx",
            image_index=0,
            operation="rotate",
            rotation=90,
        )
        assert_error(result, "not found")

    @pytest.mark.asyncio
    async def test_modify_invalid_path(self, mock_docs_root):
        """Test modifying with invalid document path."""
        result = await modify_image(
            file_path="no_slash.docx",
            image_index=0,
            operation="rotate",
            rotation=90,
        )
        assert_error(result, "must start with")

    @pytest.mark.asyncio
    async def test_modify_invalid_image_index(
        self, mock_docs_root, sample_basic_content, test_image_large
    ):
        """Test modifying with invalid image index."""
        # Create document and add image
        create_result = await create_document(
            directory="/", file_name="invalid_idx.docx", content=sample_basic_content
        )
        assert_document_created(create_result, "invalid_idx.docx")

        add_result = await add_image("/invalid_idx.docx", test_image_large, "body.p.1")
        assert_success(add_result, "added")

        # Try to modify non-existent image (index 5)
        result = await modify_image(
            file_path="/invalid_idx.docx",
            image_index=5,
            operation="rotate",
            rotation=90,
        )
        assert_error(result)

    @pytest.mark.asyncio
    async def test_modify_document_with_no_images(
        self, mock_docs_root, sample_document_basic
    ):
        """Test modifying document that has no images."""
        file_path, _ = sample_document_basic
        result = await modify_image(
            file_path=file_path,
            image_index=0,
            operation="rotate",
            rotation=90,
        )
        assert_error(result)

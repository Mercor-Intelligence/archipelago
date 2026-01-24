"""Unit tests for read_image tool."""

import shutil
import sys
from pathlib import Path

import pytest

# Add parent directory to path
sys.path.insert(
    0, str(Path(__file__).parent.parent.parent / "mcp_servers" / "docs_server")
)

from tools.read_document_content import read_document_content
from tools.read_image import read_image


class TestReadImage:
    """Test image reading functionality."""

    @pytest.fixture
    def sample_10k_with_images(self, mock_docs_root):
        """Copy the real sample document to temp directory.

        Returns:
            str: file_path
        """
        source = Path(__file__).parent / "Sun Country FY22 10-K.docx"
        dest = Path(mock_docs_root) / "sample_with_images.docx"

        if source.exists():
            shutil.copy(source, dest)
            return "/sample_with_images.docx"
        else:
            pytest.skip("Sample document not found")

    @pytest.mark.asyncio
    async def test_read_image_after_document_read(
        self, mock_docs_root, sample_10k_with_images
    ):
        """Test reading image after reading document content."""
        # First read document to populate cache
        doc_content = await read_document_content(sample_10k_with_images)

        # Document has 6 images according to metadata
        import re

        annotations = re.findall(r"@([a-z0-9_]+)", doc_content)

        # Should have images - fail if it doesn't
        assert len(annotations) > 0, "Document should contain image annotations"

        annotation = annotations[0]

        # Read the image
        result = await read_image(sample_10k_with_images, annotation)

        # Verify it's an Image object
        assert result is not None
        assert hasattr(result, "data")
        # Verify data is not empty
        assert result.data is not None
        assert len(result.data) > 0

    @pytest.mark.asyncio
    async def test_read_image_without_document_read(self, mock_docs_root):
        """Test reading image without first reading document (should fail)."""
        with pytest.raises(ValueError, match="Image not found in cache"):
            await read_image("/test.docx", "body_p_0_r0")

    @pytest.mark.asyncio
    async def test_read_image_invalid_annotation(
        self, mock_docs_root, sample_10k_with_images
    ):
        """Test reading image with invalid annotation."""
        # Read document first
        _ = await read_document_content(sample_10k_with_images)

        # Try invalid annotation
        with pytest.raises(ValueError, match="Image not found in cache"):
            await read_image(sample_10k_with_images, "invalid_annotation")

    @pytest.mark.asyncio
    async def test_read_image_empty_file_path(self, mock_docs_root):
        """Test reading image with empty file path."""
        with pytest.raises(ValueError, match="File path is required"):
            await read_image("", "body_p_0_r0")

    @pytest.mark.asyncio
    async def test_read_image_empty_annotation(self, mock_docs_root):
        """Test reading image with empty annotation."""
        with pytest.raises(ValueError, match="Annotation is required"):
            await read_image("/test.docx", "")

    @pytest.mark.asyncio
    async def test_read_multiple_images(self, mock_docs_root, sample_10k_with_images):
        """Test reading multiple images from same document."""
        # Read document first
        doc_content = await read_document_content(sample_10k_with_images)

        # Extract all annotations
        import re

        annotations = re.findall(r"@([a-z0-9_]+)", doc_content)

        # Should have at least 2 images - fail if it doesn't
        assert len(annotations) >= 2, (
            f"Document should have at least 2 images, found {len(annotations)}"
        )

        # Read first image
        image1 = await read_image(sample_10k_with_images, annotations[0])
        assert image1 is not None

        # Read second image
        image2 = await read_image(sample_10k_with_images, annotations[1])
        assert image2 is not None

        # Both should be valid images
        assert hasattr(image1, "data")
        assert hasattr(image2, "data")

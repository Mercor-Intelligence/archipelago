"""Unit tests for add_image tool."""

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
from tools.read_document_content import read_document_content

from .helpers.assertions import (
    assert_document_created,
    assert_error,
    assert_success,
)


@pytest.fixture
def test_image(mock_docs_root):
    """Create a test image file within mock_docs_root.

    Returns:
        str: Path to the test image (as docx path)
    """
    # Create image in mock_docs_root
    img_path = Path(mock_docs_root) / "test_image.png"
    # Create a simple 100x100 red image
    img = PILImage.new("RGB", (100, 100), color="red")
    img.save(str(img_path))
    return "/test_image.png"  # Return as docx-style path


@pytest.fixture
def test_jpeg_image(mock_docs_root):
    """Create a JPEG test image."""
    img_path = Path(mock_docs_root) / "test_image.jpg"
    img = PILImage.new("RGB", (100, 100), color="blue")
    img.save(str(img_path), "JPEG")
    return "/test_image.jpg"


class TestAddImage:
    """Test image addition functionality."""

    @pytest.mark.asyncio
    async def test_add_image_to_paragraph(
        self, mock_docs_root, sample_basic_content, test_image
    ):
        """Test adding image to a paragraph."""
        create_result = await create_document(
            directory="/", file_name="image_test.docx", content=sample_basic_content
        )
        assert_document_created(create_result, "image_test.docx")

        result = await add_image(
            file_path="/image_test.docx",
            image_path=test_image,
            identifier="body.p.1",
            position="end",
        )
        assert_success(result, "added")

    @pytest.mark.asyncio
    async def test_add_image_to_run(
        self, mock_docs_root, sample_basic_content, test_image
    ):
        """Test adding image targeting a specific run."""
        create_result = await create_document(
            directory="/", file_name="image_run.docx", content=sample_basic_content
        )
        assert_document_created(create_result, "image_run.docx")

        # Add image at a run
        result = await add_image(
            file_path="/image_run.docx",
            image_path=test_image,
            identifier="body.p.1.r.0",
            position="end",
        )
        assert_success(result, "added")

    @pytest.mark.asyncio
    async def test_add_image_with_both_dimensions(
        self, mock_docs_root, sample_basic_content, test_image
    ):
        """Test adding image with both width and height."""
        create_result = await create_document(
            directory="/", file_name="sized_image.docx", content=sample_basic_content
        )
        assert_document_created(create_result, "sized_image.docx")

        result = await add_image(
            file_path="/sized_image.docx",
            image_path=test_image,
            identifier="body.p.1",
            position="end",
            width=3.0,
            height=2.0,
        )
        assert_success(result, "added")

    @pytest.mark.asyncio
    async def test_add_image_with_width_only(
        self, mock_docs_root, sample_basic_content, test_image
    ):
        """Test adding image with width only."""
        create_result = await create_document(
            directory="/", file_name="width_image.docx", content=sample_basic_content
        )
        assert_document_created(create_result, "width_image.docx")

        result = await add_image(
            file_path="/width_image.docx",
            image_path=test_image,
            identifier="body.p.1",
            position="end",
            width=4.0,
        )
        assert_success(result, "added")

    @pytest.mark.asyncio
    async def test_add_image_with_height_only(
        self, mock_docs_root, sample_basic_content, test_image
    ):
        """Test adding image with height only."""
        create_result = await create_document(
            directory="/", file_name="height_image.docx", content=sample_basic_content
        )
        assert_document_created(create_result, "height_image.docx")

        result = await add_image(
            file_path="/height_image.docx",
            image_path=test_image,
            identifier="body.p.1",
            position="end",
            height=3.0,
        )
        assert_success(result, "added")

    @pytest.mark.asyncio
    async def test_add_image_at_start(
        self, mock_docs_root, sample_basic_content, test_image
    ):
        """Test adding image at start of paragraph."""
        create_result = await create_document(
            directory="/", file_name="start_image.docx", content=sample_basic_content
        )
        assert_document_created(create_result, "start_image.docx")

        result = await add_image(
            file_path="/start_image.docx",
            image_path=test_image,
            identifier="body.p.1",
            position="start",
        )
        assert_success(result, "added")

    @pytest.mark.asyncio
    async def test_add_jpeg_image(
        self, mock_docs_root, sample_basic_content, test_jpeg_image
    ):
        """Test adding JPEG image format."""
        create_result = await create_document(
            directory="/", file_name="jpeg_image.docx", content=sample_basic_content
        )
        assert_document_created(create_result, "jpeg_image.docx")

        result = await add_image(
            file_path="/jpeg_image.docx",
            image_path=test_jpeg_image,
            identifier="body.p.1",
            position="end",
        )
        assert_success(result, "added")

    @pytest.mark.asyncio
    async def test_add_image_empty_file_path(self, mock_docs_root, test_image):
        """Test adding image with empty file path."""
        result = await add_image(
            file_path="",
            image_path=test_image,
            identifier="body.p.0",
        )
        assert_error(result, "required")

    @pytest.mark.asyncio
    async def test_add_image_wrong_extension(self, mock_docs_root, test_image):
        """Test adding image with wrong file extension."""
        result = await add_image(
            file_path="/file.txt",
            image_path=test_image,
            identifier="body.p.0",
        )
        assert_error(result, "must end with")

    @pytest.mark.asyncio
    async def test_add_image_empty_image_path(
        self, mock_docs_root, sample_document_basic
    ):
        """Test adding image with empty image path."""
        file_path, _ = sample_document_basic
        result = await add_image(
            file_path=file_path,
            image_path="",
            identifier="body.p.0",
        )
        assert_error(result, "required")

    @pytest.mark.asyncio
    async def test_add_image_invalid_image_path(
        self, mock_docs_root, sample_document_basic
    ):
        """Test adding image with invalid image path (no leading /)."""
        file_path, _ = sample_document_basic
        result = await add_image(
            file_path=file_path,
            image_path="no_slash.png",
            identifier="body.p.0",
        )
        assert_error(result, "must start with")

    @pytest.mark.asyncio
    async def test_add_image_unsupported_format(
        self, mock_docs_root, sample_document_basic
    ):
        """Test adding image with unsupported format."""
        file_path, _ = sample_document_basic
        result = await add_image(
            file_path=file_path,
            image_path="/image.gif",
            identifier="body.p.0",
        )
        assert_error(result, "Unsupported")

    @pytest.mark.asyncio
    async def test_add_image_invalid_position(
        self, mock_docs_root, sample_document_basic, test_image
    ):
        """Test adding image with invalid position."""
        file_path, _ = sample_document_basic
        result = await add_image(
            file_path=file_path,
            image_path=test_image,
            identifier="body.p.0",
            position="middle",
        )
        assert_error(result, "Position must be")

    @pytest.mark.asyncio
    async def test_add_image_nonexistent_document(self, mock_docs_root, test_image):
        """Test adding image to non-existent document."""
        result = await add_image(
            file_path="/nonexistent.docx",
            image_path=test_image,
            identifier="body.p.0",
        )
        assert_error(result, "not found")

    @pytest.mark.asyncio
    async def test_add_image_invalid_path(
        self, mock_docs_root, sample_document_basic, test_image
    ):
        """Test adding image with invalid document path."""
        result = await add_image(
            file_path="no_slash.docx",
            image_path=test_image,
            identifier="body.p.0",
        )
        assert_error(result, "must start with")

    @pytest.mark.asyncio
    async def test_add_image_nonexistent_image(
        self, mock_docs_root, sample_document_basic
    ):
        """Test adding non-existent image file."""
        file_path, _ = sample_document_basic
        result = await add_image(
            file_path=file_path,
            image_path="/nonexistent.png",
            identifier="body.p.0",
        )
        assert_error(result)

    @pytest.mark.asyncio
    async def test_add_image_invalid_identifier(
        self, mock_docs_root, sample_document_basic, test_image
    ):
        """Test adding image with invalid identifier."""
        file_path, _ = sample_document_basic
        result = await add_image(
            file_path=file_path,
            image_path=test_image,
            identifier="invalid.id",
        )
        assert_error(result)

    @pytest.mark.asyncio
    async def test_add_image_empty_identifier(
        self, mock_docs_root, sample_document_basic, test_image
    ):
        """Test adding image with empty identifier."""
        file_path, _ = sample_document_basic
        result = await add_image(
            file_path=file_path,
            image_path=test_image,
            identifier="",
        )
        assert_error(result, "identifier")

    @pytest.mark.asyncio
    async def test_add_multiple_images(
        self, mock_docs_root, sample_basic_content, test_image
    ):
        """Test adding multiple images to same document."""
        create_result = await create_document(
            directory="/", file_name="multi_image.docx", content=sample_basic_content
        )
        assert_document_created(create_result, "multi_image.docx")

        result1 = await add_image(
            file_path="/multi_image.docx",
            image_path=test_image,
            identifier="body.p.0",
        )
        assert_success(result1, "added")

        result2 = await add_image(
            file_path="/multi_image.docx",
            image_path=test_image,
            identifier="body.p.1",
        )
        assert_success(result2, "added")

    @pytest.mark.asyncio
    async def test_verify_image_in_document(
        self, mock_docs_root, sample_basic_content, test_image
    ):
        """Test that added image appears in document content."""
        create_result = await create_document(
            directory="/", file_name="verify_image.docx", content=sample_basic_content
        )
        assert_document_created(create_result, "verify_image.docx")

        add_result = await add_image(
            file_path="/verify_image.docx",
            image_path=test_image,
            identifier="body.p.1",
        )
        assert_success(add_result, "added")

        read_result = await read_document_content("/verify_image.docx")
        assert "@" in read_result, "Expected image annotation in document"

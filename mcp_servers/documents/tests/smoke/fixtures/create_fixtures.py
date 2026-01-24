#!/usr/bin/env python3
"""Create fixture files for smoke tests.

Run this script once to generate the fixture files:
    python create_fixtures.py
"""

from pathlib import Path

from docx import Document
from docx.shared import Inches
from PIL import Image as PILImage


def create_sample_image() -> None:
    """Create a simple test image (100x100 red square with blue center)."""
    img = PILImage.new("RGB", (100, 100), color="red")
    # Add a blue square in the center
    for x in range(25, 75):
        for y in range(25, 75):
            img.putpixel((x, y), (0, 0, 255))

    fixtures_dir = Path(__file__).parent
    img.save(fixtures_dir / "sample_image.png")
    print("Created sample_image.png")


def create_sample_document() -> None:
    """Create a sample Documents document with various content types."""
    doc = Document()

    # Add metadata
    doc.core_properties.title = "Sample Document"
    doc.core_properties.author = "Smoke Test"
    doc.core_properties.subject = "Testing"

    # Heading 1
    doc.add_heading("Sample Document Title", level=1)

    # Regular paragraph
    doc.add_paragraph(
        "This is a sample paragraph with some text content. "
        "It contains multiple sentences for testing read and edit operations."
    )

    # Heading 2
    doc.add_heading("Section One", level=2)

    # Another paragraph
    para = doc.add_paragraph("This paragraph has ")
    para.add_run("bold text").bold = True
    para.add_run(" and ")
    para.add_run("italic text").italic = True
    para.add_run(".")

    # Bullet list
    doc.add_paragraph("First bullet item", style="List Bullet")
    doc.add_paragraph("Second bullet item", style="List Bullet")
    doc.add_paragraph("Third bullet item", style="List Bullet")

    # Table
    table = doc.add_table(rows=3, cols=3)
    table.style = "Table Grid"
    # Header row
    table.cell(0, 0).text = "Header A"
    table.cell(0, 1).text = "Header B"
    table.cell(0, 2).text = "Header C"
    # Data rows
    table.cell(1, 0).text = "Row 1, Col A"
    table.cell(1, 1).text = "Row 1, Col B"
    table.cell(1, 2).text = "Row 1, Col C"
    table.cell(2, 0).text = "Row 2, Col A"
    table.cell(2, 1).text = "Row 2, Col B"
    table.cell(2, 2).text = "Row 2, Col C"

    # Another heading
    doc.add_heading("Section Two", level=2)

    # Numbered list
    doc.add_paragraph("First numbered item", style="List Number")
    doc.add_paragraph("Second numbered item", style="List Number")

    # Add an image
    fixtures_dir = Path(__file__).parent
    image_path = fixtures_dir / "sample_image.png"
    if image_path.exists():
        doc.add_paragraph("Image below:")
        doc.add_picture(str(image_path), width=Inches(1.5))
        doc.add_paragraph("Image above.")

    # Final paragraph
    doc.add_paragraph(
        "This is the final paragraph of the sample document. "
        "It can be used for testing delete and format operations."
    )

    # Save
    doc.save(fixtures_dir / "sample_document.docx")
    print("Created sample_document.docx")


def create_document_with_image() -> None:
    """Create a document specifically for image testing."""
    doc = Document()
    doc.add_heading("Image Test Document", level=1)
    doc.add_paragraph(
        "This document contains images for testing modify_image and read_image."
    )

    fixtures_dir = Path(__file__).parent
    image_path = fixtures_dir / "sample_image.png"

    if image_path.exists():
        # Add multiple images at different sizes
        doc.add_paragraph("First image (large):")
        doc.add_picture(str(image_path), width=Inches(2.0))

        doc.add_paragraph("Second image (small):")
        doc.add_picture(str(image_path), width=Inches(1.0))

        doc.add_paragraph("Third image (medium):")
        doc.add_picture(str(image_path), width=Inches(1.5))

    doc.save(fixtures_dir / "document_with_images.docx")
    print("Created document_with_images.docx")


if __name__ == "__main__":
    # Create image first (needed for documents)
    create_sample_image()
    create_sample_document()
    create_document_with_image()
    print("\nAll fixtures created successfully!")

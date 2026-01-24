"""
Local file extractor using Python libraries for fast, lightweight extraction.

This extractor is used as a first-pass to detect changes in multi-part documents
before falling back to more expensive extraction methods like Reducto.

Supported formats:
- XLSX: openpyxl (with optional chart extraction via LibreOffice PDF conversion)
- PPTX: python-pptx
- DOCX: python-docx
- CSV: built-in csv module (always available)
- TXT: built-in (always available)
"""

import csv
import math
import shutil
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import openpyxl
import xlrd
from docx import Document
from loguru import logger
from pptx import Presentation
from tenacity import retry, stop_after_attempt, wait_fixed

from ..base import BaseFileExtractor
from ..constants import SPREADSHEET_EXTENSIONS
from ..types import ExtractedContent, ImageMetadata, SubArtifact
from ..utils.chart_extraction import (
    convert_xlsx_to_pdf,
    find_libreoffice,
    has_charts_in_xlsx,
    pdf_to_base64_images,
)


class LocalExtractor(BaseFileExtractor):
    """
    Local extractor for quick content extraction using Python libraries.

    This extractor is fast but provides basic text extraction. It's designed
    for change detection rather than high-quality content extraction.
    """

    def __init__(self):
        """Initialize the local extractor"""
        self._supported_extensions = set()

        # Check for openpyxl
        if find_spec("openpyxl") is not None:
            self._has_openpyxl = True
            self._supported_extensions.update(SPREADSHEET_EXTENSIONS)
            logger.debug("LocalExtractor: openpyxl available for XLSX files")
        else:
            self._has_openpyxl = False
            logger.debug("LocalExtractor: openpyxl not available")

        # Check for python-pptx
        if find_spec("pptx") is not None:
            self._has_pptx = True
            self._supported_extensions.update([".pptx"])
            logger.debug("LocalExtractor: python-pptx available for PPTX files")
        else:
            self._has_pptx = False
            logger.debug("LocalExtractor: python-pptx not available")

        # Check for python-docx
        if find_spec("docx") is not None:
            self._has_docx = True
            self._supported_extensions.update([".docx"])
            logger.debug("LocalExtractor: python-docx available for DOCX files")
        else:
            self._has_docx = False
            logger.debug("LocalExtractor: python-docx not available")

        # Check for xlrd (for .xls files)
        if find_spec("xlrd") is not None:
            self._has_xlrd = True
            self._supported_extensions.add(".xls")
            logger.debug("LocalExtractor: xlrd available for XLS files")
        else:
            self._has_xlrd = False
            logger.debug("LocalExtractor: xlrd not available")

        # Check for csv (built-in, always available)
        if find_spec("csv") is not None:
            self._has_csv = True
            self._supported_extensions.update([".csv"])
            logger.debug("LocalExtractor: csv available for CSV files")
        else:
            self._has_csv = False
            logger.debug("LocalExtractor: csv not available")

    @property
    def name(self) -> str:
        return "local_python_libs"

    def supports_file_type(self, file_extension: str) -> bool:
        """Check if this extractor supports the given file type"""
        return file_extension.lower() in self._supported_extensions

    async def extract_from_file(
        self,
        file_path: Path,
        *,
        include_images: bool = True,
        sub_artifact_index: int | None = None,
    ) -> ExtractedContent:
        """
        Extract content from a file using local Python libraries.

        This provides basic text extraction for change detection.
        """
        file_ext = file_path.suffix.lower()

        # Route .xls files to xlrd extractor (openpyxl doesn't support .xls)
        if file_ext == ".xls" and self._has_xlrd:
            return await self._extract_xls(file_path, sub_artifact_index)
        elif file_ext in SPREADSHEET_EXTENSIONS and self._has_openpyxl:
            return await self._extract_xlsx(file_path, sub_artifact_index)
        elif file_ext == ".pptx" and self._has_pptx:
            return await self._extract_pptx(file_path, sub_artifact_index)
        elif file_ext == ".docx" and self._has_docx:
            return await self._extract_docx(file_path, sub_artifact_index)
        elif file_ext == ".csv" and self._has_csv:
            return await self._extract_csv(file_path)
        else:
            raise ValueError(f"Unsupported file type: {file_ext}")

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(1),
        reraise=True,
    )
    async def _extract_xlsx(
        self, file_path: Path, sub_artifact_index: int | None = None
    ) -> ExtractedContent:
        """Extract content from XLSX file using openpyxl, with optional chart extraction."""
        if openpyxl is None:
            raise ImportError("openpyxl is required for XLSX extraction")

        try:
            # Load workbook (data_only=True to get computed values)
            # Note: read_only=True doesn't load sheet_state, so we need read_only=False
            wb = openpyxl.load_workbook(file_path, data_only=True, read_only=False)

            sub_artifacts = []
            full_text_parts = []
            skipped_hidden_sheet: str | None = (
                None  # Track if we skipped requested sheet
            )

            try:
                for sheet_idx, sheet_name in enumerate(wb.sheetnames):
                    # If specific sub-artifact requested, skip others
                    if (
                        sub_artifact_index is not None
                        and sheet_idx != sub_artifact_index
                    ):
                        continue

                    sheet = wb[sheet_name]

                    # Skip hidden sheets (sheet_state can be 'visible', 'hidden', or 'veryHidden')
                    if sheet.sheet_state != "visible":
                        logger.debug(
                            f"Skipping hidden sheet '{sheet_name}' (state: {sheet.sheet_state})"
                        )
                        # Track if this was the specifically requested sheet
                        if (
                            sub_artifact_index is not None
                            and sheet_idx == sub_artifact_index
                        ):
                            skipped_hidden_sheet = sheet_name
                        continue

                    # Extract cell values into text
                    sheet_text_lines = []
                    for row in sheet.iter_rows(values_only=True):
                        # Filter out None values and convert to strings
                        row_values = [str(cell) for cell in row if cell is not None]
                        if row_values:
                            sheet_text_lines.append("\t".join(row_values))

                    sheet_text = "\n".join(sheet_text_lines)

                    sheet_text = f"=== Sheet: {sheet_name} ===\n{sheet_text}"

                    # Create sub-artifact for this sheet
                    sub_artifacts.append(
                        SubArtifact(
                            index=sheet_idx,
                            type="sheet",
                            title=sheet_name,
                            content=sheet_text,
                            images=[],
                        )
                    )

                    if sub_artifact_index is None:
                        full_text_parts.append(sheet_text)
            finally:
                wb.close()

            # If specific sub-artifact requested, return only that (skip chart extraction)
            if sub_artifact_index is not None:
                if sub_artifacts:
                    return ExtractedContent(
                        text=sub_artifacts[0].content,
                        images=[],
                        extraction_method=self.name,
                        metadata={"sheet_index": sub_artifact_index},
                        sub_artifacts=[],
                    )
                elif skipped_hidden_sheet:
                    raise ValueError(
                        f"Sheet index {sub_artifact_index} ('{skipped_hidden_sheet}') is hidden"
                    )
                else:
                    raise ValueError(f"Sheet index {sub_artifact_index} not found")

            # Extract charts if present (only for full file extraction)
            chart_images: list[ImageMetadata] = []
            if has_charts_in_xlsx(file_path):
                logger.info(f"Charts detected in {file_path.name}")

                soffice_path = find_libreoffice()
                if soffice_path:
                    pdf_path = await convert_xlsx_to_pdf(file_path, soffice_path)
                    # Start try immediately to ensure cleanup on CancelledError
                    try:
                        if pdf_path:
                            chart_images = pdf_to_base64_images(pdf_path)
                            if chart_images:
                                logger.info(
                                    f"Extracted {len(chart_images)} chart image(s) from PDF"
                                )

                                # Add chart placeholders to text
                                chart_text = "\n\n=== Charts ===\n"
                                for img in chart_images:
                                    chart_text += f"{img.placeholder} - {img.caption}\n"
                                full_text_parts.append(chart_text)
                    finally:
                        if pdf_path:
                            if pdf_path.exists():
                                pdf_path.unlink()
                            if pdf_path.parent.exists():
                                shutil.rmtree(pdf_path.parent, ignore_errors=True)
                else:
                    logger.warning(
                        f"LibreOffice not found - cannot extract chart images from {file_path.name}. "
                        "Install LibreOffice for chart extraction support."
                    )

            # Return all sheets with chart images
            full_text = "\n\n".join(full_text_parts)
            return ExtractedContent(
                text=full_text,
                images=chart_images,
                extraction_method=self.name,
                metadata={
                    "sheet_count": len(sub_artifacts),
                    "chart_count": len(chart_images),
                },
                sub_artifacts=sub_artifacts,
            )

        except Exception as e:
            logger.warning(f"Failed to extract XLSX with openpyxl: {e}")
            raise

    @retry(stop=stop_after_attempt(3), wait=wait_fixed(1), reraise=True)
    async def _extract_xls(
        self, file_path: Path, sub_artifact_index: int | None = None
    ) -> ExtractedContent:
        """Extract content from XLS file using xlrd"""
        if xlrd is None:
            raise ImportError("xlrd is required for XLS extraction")

        try:
            wb = xlrd.open_workbook(str(file_path))
            sub_artifacts = []
            full_text_parts = []

            for sheet_idx in range(wb.nsheets):
                if sub_artifact_index is not None and sheet_idx != sub_artifact_index:
                    continue

                sheet = wb.sheet_by_index(sheet_idx)
                sheet_name = sheet.name

                # Skip hidden sheets (visibility: 0=visible, 1=hidden, 2=very hidden)
                if sheet.visibility != 0:
                    continue

                sheet_text_lines = []
                for row_idx in range(sheet.nrows):
                    row_values = []
                    for col_idx in range(sheet.ncols):
                        cell = sheet.cell(row_idx, col_idx)
                        if cell.ctype == xlrd.XL_CELL_EMPTY:
                            continue
                        elif cell.ctype == xlrd.XL_CELL_NUMBER:
                            try:
                                value = cell.value
                                # Check for special float values (NaN, inf, -inf)
                                if isinstance(value, float) and (
                                    math.isnan(value) or math.isinf(value)
                                ):
                                    row_values.append(str(value))
                                elif value == int(value):
                                    row_values.append(str(int(value)))
                                else:
                                    row_values.append(str(value))
                            except (ValueError, OverflowError, TypeError):
                                row_values.append(str(cell.value))
                        elif cell.ctype == xlrd.XL_CELL_DATE:
                            try:
                                dt = xlrd.xldate_as_tuple(
                                    float(cell.value), wb.datemode
                                )
                                row_values.append(f"{dt[0]}-{dt[1]:02d}-{dt[2]:02d}")
                            except Exception:
                                row_values.append(str(cell.value))
                        elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                            row_values.append("TRUE" if cell.value else "FALSE")
                        else:
                            value = str(cell.value).strip()
                            if value:
                                row_values.append(value)

                    if row_values:
                        sheet_text_lines.append("\t".join(row_values))

                sheet_text = "\n".join(sheet_text_lines)
                sheet_text = f"=== Sheet: {sheet_name} ===\n{sheet_text}"

                sub_artifacts.append(
                    SubArtifact(
                        index=sheet_idx,
                        type="sheet",
                        title=sheet_name,
                        content=sheet_text,
                        images=[],
                    )
                )

                if sub_artifact_index is None:
                    full_text_parts.append(sheet_text)

            logger.debug(
                f"[LOCAL] Extracted {len(sub_artifacts)} sub-artifacts from {file_path}"
            )

            if sub_artifact_index is not None:
                if sub_artifacts:
                    return ExtractedContent(
                        text=sub_artifacts[0].content,
                        images=[],
                        extraction_method=self.name,
                        metadata={"sheet_index": sub_artifact_index},
                        sub_artifacts=[],
                    )
                else:
                    raise ValueError(f"Sheet index {sub_artifact_index} not found")

            return ExtractedContent(
                text="\n\n".join(full_text_parts),
                images=[],
                extraction_method=self.name,
                metadata={"sheet_count": len(sub_artifacts)},
                sub_artifacts=sub_artifacts,
            )

        except Exception as e:
            logger.warning(f"Failed to extract XLS with xlrd: {e}")
            raise

    def _extract_text_from_shape(self, shape: Any) -> list[str]:
        """
        Recursively extract text from a Presentations shape.

        Handles:
        - Simple shapes with .text attribute
        - Tables (extracts all cells)
        - Grouped shapes (recursively extracts from children)
        - Text frames with paragraphs
        """
        text_parts = []

        # Handle grouped shapes recursively
        if hasattr(shape, "shapes"):
            for child_shape in shape.shapes:
                text_parts.extend(self._extract_text_from_shape(child_shape))
            return text_parts

        # Handle tables - try to extract, but fall through if not a table
        # Note: hasattr(shape, "table") returns True for all GraphicFrame shapes
        # (charts, diagrams, etc.), but .table raises ValueError for non-tables
        try:
            table = shape.table
            for row in table.rows:
                row_texts = []
                for cell in row.cells:
                    cell_text = cell.text.strip() if cell.text else ""
                    if cell_text:
                        row_texts.append(cell_text)
                if row_texts:
                    text_parts.append("\t".join(row_texts))
            return text_parts  # Only return if table extraction succeeded
        except (ValueError, AttributeError):
            pass  # Not a table shape, continue with other extraction methods

        # Handle text frames (more thorough than just .text)
        text_frame_succeeded = False
        if hasattr(shape, "text_frame"):
            try:
                text_frame = shape.text_frame
                for paragraph in text_frame.paragraphs:
                    para_text = ""
                    for run in paragraph.runs:
                        if run.text:
                            para_text += run.text
                    if para_text.strip():
                        text_parts.append(para_text.strip())
                        text_frame_succeeded = True
            except Exception:
                pass

        # Fallback to simple .text attribute (runs if text_frame failed or found nothing)
        if not text_frame_succeeded and hasattr(shape, "text") and shape.text:
            text = shape.text.strip()
            if text:
                text_parts.append(text)

        return text_parts

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(1),
        reraise=True,
    )
    async def _extract_pptx(
        self, file_path: Path, sub_artifact_index: int | None = None
    ) -> ExtractedContent:
        """Extract content from PPTX file using python-pptx"""
        if Presentation is None:
            raise ImportError("python-pptx is required for PPTX extraction")

        try:
            prs = Presentation(str(file_path))

            sub_artifacts = []
            full_text_parts = []

            for slide_idx, slide in enumerate(prs.slides):
                # If specific sub-artifact requested, skip others
                if sub_artifact_index is not None and slide_idx != sub_artifact_index:
                    continue

                # Extract text from all shapes in the slide (including tables, groups, etc.)
                slide_text_parts = []
                slide_title = None

                for shape in slide.shapes:
                    # Try to detect title placeholder first
                    if slide_title is None:
                        try:
                            if (
                                hasattr(shape, "placeholder_format")
                                and shape.placeholder_format.type == 1
                            ):
                                shape_text = getattr(shape, "text", None)
                                if shape_text:
                                    slide_title = shape_text.strip()
                        except Exception:
                            pass

                    # Extract all text from this shape (recursively handles tables, groups, etc.)
                    shape_texts = self._extract_text_from_shape(shape)
                    slide_text_parts.extend(shape_texts)

                slide_text = "\n".join(slide_text_parts)

                # Use first line as title if no title detected
                if slide_title is None and slide_text_parts:
                    slide_title = slide_text_parts[0][:100]  # First 100 chars

                # Create sub-artifact for this slide
                sub_artifacts.append(
                    SubArtifact(
                        index=slide_idx,
                        type="slide",
                        title=slide_title or f"Slide {slide_idx + 1}",
                        content=slide_text,
                        images=[],
                    )
                )

                # Add to full text if not requesting specific sub-artifact
                if sub_artifact_index is None:
                    full_text_parts.append(
                        f"=== Slide {slide_idx + 1}: {slide_title or 'Untitled'} ===\n{slide_text}"
                    )

            # If specific sub-artifact requested, return only that
            if sub_artifact_index is not None:
                if sub_artifacts:
                    return ExtractedContent(
                        text=sub_artifacts[0].content,
                        images=[],
                        extraction_method=self.name,
                        metadata={"slide_index": sub_artifact_index},
                        sub_artifacts=[],  # Empty list when extracting single sub-artifact
                    )
                else:
                    raise ValueError(f"Slide index {sub_artifact_index} not found")

            # Return all slides
            full_text = "\n\n".join(full_text_parts)
            return ExtractedContent(
                text=full_text,
                images=[],
                extraction_method=self.name,
                metadata={"slide_count": len(sub_artifacts)},
                sub_artifacts=sub_artifacts,
            )

        except Exception as e:
            logger.warning(f"Failed to extract PPTX with python-pptx: {e}")
            raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(1),
        reraise=True,
    )
    async def _extract_docx(
        self, file_path: Path, sub_artifact_index: int | None = None
    ) -> ExtractedContent:
        """
        Extract content from DOCX file using python-docx.

        Note: python-docx doesn't have page concept, so we extract sections or the full document.
        For page-level extraction, Reducto is used when changes are detected.
        """
        if Document is None:
            raise ImportError("python-docx is required for DOCX extraction")

        try:
            doc = Document(str(file_path))

            # Extract all paragraphs
            all_text_parts = []
            for para in doc.paragraphs:
                text = para.text.strip()
                if text:
                    all_text_parts.append(text)

            for table in doc.tables:
                for row in table.rows:
                    row_text = []
                    for cell in row.cells:
                        cell_text = cell.text.strip()
                        if cell_text:
                            row_text.append(cell_text)
                    if row_text:
                        # Join cells with tabs to preserve table structure
                        all_text_parts.append("\t".join(row_text))

            full_text = "\n".join(all_text_parts)

            # For local extraction, we treat the whole document as one unit for change detection
            # We don't create sub-artifacts here because python-docx doesn't have reliable page info
            # If changes are detected, Reducto will handle page-level extraction

            # Return as single artifact (no sub-artifacts for simple change detection)
            return ExtractedContent(
                text=full_text,
                images=[],
                extraction_method=self.name,
                metadata={
                    "paragraph_count": len(doc.paragraphs),
                    "table_count": len(doc.tables),
                },
                sub_artifacts=[],  # No sub-artifacts - will use Reducto if changes detected
            )

        except Exception as e:
            logger.warning(f"Failed to extract DOCX with python-docx: {e}")
            raise

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_fixed(1),
        reraise=True,
    )
    async def _extract_csv(self, file_path: Path) -> ExtractedContent:
        """
        Extract content from CSV file using built-in csv module.

        CSV files are treated as single artifacts (no sub-artifacts).
        This provides a fallback when Reducto fails (e.g., file too large).
        """
        try:
            # Try UTF-8 first, fallback to other encodings
            encodings = ["utf-8", "utf-8-sig", "latin-1", "cp1252"]
            content_lines = None
            used_encoding = None

            for encoding in encodings:
                try:
                    with open(file_path, encoding=encoding, newline="") as f:
                        reader = csv.reader(f)
                        content_lines = []
                        for row in reader:
                            # Join cells with tabs to preserve structure
                            content_lines.append("\t".join(row))
                    used_encoding = encoding
                    break
                except UnicodeDecodeError:
                    content_lines = None
                    continue

            if content_lines is None:
                raise ValueError("Could not decode CSV with any supported encoding")

            full_text = "\n".join(content_lines)

            logger.debug(
                f"Extracted CSV with {len(content_lines)} rows using {used_encoding} encoding"
            )

            return ExtractedContent(
                text=full_text,
                images=[],
                extraction_method=self.name,
                metadata={
                    "row_count": len(content_lines),
                    "encoding": used_encoding,
                },
                sub_artifacts=[],  # CSV is treated as a single artifact
            )

        except Exception as e:
            logger.warning(f"Failed to extract CSV: {e}")
            raise

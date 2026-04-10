"""USPTO patent PDF generation service.

This service generates USPTO-style patent PDFs from database content.
Uses authentic two-column layout matching official USPTO patent documents.

Data Source: Database only (not XML)
Output: PDF bytes
"""

from __future__ import annotations

import io
import re
from typing import Any
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfgen import canvas
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    FrameBreak,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Preformatted,
    Spacer,
)

# Constants for parsing description sections
DESCRIPTION_HEADINGS: list[tuple[str, str]] = [
    (r"field of the invention", "Field of the Invention"),
    (r"background(?: of the invention)?", "Background"),
    (r"summary(?: of the invention)?", "Summary"),
    (r"brief description of (?:the )?drawings?", "Brief Description of the Drawings"),
    (r"detailed description(?: of the invention)?", "Detailed Description"),
]

CLAIM_START_RE = re.compile(r"^\s*(\d+)[\.\)]\s*(.*)")
TABLE_START_RE = re.compile(r"^TABLE\s+(\d+)\s*$", re.MULTILINE)


class NumberedCanvas(canvas.Canvas):
    """Custom canvas for adding page numbers to USPTO-style PDFs."""

    def __init__(self, *args, **kwargs):
        canvas.Canvas.__init__(self, *args, **kwargs)
        self._saved_page_states = []

    def showPage(self):  # noqa: N802
        """Save page state for later numbering."""
        self._saved_page_states.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        """Add page numbers to all pages before saving."""
        num_pages = len(self._saved_page_states)
        for state in self._saved_page_states:
            self.__dict__.update(state)
            self.draw_page_number(num_pages)
            canvas.Canvas.showPage(self)
        canvas.Canvas.save(self)

    def draw_page_number(self, page_count):
        """Draw centered page number at bottom of page."""
        self.setFont("Times-Roman", 9)
        self.drawCentredString(4.25 * inch, 0.5 * inch, f"Page {self._pageNumber}")


class USPTOPDFGenerator:
    """Service for generating USPTO-style patent PDFs from database content."""

    def generate(self, application_data: dict[str, Any]) -> bytes:
        """Generate USPTO-style PDF from patent data.

        Args:
            application_data: Complete patent data from database with:
                - Patent metadata (numbers, dates, title)
                - inventors: List of inventor dicts
                - assignees: List of assignee dicts
                - cpcClassifications: List of CPC classification dicts
                - patentCitations: List of citation dicts
                - foreignPriorityClaims: List of foreign priority claim dicts (optional)
                - relatedApplications: List of related application/priority dicts (optional)
                - primaryExaminer: Examiner dict
                - assistantExaminer: Examiner dict (optional)
                - abstract: Abstract text
                - description: Description text
                - claims: Claims text

        Returns:
            PDF bytes ready for encoding

        Raises:
            Exception: PDF generation errors are propagated to caller
        """
        buffer = io.BytesIO()

        # Create document with USPTO-style templates
        doc = self._create_document(buffer, application_data)

        # Build complete story with cover, description, claims
        story = self._build_story(application_data)

        # Generate PDF with page numbers
        doc.build(story, canvasmaker=NumberedCanvas)

        # Return bytes
        buffer.seek(0)
        return buffer.read()

    def _create_document(
        self, buffer: io.BytesIO, application_data: dict[str, Any]
    ) -> BaseDocTemplate:
        """Create BaseDocTemplate with USPTO-style page templates."""
        doc = BaseDocTemplate(
            buffer,
            pagesize=letter,
            title=application_data.get("inventionTitle") or "USPTO Patent",
            author="USPTO Offline MCP",
        )

        # Define frames for different layouts
        # Cover page: two columns
        cover_left = Frame(0.5 * inch, 0.75 * inch, 3.25 * inch, 9.5 * inch, id="cover_left")
        cover_right = Frame(4 * inch, 0.75 * inch, 3.75 * inch, 9.5 * inch, id="cover_right")

        # Description: two columns (newspaper style)
        desc_left = Frame(0.5 * inch, 0.75 * inch, 3.65 * inch, 9.5 * inch, id="desc_left")
        desc_right = Frame(4.35 * inch, 0.75 * inch, 3.65 * inch, 9.5 * inch, id="desc_right")

        # Claims and references: single column
        full_frame = Frame(0.5 * inch, 0.75 * inch, 7.5 * inch, 9.5 * inch, id="full")

        # Create page templates
        cover_template = PageTemplate(id="cover", frames=[cover_left, cover_right])
        desc_template = PageTemplate(id="twocol", frames=[desc_left, desc_right])
        full_template = PageTemplate(id="full", frames=[full_frame])

        doc.addPageTemplates([cover_template, desc_template, full_template])

        return doc

    def _get_styles(self) -> dict[str, ParagraphStyle]:
        """Get USPTO-style paragraph styles."""
        styles = getSampleStyleSheet()

        return {
            "header": ParagraphStyle(
                "Header",
                parent=styles["Normal"],
                fontSize=18,
                fontName="Times-Bold",
                alignment=TA_CENTER,
                spaceAfter=10,
            ),
            "patent_num": ParagraphStyle(
                "PatentNum",
                parent=styles["Normal"],
                fontSize=11,
                fontName="Times-Roman",
                alignment=TA_RIGHT,
                spaceAfter=4,
            ),
            "label": ParagraphStyle(
                "Label",
                parent=styles["Normal"],
                fontSize=8,
                fontName="Times-Roman",
                leftIndent=0,
                spaceAfter=3,
            ),
            "data": ParagraphStyle(
                "Data",
                parent=styles["Normal"],
                fontSize=8,
                fontName="Times-Roman",
                leftIndent=10,
                spaceAfter=2,
            ),
            "body": ParagraphStyle(
                "Body",
                parent=styles["Normal"],
                fontSize=9,
                fontName="Times-Roman",
                alignment=TA_JUSTIFY,
                spaceAfter=4,
                leading=11,
            ),
            "heading": ParagraphStyle(
                "Heading",
                parent=styles["Normal"],
                fontSize=11,
                fontName="Times-Bold",
                spaceAfter=6,
                spaceBefore=8,
            ),
            "claim": ParagraphStyle(
                "Claim",
                parent=styles["Normal"],
                fontSize=9,
                fontName="Times-Roman",
                leading=11,
                leftIndent=18,
                firstLineIndent=-12,
                spaceAfter=8,
            ),
        }

    def _build_story(self, application_data: dict[str, Any]) -> list[Any]:
        """Build complete PDF story with all sections."""
        story = []

        # Cover page (two columns)
        story.extend(self._build_cover_page(application_data))

        # Switch to description template (NextPageTemplate before PageBreak!)
        story.append(NextPageTemplate("twocol"))
        story.append(PageBreak())

        # Description (two columns with paragraph numbering)
        story.extend(self._build_description(application_data))

        # Switch to full-width template for claims (NextPageTemplate before PageBreak!)
        story.append(NextPageTemplate("full"))
        story.append(PageBreak())

        # Claims (single column)
        story.extend(self._build_claims(application_data))

        # References (if available)
        citations = application_data.get("patentCitations") or []
        if citations:
            story.append(PageBreak())
            story.extend(self._build_references(application_data))

        return story

    def _build_cover_page(self, application_data: dict[str, Any]) -> list[Any]:
        """Build USPTO-style two-column cover page."""
        story = []
        styles = self._get_styles()

        # Get data
        inventors = application_data.get("inventors") or []
        assignees = application_data.get("assignees") or []
        cpc_classifications = application_data.get("cpcClassifications") or []

        # LEFT COLUMN
        # (12) United States Patent
        story.append(Paragraph("(12) <b>United States Patent</b>", styles["label"]))
        story.append(Spacer(1, 4))

        # Inventor last name
        if inventors:
            last_name = inventors[0].get("lastName") or "Unknown"
            suffix = " et al." if len(inventors) > 1 else ""
            story.append(Paragraph(f"<b>{escape(last_name)}{suffix}</b>", styles["label"]))
        story.append(Spacer(1, 12))

        # (54) Title
        title = application_data.get("inventionTitle") or "N/A"
        story.append(Paragraph(f"(54) <b>{escape(title.upper())}</b>", styles["label"]))
        story.append(Spacer(1, 12))

        # (71) Applicant
        if assignees:
            assignee_name = assignees[0].get("name") or ""
            city = assignees[0].get("city") or ""
            state = assignees[0].get("state") or ""
            location = f"{city}, {state}" if city and state else city or state or ""
            if assignee_name:
                story.append(
                    Paragraph(
                        f"(71) Applicant: <b>{escape(assignee_name)}</b>"
                        + (f", {escape(location)}" if location else ""),
                        styles["data"],
                    )
                )
                story.append(Spacer(1, 8))

        # (72) Inventors
        story.append(Paragraph("(72) Inventors:", styles["label"]))
        for inv in inventors[:5]:  # First 5 inventors
            first_name = inv.get("firstName") or ""
            last_name = inv.get("lastName") or ""
            name = f"{first_name} {last_name}".strip()
            city = inv.get("city") or ""
            state = inv.get("state") or ""
            location = f"{city}, {state}" if city and state else city or state or ""
            if name:
                inv_text = f"<b>{escape(name)}</b>"
                if location:
                    inv_text += f", {escape(location)}"
                story.append(Paragraph(inv_text, styles["data"]))
        story.append(Spacer(1, 8))

        # (73) Assignee
        if assignees:
            assignee_name = assignees[0].get("name") or ""
            city = assignees[0].get("city") or ""
            state = assignees[0].get("state") or ""
            location = f"{city}, {state}" if city and state else city or state or ""
            if assignee_name:
                story.append(
                    Paragraph(
                        f"(73) Assignee: <b>{escape(assignee_name)}</b>"
                        + (f", {escape(location)}" if location else ""),
                        styles["data"],
                    )
                )
                story.append(Spacer(1, 8))

        # (21) Application Number
        app_num = application_data.get("applicationNumberText") or "N/A"
        story.append(Paragraph(f"(21) Appl. No.: <b>{escape(app_num)}</b>", styles["label"]))
        story.append(Spacer(1, 4))

        # (22) Filing Date
        filing_date = application_data.get("filingDate") or "N/A"
        story.append(Paragraph(f"(22) Filed: <b>{escape(filing_date)}</b>", styles["label"]))
        story.append(Spacer(1, 12))

        # (30) Foreign Application Priority Data
        priority_claims = application_data.get("foreignPriorityClaims") or []
        if priority_claims and isinstance(priority_claims, list) and len(priority_claims) > 0:
            story.append(
                Paragraph("(30) <b>Foreign Application Priority Data:</b>", styles["label"])
            )
            story.append(Spacer(1, 4))
            for claim in priority_claims[:5]:  # Show first 5 priority claims
                if isinstance(claim, dict):
                    # Convert to string to handle numeric values from JSON
                    country = claim.get("country")
                    country = str(country) if country is not None else "N/A"

                    doc_number = claim.get("doc_number")
                    doc_number = str(doc_number) if doc_number is not None else "N/A"

                    date = claim.get("date")
                    date = str(date) if date is not None else "N/A"

                    # Format date as YYYY-MM-DD if it's YYYYMMDD
                    if date != "N/A" and len(date) == 8 and date.isdigit():
                        date = f"{date[:4]}-{date[4:6]}-{date[6:]}"

                    priority_text = f"{escape(country)} {escape(doc_number)} {escape(date)}"
                    story.append(Paragraph(priority_text, styles["data"]))
            story.append(Spacer(1, 8))

        # Domestic Priority / Related Applications (Continuity Data)
        parent_continuity = application_data.get("parentContinuity") or []
        child_continuity = application_data.get("childContinuity") or []
        related_apps = []
        if parent_continuity and isinstance(parent_continuity, list):
            related_apps.extend(parent_continuity)
        if child_continuity and isinstance(child_continuity, list):
            related_apps.extend(child_continuity)

        if related_apps and len(related_apps) > 0:
            story.append(Paragraph("<b>Domestic Priority (Continuity Data):</b>", styles["label"]))
            story.append(Spacer(1, 4))

            for rel_app in related_apps[:10]:  # Show first 10 related apps
                if isinstance(rel_app, dict):
                    # Compute relationship_type if not present (for backward compatibility)
                    rel_type = rel_app.get("relationship_type")
                    if not rel_type:
                        element_name = rel_app.get("_element_name")
                        parent_name = rel_app.get("_parent_name")
                        if element_name == "relation":
                            rel_type = parent_name or "related"
                        else:
                            rel_type = element_name or "related"
                    else:
                        rel_type = rel_type or "related"

                    # Format relationship type for display
                    rel_type_display = rel_type.replace("-", " ").replace("_", " ").title()

                    # Handle different relationship types
                    if rel_type in ["continuation", "continuation-in-part", "division", "reissue"]:
                        # Parent/child relations
                        parent_num = rel_app.get("parent_doc_number") or "N/A"
                        parent_date = rel_app.get("parent_filing_date") or "N/A"
                        parent_status = rel_app.get("parent_status") or ""
                        parent_grant = rel_app.get("parent_grant_number") or ""

                        # Format date
                        parent_date_str = str(parent_date)
                        if (
                            parent_date != "N/A"
                            and len(parent_date_str) == 8
                            and parent_date_str.isdigit()
                        ):
                            year = parent_date_str[:4]
                            month = parent_date_str[4:6]
                            day = parent_date_str[6:]
                            parent_date = f"{year}-{month}-{day}"

                        # Build text
                        text_parts = [
                            rel_type_display,
                            "of",
                            f"Appl. {escape(str(parent_num))}",
                            f"filed {escape(str(parent_date))}",
                        ]

                        if parent_grant:
                            text_parts.append(f"now Pat. No. {escape(str(parent_grant))}")
                        elif parent_status and parent_status != "PENDING":
                            text_parts.append(f"({escape(parent_status)})")

                        rel_text = " ".join(text_parts)
                        story.append(Paragraph(rel_text, styles["data"]))

                    elif rel_type == "us-provisional-application":
                        # Provisional applications
                        prov_num = rel_app.get("provisional_doc_number") or "N/A"
                        prov_date = rel_app.get("provisional_filing_date") or "N/A"

                        # Format date
                        prov_date_str = str(prov_date)
                        if (
                            prov_date != "N/A"
                            and len(prov_date_str) == 8
                            and prov_date_str.isdigit()
                        ):
                            year = prov_date_str[:4]
                            month = prov_date_str[4:6]
                            day = prov_date_str[6:]
                            prov_date = f"{year}-{month}-{day}"

                        rel_text = (
                            f"Provisional Application No. {escape(str(prov_num))} "
                            f"filed {escape(str(prov_date))}"
                        )
                        story.append(Paragraph(rel_text, styles["data"]))

                    elif rel_type == "related-publication":
                        # Related publications
                        pub_num = rel_app.get("publication_doc_number") or "N/A"
                        pub_kind = rel_app.get("publication_kind") or ""
                        pub_date = rel_app.get("publication_date") or "N/A"

                        # Format date
                        pub_date_str = str(pub_date)
                        if pub_date != "N/A" and len(pub_date_str) == 8 and pub_date_str.isdigit():
                            year = pub_date_str[:4]
                            month = pub_date_str[4:6]
                            day = pub_date_str[6:]
                            pub_date = f"{year}-{month}-{day}"

                        rel_text = (
                            f"Publication No. {escape(str(pub_num))} "
                            f"{escape(str(pub_kind))} {escape(str(pub_date))}"
                        )
                        story.append(Paragraph(rel_text, styles["data"]))

            story.append(Spacer(1, 8))

        # (51) Int. Cl. (Not available in offline DB)
        story.append(Paragraph("(51) Int. Cl.", styles["label"]))
        story.append(Paragraph("<i>Not available in offline database</i>", styles["data"]))
        story.append(Spacer(1, 8))

        # (52) U.S. Cl. (CPC Classifications)
        story.append(Paragraph("(52) U.S. Cl.", styles["label"]))
        cpc_codes = self._format_cpc_codes(cpc_classifications)
        for cpc in cpc_codes[:5]:  # First 5
            story.append(Paragraph(f"CPC ... <i>{escape(cpc)}</i>", styles["data"]))
        if not cpc_codes:
            story.append(Paragraph("CPC ... <i>Not available</i>", styles["data"]))

        # SWITCH TO RIGHT COLUMN
        story.append(FrameBreak())

        # RIGHT COLUMN
        # (10) Patent Number
        patent_num = application_data.get("patentNumber") or "N/A"
        kind = application_data.get("kindCode") or ""
        story.append(
            Paragraph(
                f"(10) Patent No.: <b>US {escape(patent_num)} {escape(kind)}</b>",
                styles["patent_num"],
            )
        )

        # (45) Issue Date
        issue_date = application_data.get("patentIssueDate") or "N/A"
        story.append(
            Paragraph(f"(45) Date of Patent: <b>{escape(issue_date)}</b>", styles["patent_num"])
        )
        story.append(Spacer(1, 12))

        # (58) Field of Classification Search
        story.append(Paragraph("(58) <b>Field of Classification Search</b>", styles["label"]))
        fos_text = "; ".join(cpc_codes[:5]) if cpc_codes else "Not available"
        story.append(Paragraph(f"CPC ... {escape(fos_text)}", styles["data"]))
        story.append(Spacer(1, 12))

        # Primary Examiner
        primary = application_data.get("primaryExaminer") or {}
        first = primary.get("firstName") or ""
        last = primary.get("lastName") or ""
        examiner_name = f"{first} {last}".strip()
        if examiner_name:
            story.append(
                Paragraph(f"<i>Primary Examiner</i> — {escape(examiner_name)}", styles["data"])
            )
            story.append(Spacer(1, 6))

        # Assistant Examiner (if available)
        assistant = application_data.get("assistantExaminer") or {}
        first = assistant.get("firstName") or ""
        last = assistant.get("lastName") or ""
        asst_name = f"{first} {last}".strip()
        if asst_name:
            story.append(
                Paragraph(f"<i>Assistant Examiner</i> — {escape(asst_name)}", styles["data"])
            )
            story.append(Spacer(1, 6))

        story.append(Spacer(1, 6))

        # (57) Abstract
        story.append(Paragraph("(57) <b>ABSTRACT</b>", styles["label"]))
        story.append(Spacer(1, 6))
        abstract = application_data.get("abstract") or "No abstract available."
        story.append(Paragraph(escape(abstract), styles["body"]))
        story.append(Spacer(1, 12))

        # Claims count
        num_claims = application_data.get("numberOfClaims")
        num_sheets = application_data.get("numberOfDrawingSheets")
        claims_text = str(num_claims) if num_claims is not None else "N/A"
        sheets_text = str(num_sheets) if num_sheets is not None else "N/A"
        story.append(
            Paragraph(
                f"<b>{escape(claims_text)} Claims, {escape(sheets_text)} Drawing Sheets</b>",
                styles["label"],
            )
        )

        return story

    def _build_description(self, application_data: dict[str, Any]) -> list[Any]:
        """Build two-column description with paragraph numbering.

        The two-column layout is handled by the 'twocol' PageTemplate with
        two Frame objects. ReportLab automatically flows content between frames.
        Detects and renders tables using TABLE N markers.
        """
        story = []
        styles = self._get_styles()

        # Title
        title = application_data.get("inventionTitle") or "N/A"
        story.append(Paragraph(f"<b>{escape(title.upper())}</b>", styles["heading"]))
        story.append(Spacer(1, 12))

        # Description heading
        story.append(Paragraph("<b>DETAILED DESCRIPTION</b>", styles["heading"]))
        story.append(Spacer(1, 8))

        # Description with paragraph numbering and table detection
        # ReportLab will automatically flow paragraphs between the two columns
        description = application_data.get("description") or "No description available."

        # Split content into text sections and tables
        content_items = self._split_description_with_tables(description)

        para_counter = 1
        for item_type, content in content_items:
            if item_type == "text":
                # Regular paragraphs with numbering
                paragraphs = self._split_description_paragraphs(content)
                for para in paragraphs:
                    para_num = f"[{para_counter:04d}]"  # [0001], [0002], etc.
                    story.append(Paragraph(f"{para_num} {escape(para)}", styles["body"]))
                    para_counter += 1
            elif item_type == "table":
                # Render table as formatted text
                table_num, table_text = content
                story.append(Spacer(1, 8))
                story.append(Paragraph(f"<b>TABLE {table_num}</b>", styles["heading"]))
                story.append(Spacer(1, 4))

                # Render table content as monospace text in gray box
                table_flowables = self._create_table_text_box(table_text, styles)
                story.extend(table_flowables)
                story.append(Spacer(1, 8))

        return story

    def _build_claims(self, application_data: dict[str, Any]) -> list[Any]:
        """Build single-column claims section."""
        story = []
        styles = self._get_styles()

        story.append(Paragraph("<b>WHAT IS CLAIMED IS:</b>", styles["heading"]))
        story.append(Spacer(1, 12))

        claims = self._parse_claims(application_data.get("claims"))
        if not claims:
            story.append(Paragraph("No claims available.", styles["body"]))
        else:
            for number, text in claims:
                if number:
                    claim_text = f"<b>{escape(number)}.</b> {escape(text)}"
                else:
                    claim_text = escape(text)
                story.append(Paragraph(claim_text, styles["claim"]))

        return story

    def _build_references(self, application_data: dict[str, Any]) -> list[Any]:
        """Build references cited section."""
        story = []
        styles = self._get_styles()

        story.append(Paragraph("<b>REFERENCES CITED</b>", styles["heading"]))
        story.append(Spacer(1, 12))

        # Separate US and foreign citations
        patent_citations = application_data.get("patentCitations") or []
        us_citations = []
        foreign_citations = []

        for citation in patent_citations:
            if not isinstance(citation, dict):
                continue
            country = citation.get("citedCountry") or ""
            number = citation.get("citedPatentNumber") or ""
            kind = citation.get("citedKind") or ""
            date = citation.get("citedDate") or ""
            category = citation.get("category") or ""

            if not number:
                continue

            # Format citation
            cite_text = f"{country} {number}"
            if kind:
                cite_text += f" {kind}"
            if date:
                cite_text += f" ({date})"
            if category:
                cite_text += f" [{category}]"

            if country == "US":
                us_citations.append(cite_text)
            else:
                foreign_citations.append(cite_text)

        # US Patent Documents
        if us_citations:
            story.append(Paragraph("<b>U.S. Patent Documents</b>", styles["heading"]))
            for cite in us_citations:
                story.append(Paragraph(escape(cite), styles["body"]))
            story.append(Spacer(1, 8))

        # Foreign Patent Documents
        if foreign_citations:
            story.append(Paragraph("<b>Foreign Patent Documents</b>", styles["heading"]))
            for cite in foreign_citations:
                story.append(Paragraph(escape(cite), styles["body"]))

        return story

    # Helper methods for data formatting

    def _split_description_paragraphs(self, description: str) -> list[str]:
        """Split description into paragraphs."""
        if not description:
            return []

        # Split by double newlines or single newlines
        paragraphs = []
        for para in description.split("\n\n"):
            para = para.strip()
            if para:
                paragraphs.append(para)

        # If no double newlines, try single newlines
        if len(paragraphs) <= 1 and description:
            paragraphs = [p.strip() for p in description.split("\n") if p.strip()]

        # If still just one big chunk, split by sentences (rough)
        if len(paragraphs) == 1 and len(description) > 1000:
            # Split into ~500 char chunks at sentence boundaries
            text = paragraphs[0]
            paragraphs = []
            current = ""
            sentences = text.split(". ")
            for i, sentence in enumerate(sentences):
                # Restore period except for last sentence (which may not have one)
                sentence_with_period = sentence + "." if i < len(sentences) - 1 else sentence
                if len(current) + len(sentence_with_period) > 500:
                    if current:
                        paragraphs.append(current.strip())
                    current = sentence_with_period
                else:
                    if current:
                        current += " " + sentence_with_period
                    else:
                        current = sentence_with_period
            if current:
                paragraphs.append(current.strip())

        return paragraphs

    def _format_cpc_codes(self, items: list[dict[str, Any]]) -> list[str]:
        """Format CPC codes as 'H04L 9/32'."""
        codes = []
        for item in items:
            section = item.get("section")
            cls = item.get("class")
            subclass = item.get("subclass")
            main_group = item.get("mainGroup")
            sub_group = item.get("subGroup")
            # Check for None and empty string explicitly (allow "0" values)
            if all(
                x is not None and x != "" for x in [section, cls, subclass, main_group, sub_group]
            ):
                codes.append(f"{section}{cls}{subclass} {main_group}/{sub_group}")
        return codes

    def _parse_claims(self, claims_text: str | None) -> list[tuple[str | None, str]]:
        """Parse claims text into numbered claims."""
        if not claims_text:
            return []

        lines = claims_text.splitlines()
        claims: list[tuple[str | None, str]] = []
        current_number: str | None = None
        current_lines: list[str] = []

        for line in lines:
            match = CLAIM_START_RE.match(line)
            if match:
                if current_lines:
                    claims.append((current_number, "\n".join(current_lines).strip()))
                current_number = match.group(1)
                current_lines = [match.group(2).strip()]  # Strip trailing whitespace
            else:
                current_lines.append(line)

        if current_lines:
            claims.append((current_number, "\n".join(current_lines).strip()))

        return claims

    def _split_description_with_tables(self, description: str) -> list[tuple[str, Any]]:
        """Split description into text sections and table sections.

        Returns:
            List of tuples: ("text", text_content) or ("table", (table_num, table_data))
        """
        if not description:
            return [("text", "")]

        content_items: list[tuple[str, Any]] = []
        last_pos = 0

        # Find all TABLE markers
        for match in TABLE_START_RE.finditer(description):
            table_num = match.group(1)
            table_start = match.start()
            table_end_marker = match.end()

            # Add text before table
            if table_start > last_pos:
                text_before = description[last_pos:table_start].strip()
                if text_before:
                    content_items.append(("text", text_before))

            # Find end of table (next TABLE or end of description)
            next_match = TABLE_START_RE.search(description, table_end_marker)
            if next_match:
                table_content = description[table_end_marker : next_match.start()].strip()
                last_pos = next_match.start()
            else:
                # Last table - goes to end or until next major section
                # Look for next section (Experimental Example, etc.)
                remaining = description[table_end_marker:]
                section_end = self._find_next_section(remaining)
                # section_end is position of next section, or len(remaining) if none found
                # Check if we found a section (section_end < len(remaining))
                if section_end < len(remaining):
                    # If section_end is 0, table has no content (next section immediately follows)
                    if section_end > 0:
                        table_content = remaining[:section_end].strip()
                    else:
                        table_content = ""
                    last_pos = table_end_marker + section_end
                else:
                    # No section found - table goes to end of description
                    table_content = remaining.strip()
                    last_pos = len(description)

            # Parse and add table (as text)
            # Always add table even if content is empty to preserve TABLE markers
            table_text = self._parse_table_content(table_content)
            content_items.append(("table", (table_num, table_text or "(no content)")))

        # Add remaining text after last table
        if last_pos < len(description):
            remaining_text = description[last_pos:].strip()
            if remaining_text:
                content_items.append(("text", remaining_text))

        # If no tables found, return all as text
        if not content_items:
            content_items.append(("text", description))

        return content_items

    def _find_next_section(self, text: str) -> int:
        """Find the start of next major section after a table."""
        # Look for patterns like "Experimental Example", "BRIEF DESCRIPTION", etc.
        # Use ^TABLE pattern to match TABLE_START_RE behavior (MULTILINE mode)
        section_patterns = [
            r"\n\nExperimental Example",
            r"\n\nBRIEF DESCRIPTION",
            r"\n\nDETAILED DESCRIPTION",
            r"^TABLE \d+",  # Changed to match TABLE_START_RE pattern (after any newline)
        ]

        earliest_pos = -1
        for pattern in section_patterns:
            match = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
            if match:
                if earliest_pos == -1 or match.start() < earliest_pos:
                    earliest_pos = match.start()

        return earliest_pos if earliest_pos != -1 else len(text)

    def _parse_table_content(self, content: str) -> str:
        """Parse table content and return as plain text.

        Tables in USPTO database vary significantly in format.
        Instead of trying to parse complex multi-column structures,
        we simply preserve the table as formatted text.
        """
        if not content:
            return ""

        # Just clean up excessive blank lines while preserving structure
        lines = content.split("\n")
        cleaned_lines = []
        prev_blank = False

        for line in lines:
            if line.strip():
                cleaned_lines.append(line)
                prev_blank = False
            elif not prev_blank:
                # Keep one blank line for spacing
                cleaned_lines.append("")
                prev_blank = True

        return "\n".join(cleaned_lines).strip()

    def _create_table_text_box(self, table_text: str, styles: dict[str, ParagraphStyle]) -> list:
        """Create a formatted text box for table content.

        Returns a list of flowables (preformatted text) that render the table
        as monospace text in a light gray box, preserving all whitespace.
        """
        if not table_text:
            return []

        # Create a monospace paragraph style for table content
        table_style = ParagraphStyle(
            "TableText",
            parent=styles["body"],
            fontName="Courier",
            fontSize=7,
            leading=9,
            leftIndent=6,
            rightIndent=6,
            spaceBefore=3,
            spaceAfter=3,
            backColor=colors.HexColor("#F5F5F5"),  # Light gray background
        )

        flowables = []

        # Split into lines and create Preformatted flowables
        for line in table_text.split("\n"):
            # Use Preformatted to preserve all whitespace (spaces, tabs)
            if line.strip():
                # Escape XML characters (<, >, &) to prevent parsing errors
                # Preformatted preserves spacing but still interprets markup
                flowables.append(Preformatted(escape(line), table_style))
            else:
                # Small spacer for blank lines
                flowables.append(Spacer(1, 3))

        return flowables


# Convenience function for direct invocation
def generate_uspto_pdf(application_data: dict[str, Any]) -> bytes:
    """Generate USPTO-style patent PDF from database content.

    Convenience wrapper for USPTOPDFGenerator.generate().

    Args:
        application_data: Complete patent data from database

    Returns:
        PDF bytes
    """
    generator = USPTOPDFGenerator()
    return generator.generate(application_data)

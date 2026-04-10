"""Record factory for converting extracted data to PatentGrantRecord."""

import json
from datetime import date
from typing import Any

from mcp_servers.uspto.offline.models import (
    ApplicationType,
    Assignee,
    CPCClassification,
    DocumentType,
    Examiner,
    ExaminerType,
    Inventor,
    PatentCitation,
    PatentGrantRecord,
    PatentRecord,
)


def patent_grant_record_factory(data: dict[str, Any]) -> PatentGrantRecord:
    """Convert extracted dictionary to PatentGrantRecord.

    Args:
        data: Dictionary of extracted fields from XMLExtractor

    Returns:
        PatentGrantRecord object with patent and all related data

    Example:
        >>> data = {
        ...     "application_number": "12345678",
        ...     "title": "Widget",
        ...     "inventors": [{"first_name": "John", "last_name": "Doe", ...}],
        ...     ...
        ... }
        >>> record = patent_grant_record_factory(data)
    """

    # Helper to convert date strings to date objects
    def parse_date(date_str: str | None) -> date | None:
        if not date_str:
            return None
        try:
            # Handle YYYYMMDD format from USPTO XML
            if len(date_str) == 8 and date_str.isdigit():
                year = int(date_str[0:4])
                month = int(date_str[4:6])
                day = int(date_str[6:8])
                # USPTO uses 0 for unknown day/month - skip invalid dates
                if day == 0 or month == 0:
                    return None
                return date(year, month, day)
            # Handle YYYY-MM-DD format
            elif len(date_str) == 10 and "-" in date_str:
                year, month, day = date_str.split("-")
                return date(int(year), int(month), int(day))
        except (ValueError, OverflowError):
            # Invalid date - return None
            return None
        return None

    # Helper to serialize JSON fields
    def serialize_json(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        # Convert arrays/dicts to JSON strings
        return json.dumps(value)

    # Helper to construct name from orgname OR first_name + last_name
    def construct_name(item: dict[str, Any]) -> str | None:
        """Construct name field from orgname OR first_name + last_name."""
        orgname = item.get("orgname")
        first_name = item.get("first_name")
        last_name = item.get("last_name")

        if orgname:
            return orgname
        elif first_name and last_name:
            return f"{first_name} {last_name}".strip()
        elif last_name:
            return last_name
        return None

    # Construct names for applicants_json
    if data.get("applicants_json"):
        for applicant in data["applicants_json"]:
            name = construct_name(applicant)
            if name:
                applicant["name"] = name

    # Construct names for attorneys_json
    if data.get("attorneys_json"):
        for attorney in data["attorneys_json"]:
            name = construct_name(attorney)
            if name:
                attorney["name"] = name

    # Determine document type from XML root tag (reliable)
    xml_tag = data.get("document_type_tag", "")
    document_type = DocumentType.GRANT if "grant" in xml_tag.lower() else DocumentType.APPLICATION

    # Parse application type from config field
    app_type_str = data.get("application_type")
    application_type = None
    if app_type_str:
        app_type_lower = app_type_str.lower()
        if "util" in app_type_lower:
            application_type = ApplicationType.UTILITY
        elif "design" in app_type_lower:
            application_type = ApplicationType.DESIGN
        elif "plant" in app_type_lower:
            application_type = ApplicationType.PLANT

    # Create PatentRecord (main patent data)
    patent = PatentRecord(
        application_number=data.get("application_number", ""),
        publication_number=data.get("publication_number"),
        patent_number=data.get("patent_number"),
        kind_code=data.get("kind_code"),
        document_type=document_type,
        application_type=application_type,
        country=data.get("country", "US"),
        filing_date=parse_date(data.get("filing_date")),
        publication_date=parse_date(data.get("publication_date")),
        issue_date=parse_date(data.get("issue_date")),
        title=data.get("title", ""),
        abstract=data.get("abstract"),
        description=data.get("description"),
        claims=data.get("claims"),
        applicants_json=serialize_json(data.get("applicants_json")),
        attorneys_json=serialize_json(data.get("attorneys_json")),
        ipc_codes_json=serialize_json(data.get("ipc_codes_json")),
        uspc_codes_json=serialize_json(data.get("uspc_codes_json")),
        locarno_classification=serialize_json(data.get("locarno_classification")),
        npl_citations_json=serialize_json(data.get("npl_citations_json")),
        priority_claims_json=serialize_json(data.get("priority_claims_json")),
        related_applications_json=serialize_json(data.get("related_applications_json")),
        term_of_grant=data.get("term_of_grant"),
        number_of_claims=data.get("number_of_claims"),
        number_of_figures=data.get("number_of_figures"),
        number_of_drawing_sheets=data.get("number_of_drawing_sheets"),
        pct_filing_data_json=serialize_json(data.get("pct_filing_data_json")),
        xml_file_name=data.get("xml_file_name"),
    )

    # Parse inventors
    inventors = []
    if data.get("inventors"):
        for inv_data in data["inventors"]:
            inventors.append(Inventor(**inv_data))

    # Parse assignees
    assignees = []
    if data.get("assignees"):
        for asn_data in data["assignees"]:
            # Construct name from orgname OR first_name + last_name
            name = construct_name(asn_data)
            if not name:
                # Skip assignees without any name
                continue

            # Copy and add constructed name
            asn_data = asn_data.copy()
            asn_data["name"] = name
            assignees.append(Assignee(**asn_data))

    # Parse CPC classifications
    cpc_classifications = []
    if data.get("cpc_classifications"):
        for cpc_data in data["cpc_classifications"]:
            # Skip CPC items missing required fields
            required_fields = ["section", "class", "subclass", "main_group", "subgroup"]
            if not all(cpc_data.get(field) for field in required_fields):
                continue
            # Handle the 'class' field which is aliased as 'class_' in the model
            # Copy dictionary to avoid mutating input
            cpc_dict = cpc_data.copy()
            if "class" in cpc_dict and "class_" not in cpc_dict:
                cpc_dict["class_"] = cpc_dict.pop("class")
            cpc_classifications.append(CPCClassification(**cpc_dict))

    # Parse patent citations
    patent_citations = []
    if data.get("patent_citations"):
        for cit_data in data["patent_citations"]:
            # Keep cited_date as string (YYYYMMDD format, may have day=00 for partial dates)
            patent_citations.append(PatentCitation(**cit_data))

    # Parse examiners
    examiners = []

    # Process primary examiners
    if data.get("primary_examiners"):
        for exam_data in data["primary_examiners"]:
            # Copy dictionary to avoid mutating input
            exam_dict = exam_data.copy()
            exam_dict["examiner_type"] = ExaminerType.PRIMARY
            examiners.append(Examiner(**exam_dict))

    # Process assistant examiners
    if data.get("assistant_examiners"):
        for exam_data in data["assistant_examiners"]:
            # Copy dictionary to avoid mutating input
            exam_dict = exam_data.copy()
            exam_dict["examiner_type"] = ExaminerType.ASSISTANT
            examiners.append(Examiner(**exam_dict))

    # Create and return PatentGrantRecord with all components
    return PatentGrantRecord(
        patent=patent,
        inventors=inventors,
        assignees=assignees,
        cpc_classifications=cpc_classifications,
        patent_citations=patent_citations,
        examiners=examiners,
    )

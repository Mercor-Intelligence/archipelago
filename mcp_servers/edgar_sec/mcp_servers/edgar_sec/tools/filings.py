"""SEC EDGAR API filing tools."""

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from models import (
    FilingRecord,
    FormTypeSummary,
    GetCompanyConceptInput,
    GetCompanyConceptOutput,
    GetCompanyFactsInput,
    GetCompanyFactsOutput,
    GetCompanySubmissionsInput,
    GetCompanySubmissionsOutput,
    GetFramesInput,
    GetFramesOutput,
    PaginationInfo,
)
from repositories import get_repository
from utils.cik_resolver import resolve_cik
from utils.errors import format_tool_error, is_connection_error, is_timeout_error


def _build_filing_records(recent: dict) -> list[FilingRecord]:
    """Convert SEC API parallel arrays into list of FilingRecord objects."""
    records = []

    # SEC API returns parallel arrays - zip them together
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    filing_dates = recent.get("filingDate", [])
    report_dates = recent.get("reportDate", [])
    primary_docs = recent.get("primaryDocument", [])
    descriptions = recent.get("primaryDocDescription", [])

    for i in range(len(accessions)):
        records.append(
            FilingRecord(
                accession_number=accessions[i] if i < len(accessions) else "",
                form=forms[i] if i < len(forms) else "",
                filing_date=filing_dates[i] if i < len(filing_dates) else "",
                report_date=report_dates[i] if i < len(report_dates) and report_dates[i] else None,
                primary_document=primary_docs[i]
                if i < len(primary_docs) and primary_docs[i]
                else None,
                description=descriptions[i] if i < len(descriptions) and descriptions[i] else None,
            )
        )

    return records


def _build_form_type_summary(records: list[FilingRecord]) -> list[FormTypeSummary]:
    """Build summary statistics by form type."""
    form_counts: Counter = Counter()
    form_dates: dict[str, list[str]] = {}

    for record in records:
        form_counts[record.form] += 1
        if record.form not in form_dates:
            form_dates[record.form] = []
        if record.filing_date:
            form_dates[record.form].append(record.filing_date)

    summaries = []
    for form_type, count in form_counts.most_common():
        dates = sorted(form_dates.get(form_type, []))
        summaries.append(
            FormTypeSummary(
                form_type=form_type,
                count=count,
                earliest_date=dates[0] if dates else None,
                latest_date=dates[-1] if dates else None,
            )
        )

    return summaries


async def get_company_submissions(
    request: GetCompanySubmissionsInput,
) -> GetCompanySubmissionsOutput:
    """Get SEC filings and submissions for a company with smart pagination."""
    try:
        cik = await resolve_cik(request.cik, request.ticker, request.name)
    except Exception as e:
        if is_connection_error(e) or is_timeout_error(e):
            raise ValueError(format_tool_error(e, "get_company_submissions")) from e
        raise

    try:
        # Enforce limits
        page_size = min(request.limit or 20, 50)  # Max 50 per page
        page = max(request.page or 1, 1)  # 1-indexed, minimum 1

        repo = get_repository()
        data = await repo.get_submissions(cik)
    except Exception as e:
        if is_connection_error(e) or is_timeout_error(e):
            raise ValueError(format_tool_error(e, "get_company_submissions")) from e
        raise

    # Extract company info
    company_name = data.get("name", "Unknown")
    sic = data.get("sic")
    sic_description = data.get("sicDescription")

    # Build all filing records from SEC API response
    all_records: list[FilingRecord] = []
    if "filings" in data and "recent" in data["filings"]:
        all_records = _build_filing_records(data["filings"]["recent"])

    # Apply form type filter if specified
    filter_description = None
    if request.form_types:
        form_types_upper = [ft.upper() for ft in request.form_types]
        all_records = [r for r in all_records if r.form.upper() in form_types_upper]
        filter_description = f"form_types: {', '.join(request.form_types)}"

    # Build summary (always computed)
    form_type_summary = _build_form_type_summary(all_records)
    total_filings = len(all_records)

    # Handle summary_only mode
    if request.summary_only:
        return GetCompanySubmissionsOutput(
            company_name=company_name,
            cik=cik.zfill(10),
            sic=sic,
            sic_description=sic_description,
            total_filings=total_filings,
            form_type_summary=form_type_summary,
            filings=[],  # Empty for summary mode
            pagination=None,
            filter_applied=filter_description,
        )

    # Paginate results
    total_pages = max(1, (total_filings + page_size - 1) // page_size)
    page = min(page, total_pages)  # Clamp to valid range

    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    page_records = all_records[start_idx:end_idx]

    pagination = PaginationInfo(
        page=page,
        page_size=page_size,
        total_items=total_filings,
        total_pages=total_pages,
        has_next=page < total_pages,
        has_previous=page > 1,
    )

    return GetCompanySubmissionsOutput(
        company_name=company_name,
        cik=cik.zfill(10),
        sic=sic,
        sic_description=sic_description,
        total_filings=total_filings,
        form_type_summary=form_type_summary,
        filings=page_records,
        pagination=pagination,
        filter_applied=filter_description,
    )


async def get_company_facts(request: GetCompanyFactsInput) -> GetCompanyFactsOutput:
    """Get all XBRL facts for a company from SEC filings."""
    try:
        cik = await resolve_cik(request.cik, request.ticker, request.name)
    except Exception as e:
        if is_connection_error(e) or is_timeout_error(e):
            raise ValueError(format_tool_error(e, "get_company_facts")) from e
        raise

    limit_concepts = min(request.limit_concepts or 50, 100)

    try:
        repo = get_repository()
        data = await repo.get_company_facts(cik)
    except Exception as e:
        if is_connection_error(e) or is_timeout_error(e):
            raise ValueError(format_tool_error(e, "get_company_facts")) from e
        raise

    # Limit number of concepts per taxonomy to prevent massive responses
    max_data_points = 20  # Limit time periods per concept
    if "facts" in data:
        for taxonomy in data["facts"]:
            if isinstance(data["facts"][taxonomy], dict):
                concepts = list(data["facts"][taxonomy].keys())
                if len(concepts) > limit_concepts:
                    # Keep only the first N concepts
                    limited = {k: data["facts"][taxonomy][k] for k in concepts[:limit_concepts]}
                    data["facts"][taxonomy] = limited
                    data["_truncated"] = True
                    data["_truncated_message"] = (
                        f"Response limited to {limit_concepts} concepts per taxonomy. "
                        f"Use get_company_concept for specific metrics."
                    )

                # Also limit data points per concept (time periods)
                for concept_name, concept_data in data["facts"][taxonomy].items():
                    if isinstance(concept_data, dict) and "units" in concept_data:
                        for unit_type, values in concept_data["units"].items():
                            if isinstance(values, list) and len(values) > max_data_points:
                                # Keep most recent data points
                                concept_data["units"][unit_type] = values[-max_data_points:]
                                data["_truncated"] = True

    return GetCompanyFactsOutput(data=data)


async def get_company_concept(request: GetCompanyConceptInput) -> GetCompanyConceptOutput:
    """Get specific XBRL concept data for a company across all filings.

    Accepts parameters directly (cik/ticker/name, taxonomy, tag) — FastMCP
    auto-unwraps the GetCompanyConceptInput model. Also callable with a
    pre-built GetCompanyConceptInput instance from meta-tools.
    """
    try:
        cik = await resolve_cik(request.cik, request.ticker, request.name)
    except Exception as e:
        if is_connection_error(e) or is_timeout_error(e):
            raise ValueError(format_tool_error(e, "get_company_concept")) from e
        raise

    try:
        repo = get_repository()
        data = await repo.get_company_concept(cik, request.taxonomy, request.tag)
    except Exception as e:
        if is_connection_error(e) or is_timeout_error(e):
            raise ValueError(format_tool_error(e, "get_company_concept")) from e
        raise

    return GetCompanyConceptOutput(data=data)


async def get_frames(request: GetFramesInput) -> GetFramesOutput:
    """Get aggregated XBRL data across all companies for a specific concept."""
    try:
        repo = get_repository()
        data = await repo.get_frames(request.taxonomy, request.tag, request.unit, request.period)
    except Exception as e:
        if is_connection_error(e) or is_timeout_error(e):
            raise ValueError(format_tool_error(e, "get_frames")) from e
        raise

    return GetFramesOutput(data=data)

"""Offline USPTO client implementation using local SQLite database."""

from __future__ import annotations

import asyncio
import copy
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from mcp_servers.uspto.api.client import OFFLINE_ERROR_RESPONSE
from mcp_servers.uspto.offline.db.connection import current_db_path, get_async_connection
from mcp_servers.uspto.utils.transform import transform_status_codes

# Path to shared status codes data file (project root data/ directory)
STATUS_CODES_DATA_FILE = (
    Path(__file__).parent.parent.parent.parent / "data" / "uspto" / "status_codes.json"
)

DESCRIPTION_HEADINGS: list[tuple[str, str]] = [
    (r"field of the invention", "Field of the Invention"),
    (r"background(?: of the invention)?", "Background"),
    (r"summary(?: of the invention)?", "Summary"),
    (r"brief description of (?:the )?drawings?", "Brief Description of the Drawings"),
    (r"detailed description(?: of the invention)?", "Detailed Description"),
]

CLAIM_START_RE = re.compile(r"^\s*(\d+)[\.\)]\s*(.*)")


def _offline_error() -> dict[str, Any]:
    """Return a deep copy of the offline-mode error payload."""
    return copy.deepcopy(OFFLINE_ERROR_RESPONSE)


class OfflineUSPTOClient:
    """USPTO client that queries local SQLite database for offline mode.

    Implements the USPTOClient protocol interface for offline database access.
    """

    async def aclose(self) -> None:
        """No-op; nothing to clean up."""
        pass

    async def search_applications(
        self,
        query: str,
        filters: dict[str, Any] | None = None,
        start: int = 0,
        rows: int = 25,
        sort: str | None = None,
    ) -> dict[str, Any]:
        """Search published applications and issued patents using FTS5.

        Args:
            query: Full-text search query string
            filters: Optional filters (document_type, filing_date, CPC, assignee, inventor)
            start: Pagination offset (default: 0)
            rows: Number of results to return (default: 25)
            sort: Sort order - "relevance" (default), "filing_date", "publication_date"

        Returns:
            dict with 'results', 'total', and 'db_last_updated' keys
        """
        # Validate query - allow empty string but reject whitespace-only
        stripped_query = query.strip() if query else ""
        if query and not stripped_query:
            return {
                "error": {
                    "code": "INVALID_QUERY",
                    "message": "Search query cannot be empty",
                }
            }

        # Check if database is available
        if not current_db_path():
            return _offline_error()

        try:
            import sqlite3

            from mcp_servers.uspto.offline.repository.patent_repository import PatentRepository

            # Make a copy to avoid mutating caller's dictionary
            filters = dict(filters) if filters else {}

            # Default to grants only unless explicitly overridden
            if "document_type" not in filters:
                filters["document_type"] = "grant"

            # Extract include_application flag (not a repository filter)
            include_application = filters.pop("include_application", False)

            def _search():
                with sqlite3.connect(current_db_path()) as conn:
                    repo = PatentRepository(conn)
                    return repo.search(stripped_query, filters, start, rows, sort)

            # Run repository search in thread pool
            search_result = await asyncio.to_thread(_search)

            # Format results to match online API structure
            formatted_results = [
                self._format_patent_result(row) for row in search_result["results"]
            ]

            # If include_application=True, fetch related application for each grant
            if include_application:
                for result in formatted_results:
                    if result["documentType"] == "grant":
                        app_num = result["applicationNumberText"]
                        try:
                            # Fetch the application form (pre-grant publication)
                            app_result = await self._fetch_patent_details(app_num, "application")
                            if app_result:
                                # Format complete application details
                                app_formatted = self._format_complete_application(
                                    app_result["patent"],
                                    app_result["inventors"],
                                    app_result["assignees"],
                                    app_result["cpc_codes"],
                                    app_result["citations"],
                                    app_result["examiners"],
                                )
                                result["relatedApplication"] = app_formatted
                        except Exception:
                            # Silently skip if related application fetch fails
                            pass

            return {
                "results": formatted_results,
                "total": search_result["total"],
                "db_last_updated": search_result["db_last_updated"],
            }

        except Exception as e:
            # Handle FTS5 syntax errors
            error_str = str(e).lower()
            if "fts5" in error_str or "syntax" in error_str or "malformed" in error_str:
                return {
                    "error": {
                        "code": "INVALID_FTS5_SYNTAX",
                        "message": f"Invalid search query syntax: {e}",
                    }
                }
            # Handle all other database errors gracefully (consistent with get_application)
            return {
                "error": {
                    "code": "DATABASE_ERROR",
                    "message": f"Error searching database: {str(e)}",
                }
            }

    async def get_application(self, application_number: str) -> dict[str, Any]:
        """Retrieve complete application details from local database.

        Args:
            application_number: Application number to retrieve

        Returns:
            dict with complete patent details matching online API structure
        """
        # Check if database is available
        if not current_db_path():
            return _offline_error()

        # Normalize application number (digits only, matching online client)
        normalized_app_num = "".join(ch for ch in application_number if ch.isdigit())
        if not normalized_app_num:
            normalized_app_num = application_number  # Fall back to original if no digits

        try:
            # Try grant first (matches USPTO API behavior - grants are preferred)
            result = await self._fetch_patent_details(normalized_app_num, "grant")

            # Fall back to application if grant not found
            if not result:
                result = await self._fetch_patent_details(normalized_app_num, "application")

            if not result:
                # Application not found
                return {
                    "error": {
                        "code": "APPLICATION_NOT_FOUND",
                        "message": (
                            f"Application {application_number} not found in offline database"
                        ),
                    }
                }

            # Format the complete response
            return self._format_complete_application(
                result["patent"],
                result["inventors"],
                result["assignees"],
                result["cpc_codes"],
                result["citations"],
                result["examiners"],
            )

        except Exception as e:
            # Log error and return error response
            return {
                "error": {
                    "code": "DATABASE_ERROR",
                    "message": f"Error retrieving application: {str(e)}",
                }
            }

    async def get_status_codes(self) -> dict[str, Any]:
        """Return the status code reference table from static JSON file.

        Loads all 241 USPTO status codes from the offline data file and
        applies the same transformation as the online client for consistency.

        Returns:
            dict with 'statusCodes', 'count', 'version', and 'raw_uspto_response' keys
        """
        try:
            # Load status codes from static JSON file
            def _load_status_codes() -> dict[str, Any]:
                if not STATUS_CODES_DATA_FILE.exists():
                    raise FileNotFoundError(
                        f"Status codes data file not found: {STATUS_CODES_DATA_FILE}"
                    )
                with open(STATUS_CODES_DATA_FILE) as f:
                    return json.load(f)

            # Run file I/O in thread pool to avoid blocking event loop
            raw_data = await asyncio.to_thread(_load_status_codes)

            # Apply same transformation as online client for consistency
            return transform_status_codes(raw_data)

        except FileNotFoundError as e:
            logger.error(f"Status codes file not found: {e}")
            return {
                "error": {
                    "code": "DATA_FILE_NOT_FOUND",
                    "message": "Status codes data file not found in offline database",
                }
            }
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in status codes file: {e}")
            return {
                "error": {
                    "code": "INVALID_JSON",
                    "message": f"Failed to parse status codes data file: {e}",
                }
            }
        except Exception as e:
            logger.error(f"Error loading status codes: {e}")
            return {
                "error": {
                    "code": "DATABASE_ERROR",
                    "message": f"Error loading status codes: {str(e)}",
                }
            }

    async def get_documents(
        self,
        application_number: str,
        start: int = 0,
        rows: int = 100,
    ) -> dict[str, Any]:
        """Return document inventory for a patent application.

        In offline mode, this returns an empty document list with a message
        indicating that prosecution documents (fees, office actions, amendments)
        are not available offline.

        Note: Drawing metadata from XML is not currently stored in the offline
        database. Future enhancement could include drawing metadata extraction
        during ingestion.

        Args:
            application_number: Patent application number
            start: Starting offset for pagination
            rows: Maximum number of results to return

        Returns:
            dict: Document inventory response matching online API format with
                  offline mode indicators
        """
        # Check if database is available
        if not current_db_path():
            return _offline_error()

        try:
            from mcp_servers.uspto.offline.repository.documents_repository import (
                DocumentsRepository,
            )

            # Use the repository to get documents (currently returns empty with message)
            repo = DocumentsRepository()
            return await repo.get_documents(application_number, start, rows)
        except Exception as e:
            # Handle database errors gracefully (consistent with search_applications)
            return {
                "error": {
                    "code": "DATABASE_ERROR",
                    "message": f"Error querying database: {str(e)}",
                }
            }

    async def get_foreign_priority(self, application_number: str) -> dict[str, Any]:
        """Return foreign priority claims from offline database.

        Args:
            application_number: USPTO application number

        Returns:
            Dictionary with foreignPriorityClaims array matching online API format
        """
        # Check if database is available
        if not current_db_path():
            return _offline_error()

        from mcp_servers.uspto.offline.repository.foreign_priority_repository import (
            ForeignPriorityRepository,
        )

        # Use repository to query foreign priority claims
        repo = ForeignPriorityRepository()

        # Run synchronous repository method in thread pool to avoid blocking event loop
        return await asyncio.to_thread(repo.get_foreign_priority, application_number)

    async def generate_patent_pdf(self, application_number: str) -> dict[str, Any]:
        """Generate a text-only patent PDF from offline database content."""
        if not current_db_path():
            return _offline_error()

        application_data = await self.get_application(application_number)
        if "error" in application_data:
            return application_data

        try:
            from mcp_servers.uspto.services.pdf_generator import generate_uspto_pdf

            pdf_bytes = await asyncio.to_thread(generate_uspto_pdf, application_data)
        except Exception as exc:
            logger.error(f"PDF generation failed: {exc}")
            return {
                "error": {
                    "code": "PDF_GENERATION_FAILED",
                    "message": "Failed to generate patent PDF",
                    "details": {"reason": str(exc)},
                }
            }

        generated_at = _ensure_utc_timestamp()
        safe_app = _safe_filename_component(application_number)
        filename = f"patent_{safe_app or 'unknown'}.pdf"

        return {
            "applicationNumber": application_number,
            "generatedAt": generated_at,
            "contentType": "application/pdf",
            "fileName": filename,
            "textOnly": True,
            "byteSize": len(pdf_bytes),
            "note": (
                "Text-only offline PDF generated from local database content. "
                "Drawings and images are not included."
            ),
            "pdfBytes": pdf_bytes,
        }

    async def ping(self) -> bool:
        """Check database availability.

        Returns:
            True if database is available and accessible, False otherwise
        """
        try:
            if not current_db_path():
                return False

            async with get_async_connection() as conn:
                async with conn.execute("SELECT 1") as cursor:
                    result = await cursor.fetchone()
                    return result is not None
        except Exception:
            return False

    async def _fetch_patent_details(
        self, application_number: str, document_type: str
    ) -> dict[str, Any] | None:
        """Fetch complete patent details by application number and document type.

        Args:
            application_number: Application number to fetch
            document_type: Document type ("application" or "grant")

        Returns:
            Dict with patent and related data, or None if not found

        Raises:
            Exception: Database errors are propagated to caller
        """
        import sqlite3

        from mcp_servers.uspto.offline.repository.assignee_repository import (
            AssigneeRepository,
        )
        from mcp_servers.uspto.offline.repository.citation_repository import (
            CitationRepository,
        )
        from mcp_servers.uspto.offline.repository.cpc_repository import CPCRepository
        from mcp_servers.uspto.offline.repository.examiner_repository import (
            ExaminerRepository,
        )
        from mcp_servers.uspto.offline.repository.inventor_repository import (
            InventorRepository,
        )
        from mcp_servers.uspto.offline.repository.patent_repository import PatentRepository

        def _get_patent():
            with sqlite3.connect(current_db_path()) as conn:
                # Get patent record by application_number + document_type
                patent_repo = PatentRepository(conn)
                patent_record = patent_repo.get_by_application_number(
                    application_number, document_type
                )

                if not patent_record:
                    return None

                # Get patent_id for related data queries
                patent_id = patent_record.id

                # Orchestrate repository calls for related data
                inventor_repo = InventorRepository(conn)
                assignee_repo = AssigneeRepository(conn)
                cpc_repo = CPCRepository(conn)
                citation_repo = CitationRepository(conn)
                examiner_repo = ExaminerRepository(conn)

                inventors_models = inventor_repo.get_by_patent_id(patent_id)
                assignees_models = assignee_repo.get_by_patent_id(patent_id)
                cpc_models = cpc_repo.get_by_patent_id(patent_id)
                citations_models = citation_repo.get_by_patent_id(patent_id)
                examiners_models = examiner_repo.get_by_patent_id(patent_id)

                # Convert Pydantic models to dicts for formatting
                dump_kwargs = {"by_alias": True, "mode": "json"}
                return {
                    "patent": patent_record.model_dump(**dump_kwargs),
                    "inventors": [inv.model_dump(**dump_kwargs) for inv in inventors_models],
                    "assignees": [asg.model_dump(**dump_kwargs) for asg in assignees_models],
                    "cpc_codes": [cpc.model_dump(**dump_kwargs) for cpc in cpc_models],
                    "citations": [cit.model_dump(**dump_kwargs) for cit in citations_models],
                    "examiners": [exam.model_dump(**dump_kwargs) for exam in examiners_models],
                }

        # Run repository orchestration in thread pool
        return await asyncio.to_thread(_get_patent)

    def _format_patent_result(self, row: Any) -> dict[str, Any]:
        """Format a database row into online API result format.

        Args:
            row: Database row (aiosqlite.Row)

        Returns:
            dict matching online API result structure (camelCase field names)
        """

        # Helper to safely get values from Row objects (which don't have .get())
        def safe_get(key: str) -> Any:
            try:
                return row[key]
            except (KeyError, IndexError):
                return None

        # Format firstNamedApplicant as object to match online API
        # Use assignee as first named applicant (matches USPTO live API behavior)
        first_assignee = safe_get("first_assignee_name")
        first_assignee_role = safe_get("first_assignee_role")
        first_assignee_country = safe_get("first_assignee_country")

        # Parse applicants_json for orgname and as fallback when no assignees
        applicants_json_str = safe_get("applicants_json")
        applicant_orgname = None
        fallback_applicant = None
        if applicants_json_str:
            try:
                import json

                applicants = json.loads(applicants_json_str)
                if applicants and isinstance(applicants, list) and len(applicants) > 0:
                    first_applicant = applicants[0]
                    applicant_orgname = first_applicant.get("orgname")
                    # Store for fallback use
                    fallback_applicant = first_applicant
            except (json.JSONDecodeError, TypeError, KeyError, AttributeError):
                pass

        # Use assignee from assignees table, fallback to applicants_json if no assignees
        if not first_assignee and fallback_applicant:
            first_assignee = fallback_applicant.get("name")
            first_assignee_country = fallback_applicant.get("country")
            first_assignee_role = fallback_applicant.get("role")  # May be None if not present

        first_applicant_obj = None
        if first_assignee:
            first_applicant_obj = {
                "name": first_assignee,
                "applicantName": first_assignee,
                "country": first_assignee_country,
                "role": first_assignee_role,
                "organization": applicant_orgname,
            }

        # Parse priority_claims_json
        priority_claims_str = safe_get("priority_claims_json")
        priority_claims = None
        if priority_claims_str:
            try:
                import json

                parsed = json.loads(priority_claims_str)
                if parsed and isinstance(parsed, list) and len(parsed) > 0:
                    priority_claims = parsed
            except (json.JSONDecodeError, TypeError):
                pass

        # Parse related_applications_json and split into parent/child continuity
        related_applications_str = safe_get("related_applications_json")
        parent_continuity = None
        child_continuity = None
        if related_applications_str:
            try:
                import json

                parsed = json.loads(related_applications_str)
                if parsed and isinstance(parsed, list):
                    # Split into parent and child based on relationship_type
                    parents = []
                    children = []
                    for rel_app in parsed:
                        if not isinstance(rel_app, dict):
                            continue
                        # Determine relationship type - compute if not present
                        rel_type = rel_app.get("relationship_type")
                        if not rel_type:
                            # Compute from _element_name and _parent_name
                            element_name = rel_app.get("_element_name")
                            parent_name = rel_app.get("_parent_name")
                            if element_name == "relation":
                                rel_type = parent_name or "related"
                            else:
                                rel_type = element_name or "related"

                        # Filter out null/empty values from the relationship object
                        filtered_rel = {
                            k: v for k, v in rel_app.items() if v is not None and v != ""
                        }

                        # Parent relationships: continuation, division, CIP, reissue, provisional
                        # Child relationships: related-publication
                        if rel_type == "related-publication":
                            children.append(filtered_rel)
                        else:
                            # All others are parent relationships
                            parents.append(filtered_rel)

                    if parents:
                        parent_continuity = parents
                    if children:
                        child_continuity = children
            except (json.JSONDecodeError, TypeError):
                pass

        return {
            "applicationNumberText": row["application_number"],
            "inventionTitle": row["title"],
            "documentType": row["document_type"],  # "application" or "grant"
            "applicationType": row["application_type"],
            "filingDate": row["filing_date"],
            "publicationDate": row["publication_date"],
            "publicationNumber": row["publication_number"],
            "applicationStatusCode": None,  # Not available in offline database
            "applicationStatusDescriptionText": None,  # Not available in offline database
            "patentNumber": row["patent_number"],
            "patentIssueDate": row["issue_date"],
            "firstNamedApplicant": first_applicant_obj,
            "assigneeEntityName": first_assignee,
            "foreignPriorityClaims": priority_claims,
            "parentContinuity": parent_continuity,
            "childContinuity": child_continuity,
        }

    def _format_complete_application(
        self,
        patent_row: Any,
        inventors: list[Any],
        assignees: list[Any],
        cpc_codes: list[Any],
        citations: list[Any],
        examiners: list[Any],
    ) -> dict[str, Any]:
        """Format complete application data into online API format.

        Args:
            patent_row: Main patent database row
            inventors: List of inventor rows
            assignees: List of assignee rows
            cpc_codes: List of CPC classification rows
            citations: List of citation rows
            examiners: List of examiner rows

        Returns:
            dict with complete patent details matching online API structure
        """

        # Helper to safely get values from Row objects
        def safe_get(key: str) -> Any:
            try:
                return patent_row[key]
            except (KeyError, IndexError):
                return None

        # Helper to parse JSON fields
        def parse_json_field(field_name: str) -> Any:
            value = safe_get(field_name)
            if value:
                try:
                    return json.loads(value)
                except (json.JSONDecodeError, TypeError):
                    return None
            return None

        # Format inventors array
        inventors_list = []
        for inv in inventors:
            inventors_list.append(
                {
                    "firstName": inv["first_name"],
                    "lastName": inv["last_name"],
                    "fullName": inv["full_name"],
                    "city": inv["city"],
                    "state": inv["state"],
                    "country": inv["country"],
                    "sequence": inv["sequence"],
                }
            )

        # Format assignees array
        assignees_list = []
        for asg in assignees:
            assignees_list.append(
                {
                    "name": asg["name"],
                    "role": asg["role"],
                    "city": asg["city"],
                    "state": asg["state"],
                    "country": asg["country"],
                }
            )

        # Format CPC classifications
        cpc_list = []
        for cpc in cpc_codes:
            cpc_list.append(
                {
                    "section": cpc["section"],
                    "class": cpc["class"],
                    "subclass": cpc["subclass"],
                    "mainGroup": cpc["main_group"],
                    "subGroup": cpc["subgroup"],
                    "isMain": bool(cpc["is_main"]),
                }
            )

        # Format patent citations
        citations_list = []
        for citation in citations:
            citations_list.append(
                {
                    "citedPatentNumber": citation["cited_patent_number"],
                    "citedCountry": citation["cited_country"],
                    "citedKind": citation["cited_kind"],
                    "citedDate": citation["cited_date"],
                    "category": citation["category"],
                }
            )

        # Format examiners
        primary_examiner = None
        assistant_examiner = None
        examiner_name = None
        group_art_unit = None

        for examiner in examiners:
            examiner_obj = {
                "firstName": examiner["first_name"],
                "lastName": examiner["last_name"],
                "department": examiner["department"],
            }
            if examiner["examiner_type"] == "primary":
                primary_examiner = examiner_obj
                # Format examiner name for compatibility
                if examiner["first_name"] and examiner["last_name"]:
                    examiner_name = f"{examiner['first_name']} {examiner['last_name']}"
                elif examiner["last_name"]:
                    examiner_name = examiner["last_name"]
                # Group art unit from department
                group_art_unit = examiner["department"]
            elif examiner["examiner_type"] == "assistant":
                assistant_examiner = examiner_obj

        # Get first inventor and assignee for compatibility fields
        first_inventor_name = None
        if inventors_list:
            first_inv = inventors_list[0]
            first_inventor_name = first_inv.get("fullName")

        # Parse JSON fields first to enable fallback
        applicants_json = parse_json_field("applicants_json")

        # Extract orgname from applicants_json for organization field
        applicant_orgname = None
        if applicants_json and isinstance(applicants_json, list) and len(applicants_json) > 0:
            applicant_orgname = applicants_json[0].get("orgname")

        assignee_entity_name = None
        first_applicant_name = None
        first_applicant_obj = None
        if assignees_list:
            first_asg = assignees_list[0]
            assignee_entity_name = first_asg.get("name")
            first_applicant_name = first_asg.get("name")
            # Only create object if name exists
            if first_applicant_name:
                first_applicant_obj = {
                    "name": first_applicant_name,
                    "applicantName": first_applicant_name,
                    "country": first_asg.get("country"),
                    "role": first_asg.get("role"),
                    "organization": applicant_orgname,
                }
        elif applicants_json and isinstance(applicants_json, list) and len(applicants_json) > 0:
            # Fallback to applicants_json when no assignees (consistent with search behavior)
            first_applicant = applicants_json[0]
            first_applicant_name = first_applicant.get("orgname") or first_applicant.get("name")
            assignee_entity_name = first_applicant_name
            if first_applicant_name:
                first_applicant_obj = {
                    "name": first_applicant_name,
                    "applicantName": first_applicant_name,
                    "country": first_applicant.get("country"),
                    "role": first_applicant.get("role"),  # May be None for applicants_json
                    "organization": applicant_orgname,
                }
        attorneys_json = parse_json_field("attorneys_json")
        ipc_codes_json = parse_json_field("ipc_codes_json")
        uspc_codes_json = parse_json_field("uspc_codes_json")
        priority_claims_json = parse_json_field("priority_claims_json")
        npl_citations_json = parse_json_field("npl_citations_json")
        pct_filing_data_json = parse_json_field("pct_filing_data_json")
        locarno_classification = parse_json_field("locarno_classification")

        # Split related_applications_json into parent and child continuity
        related_applications_json = parse_json_field("related_applications_json")
        parent_continuity_json = None
        child_continuity_json = None
        if related_applications_json and isinstance(related_applications_json, list):
            parents = []
            children = []
            for rel_app in related_applications_json:
                if not isinstance(rel_app, dict):
                    continue
                # Determine relationship type - compute if not present
                rel_type = rel_app.get("relationship_type")
                if not rel_type:
                    # Compute from _element_name and _parent_name
                    element_name = rel_app.get("_element_name")
                    parent_name = rel_app.get("_parent_name")
                    if element_name == "relation":
                        rel_type = parent_name or "related"
                    else:
                        rel_type = element_name or "related"

                # Filter out null/empty values from the relationship object
                filtered_rel = {k: v for k, v in rel_app.items() if v is not None and v != ""}

                # Parent relationships: continuation, division, CIP, reissue, provisional
                # Child relationships: related-publication
                if rel_type == "related-publication":
                    children.append(filtered_rel)
                else:
                    parents.append(filtered_rel)
            if parents:
                parent_continuity_json = parents
            if children:
                child_continuity_json = children

        # Extract USPC class/subclass from JSON
        uspc_class = None
        uspc_subclass = None
        if uspc_codes_json and isinstance(uspc_codes_json, list) and len(uspc_codes_json) > 0:
            first_uspc = uspc_codes_json[0]
            if isinstance(first_uspc, dict):
                # Handle null values in JSON by using 'or ""' instead of default parameter
                code = first_uspc.get("code") or ""
                if "/" in code:
                    parts = code.split("/")
                    uspc_class = parts[0].strip()
                    uspc_subclass = parts[1].strip() if len(parts) > 1 else None

        # Build comprehensive response
        return {
            # Core bibliographic data
            "applicationNumberText": patent_row["application_number"],
            "inventionTitle": patent_row["title"],
            "applicationType": patent_row["application_type"],
            "filingDate": patent_row["filing_date"],
            "publicationDate": patent_row["publication_date"],
            "publicationNumber": patent_row["publication_number"],
            "patentNumber": patent_row["patent_number"],
            "patentIssueDate": patent_row["issue_date"],
            "kindCode": patent_row["kind_code"],
            "country": patent_row["country"],
            "documentType": patent_row["document_type"],
            # Status fields (not available offline)
            "applicationStatusCode": None,
            "applicationStatusDescriptionText": None,
            "entityStatus": None,  # Not available in offline database
            "confidential": None,  # Not available in offline database
            # Full text content
            "abstract": patent_row["abstract"],
            "description": patent_row["description"],
            "claims": patent_row["claims"],
            # Parties - compatibility fields (single values)
            "firstInventorName": first_inventor_name,
            "firstApplicantName": first_applicant_name,
            "firstNamedApplicant": first_applicant_obj,
            "assigneeEntityName": assignee_entity_name,
            "examinerName": examiner_name,
            "groupArtUnitNumber": group_art_unit,
            # Parties - complete arrays
            "inventors": inventors_list,
            "assignees": assignees_list,
            "applicants": applicants_json,
            "attorneys": attorneys_json,
            # Examiners
            "primaryExaminer": primary_examiner,
            "assistantExaminer": assistant_examiner,
            # Classifications
            "cpcClassifications": cpc_list,
            "ipcCodes": ipc_codes_json,
            "uspcClass": uspc_class,
            "uspcSubclass": uspc_subclass,
            "uspcCodes": uspc_codes_json,
            "locarnoClassification": locarno_classification,
            # Citations
            "patentCitations": citations_list,
            "nplCitations": npl_citations_json,
            # Related applications and priorities
            "foreignPriorityClaims": priority_claims_json,
            "parentContinuity": parent_continuity_json,
            "childContinuity": child_continuity_json,
            "pctFilingData": pct_filing_data_json,
            # Grant-specific metadata
            "termOfGrant": patent_row["term_of_grant"],
            "numberOfClaims": patent_row["number_of_claims"],
            "numberOfFigures": patent_row["number_of_figures"],
            "numberOfDrawingSheets": patent_row["number_of_drawing_sheets"],
            # Metadata
            "xmlFileName": patent_row["xml_file_name"],
            "ingestionDate": patent_row["ingestion_date"],
            "db_last_updated": patent_row["ingestion_date"],
        }


def _ensure_utc_timestamp() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _safe_filename_component(value: str) -> str:
    """Return a filesystem-safe identifier for filenames."""
    return "".join(ch for ch in value if ch.isalnum())


def _coerce_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _format_location(city: str | None, state: str | None, country: str | None) -> str:
    parts = [part for part in (city, state, country) if part]
    return ", ".join(parts)


def _format_people(items: list[dict[str, Any]]) -> list[str]:
    lines = []
    for item in items:
        if isinstance(item, str):
            lines.append(item)
            continue
        if not isinstance(item, dict):
            continue
        name = (
            item.get("fullName")
            or item.get("name")
            or item.get("applicantName")
            or item.get("organization")
            or " ".join(
                part for part in (item.get("firstName"), item.get("lastName")) if part
            ).strip()
        )
        location = _format_location(item.get("city"), item.get("state"), item.get("country"))
        if name and location:
            lines.append(f"{name} ({location})")
        elif name:
            lines.append(name)
    return lines


def _format_assignees(items: list[dict[str, Any]]) -> list[str]:
    lines = []
    for item in items:
        if isinstance(item, str):
            lines.append(item)
            continue
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        location = _format_location(item.get("city"), item.get("state"), item.get("country"))
        if name and location:
            lines.append(f"{name} ({location})")
        elif name:
            lines.append(name)
    return lines


def _format_cpc_codes(items: list[dict[str, Any]]) -> list[str]:
    codes = []
    for item in items:
        section = item.get("section")
        cls = item.get("class")
        subclass = item.get("subclass")
        main_group = item.get("mainGroup")
        sub_group = item.get("subGroup")
        if section and cls and subclass and main_group and sub_group:
            codes.append(f"{section}{cls}{subclass} {main_group}/{sub_group}")
    return codes


def _format_ipc_codes(items: list[Any] | None) -> list[str]:
    if not items:
        return []
    codes = []
    for item in items:
        if isinstance(item, dict):
            value = item.get("code") or item.get("ipc") or item.get("ipc_code")
            if value:
                codes.append(str(value))
        elif isinstance(item, str):
            codes.append(item)
    return codes


def _format_uspc_codes(items: list[Any] | None) -> list[str]:
    if not items:
        return []
    codes = []
    for item in items:
        if isinstance(item, dict):
            value = item.get("code") or item.get("uspc") or item.get("uspc_code")
            if value:
                codes.append(str(value))
        elif isinstance(item, str):
            codes.append(item)
    return codes


def _extract_description_sections(description: str | None) -> dict[str, str]:
    if not description:
        return {}

    pattern = r"(?im)^\s*(" + "|".join(key for key, _ in DESCRIPTION_HEADINGS) + r")\s*$"
    matches = []
    for match in re.finditer(pattern, description):
        key = match.group(1).strip().lower()
        title = match.group(1).strip().title()
        for regex_pattern, candidate_title in DESCRIPTION_HEADINGS:
            if re.fullmatch(regex_pattern, key, flags=re.IGNORECASE):
                title = candidate_title
                break
        matches.append((match.start(), match.end(), title))

    if not matches:
        return {"Detailed Description": description.strip()}

    matches.sort(key=lambda item: item[0])
    sections: dict[str, str] = {}
    for index, (start, end, title) in enumerate(matches):
        next_start = matches[index + 1][0] if index + 1 < len(matches) else len(description)
        section_text = description[end:next_start].strip()
        if section_text:
            sections[title] = section_text

    return sections or {"Detailed Description": description.strip()}


def _parse_claims(claims_text: str | None) -> list[tuple[str | None, str]]:
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
            current_lines = [match.group(2)]
        else:
            current_lines.append(line)

    if current_lines:
        claims.append((current_number, "\n".join(current_lines).strip()))

    return claims


def _format_priority_claims(items: list[Any] | None) -> list[str]:
    if not items:
        return []
    lines = []
    for item in items:
        if isinstance(item, dict):
            country = item.get("country") or item.get("ipOfficeCode")
            doc_number = item.get("doc_number") or item.get("foreignApplicationNumber")
            date_value = item.get("date") or item.get("foreignFilingDate")
            parts = [part for part in (country, doc_number, date_value) if part]
            if parts:
                lines.append(" - ".join(parts))
        elif isinstance(item, str):
            lines.append(item)
    return lines


def _format_related_applications(items: list[Any] | None) -> list[str]:
    if not items:
        return []
    lines = []
    for item in items:
        if isinstance(item, dict):
            app_number = item.get("application_number") or item.get("applicationNumber")
            relation = item.get("relation_type") or item.get("relationType")
            if app_number and relation:
                lines.append(f"{app_number} ({relation})")
            elif app_number:
                lines.append(str(app_number))
        elif isinstance(item, str):
            lines.append(item)
    return lines


def _format_npl_citations(items: list[Any] | None) -> list[str]:
    if not items:
        return []
    lines = []
    for item in items:
        if isinstance(item, dict):
            text = item.get("text") or item.get("citation") or item.get("npl")
            if text:
                lines.append(str(text))
        elif isinstance(item, str):
            lines.append(item)
    return lines


__all__ = ["OfflineUSPTOClient"]

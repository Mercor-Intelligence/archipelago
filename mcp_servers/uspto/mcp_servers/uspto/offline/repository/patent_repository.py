"""Patent repository for USPTO offline mode."""

from __future__ import annotations

from typing import Any

from mcp_servers.uspto.offline.models import PatentRecord
from mcp_servers.uspto.offline.repository.base import BaseRepository


class PatentRepository(BaseRepository):
    """Repository for managing patent data."""

    def get_by_application_number(self, app_num: str, document_type: str) -> PatentRecord | None:
        """Get a patent by application number and document type.

        Args:
            app_num: Application number to query
            document_type: Document type ("application" or "grant")

        Returns:
            PatentRecord instance or None if not found
        """
        query = """
            SELECT * FROM patents
            WHERE application_number = ? AND document_type = ?
        """
        row = self.fetch_one(query, (app_num, document_type))
        return PatentRecord(**row) if row else None

    def search(
        self,
        query: str,
        filters: dict[str, Any],
        start: int = 0,
        rows: int = 25,
        sort: str | None = None,
    ) -> dict[str, Any]:
        """Search patents using FTS5 full-text search.

        Args:
            query: FTS5 search query (empty string means query all patents)
            filters: Filters (application_number, document_type, filing_date, CPC, assignee,
                inventor)
            start: Offset for pagination
            rows: Number of results to return
            sort: Sort order - "relevance" (default), "filing_date", "publication_date"

        Returns:
            dict with 'results' (list of rows), 'total' (int), 'db_last_updated' (str|None)
        """
        # Build FTS5 query
        sql_query, params = self._build_search_query(query, filters, start, rows, sort)

        # Execute search
        result_rows = self.fetch_all(sql_query, tuple(params))

        # Get total count
        count_query, count_params = self._build_count_query(query, filters)
        count_result = self.fetch_one(count_query, tuple(count_params))
        total = count_result["count"] if count_result else 0

        # Get last update timestamp from fetched results
        db_last_updated = None
        if result_rows:
            ingestion_dates = []
            for row in result_rows:
                if row.get("ingestion_date"):
                    ingestion_dates.append(row["ingestion_date"])
            if ingestion_dates:
                db_last_updated = max(ingestion_dates)

        return {
            "results": result_rows,
            "total": total,
            "db_last_updated": db_last_updated,
        }

    def count_total(self, query: str, filters: dict[str, Any]) -> int:
        """Count total results for a search query.

        Args:
            query: FTS5 search query (empty string means query all patents)
            filters: Filters to apply

        Returns:
            Total count of matching patents
        """
        count_query, count_params = self._build_count_query(query, filters)
        count_result = self.fetch_one(count_query, tuple(count_params))
        return count_result["count"] if count_result else 0

    def _build_filter_clauses(self, filters: dict[str, Any]) -> tuple[str, list[Any], list[str]]:
        """Build SQL WHERE clauses and FTS MATCH terms for filters.

        Args:
            filters: Filters to apply

        Returns:
            Tuple of (WHERE clauses string, parameters list, FTS MATCH terms list)
        """
        clauses = []
        params: list[Any] = []
        fts_match_terms: list[str] = []

        # Application number filter (normalize to digits only for consistency)
        if filters.get("application_number"):
            # Normalize application number (digits only, matching get_application behavior)
            # Convert to string first to handle numeric values from JSON
            app_num = str(filters["application_number"])
            normalized_app_num = "".join(ch for ch in app_num if ch.isdigit())
            if not normalized_app_num:
                normalized_app_num = app_num  # Fall back to original if no digits
            clauses.append("p.application_number = ?")
            params.append(normalized_app_num)

        # Document type filter
        if filters.get("document_type"):
            clauses.append("p.document_type = ?")
            params.append(filters["document_type"])

        # Application type filter
        if filters.get("application_type"):
            clauses.append("p.application_type = ?")
            params.append(filters["application_type"])

        # Country filter
        if filters.get("country"):
            clauses.append("p.country = ?")
            params.append(filters["country"])

        # Date range filters
        if filters.get("filing_date_from"):
            clauses.append("p.filing_date >= ?")
            params.append(filters["filing_date_from"])

        if filters.get("filing_date_to"):
            clauses.append("p.filing_date <= ?")
            params.append(filters["filing_date_to"])

        if filters.get("publication_date_from"):
            clauses.append("p.publication_date >= ?")
            params.append(filters["publication_date_from"])

        if filters.get("publication_date_to"):
            clauses.append("p.publication_date <= ?")
            params.append(filters["publication_date_to"])

        # FTS filters - add to MATCH terms (not WHERE clauses)
        # Note: FTS table is contentless, so columns are NULL. Must use MATCH syntax.
        if filters.get("cpc_code"):
            # Escape any special FTS5 characters and use column-specific search
            cpc_code = filters["cpc_code"].replace('"', '""')
            fts_match_terms.append(f'cpc_codes:"{cpc_code}"')

        if filters.get("assignee"):
            # Escape any special FTS5 characters and use column-specific search
            assignee = filters["assignee"].replace('"', '""')
            fts_match_terms.append(f'assignees:"{assignee}"')

        if filters.get("inventor"):
            # Escape any special FTS5 characters and use column-specific search
            inventor = filters["inventor"].replace('"', '""')
            fts_match_terms.append(f'inventors:"{inventor}"')

        # Combine clauses with AND
        where_clause = ""
        if clauses:
            where_clause = " AND " + " AND ".join(clauses)

        return where_clause, params, fts_match_terms

    def _build_search_query(
        self,
        query: str,
        filters: dict[str, Any],
        start: int,
        rows: int,
        sort: str | None,
    ) -> tuple[str, list[Any]]:
        """Build FTS5 search query with filters, sorting, and pagination.

        Args:
            query: FTS5 search query (empty string means query all patents)
            filters: Filters to apply
            start: Offset for pagination
            rows: Limit for pagination
            sort: Sort order

        Returns:
            Tuple of (SQL query string, parameters list)
        """
        params: list[Any] = []

        # Get filter clauses and FTS match terms
        filter_clause, filter_params, fts_match_terms = self._build_filter_clauses(filters)

        # Build combined FTS MATCH expression
        fts_match_expr = query
        if fts_match_terms:
            if query:
                # Combine main query with FTS filter terms using AND
                # Wrap query in parens to preserve OR operator precedence
                fts_match_expr = f"({query}) AND {' AND '.join(fts_match_terms)}"
            else:
                # Only FTS filters, no main query
                fts_match_expr = " AND ".join(fts_match_terms)

        if fts_match_expr:
            # FTS5 query with JOINs for assignee and inventor
            sql = """
                SELECT
                    p.id,
                    p.application_number,
                    p.publication_number,
                    p.patent_number,
                    p.document_type,
                    p.application_type,
                    p.filing_date,
                    p.publication_date,
                    p.issue_date,
                    p.title,
                    p.ingestion_date,
                    p.applicants_json,
                    p.priority_claims_json,
                    p.related_applications_json,
                    fts.rank as relevance,
                    inv.full_name as first_inventor_name,
                    asg.name as first_assignee_name,
                    asg.role as first_assignee_role,
                    asg.country as first_assignee_country
                FROM patents p
                INNER JOIN patents_fts fts ON p.id = fts.rowid
                LEFT JOIN (
                    SELECT patent_id, full_name
                    FROM (
                        SELECT patent_id, full_name,
                               ROW_NUMBER() OVER (
                                   PARTITION BY patent_id
                                   ORDER BY sequence, id
                               ) AS rn
                        FROM inventors
                    )
                    WHERE rn = 1
                ) inv ON p.id = inv.patent_id
                LEFT JOIN (
                    SELECT a.patent_id, a.name, a.role, a.country
                    FROM assignees a
                    WHERE a.id = (
                        SELECT MIN(id)
                        FROM assignees a2
                        WHERE a2.patent_id = a.patent_id
                    )
                ) asg ON p.id = asg.patent_id
                WHERE patents_fts MATCH ?
            """
            params.append(fts_match_expr)
        else:
            # Query all patents without FTS (empty query)
            sql = """
                SELECT
                    p.id,
                    p.application_number,
                    p.publication_number,
                    p.patent_number,
                    p.document_type,
                    p.application_type,
                    p.filing_date,
                    p.publication_date,
                    p.issue_date,
                    p.title,
                    p.ingestion_date,
                    p.applicants_json,
                    p.priority_claims_json,
                    p.related_applications_json,
                    NULL as relevance,
                    inv.full_name as first_inventor_name,
                    asg.name as first_assignee_name,
                    asg.role as first_assignee_role,
                    asg.country as first_assignee_country
                FROM patents p
                LEFT JOIN patents_fts fts ON p.id = fts.rowid
                LEFT JOIN (
                    SELECT patent_id, full_name
                    FROM (
                        SELECT patent_id, full_name,
                               ROW_NUMBER() OVER (
                                   PARTITION BY patent_id
                                   ORDER BY sequence, id
                               ) AS rn
                        FROM inventors
                    )
                    WHERE rn = 1
                ) inv ON p.id = inv.patent_id
                LEFT JOIN (
                    SELECT a.patent_id, a.name, a.role, a.country
                    FROM assignees a
                    WHERE a.id = (
                        SELECT MIN(id)
                        FROM assignees a2
                        WHERE a2.patent_id = a.patent_id
                    )
                ) asg ON p.id = asg.patent_id
                WHERE 1=1
            """

        # Apply non-FTS filters (FTS filters already in MATCH expression)
        sql += filter_clause
        params.extend(filter_params)

        # Add sorting
        sort_clause = self._get_sort_clause(sort, has_fts=bool(fts_match_expr))
        sql += f" ORDER BY {sort_clause}"

        # Add pagination
        sql += " LIMIT ? OFFSET ?"
        params.extend([rows, start])

        return sql, params

    def _build_count_query(self, query: str, filters: dict[str, Any]) -> tuple[str, list[Any]]:
        """Build count query with same filters as search.

        Args:
            query: FTS5 search query (empty string means query all patents)
            filters: Filters to apply

        Returns:
            Tuple of (SQL query string, parameters list)
        """
        params: list[Any] = []

        # Get filter clauses and FTS match terms
        filter_clause, filter_params, fts_match_terms = self._build_filter_clauses(filters)

        # Build combined FTS MATCH expression
        fts_match_expr = query
        if fts_match_terms:
            if query:
                # Combine main query with FTS filter terms using AND
                # Wrap query in parens to preserve OR operator precedence
                fts_match_expr = f"({query}) AND {' AND '.join(fts_match_terms)}"
            else:
                # Only FTS filters, no main query
                fts_match_expr = " AND ".join(fts_match_terms)

        if fts_match_expr:
            # FTS5 count query
            sql = """
                SELECT COUNT(*) as count
                FROM patents p
                INNER JOIN patents_fts fts ON p.id = fts.rowid
                WHERE patents_fts MATCH ?
            """
            params.append(fts_match_expr)
        else:
            # Count all patents without FTS (empty query)
            sql = """
                SELECT COUNT(*) as count
                FROM patents p
                LEFT JOIN patents_fts fts ON p.id = fts.rowid
                WHERE 1=1
            """

        # Apply non-FTS filters (FTS filters already in MATCH expression)
        sql += filter_clause
        params.extend(filter_params)

        return sql, params

    def _get_sort_clause(self, sort: str | None, has_fts: bool = True) -> str:
        """Get SQL ORDER BY clause based on sort parameter.

        Args:
            sort: Sort parameter (relevance, filing_date, publication_date)
            has_fts: Whether FTS is available (for relevance sorting)

        Returns:
            SQL ORDER BY clause
        """
        if sort == "filing_date":
            return "p.filing_date DESC"
        elif sort == "publication_date":
            return "p.publication_date DESC NULLS LAST"
        else:
            # Default to relevance (FTS5 rank - lower is better)
            # Fall back to filing_date if FTS not available
            if has_fts:
                return "fts.rank"
            else:
                return "p.filing_date DESC"

    def insert(self, patent: PatentRecord) -> int:
        """Insert a single patent.

        Args:
            patent: PatentRecord model instance

        Returns:
            Inserted patent ID
        """
        query = """
            INSERT INTO patents (
                application_number, publication_number, patent_number,
                kind_code, document_type, application_type, country,
                filing_date, publication_date, issue_date,
                title, abstract, description, claims,
                applicants_json, attorneys_json, ipc_codes_json, uspc_codes_json,
                locarno_classification, npl_citations_json, priority_claims_json,
                related_applications_json, term_of_grant, number_of_claims,
                number_of_figures, number_of_drawing_sheets, pct_filing_data_json,
                xml_file_name
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
        """
        self.execute(
            query,
            (
                patent.application_number,
                patent.publication_number,
                patent.patent_number,
                patent.kind_code,
                patent.document_type,
                patent.application_type,
                patent.country,
                patent.filing_date,
                patent.publication_date,
                patent.issue_date,
                patent.title,
                patent.abstract,
                patent.description,
                patent.claims,
                patent.applicants_json,
                patent.attorneys_json,
                patent.ipc_codes_json,
                patent.uspc_codes_json,
                patent.locarno_classification,
                patent.npl_citations_json,
                patent.priority_claims_json,
                patent.related_applications_json,
                patent.term_of_grant,
                patent.number_of_claims,
                patent.number_of_figures,
                patent.number_of_drawing_sheets,
                patent.pct_filing_data_json,
                patent.xml_file_name,
            ),
        )
        return self.last_insert_rowid()

    def insert_batch(self, patents: list[PatentRecord]) -> int:
        """Insert multiple patents in batch.

        Args:
            patents: List of PatentRecord model instances

        Returns:
            Number of patents inserted
        """
        if not patents:
            return 0

        query = """
            INSERT INTO patents (
                application_number, publication_number, patent_number,
                kind_code, document_type, application_type, country,
                filing_date, publication_date, issue_date,
                title, abstract, description, claims,
                applicants_json, attorneys_json, ipc_codes_json, uspc_codes_json,
                locarno_classification, npl_citations_json, priority_claims_json,
                related_applications_json, term_of_grant, number_of_claims,
                number_of_figures, number_of_drawing_sheets, pct_filing_data_json,
                xml_file_name
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
        """
        params_list = [
            (
                p.application_number,
                p.publication_number,
                p.patent_number,
                p.kind_code,
                p.document_type,
                p.application_type,
                p.country,
                p.filing_date,
                p.publication_date,
                p.issue_date,
                p.title,
                p.abstract,
                p.description,
                p.claims,
                p.applicants_json,
                p.attorneys_json,
                p.ipc_codes_json,
                p.uspc_codes_json,
                p.locarno_classification,
                p.npl_citations_json,
                p.priority_claims_json,
                p.related_applications_json,
                p.term_of_grant,
                p.number_of_claims,
                p.number_of_figures,
                p.number_of_drawing_sheets,
                p.pct_filing_data_json,
                p.xml_file_name,
            )
            for p in patents
        ]
        self.execute_many(query, params_list)
        return len(patents)

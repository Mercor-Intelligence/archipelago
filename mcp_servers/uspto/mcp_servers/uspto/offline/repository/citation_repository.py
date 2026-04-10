"""Patent Citation repository for USPTO offline mode."""

from mcp_servers.uspto.offline.models import PatentCitation
from mcp_servers.uspto.offline.repository.base import BaseRepository


class CitationRepository(BaseRepository):
    """Repository for managing patent citation data."""

    def insert_batch(self, citations: list[PatentCitation], patent_id: int) -> int:
        """Insert multiple patent citations.

        Args:
            citations: List of PatentCitation model instances
            patent_id: Foreign key to patents table

        Returns:
            Number of citations inserted
        """
        if not citations:
            return 0

        query = """
            INSERT INTO patent_citations (
                patent_id, cited_patent_number, cited_country,
                cited_kind, cited_date, category
            ) VALUES (?, ?, ?, ?, ?, ?)
        """
        params_list = [
            (
                patent_id,
                citation.cited_patent_number,
                citation.cited_country,
                citation.cited_kind,
                citation.cited_date,
                citation.category,
            )
            for citation in citations
        ]
        self.execute_many(query, params_list)
        return len(citations)

    def get_by_patent_id(self, patent_id: int) -> list[PatentCitation]:
        """Get all citations for a patent.

        Args:
            patent_id: Patent ID to query

        Returns:
            List of PatentCitation instances
        """
        query = """
            SELECT * FROM patent_citations
            WHERE patent_id = ?
            ORDER BY cited_date DESC
        """
        rows = self.fetch_all(query, (patent_id,))
        return [PatentCitation(**row) for row in rows]

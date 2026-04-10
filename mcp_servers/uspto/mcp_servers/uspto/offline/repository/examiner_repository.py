"""Examiner repository for USPTO offline mode."""

from mcp_servers.uspto.offline.models import Examiner
from mcp_servers.uspto.offline.repository.base import BaseRepository


class ExaminerRepository(BaseRepository):
    """Repository for managing examiner data."""

    def insert_batch(self, examiners: list[Examiner], patent_id: int) -> int:
        """Insert multiple examiners for a patent.

        Args:
            examiners: List of Examiner model instances
            patent_id: Foreign key to patents table

        Returns:
            Number of examiners inserted
        """
        if not examiners:
            return 0

        query = """
            INSERT INTO examiners (
                patent_id, examiner_type, last_name, first_name, department
            ) VALUES (?, ?, ?, ?, ?)
        """
        params_list = [
            (
                patent_id,
                examiner.examiner_type,
                examiner.last_name,
                examiner.first_name,
                examiner.department,
            )
            for examiner in examiners
        ]
        self.execute_many(query, params_list)
        return len(examiners)

    def get_by_patent_id(self, patent_id: int) -> list[Examiner]:
        """Get all examiners for a patent.

        Args:
            patent_id: Patent ID to query

        Returns:
            List of Examiner instances
        """
        query = """
            SELECT * FROM examiners
            WHERE patent_id = ?
            ORDER BY examiner_type
        """
        rows = self.fetch_all(query, (patent_id,))
        return [Examiner(**row) for row in rows]

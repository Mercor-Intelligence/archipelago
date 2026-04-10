"""CPC Classification repository for USPTO offline mode."""

from mcp_servers.uspto.offline.models import CPCClassification
from mcp_servers.uspto.offline.repository.base import BaseRepository


class CPCRepository(BaseRepository):
    """Repository for managing CPC classification data."""

    def insert(self, cpc: CPCClassification, patent_id: int) -> int:
        """Insert a single CPC classification.

        Args:
            cpc: CPCClassification model instance
            patent_id: Foreign key to patents table

        Returns:
            Inserted CPC ID
        """
        query = """
            INSERT INTO cpc_classifications (
                patent_id, is_main, section, class, subclass, main_group, subgroup
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        self.execute(
            query,
            (
                patent_id,
                cpc.is_main,
                cpc.section,
                cpc.class_,
                cpc.subclass,
                cpc.main_group,
                cpc.subgroup,
            ),
        )
        return self.last_insert_rowid()

    def insert_batch(self, cpcs: list[CPCClassification], patent_id: int) -> int:
        """Insert multiple CPC classifications for a patent.

        Args:
            cpcs: List of CPCClassification model instances
            patent_id: Foreign key to patents table

        Returns:
            Number of CPC classifications inserted
        """
        if not cpcs:
            return 0

        query = """
            INSERT INTO cpc_classifications (
                patent_id, is_main, section, class, subclass, main_group, subgroup
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """
        params_list = [
            (
                patent_id,
                cpc.is_main,
                cpc.section,
                cpc.class_,
                cpc.subclass,
                cpc.main_group,
                cpc.subgroup,
            )
            for cpc in cpcs
        ]
        self.execute_many(query, params_list)
        return len(cpcs)

    def get_by_patent_id(self, patent_id: int) -> list[CPCClassification]:
        """Get all CPC classifications for a patent.

        Args:
            patent_id: Patent ID to query

        Returns:
            List of CPCClassification instances
        """
        query = """
            SELECT * FROM cpc_classifications
            WHERE patent_id = ?
            ORDER BY is_main DESC, full_code
        """
        rows = self.fetch_all(query, (patent_id,))
        return [CPCClassification(**row) for row in rows]

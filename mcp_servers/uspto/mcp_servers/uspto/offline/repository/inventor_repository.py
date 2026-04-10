"""Inventor repository for USPTO offline mode."""

from mcp_servers.uspto.offline.models import Inventor
from mcp_servers.uspto.offline.repository.base import BaseRepository


class InventorRepository(BaseRepository):
    """Repository for managing inventor data."""

    def insert(self, inventor: Inventor, patent_id: int) -> int:
        """Insert a single inventor.

        Args:
            inventor: Inventor model instance
            patent_id: Foreign key to patents table

        Returns:
            Inserted inventor ID
        """
        query = """
            INSERT INTO inventors (
                patent_id, sequence, first_name, last_name, full_name,
                city, state, country
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        self.execute(
            query,
            (
                patent_id,
                inventor.sequence,
                inventor.first_name,
                inventor.last_name,
                inventor.full_name,
                inventor.city,
                inventor.state,
                inventor.country,
            ),
        )
        return self.last_insert_rowid()

    def insert_batch(self, inventors: list[Inventor], patent_id: int) -> int:
        """Insert multiple inventors for a patent.

        Args:
            inventors: List of Inventor model instances
            patent_id: Foreign key to patents table

        Returns:
            Number of inventors inserted
        """
        if not inventors:
            return 0

        query = """
            INSERT INTO inventors (
                patent_id, sequence, first_name, last_name, full_name,
                city, state, country
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """
        params_list = [
            (
                patent_id,
                inv.sequence,
                inv.first_name,
                inv.last_name,
                inv.full_name,
                inv.city,
                inv.state,
                inv.country,
            )
            for inv in inventors
        ]
        self.execute_many(query, params_list)
        return len(inventors)

    def get_by_patent_id(self, patent_id: int) -> list[Inventor]:
        """Get all inventors for a patent.

        Args:
            patent_id: Patent ID to query

        Returns:
            List of Inventor instances
        """
        query = """
            SELECT * FROM inventors
            WHERE patent_id = ?
            ORDER BY sequence
        """
        rows = self.fetch_all(query, (patent_id,))
        return [Inventor(**row) for row in rows]

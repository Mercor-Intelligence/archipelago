"""Assignee repository for USPTO offline mode."""

from mcp_servers.uspto.offline.models import Assignee
from mcp_servers.uspto.offline.repository.base import BaseRepository


class AssigneeRepository(BaseRepository):
    """Repository for managing assignee data."""

    def insert(self, assignee: Assignee, patent_id: int) -> int:
        """Insert a single assignee.

        Args:
            assignee: Assignee model instance
            patent_id: Foreign key to patents table

        Returns:
            Inserted assignee ID
        """
        query = """
            INSERT INTO assignees (
                patent_id, name, role, city, state, country
            ) VALUES (?, ?, ?, ?, ?, ?)
        """
        self.execute(
            query,
            (
                patent_id,
                assignee.name,
                assignee.role,
                assignee.city,
                assignee.state,
                assignee.country,
            ),
        )
        return self.last_insert_rowid()

    def insert_batch(self, assignees: list[Assignee], patent_id: int) -> int:
        """Insert multiple assignees for a patent.

        Args:
            assignees: List of Assignee model instances
            patent_id: Foreign key to patents table

        Returns:
            Number of assignees inserted
        """
        if not assignees:
            return 0

        query = """
            INSERT INTO assignees (
                patent_id, name, role, city, state, country
            ) VALUES (?, ?, ?, ?, ?, ?)
        """
        params_list = [
            (
                patent_id,
                assignee.name,
                assignee.role,
                assignee.city,
                assignee.state,
                assignee.country,
            )
            for assignee in assignees
        ]
        self.execute_many(query, params_list)
        return len(assignees)

    def get_by_patent_id(self, patent_id: int) -> list[Assignee]:
        """Get all assignees for a patent.

        Args:
            patent_id: Patent ID to query

        Returns:
            List of Assignee instances
        """
        query = """
            SELECT * FROM assignees
            WHERE patent_id = ?
            ORDER BY id
        """
        rows = self.fetch_all(query, (patent_id,))
        return [Assignee(**row) for row in rows]

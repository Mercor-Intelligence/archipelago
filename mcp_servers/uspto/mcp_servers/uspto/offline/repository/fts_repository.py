"""FTS5 repository for USPTO offline mode."""

from mcp_servers.uspto.offline.repository.base import BaseRepository


class FTS5Repository(BaseRepository):
    """Repository for managing FTS5 full-text search index."""

    def rebuild_index(self) -> int:
        """Rebuild the entire FTS5 index from patents table.

        This should be called after batch ingestion of patents is complete.
        It populates the patents_fts table with denormalized data from
        the patents table and related tables (inventors, assignees, cpc_classifications).

        Uses a savepoint to ensure atomicity: if the rebuild fails, the FTS index
        is left in its previous state rather than being cleared.

        Returns:
            Number of patents indexed
        """
        # Use savepoint for atomicity
        self.execute("SAVEPOINT fts_rebuild")

        try:
            # First, clear the existing FTS index
            # Use FTS5 'delete-all' command for contentless tables
            self.execute("INSERT INTO patents_fts(patents_fts) VALUES('delete-all')")

            # Rebuild FTS index with denormalized data
            rebuild_query = """
                INSERT INTO patents_fts(
                    rowid,
                    application_number,
                    title,
                    abstract,
                    description,
                    claims,
                    inventors,
                    assignees,
                    cpc_codes
                )
                SELECT
                    p.id,
                    p.application_number,
                    p.title,
                    p.abstract,
                    p.description,
                    p.claims,
                    (
                        SELECT group_concat(full_name, ' ')
                        FROM inventors WHERE patent_id = p.id
                    ),
                    (
                        SELECT group_concat(name, ' ')
                        FROM assignees WHERE patent_id = p.id
                    ),
                    (
                        SELECT group_concat(full_code, ' ')
                        FROM cpc_classifications WHERE patent_id = p.id
                    )
                FROM patents p
            """
            self.execute(rebuild_query)

            # Get count of indexed patents
            count_result = self.fetch_one("SELECT COUNT(*) as count FROM patents_fts")
            count = count_result["count"] if count_result else 0

            # Optimize the FTS index
            self.execute("INSERT INTO patents_fts(patents_fts) VALUES('optimize')")

            # Commit the savepoint - rebuild succeeded
            self.execute("RELEASE SAVEPOINT fts_rebuild")

            return count

        except Exception:
            # Rollback to savepoint - restore FTS to previous state
            self.execute("ROLLBACK TO SAVEPOINT fts_rebuild")
            self.execute("RELEASE SAVEPOINT fts_rebuild")
            raise

    def get_index_stats(self) -> dict[str, int]:
        """Get statistics about the FTS index.

        Returns:
            Dictionary with index statistics
        """
        total_patents = self.fetch_one("SELECT COUNT(*) as count FROM patents")
        indexed_patents = self.fetch_one("SELECT COUNT(*) as count FROM patents_fts")

        return {
            "total_patents": total_patents["count"] if total_patents else 0,
            "indexed_patents": indexed_patents["count"] if indexed_patents else 0,
        }

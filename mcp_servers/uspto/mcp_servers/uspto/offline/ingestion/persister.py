"""USPTO-specific persister implementation using repository pattern."""

import logging
import sqlite3

from data_ingestion import BatchResult, Persister

from mcp_servers.uspto.offline.models import PatentGrantRecord
from mcp_servers.uspto.offline.repository import (
    AssigneeRepository,
    CitationRepository,
    CPCRepository,
    ExaminerRepository,
    InventorRepository,
    PatentRepository,
)

logger = logging.getLogger(__name__)


class USPTOPatentPersister(Persister[PatentGrantRecord]):
    """Persister for USPTO patent grant records using repository pattern.

    Implements the generic Persister interface for PatentGrantRecord domain objects.
    Handles batch inserts into patents table and all related normalized tables.

    Args:
        conn: SQLite database connection
        enable_fts: Whether to enable FTS5 updates (default: False for batch mode)

    Example:
        >>> conn = sqlite3.connect('patents.db')
        >>> persister = USPTOPatentPersister(conn, enable_fts=False)
        >>> # ... use with IngestionPipeline
        >>> persister.close()
    """

    def __init__(self, conn: sqlite3.Connection, enable_fts: bool = False):
        """Initialize USPTO patent persister.

        Args:
            conn: SQLite database connection
            enable_fts: Whether to enable FTS5 updates (default: False)
        """
        self.conn = conn
        self.enable_fts = enable_fts

        # Initialize repositories
        self.patent_repo = PatentRepository(conn)
        self.inventor_repo = InventorRepository(conn)
        self.assignee_repo = AssigneeRepository(conn)
        self.cpc_repo = CPCRepository(conn)
        self.citation_repo = CitationRepository(conn)
        self.examiner_repo = ExaminerRepository(conn)

    def persist_batch(self, records: list[PatentGrantRecord]) -> BatchResult:
        """Persist a batch of patent grant records with all related data.

        Processes each record individually to handle errors gracefully.
        Records that fail (e.g., duplicates) are logged and skipped.

        Args:
            records: List of PatentGrantRecord objects to persist

        Returns:
            BatchResult with inserted and error counts
        """
        if not records:
            return BatchResult(inserted=0, errors=0)

        inserted_count = 0
        error_count = 0

        for record in records:
            try:
                # Insert main patent record
                patent_id = self.patent_repo.insert(record.patent)

                # Insert related data from normalized tables
                if record.inventors:
                    self.inventor_repo.insert_batch(record.inventors, patent_id)

                if record.assignees:
                    self.assignee_repo.insert_batch(record.assignees, patent_id)

                if record.cpc_classifications:
                    self.cpc_repo.insert_batch(record.cpc_classifications, patent_id)

                if record.patent_citations:
                    self.citation_repo.insert_batch(record.patent_citations, patent_id)

                if record.examiners:
                    self.examiner_repo.insert_batch(record.examiners, patent_id)

                # Commit this individual record
                self.conn.commit()
                inserted_count += 1

            except sqlite3.IntegrityError:
                # Rollback this record and continue
                self.conn.rollback()
                app_num = record.patent.application_number
                pub_num = record.patent.publication_number
                doc_type = record.patent.document_type
                logger.warning(f"Duplicate - App: {app_num}, Pub: {pub_num}, Type: {doc_type}")
                error_count += 1

            except sqlite3.Error as e:
                # Rollback this record and continue
                self.conn.rollback()
                app_num = record.patent.application_number
                pub_num = record.patent.publication_number
                doc_type = record.patent.document_type
                logger.error(f"DB Error - App: {app_num}, Pub: {pub_num}, Type: {doc_type}: {e}")
                error_count += 1

        logger.debug(f"Batch complete: {inserted_count}/{len(records)} records inserted")
        return BatchResult(inserted=inserted_count, errors=error_count)

    def close(self) -> None:
        """Cleanup and release database connection.

        Note: Does not close the connection (managed externally),
        just commits any pending transactions.
        """
        try:
            self.conn.commit()
        except sqlite3.Error:
            # Ignore commit errors on close
            pass

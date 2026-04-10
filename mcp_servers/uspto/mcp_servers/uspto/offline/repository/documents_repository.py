"""Repository for handling document inventory queries in offline mode.

This repository provides document-related operations for the offline USPTO client.
Currently, offline mode does not store prosecution document metadata (fees, office
actions, amendments) as these are not included in the bulk XML patent data files.
"""

from __future__ import annotations

from typing import Any

from mcp_servers.uspto.offline.db.connection import get_async_connection


class DocumentsRepository:
    """Repository for document inventory operations in offline mode."""

    async def get_documents(
        self,
        application_number: str,
        start: int = 0,
        rows: int = 100,
    ) -> dict[str, Any]:
        """Return document inventory for a patent application.

        In offline mode, prosecution documents (fees, office actions, amendments)
        are not available because they are not included in USPTO bulk XML files.

        This method verifies the application exists in the database and returns
        an empty document list with appropriate offline mode messaging.

        Args:
            application_number: Patent application number (normalized or raw)
            start: Starting offset for pagination
            rows: Maximum number of documents to return

        Returns:
            dict: Response matching online API format:
                {
                    "documents": [],
                    "total": 0,
                    "offlineMode": True,
                    "message": "...",
                    "unavailableDocumentTypes": [...],
                    "applicationNumber": "...",
                    "applicationExists": bool
                }
        """
        # Normalize application number (remove non-digits)
        normalized_app_number = "".join(ch for ch in application_number if ch.isdigit())
        if not normalized_app_number:
            normalized_app_number = application_number

        # Check if application exists in database
        application_exists = await self._application_exists(normalized_app_number)

        # Build response matching online API format
        response: dict[str, Any] = {
            "documents": [],
            "total": 0,
            "start": start,
            "rows": rows,
            "offlineMode": True,
            "applicationNumber": normalized_app_number,
            "applicationExists": application_exists,
            "message": self._get_offline_message(application_exists),
            "unavailableDocumentTypes": [
                "WFEE (Fee Worksheets)",
                "RESR (Office Action - Restriction)",
                "CTNF (Office Action - Non-Final Rejection)",
                "CTFR (Office Action - Final Rejection)",
                "A... (Amendments)",
                "IDS (Information Disclosure Statements)",
                "N... (Notices)",
                "RCE (Request for Continued Examination)",
                "All prosecution history documents",
            ],
            "availableInFuture": [
                (
                    "Drawing metadata (figure IDs, descriptions, dimensions) - "
                    "requires schema enhancement"
                ),
            ],
        }

        # Note: Drawing metadata is not currently stored in offline database
        # Future enhancement could add a 'drawings' table during ingestion
        # to store figure metadata from XML <drawings> section

        return response

    async def _application_exists(self, application_number: str) -> bool:
        """Check if an application exists in the offline database.

        Args:
            application_number: Normalized application number

        Returns:
            bool: True if application exists, False otherwise
        """
        async with get_async_connection() as conn:
            cursor = await conn.execute(
                "SELECT 1 FROM patents WHERE application_number = ? LIMIT 1",
                (application_number,),
            )
            row = await cursor.fetchone()
            return row is not None

    def _get_offline_message(self, application_exists: bool) -> str:
        """Generate appropriate message based on application existence.

        Args:
            application_exists: Whether the application was found

        Returns:
            str: User-friendly message explaining offline limitations
        """
        if not application_exists:
            return (
                "Application not found in offline database. "
                "The offline database contains patent grant data only. "
                "Patent applications and prosecution documents are not available offline."
            )

        return (
            "Offline mode active. Prosecution documents (fees, office actions, "
            "amendments, notices) are not available in offline mode. These documents "
            "are not included in USPTO bulk XML data files. Only patent grant metadata "
            "and full text (title, abstract, claims, description) are available offline."
        )


__all__ = ["DocumentsRepository"]

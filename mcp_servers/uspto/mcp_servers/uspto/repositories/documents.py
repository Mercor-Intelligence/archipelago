"""Documents repository for session-scoped database operations."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_servers.uspto.db.models import DocumentRecord
from mcp_servers.uspto.models import DocumentRecord as DocumentRecordModel
from mcp_servers.uspto.models import DownloadOption


class DocumentsRepository:
    """Session-scoped database operations for document records."""

    def __init__(self, session: AsyncSession):
        self.session = session

    @staticmethod
    def ensure_utc_timestamp() -> str:
        """Return current UTC timestamp in ISO 8601 format."""
        return datetime.now(UTC).isoformat().replace("+00:00", "Z")

    @staticmethod
    def parse_download_option_from_api(data: dict[str, Any] | None) -> DownloadOption | None:
        """Parse download option from USPTO API response data.

        This is used for parsing API responses, while _parse_download_option
        is used for parsing from database JSON.
        """
        if not data:
            return None

        # Get required fields with explicit None checks (not 'or' to preserve falsy values)
        mime_type = data.get("mimeTypeIdentifier")
        if mime_type is None:
            mime_type = data.get("mimeType")
        download_url = data.get("downloadUrl")
        if download_url is None:
            download_url = data.get("url")

        # Required fields must be present
        if mime_type is None or download_url is None:
            return None

        # Get optional fields with explicit None checks
        page_count = data.get("pageCount")
        if page_count is None:
            page_count = data.get("pageTotalQuantity")
        file_size_bytes = data.get("fileSizeBytes")
        if file_size_bytes is None:
            file_size_bytes = data.get("fileSize")

        try:
            return DownloadOption(
                mime_type_identifier=mime_type,
                download_url=download_url,
                page_count=page_count,
                file_size_bytes=file_size_bytes,
            )
        except Exception:
            # Gracefully skip malformed download options
            return None

    @staticmethod
    def parse_document_from_api(data: dict[str, Any] | None) -> DocumentRecordModel | None:
        """Parse document record from USPTO API response data."""
        if not data:
            return None

        # Get required document_identifier with explicit None check
        document_identifier = data.get("documentIdentifier")
        if document_identifier is None:
            document_identifier = data.get("documentId")

        # Required field must be present
        if document_identifier is None:
            return None

        # Parse download options
        download_options_raw = data.get("downloadOptions")
        if download_options_raw is None:
            download_options_raw = data.get("downloadOptionBag")
        if download_options_raw is None:
            download_options_raw = []
        download_options = []
        for option_data in download_options_raw:
            option = DocumentsRepository.parse_download_option_from_api(option_data)
            if option:
                download_options.append(option)

        # Get optional fields with explicit None checks for string fallbacks
        document_code_description = data.get("documentCodeDescriptionText")
        if document_code_description is None:
            document_code_description = data.get("documentDescription")

        try:
            return DocumentRecordModel(
                document_identifier=document_identifier,
                document_code=data.get("documentCode"),
                document_code_description_text=document_code_description,
                official_date=data.get("officialDate"),
                direction_category=data.get("directionCategory"),
                mail_room_date=data.get("mailRoomDate"),
                download_options=download_options,
            )
        except Exception:
            # Gracefully skip malformed documents
            return None

    async def get_document(
        self,
        application_number_text: str,
        document_identifier: str,
        workspace_id: str | None = None,
    ) -> DocumentRecord | None:
        """Get document record by application number and document identifier.

        Args:
            application_number_text: Application number that owns the document
            document_identifier: USPTO identifier for the specific document
            workspace_id: Optional workspace ID to filter by. If not provided,
                returns the first matching document from any workspace.

        Returns:
            DocumentRecord if found, None otherwise. If multiple documents exist
            and workspace_id is not provided, returns the first match.
        """
        query = select(DocumentRecord).where(
            DocumentRecord.application_number_text == application_number_text,
            DocumentRecord.document_identifier == document_identifier,
        )
        if workspace_id is not None:
            query = query.where(DocumentRecord.workspace_id == workspace_id)

        result = await self.session.execute(query.limit(1))
        return result.scalar_one_or_none()

    async def has_documents_for_application(
        self,
        application_number_text: str,
        workspace_id: str | None = None,
    ) -> bool:
        """Check if any documents exist for the given application number.

        Args:
            application_number_text: Application number to check
            workspace_id: Optional workspace ID to filter by. If not provided,
                checks across all workspaces.

        Returns:
            True if any documents exist, False otherwise.
        """
        query = select(DocumentRecord).where(
            DocumentRecord.application_number_text == application_number_text,
        )
        if workspace_id is not None:
            query = query.where(DocumentRecord.workspace_id == workspace_id)

        result = await self.session.execute(query.limit(1))
        return result.first() is not None

    def parse_download_options(
        self,
        download_options_json: str | None,
    ) -> list[DownloadOption]:
        """Parse download options from JSON string into Pydantic models.

        Args:
            download_options_json: JSON string containing download options array

        Returns:
            List of parsed DownloadOption models

        Raises:
            ValueError: If JSON is invalid or cannot be parsed
        """
        if not download_options_json:
            return []

        try:
            download_options_data = json.loads(download_options_json)
        except (json.JSONDecodeError, TypeError) as e:
            raise ValueError(f"Invalid download options format: {e}") from e

        if not isinstance(download_options_data, list):
            raise ValueError("Download options must be a list")

        download_options = []
        for option_data in download_options_data:
            option = self._parse_download_option(option_data)
            if option:
                download_options.append(option)

        return download_options

    def _parse_download_option(self, data: dict[str, Any] | None) -> DownloadOption | None:
        """Parse a single download option from USPTO response data."""
        if not data:
            return None

        # Get required fields with explicit None checks (not 'or' to preserve falsy values)
        mime_type = data.get("mimeTypeIdentifier")
        if mime_type is None:
            mime_type = data.get("mime_type_identifier")
        if mime_type is None:
            mime_type = data.get("mimeType")
        download_url = data.get("downloadUrl")
        if download_url is None:
            download_url = data.get("download_url")
        if download_url is None:
            download_url = data.get("url")

        # Required fields must be present
        if mime_type is None or download_url is None:
            return None

        # Get optional fields with explicit None checks
        page_count = data.get("pageCount")
        if page_count is None:
            page_count = data.get("page_count")
        if page_count is None:
            page_count = data.get("pageTotalQuantity")
        file_size_bytes = data.get("fileSizeBytes")
        if file_size_bytes is None:
            file_size_bytes = data.get("file_size_bytes")
        if file_size_bytes is None:
            file_size_bytes = data.get("fileSize")

        try:
            return DownloadOption(
                mime_type_identifier=mime_type,
                download_url=download_url,
                page_count=page_count,
                file_size_bytes=file_size_bytes,
            )
        except Exception:
            # Gracefully skip malformed download options
            return None


__all__ = ["DocumentsRepository"]

"""USPTO data access contract used by both online and offline implementations."""

from __future__ import annotations

from typing import Any, Protocol


class USPTOClient(Protocol):
    """Defines the async interface the MCP tools rely on."""

    async def aclose(self) -> None:
        """Close any resources (HTTP client, DB handles) held by the client."""

    async def search_applications(
        self,
        query: str,
        filters: dict[str, Any] | None = None,
        start: int = 0,
        rows: int = 25,
        sort: str | None = None,
    ) -> dict[str, Any]:
        """Search published applications and issued patents."""

    async def get_application(self, application_number: str) -> dict[str, Any]:
        """Return the normalized details for a single application."""

    async def get_status_codes(self) -> dict[str, Any]:
        """Return the status code reference table."""

    async def get_documents(
        self,
        application_number: str,
        start: int = 0,
        rows: int = 100,
    ) -> dict[str, Any]:
        """Return the document inventory for a given application."""

    async def get_foreign_priority(self, application_number: str) -> dict[str, Any]:
        """Return the foreign priority claims for an application."""

    async def generate_patent_pdf(self, application_number: str) -> dict[str, Any]:
        """Generate a text-only patent PDF from offline database content."""

    async def ping(self) -> bool:
        """Lightweight health check for API availability."""


__all__ = ["USPTOClient"]

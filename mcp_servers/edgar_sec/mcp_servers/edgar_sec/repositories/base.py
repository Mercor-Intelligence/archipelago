"""Base repository protocol for EDGAR data access."""

from typing import Protocol


class EdgarRepository(Protocol):
    """Protocol defining the interface for EDGAR data access.

    Implementations:
        - OnlineEdgarRepository: Fetches from SEC API (data.sec.gov)
        - OfflineEdgarRepository: Reads from local files
    """

    async def get_company_tickers(self) -> dict:
        """Get all company ticker/CIK mappings.

        Returns:
            Dict with company data:
            {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}, ...}
        """
        ...

    async def get_submissions(self, cik: str) -> dict:
        """Get filing submissions for a company.

        Args:
            cik: 10-digit zero-padded CIK (e.g., "0000320193")

        Returns:
            Dict with company info and filing history
        """
        ...

    async def get_company_facts(self, cik: str) -> dict:
        """Get all XBRL facts for a company.

        Args:
            cik: 10-digit zero-padded CIK (e.g., "0000320193")

        Returns:
            Dict with all XBRL financial data
        """
        ...

    async def get_company_concept(self, cik: str, taxonomy: str, tag: str) -> dict:
        """Get specific XBRL concept data for a company.

        Args:
            cik: 10-digit zero-padded CIK
            taxonomy: XBRL taxonomy (e.g., "us-gaap")
            tag: XBRL tag (e.g., "Revenues")

        Returns:
            Dict with concept data across all filings
        """
        ...

    async def get_frames(self, taxonomy: str, tag: str, unit: str, period: str) -> dict:
        """Get aggregated XBRL data across all companies.

        Args:
            taxonomy: XBRL taxonomy (e.g., "us-gaap")
            tag: XBRL tag (e.g., "Revenues")
            unit: Unit type (e.g., "USD")
            period: Period (e.g., "CY2023Q1I")

        Returns:
            Dict with aggregated data across companies
        """
        ...

    async def get_filing_html(self, cik: str, accession: str, document: str) -> str | None:
        """Get HTML content of a filing document.

        Args:
            cik: 10-digit zero-padded CIK
            accession: Accession number (e.g., "0000320193-24-000123")
            document: Primary document filename (e.g., "aapl-20240928.htm")

        Returns:
            HTML content as string, or None if not available
        """
        ...

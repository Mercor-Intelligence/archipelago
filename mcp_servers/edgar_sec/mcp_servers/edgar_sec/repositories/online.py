"""Online repository implementation - fetches from SEC API."""

import httpx
from aiolimiter import AsyncLimiter
from config import (
    EDGAR_ARCHIVES_BASE_URL,
    EDGAR_COMPANY_CONCEPT_PATH,
    EDGAR_COMPANY_FACTS_PATH,
    EDGAR_COMPANY_TICKERS_URL,
    EDGAR_FRAMES_PATH,
    EDGAR_SUBMISSIONS_PATH,
)
from loguru import logger


class OnlineEdgarRepository:
    """Repository that fetches EDGAR data from SEC API.

    Uses rate limiting (10 req/sec) per SEC guidelines.
    """

    def __init__(self, base_url: str, user_agent: str):
        """Initialize online repository.

        Args:
            base_url: SEC API base URL (e.g., "https://data.sec.gov")
            user_agent: User-Agent header required by SEC
        """
        self.base_url = base_url.rstrip("/")
        self.user_agent = user_agent
        self._rate_limiter: AsyncLimiter | None = None

        logger.info(f"Initialized OnlineEdgarRepository with base_url={self.base_url}")

    def _get_rate_limiter(self) -> AsyncLimiter:
        """Get or create the rate limiter (lazy initialization)."""
        if self._rate_limiter is None:
            self._rate_limiter = AsyncLimiter(10, 1.0)
        return self._rate_limiter

    async def _rate_limited_get(self, path: str) -> httpx.Response:
        """Make a rate-limited GET request.

        Creates a fresh HTTP client per request to avoid stale TCP DNS
        connections when resolv.conf uses 'options use-vc' (DNS over TCP).
        """
        async with self._get_rate_limiter():
            async with httpx.AsyncClient(
                base_url=self.base_url,
                headers={"User-Agent": self.user_agent},
                timeout=300.0,
                follow_redirects=True,
            ) as client:
                response = await client.get(path)
                response.raise_for_status()
                return response

    async def get_company_tickers(self) -> dict:
        """Get all company ticker/CIK mappings from SEC API."""
        # Note: This endpoint is on www.sec.gov, not data.sec.gov
        async with self._get_rate_limiter():
            async with httpx.AsyncClient(
                headers={"User-Agent": self.user_agent},
                timeout=30.0,
            ) as client:
                response = await client.get(EDGAR_COMPANY_TICKERS_URL)
                response.raise_for_status()
                data = response.json()

        logger.debug(f"Fetched {len(data)} companies from SEC API")
        return data

    async def get_submissions(self, cik: str) -> dict:
        """Get filing submissions for a company from SEC API.

        Args:
            cik: 10-digit zero-padded CIK (e.g., "0000320193")

        Returns:
            Dict with company info and filing history
        """
        cik_padded = cik.zfill(10)
        response = await self._rate_limited_get(EDGAR_SUBMISSIONS_PATH.format(cik=cik_padded))
        data = response.json()
        logger.debug(f"Fetched submissions for CIK {cik_padded}")
        return data

    async def get_company_facts(self, cik: str) -> dict:
        """Get all XBRL facts for a company from SEC API.

        Args:
            cik: 10-digit zero-padded CIK (e.g., "0000320193")

        Returns:
            Dict with all XBRL financial data
        """
        cik_padded = cik.zfill(10)
        response = await self._rate_limited_get(EDGAR_COMPANY_FACTS_PATH.format(cik=cik_padded))
        data = response.json()
        logger.debug(f"Fetched company facts for CIK {cik_padded}")
        return data

    async def get_company_concept(self, cik: str, taxonomy: str, tag: str) -> dict:
        """Get specific XBRL concept data for a company from SEC API.

        Args:
            cik: 10-digit zero-padded CIK
            taxonomy: XBRL taxonomy (e.g., "us-gaap")
            tag: XBRL tag (e.g., "Revenues")

        Returns:
            Dict with concept data across all filings
        """
        cik_padded = cik.zfill(10)
        response = await self._rate_limited_get(
            EDGAR_COMPANY_CONCEPT_PATH.format(cik=cik_padded, taxonomy=taxonomy, tag=tag)
        )
        data = response.json()
        logger.debug(f"Fetched concept {taxonomy}:{tag} for CIK {cik_padded}")
        return data

    async def get_frames(self, taxonomy: str, tag: str, unit: str, period: str) -> dict:
        """Get aggregated XBRL data across all companies from SEC API.

        Args:
            taxonomy: XBRL taxonomy (e.g., "us-gaap")
            tag: XBRL tag (e.g., "Revenues")
            unit: Unit type (e.g., "USD")
            period: Period (e.g., "CY2023Q1I")

        Returns:
            Dict with aggregated data across companies
        """
        response = await self._rate_limited_get(
            EDGAR_FRAMES_PATH.format(taxonomy=taxonomy, tag=tag, unit=unit, period=period)
        )
        data = response.json()
        logger.debug(f"Fetched frames for {taxonomy}:{tag}/{unit}/{period}")
        return data

    async def get_filing_html(self, cik: str, accession: str, document: str) -> str | None:
        """Get HTML content of a filing document from SEC Archives.

        Args:
            cik: 10-digit zero-padded CIK
            accession: Accession number (e.g., "0000320193-24-000123")
            document: Primary document filename (e.g., "aapl-20240928.htm")

        Returns:
            HTML content as string, or None if not available
        """
        # SEC Archives URL format
        cik_int = str(int(cik))  # Remove leading zeros
        accession_clean = accession.replace("-", "")

        url = f"{EDGAR_ARCHIVES_BASE_URL}/{cik_int}/{accession_clean}/{document}"

        try:
            async with self._get_rate_limiter():
                async with httpx.AsyncClient(
                    headers={"User-Agent": self.user_agent},
                    timeout=60.0,
                ) as client:
                    response = await client.get(url)
                    response.raise_for_status()
                    return response.text

        except httpx.HTTPStatusError as e:
            logger.warning(f"Failed to fetch filing HTML: {e}")
            return None

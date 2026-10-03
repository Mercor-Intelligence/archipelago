"""CIK lookup tool for mapping ticker/company name to CIK.

Uses the SEC ``company_tickers.json`` dataset as the primary source.  When
that file does not cover a ticker (it only holds ~10 K active filers), a
fuzzy ticker match and the SEC EFTS full-text search API are tried as
fallbacks.
"""

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from config import EDGAR_USER_AGENT
from loguru import logger
from models import LookupCIKInput, LookupCIKOutput
from rapidfuzz import fuzz, process
from utils.cik_cache import get_company_tickers

_EFTS_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
_CIK_RE = re.compile(r"\(CIK\s+(\d+)\)")


def _normalize_ticker(ticker: str) -> str:
    """Normalize ticker for matching (uppercase, strip whitespace)."""
    return ticker.strip().upper()


def _normalize_name(name: str) -> str:
    """Normalize company name for matching (lowercase, strip common suffixes)."""
    name = name.lower().strip()
    # Only strip legal-entity suffixes. Business-descriptor words like
    # "financial", "holdings", "group" etc. are intentionally excluded
    # because stripping them collapses distinct companies into the same
    # normalized form (e.g. "XYZ Holdings" vs "XYZ International").
    suffixes = [
        " inc.",
        " inc",
        " corp.",
        " corp",
        " corporation",
        " co.",
        " co",
        " ltd.",
        " ltd",
        " llc",
        " lp",
        " l.p.",
        " plc",
        " n.v.",
        " s.a.",
        " ag",
        " se",
        ",",
    ]
    for suffix in suffixes:
        if name.endswith(suffix):
            name = name[: -len(suffix)].strip()
    return name


def _pad_cik(cik: int | str) -> str:
    """Pad CIK to 10 digits with leading zeros."""
    return str(cik).zfill(10)


async def _efts_lookup(query: str) -> LookupCIKOutput | None:
    """Last-resort CIK lookup via the SEC EFTS full-text search API.

    The EFTS response embeds entity names in the format
    ``COMPANY NAME  (TICKER)  (CIK 0001234567)``.  We parse the first
    match and return it, or ``None`` if the search fails.
    """
    try:
        async with httpx.AsyncClient(
            headers={"User-Agent": EDGAR_USER_AGENT},
            timeout=10.0,
        ) as client:
            resp = await client.get(
                _EFTS_SEARCH_URL,
                params={"q": f'"{query}"', "forms": "10-K,10-Q,8-K"},
            )
            resp.raise_for_status()
            data = resp.json()

        hits = data.get("hits", {}).get("hits", [])
        if not hits:
            return None

        display_names = hits[0].get("_source", {}).get("display_names", [])
        if not display_names:
            return None

        entry = display_names[0]
        cik_match = _CIK_RE.search(entry)
        if not cik_match:
            return None

        cik_raw = cik_match.group(1)

        ticker_match = re.search(r"\(([A-Z0-9./-]+)\)", entry)
        ticker = ticker_match.group(1) if ticker_match else None
        name = entry.split("(")[0].strip()

        return LookupCIKOutput(
            cik=_pad_cik(cik_raw),
            name=name,
            ticker=ticker,
            exchange=None,
        )
    except Exception as e:
        logger.debug(f"EFTS lookup failed for '{query}': {e}")
        return None


async def lookup_cik(request: LookupCIKInput) -> LookupCIKOutput:
    """Map company ticker symbol or name to 10-digit zero-padded CIK."""
    try:
        companies_data = await get_company_tickers()
    except Exception as e:
        logger.error(f"Failed to fetch company tickers data: {e}")
        raise ValueError(f"Unable to fetch company data from SEC: {e}")

    companies = list(companies_data.values())

    # --- 1. Exact ticker match ---
    if request.ticker:
        normalized_ticker = _normalize_ticker(request.ticker)

        for company in companies:
            raw_ticker = company.get("ticker")
            if not raw_ticker:
                continue
            if _normalize_ticker(raw_ticker) == normalized_ticker:
                return LookupCIKOutput(
                    cik=_pad_cik(company["cik_str"]),
                    name=company["title"],
                    ticker=raw_ticker,
                    exchange=None,
                )

        # --- 2. Fuzzy ticker match ---
        ticker_list = [
            (company, company["ticker"]) for company in companies if company.get("ticker")
        ]
        if ticker_list:
            candidates = [t for _, t in ticker_list]
            match = process.extractOne(
                normalized_ticker,
                [_normalize_ticker(t) for t in candidates],
                scorer=fuzz.ratio,
            )
            if match and match[1] >= 90:
                company = ticker_list[match[2]][0]
                return LookupCIKOutput(
                    cik=_pad_cik(company["cik_str"]),
                    name=company["title"],
                    ticker=company["ticker"],
                    exchange=None,
                )

    # --- 3. Fuzzy name match (threshold 70) ---
    if request.name:
        normalized_name = _normalize_name(request.name)

        company_titles = [
            (company["title"], idx) for idx, company in enumerate(companies) if company.get("title")
        ]
        match = process.extractOne(
            normalized_name,
            [_normalize_name(title) for title, _ in company_titles],
            scorer=fuzz.token_sort_ratio,
        )

        if match and match[1] >= 70:
            matched_title_idx = match[2]
            original_idx = company_titles[matched_title_idx][1]
            company = companies[original_idx]
            return LookupCIKOutput(
                cik=_pad_cik(company["cik_str"]),
                name=company["title"],
                ticker=company.get("ticker"),
                exchange=None,
            )

    # --- 4. EFTS fallback (covers tickers absent from company_tickers.json) ---
    efts_query = request.ticker or request.name
    if efts_query:
        result = await _efts_lookup(efts_query)
        if result:
            logger.info(f"Resolved '{efts_query}' via EFTS fallback → CIK {result.cik}")
            return result

    raise ValueError(
        f"No company found matching ticker='{request.ticker}' or name='{request.name}'"
    )

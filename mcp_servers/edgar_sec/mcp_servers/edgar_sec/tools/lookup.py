"""CIK lookup tool for mapping ticker/company name to CIK.

Uses the SEC ``company_tickers.json`` dataset as the primary source.  When
that file does not cover a ticker (it only holds ~10 K active filers), the
SEC EFTS full-text search API is tried online.  A lookup never returns a
company it cannot tie to the request: a miss raises with the closest names.
"""

import asyncio
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

import httpx
from config import EDGAR_OFFLINE_DATA_DIR, EDGAR_OFFLINE_MODE, EDGAR_USER_AGENT
from loguru import logger
from models import LookupCIKInput, LookupCIKOutput
from rapidfuzz import fuzz, process
from utils.cik_cache import get_company_tickers

_EFTS_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
_CIK_RE = re.compile(r"\(CIK\s+(\d+)\)")


def _normalize_ticker(ticker: str) -> str:
    """Normalize ticker for matching (uppercase, no whitespace or share-class separators)."""
    return re.sub(r"[\s./-]", "", ticker.upper())


def _normalize_name(name: str) -> str:
    """Normalize company name for matching (lowercase, strip common suffixes)."""
    # SEC titles end in a state-of-incorporation tag ("MATTEL INC /DE/") and mix
    # hyphens with spaces ("Knight-Swift"), neither of which names a different company.
    name = re.sub(r"\s+/[a-z]{2,3}/?$", "", name.lower().strip())
    name = re.sub(r"\s+", " ", name.replace("-", " "))
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


async def _efts_lookup(query: str, *, is_ticker: bool = True) -> LookupCIKOutput | None:
    """Last-resort CIK lookup via the SEC EFTS full-text search API.

    The EFTS response embeds entity names in the format
    ``COMPANY NAME  (TICKER)  (CIK 0001234567)``.  Full-text hits include any
    filing that mentions the query, so an entity is returned only when its
    ticker (or, for a name query, its normalized name) equals the query.
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
    except Exception as e:
        logger.debug(f"EFTS lookup failed for '{query}': {e}")
        return None

    for hit in data.get("hits", {}).get("hits", []):
        for entry in hit.get("_source", {}).get("display_names", []):
            cik_match = _CIK_RE.search(entry)
            if not cik_match:
                continue
            ticker_match = re.search(r"\(([A-Z0-9., /-]+)\)", entry.split("(CIK")[0])
            entry_tickers = (
                [_normalize_ticker(t) for t in ticker_match.group(1).split(",")]
                if ticker_match
                else []
            )
            entry_name = entry.split("(")[0].strip()
            if is_ticker:
                matched = _normalize_ticker(query) in entry_tickers
            else:
                matched = _normalize_name(entry_name) == _normalize_name(query)
            if matched:
                return LookupCIKOutput(
                    cik=_pad_cik(cik_match.group(1)),
                    name=entry_name,
                    ticker=ticker_match.group(1).split(",")[0].strip() if ticker_match else None,
                    exchange=None,
                    match_type="efts",
                )
    return None


def _closest_names(request: LookupCIKInput, companies: list[dict], limit: int = 5) -> list[str]:
    """Up to ``limit`` companies resembling the request, as 'TITLE (TICKER)'."""
    if request.ticker:
        pool = [c for c in companies if c.get("ticker") and c.get("title")]
        query, choices = (
            _normalize_ticker(request.ticker),
            [_normalize_ticker(c["ticker"]) for c in pool],
        )
        scorer, cutoff = fuzz.ratio, 60
    else:
        pool = [c for c in companies if c.get("title")]
        query, choices = (
            _normalize_name(request.name or ""),
            [_normalize_name(c["title"]) for c in pool],
        )
        scorer, cutoff = fuzz.WRatio, 60
    out = []
    for _, _, idx in process.extract(
        query, choices, scorer=scorer, limit=limit, score_cutoff=cutoff
    ):
        c = pool[idx]
        out.append(f"{c['title']} ({c['ticker']})" if c.get("ticker") else c["title"])
    return out


def _offline_exchange(cik: str, ticker: str | None) -> str | None:
    """Exchange listed for ``ticker`` in the offline submissions file, if any."""
    path = EDGAR_OFFLINE_DATA_DIR / "submissions" / f"CIK{cik}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    tickers, exchanges = data.get("tickers") or [], data.get("exchanges") or []
    if ticker in tickers and tickers.index(ticker) < len(exchanges):
        return exchanges[tickers.index(ticker)]
    return exchanges[0] if exchanges else None


async def _output(company: dict, match_type: str) -> LookupCIKOutput:
    cik = _pad_cik(company["cik_str"])
    exchange = None
    if EDGAR_OFFLINE_MODE:
        exchange = await asyncio.to_thread(_offline_exchange, cik, company.get("ticker"))
    return LookupCIKOutput(
        cik=cik,
        name=company["title"],
        ticker=company.get("ticker"),
        exchange=exchange,
        match_type=match_type,
    )


async def lookup_cik(request: LookupCIKInput) -> LookupCIKOutput:
    """Map company ticker symbol or name to 10-digit zero-padded CIK."""
    try:
        companies_data = await get_company_tickers()
    except Exception as e:
        logger.error(f"Failed to fetch company tickers data: {e}")
        raise ValueError(f"Unable to fetch company data from SEC: {e}")

    companies = list(companies_data.values())

    # --- 1. Exact ticker match (share-class separators ignored: BRK.B == BRK-B) ---
    if request.ticker:
        normalized_ticker = _normalize_ticker(request.ticker)
        for company in companies:
            raw_ticker = company.get("ticker")
            if raw_ticker and _normalize_ticker(raw_ticker) == normalized_ticker:
                return await _output(company, "exact_ticker")

    # --- 2. Exact name match (legal suffixes and punctuation ignored) ---
    # A ticker the dataset lacks means the company is absent; its name would only find lookalikes.
    if request.name and not request.ticker:
        normalized_name = _normalize_name(request.name)
        exact = {
            c["cik_str"]: c
            for c in companies
            if c.get("title") and _normalize_name(c["title"]) == normalized_name
        }
        if len(exact) == 1:
            return await _output(next(iter(exact.values())), "exact_name")

    # --- 3. EFTS fallback (covers tickers absent from company_tickers.json) ---
    # Offline, a CIK found online would have no submissions or facts to serve.
    if not EDGAR_OFFLINE_MODE:
        result = await _efts_lookup(
            request.ticker or request.name or "", is_ticker=bool(request.ticker)
        )
        if result:
            logger.info(f"Resolved '{request.ticker or request.name}' via EFTS → CIK {result.cik}")
            return result

    message = f"No company found matching ticker='{request.ticker}' or name='{request.name}'."
    closest = _closest_names(request, companies)
    if closest:
        message += " Closest companies available: " + "; ".join(closest) + "."
    raise ValueError(message)

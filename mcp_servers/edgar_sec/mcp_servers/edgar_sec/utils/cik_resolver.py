"""Helper to resolve CIK from ticker/name or validate provided CIK."""

from models import LookupCIKInput
from tools.lookup import lookup_cik


async def resolve_cik(cik: str | None, ticker: str | None, name: str | None) -> str:
    """Resolve CIK from provided identifiers.

    Args:
        cik: Optional CIK (will be validated/padded if provided)
        ticker: Optional ticker symbol
        name: Optional company name

    Returns:
        10-digit zero-padded CIK

    Raises:
        ValueError: If lookup fails or no identifier provided
    """
    # If CIK is provided, strip "CIK" prefix and pad it
    if cik:
        cik = cik.strip()
        # Remove "CIK" prefix if present (case-insensitive), handle multiple prefixes
        while cik.upper().startswith("CIK"):
            cik = cik[3:].strip()
        return cik.zfill(10)

    # Otherwise lookup from ticker or name
    if ticker or name:
        result = await lookup_cik(LookupCIKInput(ticker=ticker, name=name))
        return result.cik

    raise ValueError("Must provide either cik, ticker, or name")

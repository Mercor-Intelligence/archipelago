"""Parse Bloomberg security identifiers to tickers."""


def parse_bloomberg_security(security: str) -> tuple[str, str | None]:
    """Parse Bloomberg format to ticker and exchange.

    Examples:
        "AAPL US Equity" -> ("AAPL", "US")
        "MSFT" -> ("MSFT", None)
    """
    parts = security.split()
    if len(parts) >= 2 and parts[1].upper() == "US" and parts[-1].upper() == "EQUITY":
        return parts[0].upper(), parts[1].upper()
    return security.upper(), None

"""Maps Bloomberg field mnemonics to OpenBB provider fields."""

from typing import Any

# Bloomberg → OpenBB field mappings per provider
FIELD_MAPPINGS = {
    "fmp": {
        "PX_LAST": "price",
        "PX_OPEN": "open",
        "PX_HIGH": "high",
        "PX_LOW": "low",
        "VOLUME": "volume",
        "PX_BID": "bid",
        "PX_ASK": "ask",
    },
    "yfinance": {
        "PX_LAST": "regularMarketPrice",
        "PX_OPEN": "regularMarketOpen",
        "PX_HIGH": "regularMarketDayHigh",
        "PX_LOW": "regularMarketDayLow",
        "VOLUME": "regularMarketVolume",
    },
}


def map_bloomberg_to_provider(bloomberg_fields: list[str], provider: str) -> dict[str, str]:
    """Map Bloomberg fields to provider-specific fields."""
    provider_map = FIELD_MAPPINGS.get(provider, FIELD_MAPPINGS["fmp"])
    return {bbg: provider_map[bbg] for bbg in bloomberg_fields if bbg in provider_map}


def map_provider_to_bloomberg(
    provider_data: dict[str, Any], field_map: dict[str, str]
) -> dict[str, Any]:
    """Map provider data back to Bloomberg field names."""
    reverse_map = {v: k for k, v in field_map.items()}
    return {reverse_map[k]: v for k, v in provider_data.items() if k in reverse_map}

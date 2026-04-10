from typing import Any


class SectorMapper:
    """
    Utility class to map common/GICS sector names to the specific,
    lowercased strings required by the underlying OpenBB provider for screening.
    """

    # Map GICS sectors (from enums.py) and common client-side names
    # to the OpenBB provider's expected keys (lowercase, specific).
    SECTOR_MAP = {
        "Energy": "energy",
        "Materials": "materials",
        "Industrials": "industrials",
        "Consumer Discretionary": "consumer_cyclical",
        "Consumer Staples": "consumer_defensive",
        "Health Care": "healthcare",
        "Financials": "financial_services",
        "Information Technology": "technology",
        "Technology": "technology",
        "Communication Services": "communication_services",
        "Utilities": "utilities",
        "Real Estate": "real_estate",
    }

    @staticmethod
    def map_to_provider(sector_value: str | Any) -> str:
        """
        Translates a sector name (like 'Technology') into the provider's
        required format (like 'technology').

        Args:
            sector_value: The input sector name (string or Enum value).

        Returns:
            The provider-compatible sector name (str).
        """
        # 1. Convert Enum/object to a standard string key
        sector_key = str(sector_value)

        # 2. Map the key, defaulting to the original key if no map entry is found.
        return SectorMapper.SECTOR_MAP.get(sector_key, sector_key)

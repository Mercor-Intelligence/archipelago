"""
Field Registry for Bloomberg Field Mappings

This module manages Bloomberg field definitions and their mappings to OpenBB/yfinance fields.
Supports classification of fields as fully_supported, approximated, or unsupported.
"""

from enum import Enum

from pydantic import BaseModel, Field


class SupportLevel(str, Enum):
    """Field support levels."""

    FULLY_SUPPORTED = "fully_supported"
    APPROXIMATED = "approximated"
    UNSUPPORTED = "unsupported"


class FieldDefinition(BaseModel):
    """
    Definition of a Bloomberg field with mapping information.

    Attributes:
        bloomberg_mnemonic: Bloomberg field identifier (e.g., "PX_LAST")
        support_level: Level of support for this field
        openbb_mapping: OpenBB/yfinance field name (None if unsupported)
        description: Human-readable description
        note: Additional notes about approximations or limitations
        reason: Reason for lack of support (for unsupported fields)
    """

    bloomberg_mnemonic: str = Field(..., description="Bloomberg field mnemonic")
    support_level: SupportLevel = Field(..., description="Support level")
    openbb_mapping: str | None = Field(default=None, description="OpenBB/yfinance field mapping")
    description: str = Field(..., description="Field description")
    note: str | None = Field(default=None, description="Additional notes")
    reason: str | None = Field(default=None, description="Reason for unsupported status")


class FieldRegistry:
    """
    Registry of all supported Bloomberg fields.

    Provides lookup capabilities and validation for field requests.
    """

    # Core price fields (fully supported)
    _PRICE_FIELDS: dict[str, FieldDefinition] = {
        "PX_LAST": FieldDefinition(
            bloomberg_mnemonic="PX_LAST",
            support_level=SupportLevel.FULLY_SUPPORTED,
            openbb_mapping="close",
            description="Last price",
        ),
        "PX_OPEN": FieldDefinition(
            bloomberg_mnemonic="PX_OPEN",
            support_level=SupportLevel.FULLY_SUPPORTED,
            openbb_mapping="open",
            description="Opening price",
        ),
        "PX_HIGH": FieldDefinition(
            bloomberg_mnemonic="PX_HIGH",
            support_level=SupportLevel.FULLY_SUPPORTED,
            openbb_mapping="high",
            description="High price",
        ),
        "PX_LOW": FieldDefinition(
            bloomberg_mnemonic="PX_LOW",
            support_level=SupportLevel.FULLY_SUPPORTED,
            openbb_mapping="low",
            description="Low price",
        ),
        "VOLUME": FieldDefinition(
            bloomberg_mnemonic="VOLUME",
            support_level=SupportLevel.FULLY_SUPPORTED,
            openbb_mapping="volume",
            description="Trading volume",
        ),
        "PX_VOLUME": FieldDefinition(
            bloomberg_mnemonic="PX_VOLUME",
            support_level=SupportLevel.FULLY_SUPPORTED,
            openbb_mapping="volume",
            description="Last trade volume (alias for VOLUME)",
            note=None,
        ),
    }

    # Calculated/approximated fields
    _APPROXIMATED_FIELDS: dict[str, FieldDefinition] = {
        "VWAP": FieldDefinition(
            bloomberg_mnemonic="VWAP",
            support_level=SupportLevel.APPROXIMATED,
            openbb_mapping=None,  # Custom calculation required
            description="Volume-weighted average price",
            note="Approximated as cumulative volume-weighted typical price: Σ((high + low + close) / 3 × volume) / Σ(volume). Note: Simplified calculation, may differ from intraday VWAP.",
        ),
        "TURNOVER": FieldDefinition(
            bloomberg_mnemonic="TURNOVER",
            support_level=SupportLevel.APPROXIMATED,
            openbb_mapping=None,
            description="Trading turnover (value traded)",
            note="Approximated as volume * close price",
        ),
    }

    # Unsupported fields (not available in OpenBB/yfinance)
    _UNSUPPORTED_FIELDS: dict[str, FieldDefinition] = {
        "TRADE_COUNT": FieldDefinition(
            bloomberg_mnemonic="TRADE_COUNT",
            support_level=SupportLevel.UNSUPPORTED,
            openbb_mapping=None,
            description="Number of trades",
            reason="Not available in OpenBB/yfinance historical data",
        ),
        "BID": FieldDefinition(
            bloomberg_mnemonic="BID",
            support_level=SupportLevel.UNSUPPORTED,
            openbb_mapping=None,
            description="Bid price",
            reason="Not available for historical data (intraday only)",
        ),
        "ASK": FieldDefinition(
            bloomberg_mnemonic="ASK",
            support_level=SupportLevel.UNSUPPORTED,
            openbb_mapping=None,
            description="Ask price",
            reason="Not available for historical data (intraday only)",
        ),
        "BID_SIZE": FieldDefinition(
            bloomberg_mnemonic="BID_SIZE",
            support_level=SupportLevel.UNSUPPORTED,
            openbb_mapping=None,
            description="Bid size",
            reason="Not available for historical data (intraday only)",
        ),
        "ASK_SIZE": FieldDefinition(
            bloomberg_mnemonic="ASK_SIZE",
            support_level=SupportLevel.UNSUPPORTED,
            openbb_mapping=None,
            description="Ask size",
            reason="Not available for historical data (intraday only)",
        ),
        "NUM_TRADES": FieldDefinition(
            bloomberg_mnemonic="NUM_TRADES",
            support_level=SupportLevel.UNSUPPORTED,
            openbb_mapping=None,
            description="Number of trades",
            reason="Not available in OpenBB/yfinance",
        ),
    }

    def __init__(self):
        """Initialize the field registry."""
        self._all_fields = {
            **self._PRICE_FIELDS,
            **self._APPROXIMATED_FIELDS,
            **self._UNSUPPORTED_FIELDS,
        }

    def get_field(self, mnemonic: str) -> FieldDefinition | None:
        """Get field definition by Bloomberg mnemonic."""
        return self._all_fields.get(mnemonic)

    def is_supported(self, mnemonic: str) -> bool:
        """Check if a field is supported (fully or approximated)."""
        field = self.get_field(mnemonic)
        if not field:
            return False
        return field.support_level in [SupportLevel.FULLY_SUPPORTED, SupportLevel.APPROXIMATED]

    def is_fully_supported(self, mnemonic: str) -> bool:
        """Check if a field is fully supported."""
        field = self.get_field(mnemonic)
        return field is not None and field.support_level == SupportLevel.FULLY_SUPPORTED

    def get_openbb_mapping(self, mnemonic: str) -> str | None:
        """Get OpenBB field mapping for a Bloomberg mnemonic."""
        field = self.get_field(mnemonic)
        return field.openbb_mapping if field else None

    def get_unsupported_fields(self, mnemonics: list[str]) -> list[FieldDefinition]:
        """Get list of unsupported fields from a list of mnemonics."""
        result: list[FieldDefinition] = []
        for m in mnemonics:
            field = self.get_field(m)
            if field and field.support_level == SupportLevel.UNSUPPORTED:
                result.append(field)
        return result

    def validate_fields(self, mnemonics: list[str]) -> tuple[list[str], list[str], list[str]]:
        """
        Validate a list of field mnemonics.

        Returns:
            Tuple of (supported_fields, approximated_fields, unsupported_fields)
        """
        supported = []
        approximated = []
        unsupported = []

        for mnemonic in mnemonics:
            field = self.get_field(mnemonic)
            if not field:
                unsupported.append(mnemonic)
            elif field.support_level == SupportLevel.FULLY_SUPPORTED:
                supported.append(mnemonic)
            elif field.support_level == SupportLevel.APPROXIMATED:
                approximated.append(mnemonic)
            else:
                unsupported.append(mnemonic)

        return supported, approximated, unsupported

    def get_all_supported_mnemonics(self) -> list[str]:
        """Get list of all supported field mnemonics."""
        return [
            m
            for m, f in self._all_fields.items()
            if f.support_level in [SupportLevel.FULLY_SUPPORTED, SupportLevel.APPROXIMATED]
        ]

    def to_dict(self) -> dict[str, dict]:
        """Export registry as dictionary for documentation."""
        return {mnemonic: field.model_dump() for mnemonic, field in self._all_fields.items()}

    def list_all_fields(self) -> list[dict]:
        """
        List all fields as dictionaries for API responses.

        Returns:
            List of field definitions as dictionaries
        """
        return [
            {
                "mnemonic": field.bloomberg_mnemonic,
                "support_level": field.support_level.value,
                "mapping": field.openbb_mapping,
                "description": field.description,
                "note": field.note,
                "reason": field.reason,
            }
            for field in self._all_fields.values()
        ]


# Singleton instance
field_registry = FieldRegistry()

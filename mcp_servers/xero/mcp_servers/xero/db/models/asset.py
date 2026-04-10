"""Asset model for fixed asset register."""

import json

from sqlalchemy import Boolean, Column, Float, String, Text

from mcp_servers.xero.db.models.invoice import normalize_xero_date
from mcp_servers.xero.db.session import Base


def parse_bool(value) -> bool:
    """Parse boolean from various formats (bool, string, None)."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("true", "1", "yes")
    return bool(value)


class Asset(Base):
    """Asset database model for fixed asset register."""

    __tablename__ = "assets"

    asset_id = Column(String, primary_key=True)
    asset_name = Column(String, nullable=True)
    asset_number = Column(String, nullable=True)
    purchase_date = Column(String, nullable=True)
    purchase_price = Column(Float, nullable=True)
    disposal_price = Column(Float, nullable=True)
    disposal_date = Column(String, nullable=True)
    asset_status = Column(String, nullable=True)  # Draft, Registered, Disposed
    serial_number = Column(String, nullable=True)
    warranty_expiry_date = Column(String, nullable=True)
    description = Column(Text, nullable=True)
    asset_type_id = Column(String, nullable=True)
    accounting_book_value = Column(Float, nullable=True)
    can_rollback = Column(Boolean, default=False)
    book_depreciation_setting = Column(Text, nullable=True)  # JSON
    book_depreciation_detail = Column(Text, nullable=True)  # JSON

    def to_dict(self) -> dict:
        """Convert to Xero Assets API format (camelCase)."""
        result: dict = {
            "assetId": self.asset_id,
            "assetName": self.asset_name,
            "assetNumber": self.asset_number,
            "purchaseDate": self.purchase_date,
            "purchasePrice": self.purchase_price,
            "disposalPrice": self.disposal_price,
            "assetStatus": self.asset_status,
        }
        if self.disposal_date is not None:
            result["disposalDate"] = self.disposal_date
        if self.serial_number is not None:
            result["serialNumber"] = self.serial_number
        if self.warranty_expiry_date is not None:
            result["warrantyExpiryDate"] = self.warranty_expiry_date
        if self.description is not None:
            result["description"] = self.description
        if self.asset_type_id is not None:
            result["assetTypeId"] = self.asset_type_id
        if self.accounting_book_value is not None:
            result["accountingBookValue"] = self.accounting_book_value
        if self.can_rollback is not None:
            result["canRollback"] = self.can_rollback
        if self.book_depreciation_setting is not None:
            result["bookDepreciationSetting"] = json.loads(str(self.book_depreciation_setting))
        if self.book_depreciation_detail is not None:
            result["bookDepreciationDetail"] = json.loads(str(self.book_depreciation_detail))
        return result

    @classmethod
    def from_dict(cls, data: dict):
        """Create from dict (supports both API format and CSV format)."""
        book_depreciation_setting = data.get("bookDepreciationSetting") or data.get(
            "book_depreciation_setting"
        )
        book_depreciation_detail = data.get("bookDepreciationDetail") or data.get(
            "book_depreciation_detail"
        )

        # Handle JSON strings from CSV (already serialized)
        if isinstance(book_depreciation_setting, str):
            book_depreciation_setting_json = book_depreciation_setting
        else:
            book_depreciation_setting_json = (
                json.dumps(book_depreciation_setting) if book_depreciation_setting else None
            )

        if isinstance(book_depreciation_detail, str):
            book_depreciation_detail_json = book_depreciation_detail
        else:
            book_depreciation_detail_json = (
                json.dumps(book_depreciation_detail) if book_depreciation_detail else None
            )

        # Handle date fields
        raw_purchase_date = data.get("purchaseDate") or data.get("purchase_date")
        raw_disposal_date = data.get("disposalDate") or data.get("disposal_date")
        raw_warranty_expiry_date = data.get("warrantyExpiryDate") or data.get(
            "warranty_expiry_date"
        )

        # Handle numeric fields - use 'in' check to preserve zero values
        # Treat empty strings as None for numeric conversion
        purchase_price = (
            data["purchasePrice"] if "purchasePrice" in data else data.get("purchase_price")
        )
        if purchase_price is None or purchase_price == "":
            purchase_price = 0

        disposal_price = (
            data["disposalPrice"] if "disposalPrice" in data else data.get("disposal_price")
        )
        if disposal_price is None or disposal_price == "":
            disposal_price = 0

        accounting_book_value = (
            data["accountingBookValue"]
            if "accountingBookValue" in data
            else data.get("accounting_book_value")
        )
        # Treat empty string as None for optional numeric field
        if accounting_book_value == "":
            accounting_book_value = None

        return cls(
            asset_id=data.get("assetId") or data.get("asset_id"),
            asset_name=data.get("assetName") or data.get("asset_name"),
            asset_number=data.get("assetNumber") or data.get("asset_number"),
            purchase_date=normalize_xero_date(raw_purchase_date),
            purchase_price=float(purchase_price),
            disposal_price=float(disposal_price),
            disposal_date=normalize_xero_date(raw_disposal_date),
            asset_status=data.get("assetStatus") or data.get("asset_status"),
            serial_number=data.get("serialNumber") or data.get("serial_number"),
            warranty_expiry_date=normalize_xero_date(raw_warranty_expiry_date),
            description=data.get("description"),
            asset_type_id=data.get("assetTypeId") or data.get("asset_type_id"),
            accounting_book_value=float(accounting_book_value)
            if accounting_book_value is not None
            else None,
            can_rollback=parse_bool(
                data["canRollback"] if "canRollback" in data else data.get("can_rollback")
            ),
            book_depreciation_setting=book_depreciation_setting_json,
            book_depreciation_detail=book_depreciation_detail_json,
        )

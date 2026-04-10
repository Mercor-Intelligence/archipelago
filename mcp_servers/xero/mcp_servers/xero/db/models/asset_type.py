"""Asset type model for asset depreciation types."""

import json

from sqlalchemy import Column, Integer, String, Text

from mcp_servers.xero.db.session import Base


class AssetType(Base):
    """Asset type database model for asset depreciation types."""

    __tablename__ = "asset_types"

    asset_type_id = Column(String, primary_key=True)
    asset_type_name = Column(String, nullable=True)
    fixed_asset_account_id = Column(String, nullable=True)
    depreciation_expense_account_id = Column(String, nullable=True)
    accumulated_depreciation_account_id = Column(String, nullable=True)
    book_depreciation_setting = Column(Text, nullable=True)  # JSON
    locks = Column(Integer, nullable=True)  # Number of assets using this type

    def to_dict(self) -> dict:
        """Convert to Xero Assets API format (camelCase)."""
        result: dict = {
            "assetTypeId": self.asset_type_id,
            "assetTypeName": self.asset_type_name,
            "fixedAssetAccountId": self.fixed_asset_account_id,
            "depreciationExpenseAccountId": self.depreciation_expense_account_id,
            "accumulatedDepreciationAccountId": self.accumulated_depreciation_account_id,
        }
        if self.book_depreciation_setting is not None:
            result["bookDepreciationSetting"] = json.loads(str(self.book_depreciation_setting))
        if self.locks is not None:
            result["locks"] = self.locks
        return result

    @classmethod
    def from_dict(cls, data: dict):
        """Create from dict (supports both API format and CSV format)."""
        book_depreciation_setting = data.get("bookDepreciationSetting") or data.get(
            "book_depreciation_setting"
        )

        # Handle JSON strings from CSV (already serialized)
        if isinstance(book_depreciation_setting, str):
            book_depreciation_setting_json = book_depreciation_setting
        else:
            book_depreciation_setting_json = (
                json.dumps(book_depreciation_setting) if book_depreciation_setting else None
            )

        # Handle locks field - treat empty strings as None for numeric conversion
        locks = data.get("locks")
        if locks is not None and locks != "":
            locks = int(locks)
        elif locks == "":
            locks = None

        return cls(
            asset_type_id=data.get("assetTypeId") or data.get("asset_type_id"),
            asset_type_name=data.get("assetTypeName") or data.get("asset_type_name"),
            fixed_asset_account_id=data.get("fixedAssetAccountId")
            or data.get("fixed_asset_account_id"),
            depreciation_expense_account_id=(
                data.get("depreciationExpenseAccountId")
                or data.get("depreciation_expense_account_id")
            ),
            accumulated_depreciation_account_id=(
                data.get("accumulatedDepreciationAccountId")
                or data.get("accumulated_depreciation_account_id")
            ),
            book_depreciation_setting=book_depreciation_setting_json,
            locks=locks,
        )

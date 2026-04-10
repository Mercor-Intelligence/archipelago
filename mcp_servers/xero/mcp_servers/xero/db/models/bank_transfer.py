"""Bank transfer model for inter-account transfers."""

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


class BankTransfer(Base):
    """Bank transfer database model for inter-account transfers."""

    __tablename__ = "bank_transfers"

    bank_transfer_id = Column(String, primary_key=True)
    created_date_utc_string = Column(String, nullable=True)
    created_date_utc = Column(String, nullable=True)
    date_string = Column(String, nullable=True)
    date = Column(String, nullable=True)
    amount = Column(Float, nullable=True)
    from_bank_account = Column(Text, nullable=True)  # JSON
    to_bank_account = Column(Text, nullable=True)  # JSON
    from_bank_transaction_id = Column(String, nullable=True)
    to_bank_transaction_id = Column(String, nullable=True)
    from_is_reconciled = Column(Boolean, default=False)
    to_is_reconciled = Column(Boolean, default=False)
    reference = Column(String, nullable=True)

    def to_dict(self) -> dict:
        """Convert to Xero API format."""
        from_bank_account_data = (
            json.loads(str(self.from_bank_account)) if self.from_bank_account is not None else {}
        )
        to_bank_account_data = (
            json.loads(str(self.to_bank_account)) if self.to_bank_account is not None else {}
        )

        return {
            "BankTransferID": self.bank_transfer_id,
            "CreatedDateUTCString": self.created_date_utc_string,
            "CreatedDateUTC": self.created_date_utc,
            "DateString": self.date_string,
            "Date": self.date,
            "Amount": self.amount,
            "FromBankAccount": from_bank_account_data,
            "ToBankAccount": to_bank_account_data,
            "FromBankTransactionID": self.from_bank_transaction_id,
            "ToBankTransactionID": self.to_bank_transaction_id,
            "FromIsReconciled": self.from_is_reconciled,
            "ToIsReconciled": self.to_is_reconciled,
            "Reference": self.reference,
        }

    @classmethod
    def from_dict(cls, data: dict):
        """Create from dict (supports both API format and CSV format)."""
        from_bank_account = data.get("FromBankAccount") or data.get("from_bank_account")
        to_bank_account = data.get("ToBankAccount") or data.get("to_bank_account")

        # Handle JSON strings from CSV (already serialized)
        if isinstance(from_bank_account, str):
            from_bank_account_json = from_bank_account
        else:
            from_bank_account_json = json.dumps(from_bank_account) if from_bank_account else "{}"

        if isinstance(to_bank_account, str):
            to_bank_account_json = to_bank_account
        else:
            to_bank_account_json = json.dumps(to_bank_account) if to_bank_account else "{}"

        # Handle date fields
        raw_date = data.get("DateString") or data.get("Date") or data.get("date")

        # Handle amount - use 'in' check to preserve zero values
        # Treat empty strings as None for numeric conversion
        amount_val = data["Amount"] if "Amount" in data else data.get("amount")
        if amount_val is None or amount_val == "":
            amount_val = 0
        elif isinstance(amount_val, str):
            amount_val = float(amount_val)

        return cls(
            bank_transfer_id=data.get("BankTransferID") or data.get("bank_transfer_id"),
            created_date_utc_string=data.get("CreatedDateUTCString")
            or data.get("created_date_utc_string"),
            created_date_utc=data.get("CreatedDateUTC") or data.get("created_date_utc"),
            date_string=data.get("DateString") or data.get("date_string"),
            date=normalize_xero_date(raw_date),
            amount=float(amount_val),
            from_bank_account=from_bank_account_json,
            to_bank_account=to_bank_account_json,
            from_bank_transaction_id=data.get("FromBankTransactionID")
            or data.get("from_bank_transaction_id"),
            to_bank_transaction_id=data.get("ToBankTransactionID")
            or data.get("to_bank_transaction_id"),
            from_is_reconciled=parse_bool(
                data["FromIsReconciled"]
                if "FromIsReconciled" in data
                else data.get("from_is_reconciled")
            ),
            to_is_reconciled=parse_bool(
                data["ToIsReconciled"] if "ToIsReconciled" in data else data.get("to_is_reconciled")
            ),
            reference=data.get("Reference") or data.get("reference"),
        )

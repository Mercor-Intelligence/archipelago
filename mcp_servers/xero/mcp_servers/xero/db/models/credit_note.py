"""Credit note model for returns and adjustments."""

import json

from sqlalchemy import Column, Float, String, Text

from mcp_servers.xero.db.models.invoice import normalize_xero_date
from mcp_servers.xero.db.session import Base


class CreditNote(Base):
    """Credit note database model for returns and adjustments."""

    __tablename__ = "credit_notes"

    credit_note_id = Column(String, primary_key=True)
    credit_note_number = Column(String, nullable=True)
    type = Column(String, nullable=True)  # ACCRECCREDIT or ACCPAYCREDIT
    status = Column(String, nullable=True)
    contact = Column(Text, nullable=True)  # JSON
    date = Column(String, nullable=True)
    line_items = Column(Text, nullable=True)  # JSON
    line_amount_types = Column(String, nullable=True)
    sub_total = Column(Float, nullable=True)
    total_tax = Column(Float, nullable=True)
    total = Column(Float, nullable=True)
    remaining_credit = Column(Float, nullable=True)
    currency_code = Column(String, nullable=True)
    currency_rate = Column(Float, nullable=True)
    fully_paid_on_date = Column(String, nullable=True)
    allocations = Column(Text, nullable=True)  # JSON array
    updated_date_utc = Column(String, nullable=True)
    reference = Column(String, nullable=True)

    def to_dict(self) -> dict:
        """Convert to Xero API format."""
        contact_data = json.loads(str(self.contact)) if self.contact is not None else {}
        line_items_data = json.loads(str(self.line_items)) if self.line_items is not None else []

        result: dict = {
            "CreditNoteID": self.credit_note_id,
            "CreditNoteNumber": self.credit_note_number,
            "Type": self.type,
            "Status": self.status,
            "Contact": contact_data,
            "Date": self.date,
            "LineItems": line_items_data,
            "SubTotal": self.sub_total,
            "TotalTax": self.total_tax,
            "Total": self.total,
            "RemainingCredit": self.remaining_credit,
            "CurrencyCode": self.currency_code,
        }
        if self.line_amount_types is not None:
            result["LineAmountTypes"] = self.line_amount_types
        if self.currency_rate is not None:
            result["CurrencyRate"] = self.currency_rate
        if self.fully_paid_on_date is not None:
            result["FullyPaidOnDate"] = self.fully_paid_on_date
        if self.allocations is not None:
            result["Allocations"] = json.loads(str(self.allocations))
        if self.updated_date_utc is not None:
            result["UpdatedDateUTC"] = self.updated_date_utc
        if self.reference is not None:
            result["Reference"] = self.reference
        return result

    @classmethod
    def from_dict(cls, data: dict):
        """Create from dict (supports both API format and CSV format)."""
        contact = data.get("Contact") or data.get("contact")
        line_items = data.get("LineItems") or data.get("line_items")
        allocations = data.get("Allocations") or data.get("allocations")

        # Handle JSON strings from CSV (already serialized)
        if isinstance(contact, str):
            contact_json = contact
        else:
            contact_json = json.dumps(contact) if contact else "{}"

        if isinstance(line_items, str):
            line_items_json = line_items
        else:
            line_items_json = json.dumps(line_items) if line_items else "[]"

        if isinstance(allocations, str):
            allocations_json = allocations
        else:
            allocations_json = json.dumps(allocations) if allocations else None

        # Handle date fields
        raw_date = data.get("DateString") or data.get("Date") or data.get("date")
        raw_fully_paid_on_date = (
            data.get("FullyPaidOnDateString")
            or data.get("FullyPaidOnDate")
            or data.get("fully_paid_on_date")
        )

        # Handle currency rate - use 'in' check to preserve zero values
        # Treat empty strings as None for numeric conversion
        currency_rate = (
            data["CurrencyRate"] if "CurrencyRate" in data else data.get("currency_rate")
        )
        if currency_rate is not None and currency_rate != "":
            currency_rate = float(currency_rate)
        elif currency_rate == "":
            currency_rate = None

        return cls(
            credit_note_id=data.get("CreditNoteID") or data.get("credit_note_id"),
            credit_note_number=data.get("CreditNoteNumber") or data.get("credit_note_number"),
            type=data.get("Type") or data.get("type"),
            status=data.get("Status") or data.get("status"),
            contact=contact_json,
            date=normalize_xero_date(raw_date),
            line_items=line_items_json,
            line_amount_types=data.get("LineAmountTypes") or data.get("line_amount_types"),
            sub_total=float(data.get("SubTotal") or data.get("sub_total") or 0),
            total_tax=float(data.get("TotalTax") or data.get("total_tax") or 0),
            total=float(data.get("Total") or data.get("total") or 0),
            remaining_credit=float(
                data.get("RemainingCredit") or data.get("remaining_credit") or 0
            ),
            currency_code=data.get("CurrencyCode") or data.get("currency_code"),
            currency_rate=currency_rate,
            fully_paid_on_date=normalize_xero_date(raw_fully_paid_on_date),
            allocations=allocations_json,
            updated_date_utc=data.get("UpdatedDateUTC") or data.get("updated_date_utc"),
            reference=data.get("Reference") or data.get("reference"),
        )

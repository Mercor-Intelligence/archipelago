"""Quote model for sales quotes/estimates."""

import json

from sqlalchemy import Column, Float, String, Text

from mcp_servers.xero.db.models.invoice import normalize_xero_date
from mcp_servers.xero.db.session import Base


class Quote(Base):
    """Quote database model for sales quotes/estimates."""

    __tablename__ = "quotes"

    quote_id = Column(String, primary_key=True)
    quote_number = Column(String, nullable=True)
    reference = Column(String, nullable=True)
    status = Column(String, nullable=True)
    contact = Column(Text, nullable=True)  # JSON
    date = Column(String, nullable=True)
    expiry_date = Column(String, nullable=True)
    line_items = Column(Text, nullable=True)  # JSON
    sub_total = Column(Float, nullable=True)
    total_tax = Column(Float, nullable=True)
    total = Column(Float, nullable=True)
    currency_code = Column(String, nullable=True)
    title = Column(String, nullable=True)
    summary = Column(String, nullable=True)
    terms = Column(Text, nullable=True)
    branding_theme_id = Column(String, nullable=True)
    updated_date_utc = Column(String, nullable=True)

    def to_dict(self) -> dict:
        """Convert to Xero API format."""
        contact_data = json.loads(str(self.contact)) if self.contact is not None else {}
        line_items_data = json.loads(str(self.line_items)) if self.line_items is not None else []

        result: dict = {
            "QuoteID": self.quote_id,
            "QuoteNumber": self.quote_number,
            "Reference": self.reference,
            "Status": self.status,
            "Contact": contact_data,
            "Date": self.date,
            "ExpiryDate": self.expiry_date,
            "LineItems": line_items_data,
            "SubTotal": self.sub_total,
            "TotalTax": self.total_tax,
            "Total": self.total,
            "CurrencyCode": self.currency_code,
        }
        if self.title is not None:
            result["Title"] = self.title
        if self.summary is not None:
            result["Summary"] = self.summary
        if self.terms is not None:
            result["Terms"] = self.terms
        if self.branding_theme_id is not None:
            result["BrandingThemeID"] = self.branding_theme_id
        if self.updated_date_utc is not None:
            result["UpdatedDateUTC"] = self.updated_date_utc
        return result

    @classmethod
    def from_dict(cls, data: dict):
        """Create from dict (supports both API format and CSV format)."""
        contact = data.get("Contact") or data.get("contact")
        line_items = data.get("LineItems") or data.get("line_items")

        # Handle JSON strings from CSV (already serialized)
        if isinstance(contact, str):
            contact_json = contact
        else:
            contact_json = json.dumps(contact) if contact else "{}"

        if isinstance(line_items, str):
            line_items_json = line_items
        else:
            line_items_json = json.dumps(line_items) if line_items else "[]"

        # Handle date fields
        raw_date = data.get("DateString") or data.get("Date") or data.get("date")
        raw_expiry_date = (
            data.get("ExpiryDateString") or data.get("ExpiryDate") or data.get("expiry_date")
        )

        return cls(
            quote_id=data.get("QuoteID") or data.get("quote_id"),
            quote_number=data.get("QuoteNumber") or data.get("quote_number"),
            reference=data.get("Reference") or data.get("reference"),
            status=data.get("Status") or data.get("status"),
            contact=contact_json,
            date=normalize_xero_date(raw_date),
            expiry_date=normalize_xero_date(raw_expiry_date),
            line_items=line_items_json,
            sub_total=float(data.get("SubTotal") or data.get("sub_total") or 0),
            total_tax=float(data.get("TotalTax") or data.get("total_tax") or 0),
            total=float(data.get("Total") or data.get("total") or 0),
            currency_code=data.get("CurrencyCode") or data.get("currency_code"),
            title=data.get("Title") or data.get("title"),
            summary=data.get("Summary") or data.get("summary"),
            terms=data.get("Terms") or data.get("terms"),
            branding_theme_id=data.get("BrandingThemeID") or data.get("branding_theme_id"),
            updated_date_utc=data.get("UpdatedDateUTC") or data.get("updated_date_utc"),
        )

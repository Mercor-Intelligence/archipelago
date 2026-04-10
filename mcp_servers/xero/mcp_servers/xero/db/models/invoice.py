"""Invoice model."""

import json
import re
from datetime import datetime

from sqlalchemy import Column, Float, String, Text

from mcp_servers.xero.db.session import Base


def normalize_xero_date(date_value: str | None) -> str | None:
    """
    Convert various date formats to YYYY-MM-DD format.

    Handles:
    - /Date(1751673600000+0000)/ → 2025-07-05
    - 2025-07-05T00:00:00 → 2025-07-05
    - 2025-07-05 → 2025-07-05 (already correct)
    - 15/07/2024 → 2024-07-15
    - 07/15/2024 → 2024-07-15
    - 15-07-2024 → 2024-07-15
    - And many other common CSV formats

    Args:
        date_value: Date string in any common format

    Returns:
        Date in YYYY-MM-DD format if parseable, otherwise returns the original
        value to prevent data loss. Returns None only if input is None/empty.
    """
    if not date_value:
        return None

    # Already in YYYY-MM-DD format
    if re.match(r"^\d{4}-\d{2}-\d{2}$", date_value):
        return date_value

    # ISO format with time: 2025-07-05T00:00:00 or 2025-07-05T00:00:00Z
    if "T" in date_value:
        try:
            dt = datetime.fromisoformat(date_value.replace("Z", "+00:00"))
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            pass

    # Xero timestamp format: /Date(1751673600000+0000)/
    match = re.match(r"/Date\((\d+)([+-]\d+)?\)/", date_value)
    if match:
        timestamp_ms = int(match.group(1))
        dt = datetime.utcfromtimestamp(timestamp_ms / 1000)
        return dt.strftime("%Y-%m-%d")

    # Try common date formats found in CSV uploads
    # Format priority order matters for ambiguous dates (e.g., 01/02/2024)
    common_formats = [
        "%Y/%m/%d",  # 2024/07/15
        "%Y-%m-%d",  # 2024-07-15 (redundant but safe)
        "%d/%m/%Y",  # 15/07/2024 (DD/MM/YYYY - common in EU/UK)
        "%d-%m-%Y",  # 15-07-2024
        "%d.%m.%Y",  # 15.07.2024
        "%m/%d/%Y",  # 07/15/2024 (MM/DD/YYYY - common in US)
        "%m-%d-%Y",  # 07-15-2024
        "%d/%m/%y",  # 15/07/24 (2-digit year)
        "%m/%d/%y",  # 07/15/24
        "%d-%m-%y",  # 15-07-24
        "%m-%d-%y",  # 07-15-24
        "%Y%m%d",  # 20240715 (compact format)
        "%d %b %Y",  # 15 Jul 2024
        "%d %B %Y",  # 15 July 2024
        "%b %d, %Y",  # Jul 15, 2024
        "%B %d, %Y",  # July 15, 2024
    ]

    for fmt in common_formats:
        try:
            dt = datetime.strptime(date_value, fmt)
            return dt.strftime("%Y-%m-%d")
        except ValueError:
            continue

    # If all parsing attempts fail, preserve the original value to prevent data loss
    # The original value may still be usable depending on context
    return date_value


class Invoice(Base):
    """Invoice database model."""

    __tablename__ = "invoices"

    invoice_id = Column(String, primary_key=True)
    invoice_number = Column(String)
    type = Column(String)
    status = Column(String)
    date = Column(String)
    due_date = Column(String, nullable=True)
    contact = Column(Text)  # JSON
    line_items = Column(Text)  # JSON
    sub_total = Column(Float, nullable=True)
    total_tax = Column(Float, nullable=True)
    total = Column(Float, nullable=True)
    currency_code = Column(String, nullable=True)
    amount_due = Column(Float, nullable=True)
    amount_paid = Column(Float, nullable=True)
    amount_credited = Column(Float, nullable=True)

    def to_dict(self) -> dict:
        """Convert to Xero API format."""
        contact_data = json.loads(self.contact) if self.contact else {}  # type: ignore
        line_items_data = json.loads(self.line_items) if self.line_items else []  # type: ignore

        return {
            "InvoiceID": self.invoice_id,
            "InvoiceNumber": self.invoice_number,
            "Type": self.type,
            "Status": self.status,
            "Date": self.date,
            "DueDate": self.due_date,
            "Contact": contact_data,
            "LineItems": line_items_data,
            "SubTotal": self.sub_total,
            "TotalTax": self.total_tax,
            "Total": self.total,
            "CurrencyCode": self.currency_code,
            "AmountDue": self.amount_due,
            "AmountPaid": self.amount_paid,
            "AmountCredited": self.amount_credited,
        }

    @classmethod
    def from_dict(cls, data: dict):
        """Create from dict."""
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

        # Prefer DateString over Date for better format consistency
        raw_date = data.get("DateString") or data.get("Date") or data.get("date")
        raw_due_date = data.get("DueDateString") or data.get("DueDate") or data.get("due_date")

        return cls(
            invoice_id=data.get("InvoiceID") or data.get("invoice_id"),
            invoice_number=data.get("InvoiceNumber") or data.get("invoice_number"),
            type=data.get("Type") or data.get("type"),
            status=data.get("Status") or data.get("status"),
            date=normalize_xero_date(raw_date),
            due_date=normalize_xero_date(raw_due_date),
            contact=contact_json,
            line_items=line_items_json,
            sub_total=float(data.get("SubTotal") or data.get("sub_total") or 0),
            total_tax=float(data.get("TotalTax") or data.get("total_tax") or 0),
            total=float(data.get("Total") or data.get("total") or 0),
            currency_code=data.get("CurrencyCode") or data.get("currency_code"),
            amount_due=float(data.get("AmountDue") or data.get("amount_due") or 0),
            amount_paid=float(data.get("AmountPaid") or data.get("amount_paid") or 0),
            amount_credited=float(data.get("AmountCredited") or data.get("amount_credited") or 0),
        )

"""Purchase order model for procurement tracking."""

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


class PurchaseOrder(Base):
    """Purchase order database model for procurement tracking."""

    __tablename__ = "purchase_orders"

    purchase_order_id = Column(String, primary_key=True)
    purchase_order_number = Column(String, nullable=True)
    status = Column(String, nullable=True)
    contact = Column(Text, nullable=True)  # JSON
    date = Column(String, nullable=True)
    delivery_date = Column(String, nullable=True)
    expected_arrival_date = Column(String, nullable=True)
    line_items = Column(Text, nullable=True)  # JSON
    sub_total = Column(Float, nullable=True)
    total_tax = Column(Float, nullable=True)
    total = Column(Float, nullable=True)
    currency_code = Column(String, nullable=True)
    currency_rate = Column(Float, nullable=True)
    reference = Column(String, nullable=True)
    attention_to = Column(String, nullable=True)
    telephone = Column(String, nullable=True)
    delivery_address = Column(Text, nullable=True)
    delivery_instructions = Column(Text, nullable=True)
    is_discounted = Column(Boolean, default=False)
    type = Column(String, nullable=True)
    branding_theme_id = Column(String, nullable=True)
    line_amount_types = Column(String, nullable=True)
    updated_date_utc = Column(String, nullable=True)

    def to_dict(self) -> dict:
        """Convert to Xero API format."""
        contact_data = json.loads(str(self.contact)) if self.contact is not None else {}
        line_items_data = json.loads(str(self.line_items)) if self.line_items is not None else []

        result: dict = {
            "PurchaseOrderID": self.purchase_order_id,
            "PurchaseOrderNumber": self.purchase_order_number,
            "Status": self.status,
            "Contact": contact_data,
            "Date": self.date,
            "DeliveryDate": self.delivery_date,
            "LineItems": line_items_data,
            "SubTotal": self.sub_total,
            "TotalTax": self.total_tax,
            "Total": self.total,
            "CurrencyCode": self.currency_code,
        }
        if self.expected_arrival_date is not None:
            result["ExpectedArrivalDate"] = self.expected_arrival_date
        if self.currency_rate is not None:
            result["CurrencyRate"] = self.currency_rate
        if self.reference is not None:
            result["Reference"] = self.reference
        if self.attention_to is not None:
            result["AttentionTo"] = self.attention_to
        if self.telephone is not None:
            result["Telephone"] = self.telephone
        if self.delivery_address is not None:
            result["DeliveryAddress"] = self.delivery_address
        if self.delivery_instructions is not None:
            result["DeliveryInstructions"] = self.delivery_instructions
        if self.is_discounted is not None:
            result["IsDiscounted"] = self.is_discounted
        if self.type is not None:
            result["Type"] = self.type
        if self.branding_theme_id is not None:
            result["BrandingThemeID"] = self.branding_theme_id
        if self.line_amount_types is not None:
            result["LineAmountTypes"] = self.line_amount_types
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
        raw_delivery_date = (
            data.get("DeliveryDateString") or data.get("DeliveryDate") or data.get("delivery_date")
        )
        raw_expected_arrival_date = (
            data.get("ExpectedArrivalDateString")
            or data.get("ExpectedArrivalDate")
            or data.get("expected_arrival_date")
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
            purchase_order_id=data.get("PurchaseOrderID") or data.get("purchase_order_id"),
            purchase_order_number=data.get("PurchaseOrderNumber")
            or data.get("purchase_order_number"),
            status=data.get("Status") or data.get("status"),
            contact=contact_json,
            date=normalize_xero_date(raw_date),
            delivery_date=normalize_xero_date(raw_delivery_date),
            expected_arrival_date=normalize_xero_date(raw_expected_arrival_date),
            line_items=line_items_json,
            sub_total=float(data.get("SubTotal") or data.get("sub_total") or 0),
            total_tax=float(data.get("TotalTax") or data.get("total_tax") or 0),
            total=float(data.get("Total") or data.get("total") or 0),
            currency_code=data.get("CurrencyCode") or data.get("currency_code"),
            currency_rate=currency_rate,
            reference=data.get("Reference") or data.get("reference"),
            attention_to=data.get("AttentionTo") or data.get("attention_to"),
            telephone=data.get("Telephone") or data.get("telephone"),
            delivery_address=data.get("DeliveryAddress") or data.get("delivery_address"),
            delivery_instructions=data.get("DeliveryInstructions")
            or data.get("delivery_instructions"),
            is_discounted=parse_bool(
                data["IsDiscounted"] if "IsDiscounted" in data else data.get("is_discounted")
            ),
            type=data.get("Type") or data.get("type"),
            branding_theme_id=data.get("BrandingThemeID") or data.get("branding_theme_id"),
            line_amount_types=data.get("LineAmountTypes") or data.get("line_amount_types"),
            updated_date_utc=data.get("UpdatedDateUTC") or data.get("updated_date_utc"),
        )

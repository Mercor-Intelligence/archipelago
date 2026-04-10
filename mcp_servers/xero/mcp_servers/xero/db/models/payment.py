"""Payment model."""

from sqlalchemy import Column, Float, String

from mcp_servers.xero.db.models.invoice import normalize_xero_date
from mcp_servers.xero.db.session import Base


def _parse_optional_float(value: str | float | None) -> float | None:
    """Normalize numeric input for optional currency rate fields."""
    if value is None or value == "":
        return None

    try:
        return float(value)
    except (ValueError, TypeError):
        return None


class Payment(Base):
    """Payment database model."""

    __tablename__ = "payments"

    payment_id = Column(String, primary_key=True)
    invoice_id = Column(String)
    date = Column(String)
    amount = Column(Float)
    currency_code = Column(String, nullable=True)
    payment_type = Column(String, nullable=True)
    reference = Column(String, nullable=True)
    account_id = Column(String, nullable=True)
    currency_rate = Column(Float, nullable=True)
    status = Column(String, default="AUTHORISED")

    def to_dict(self) -> dict:
        """Convert to Xero API format."""
        return {
            "PaymentID": self.payment_id,
            "InvoiceID": self.invoice_id,
            "Date": self.date,
            "Amount": self.amount,
            "CurrencyCode": self.currency_code,
            "CurrencyRate": self.currency_rate,
            "Status": self.status,
            "PaymentType": self.payment_type,
            "Reference": self.reference,
            "AccountID": self.account_id,
        }

    @classmethod
    def from_dict(cls, data: dict):
        """Create from dict."""
        # Prefer DateString over Date for better format consistency
        raw_date = data.get("DateString") or data.get("Date") or data.get("date")

        return cls(
            payment_id=data.get("PaymentID") or data.get("payment_id"),
            invoice_id=data.get("InvoiceID") or data.get("invoice_id"),
            date=normalize_xero_date(raw_date),
            amount=float(data.get("Amount") or data.get("amount") or 0),
            currency_code=data.get("CurrencyCode") or data.get("currency_code"),
            payment_type=data.get("PaymentType") or data.get("payment_type"),
            reference=data.get("Reference") or data.get("reference"),
            account_id=data.get("AccountID") or data.get("account_id"),
            currency_rate=_parse_optional_float(
                data.get("CurrencyRate") or data.get("currency_rate")
            ),
            status=data.get("Status") or data.get("status") or "AUTHORISED",
        )

"""Account model."""

from sqlalchemy import Column, Float, String

from mcp_servers.xero.db.session import Base


def _parse_float(value: str | float | None) -> float | None:
    """Safely parse numeric strings into floats for optional fields."""
    if value is None or value == "":
        return 0.0

    try:
        return float(value)
    except (ValueError, TypeError):
        return 0.0


class Account(Base):
    """Account database model."""

    __tablename__ = "accounts"

    account_id = Column(String, primary_key=True)
    code = Column(String)
    name = Column(String)
    status = Column(String)
    type = Column(String)
    tax_type = Column(String, nullable=True)
    class_ = Column("class", String, nullable=True)
    currency_code = Column(String, nullable=True)
    bank_account_number = Column(String, nullable=True)
    opening_balance = Column(Float, nullable=True, default=0.0)

    def to_dict(self) -> dict:
        """Convert to Xero API format."""
        return {
            "AccountID": self.account_id,
            "Code": self.code,
            "Name": self.name,
            "Status": self.status,
            "Type": self.type,
            "TaxType": self.tax_type,
            "Class": self.class_,
            "CurrencyCode": self.currency_code,
            "BankAccountNumber": self.bank_account_number,
            "OpeningBalance": self.opening_balance,
        }

    @classmethod
    def from_dict(cls, data: dict):
        """Create from dict (supports both API format and CSV format)."""

        def get_field(primary: str, fallback: str):
            return data.get(primary) or data.get(fallback)

        def _first_present(*keys):
            for key in keys:
                if key in data:
                    value = data[key]
                    if value is not None and value != "":
                        return value
            return None

        return cls(
            account_id=get_field("AccountID", "account_id"),
            code=get_field("Code", "code"),
            name=get_field("Name", "name"),
            status=get_field("Status", "status"),
            type=get_field("Type", "type"),
            tax_type=get_field("TaxType", "tax_type"),
            class_=get_field("Class", "class"),
            currency_code=get_field("CurrencyCode", "currency_code"),
            bank_account_number=get_field("BankAccountNumber", "bank_account_number"),
            opening_balance=_parse_float(
                _first_present(
                    "OpeningBalance",
                    "opening_balance",
                    "Balance",
                    "BalanceUSD",
                )
            ),
        )

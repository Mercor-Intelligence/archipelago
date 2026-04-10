"""Contact model."""

import json

from sqlalchemy import Boolean, Column, String, Text

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


class Contact(Base):
    """Contact database model."""

    __tablename__ = "contacts"

    contact_id = Column(String, primary_key=True)
    name = Column(String)
    email_address = Column(String, nullable=True)
    contact_status = Column(String)
    is_supplier = Column(Boolean, default=False)
    is_customer = Column(Boolean, default=False)
    addresses = Column(Text, nullable=True)  # JSON
    phones = Column(Text, nullable=True)  # JSON

    def to_dict(self) -> dict:
        """Convert to Xero API format."""
        return {
            "ContactID": self.contact_id,
            "Name": self.name,
            "EmailAddress": self.email_address,
            "ContactStatus": self.contact_status,
            "IsSupplier": self.is_supplier,
            "IsCustomer": self.is_customer,
            "Addresses": json.loads(self.addresses) if self.addresses else [],  # type: ignore
            "Phones": json.loads(self.phones) if self.phones else [],  # type: ignore
        }

    @classmethod
    def from_dict(cls, data: dict):
        """Create from dict."""
        addresses = data.get("Addresses") or data.get("addresses")
        phones = data.get("Phones") or data.get("phones")

        # Handle JSON strings from CSV (already serialized)
        if isinstance(addresses, str):
            addresses_json = addresses
        else:
            addresses_json = json.dumps(addresses) if addresses else "[]"

        if isinstance(phones, str):
            phones_json = phones
        else:
            phones_json = json.dumps(phones) if phones else "[]"

        return cls(
            contact_id=data.get("ContactID") or data.get("contact_id"),
            name=data.get("Name") or data.get("name"),
            email_address=data.get("EmailAddress") or data.get("email_address"),
            contact_status=data.get("ContactStatus") or data.get("contact_status") or "ACTIVE",
            is_supplier=parse_bool(data.get("IsSupplier") or data.get("is_supplier")),
            is_customer=parse_bool(data.get("IsCustomer") or data.get("is_customer")),
            addresses=addresses_json,
            phones=phones_json,
        )

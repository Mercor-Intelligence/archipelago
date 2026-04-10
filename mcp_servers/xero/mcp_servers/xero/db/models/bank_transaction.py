"""Bank transaction model."""

import json

from sqlalchemy import Column, Float, String, Text

from mcp_servers.xero.db.models.invoice import normalize_xero_date
from mcp_servers.xero.db.session import Base


class BankTransaction(Base):
    """Bank transaction database model."""

    __tablename__ = "bank_transactions"

    bank_transaction_id = Column(String, primary_key=True)
    type = Column(String)
    status = Column(String)
    date = Column(String)
    reference = Column(String, nullable=True)
    contact = Column(Text)  # JSON
    bank_account_id = Column(String, nullable=True)
    bank_account_code = Column(String, nullable=True)
    bank_account_name = Column(String, nullable=True)
    bank_account_currency = Column(String, nullable=True)
    currency_code = Column(String, nullable=True)
    line_items = Column(Text)  # JSON
    sub_total = Column(Float, nullable=True)
    total_tax = Column(Float, nullable=True)
    total = Column(Float, nullable=True)

    def to_dict(self) -> dict:
        """Convert to Xero API format."""
        result = {
            "BankTransactionID": self.bank_transaction_id,
            "Type": self.type,
            "Status": self.status,
            "Date": self.date,
            "Contact": json.loads(self.contact) if self.contact else {},  # type: ignore
            "LineItems": json.loads(self.line_items) if self.line_items else [],  # type: ignore
            "SubTotal": self.sub_total,
            "TotalTax": self.total_tax,
            "Total": self.total,
        }
        if self.reference:
            result["Reference"] = self.reference
        if self.currency_code:
            result["CurrencyCode"] = self.currency_code
        if self.bank_account_id or self.bank_account_code or self.bank_account_name:
            result["BankAccount"] = {
                "AccountID": self.bank_account_id,
                "Code": self.bank_account_code,
                "Name": self.bank_account_name,
                "CurrencyCode": self.bank_account_currency or self.currency_code,
            }
        return result

    @classmethod
    def from_dict(cls, data: dict):
        """Create from dict.

        Supports multiple CSV formats:
        - Standard Xero format: Type, LineItems, Total, SubTotal
        - Payment-style format: PaymentType, Amount (auto-generates LineItems)
        """
        contact = data.get("Contact") or data.get("contact")
        line_items = data.get("LineItems") or data.get("line_items")
        bank_account = data.get("BankAccount") or data.get("bank_account")

        # Handle JSON strings from CSV (already serialized)
        if isinstance(contact, str):
            contact_json = contact
        else:
            contact_json = json.dumps(contact) if contact else "{}"

        # Prefer DateString over Date for better format consistency
        raw_date = data.get("DateString") or data.get("Date") or data.get("date")

        reference = data.get("Reference") or data.get("reference")
        currency_code = data.get("CurrencyCode") or data.get("currency_code")

        # Handle Type field - support PaymentType as alternate column name
        # PaymentType values: ACCRECPAYMENT, ACCPAYPAYMENT -> map to RECEIVE, SPEND
        raw_type = data.get("Type") or data.get("type")
        if not raw_type:
            payment_type = data.get("PaymentType") or data.get("payment_type")
            if payment_type:
                payment_type_upper = payment_type.upper()
                if "ACCREC" in payment_type_upper:
                    raw_type = "RECEIVE"
                elif "ACCPAY" in payment_type_upper:
                    raw_type = "SPEND"
                else:
                    raw_type = payment_type  # Keep original if unknown

        # Handle Total/Amount - support Amount as alternate column name
        # Use 'is not None' checks to preserve explicit zero values
        raw_total = data.get("Total") if data.get("Total") is not None else data.get("total")
        raw_sub_total = (
            data.get("SubTotal") if data.get("SubTotal") is not None else data.get("sub_total")
        )
        raw_amount = data.get("Amount") if data.get("Amount") is not None else data.get("amount")

        # Use Amount if Total not provided
        if raw_total is None and raw_amount is not None:
            raw_total = raw_amount
        if raw_sub_total is None and raw_amount is not None:
            raw_sub_total = raw_amount

        total = float(raw_total) if raw_total else 0.0
        sub_total = float(raw_sub_total) if raw_sub_total else 0.0
        raw_total_tax = (
            data.get("TotalTax") if data.get("TotalTax") is not None else data.get("total_tax")
        )
        total_tax = float(raw_total_tax) if raw_total_tax else 0.0

        # Handle LineItems - auto-generate if not present but we have amount
        if isinstance(line_items, str):
            line_items_json = line_items
        elif line_items:
            line_items_json = json.dumps(line_items)
        elif total != 0 or sub_total != 0:
            # Auto-generate a single line item from the amount
            # Use sub_total (net, excluding tax) for LineAmount per Xero's data model
            # Fall back to total if sub_total not available
            line_amount = sub_total if sub_total != 0 else total
            description = reference or "Bank Transaction"
            # Include AccountCode if provided at transaction level (common in simple CSV imports)
            account_code = data.get("AccountCode") or data.get("account_code")
            generated_line_item: dict[str, object] = {
                "LineAmount": line_amount,
                "Description": description,
            }
            if account_code:
                generated_line_item["AccountCode"] = account_code
            generated_line_items = [generated_line_item]
            line_items_json = json.dumps(generated_line_items)
        else:
            line_items_json = "[]"

        bank_account_dict = bank_account if isinstance(bank_account, dict) else {}
        bank_account_id = (
            bank_account_dict.get("AccountID")
            or data.get("BankAccountID")
            or data.get("bank_account_id")
            # Also check AccountID at root level (alternate CSV format)
            or data.get("AccountID")
            or data.get("account_id")
        )
        bank_account_code = (
            bank_account_dict.get("Code")
            or data.get("BankAccountCode")
            or data.get("bank_account_code")
        )
        bank_account_name = (
            bank_account_dict.get("Name")
            or data.get("BankAccountName")
            or data.get("bank_account_name")
        )
        bank_account_currency = (
            bank_account_dict.get("CurrencyCode")
            or data.get("BankAccountCurrency")
            or data.get("bank_account_currency")
        )

        # Calculate totals - prefer explicit values, fallback to line item sums
        # Use 'is not None' checks to preserve explicit zero values
        raw_sub_total = (
            data.get("SubTotal") if data.get("SubTotal") is not None else data.get("sub_total")
        )
        raw_total_tax = (
            data.get("TotalTax") if data.get("TotalTax") is not None else data.get("total_tax")
        )
        raw_total = data.get("Total") if data.get("Total") is not None else data.get("total")

        # Parse line items to calculate totals if not provided
        parsed_line_items = []
        if isinstance(line_items, str):
            try:
                parsed_line_items = json.loads(line_items)
                if not isinstance(parsed_line_items, list):
                    parsed_line_items = []
            except (json.JSONDecodeError, TypeError):
                parsed_line_items = []
        elif isinstance(line_items, list):
            parsed_line_items = line_items

        # Calculate line item sum for fallback
        line_items_sum = 0.0
        for item in parsed_line_items:
            if not isinstance(item, dict):
                continue
            try:
                line_items_sum += float(item.get("LineAmount", 0) or 0)
            except (ValueError, TypeError):
                pass

        # Use provided values or calculate from line items
        # Use 'is not None' to respect explicit zero values
        # Wrap in try/except to handle non-numeric values gracefully
        try:
            sub_total = float(raw_sub_total) if raw_sub_total is not None else line_items_sum
        except (ValueError, TypeError):
            sub_total = line_items_sum
        try:
            total_tax = float(raw_total_tax) if raw_total_tax is not None else 0.0
        except (ValueError, TypeError):
            total_tax = 0.0
        # Total should be subtotal + tax, or use line items sum as fallback
        if raw_total is not None:
            try:
                total = float(raw_total)
            except (ValueError, TypeError):
                total = (
                    sub_total + total_tax if (sub_total != 0 or total_tax != 0) else line_items_sum
                )
        elif sub_total != 0 or total_tax != 0:
            total = sub_total + total_tax
        else:
            total = line_items_sum

        return cls(
            bank_transaction_id=data.get("BankTransactionID") or data.get("bank_transaction_id"),
            type=raw_type,
            status=data.get("Status") or data.get("status"),
            date=normalize_xero_date(raw_date),
            contact=contact_json,
            line_items=line_items_json,
            sub_total=sub_total,
            total_tax=total_tax,
            total=total,
            reference=reference,
            bank_account_id=bank_account_id,
            bank_account_code=bank_account_code,
            bank_account_name=bank_account_name,
            bank_account_currency=bank_account_currency,
            currency_code=currency_code,
        )

"""Foreign key validation utilities for CSV imports.

This module provides validation for foreign key references to prevent
orphan records and improve data integrity during CSV imports.
"""

from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from mcp_servers.xero.db.models import (
    Account,
    AssetType,
    Contact,
    Invoice,
    Project,
)


class FKValidationError(Exception):
    """Raised when foreign key validation fails."""

    pass


class FKValidator:
    """Validates foreign key references before insertion."""

    def __init__(self, session: AsyncSession):
        """Initialize with database session."""
        self.session = session
        # Cache for already validated IDs to avoid redundant queries
        self._validated_cache: dict[str, set[str]] = {}

    async def validate_account_exists(
        self, account_id: str | None, field_name: str = "AccountID"
    ) -> None:
        """Validate that an account exists.

        Args:
            account_id: The account ID to validate
            field_name: Name of the field (for error messages)

        Raises:
            FKValidationError: If account doesn't exist
        """
        if not account_id:
            return  # Null/empty references are allowed (nullable fields)

        # Check cache first
        if "accounts" not in self._validated_cache:
            self._validated_cache["accounts"] = set()

        if account_id in self._validated_cache["accounts"]:
            return  # Already validated

        # Query database
        stmt = select(Account).where(Account.account_id == account_id)
        result = await self.session.execute(stmt)
        account = result.scalar_one_or_none()

        if not account:
            raise FKValidationError(
                f"{field_name} '{account_id}' does not exist. "
                f"Please ensure Accounts are imported before dependent entities."
            )

        # Add to cache
        self._validated_cache["accounts"].add(account_id)
        logger.debug(f"Validated account reference: {account_id}")

    async def validate_contact_exists(
        self, contact_id: str | None, field_name: str = "ContactID"
    ) -> None:
        """Validate that a contact exists.

        Args:
            contact_id: The contact ID to validate
            field_name: Name of the field (for error messages)

        Raises:
            FKValidationError: If contact doesn't exist
        """
        if not contact_id:
            return

        if "contacts" not in self._validated_cache:
            self._validated_cache["contacts"] = set()

        if contact_id in self._validated_cache["contacts"]:
            return

        stmt = select(Contact).where(Contact.contact_id == contact_id)
        result = await self.session.execute(stmt)
        contact = result.scalar_one_or_none()

        if not contact:
            raise FKValidationError(
                f"{field_name} '{contact_id}' does not exist. "
                f"Please ensure Contacts are imported before dependent entities."
            )

        self._validated_cache["contacts"].add(contact_id)
        logger.debug(f"Validated contact reference: {contact_id}")

    async def validate_invoice_exists(
        self, invoice_id: str | None, field_name: str = "InvoiceID"
    ) -> None:
        """Validate that an invoice exists.

        Args:
            invoice_id: The invoice ID to validate
            field_name: Name of the field (for error messages)

        Raises:
            FKValidationError: If invoice doesn't exist
        """
        if not invoice_id:
            return

        if "invoices" not in self._validated_cache:
            self._validated_cache["invoices"] = set()

        if invoice_id in self._validated_cache["invoices"]:
            return

        stmt = select(Invoice).where(Invoice.invoice_id == invoice_id)
        result = await self.session.execute(stmt)
        invoice = result.scalar_one_or_none()

        if not invoice:
            raise FKValidationError(
                f"{field_name} '{invoice_id}' does not exist. "
                f"Please ensure Invoices are imported before Payments."
            )

        self._validated_cache["invoices"].add(invoice_id)
        logger.debug(f"Validated invoice reference: {invoice_id}")

    async def validate_asset_type_exists(
        self, asset_type_id: str | None, field_name: str = "assetTypeId"
    ) -> None:
        """Validate that an asset type exists.

        Args:
            asset_type_id: The asset type ID to validate
            field_name: Name of the field (for error messages)

        Raises:
            FKValidationError: If asset type doesn't exist
        """
        if not asset_type_id:
            return

        if "asset_types" not in self._validated_cache:
            self._validated_cache["asset_types"] = set()

        if asset_type_id in self._validated_cache["asset_types"]:
            return

        stmt = select(AssetType).where(AssetType.asset_type_id == asset_type_id)
        result = await self.session.execute(stmt)
        asset_type = result.scalar_one_or_none()

        if not asset_type:
            raise FKValidationError(
                f"{field_name} '{asset_type_id}' does not exist. "
                f"Please ensure Asset Types are imported before Assets."
            )

        self._validated_cache["asset_types"].add(asset_type_id)
        logger.debug(f"Validated asset type reference: {asset_type_id}")

    async def validate_project_exists(
        self, project_id: str | None, field_name: str = "projectId"
    ) -> None:
        """Validate that a project exists.

        Args:
            project_id: The project ID to validate
            field_name: Name of the field (for error messages)

        Raises:
            FKValidationError: If project doesn't exist
        """
        if not project_id:
            return

        if "projects" not in self._validated_cache:
            self._validated_cache["projects"] = set()

        if project_id in self._validated_cache["projects"]:
            return

        stmt = select(Project).where(Project.project_id == project_id)
        result = await self.session.execute(stmt)
        project = result.scalar_one_or_none()

        if not project:
            raise FKValidationError(
                f"{field_name} '{project_id}' does not exist. "
                f"Please ensure Projects are imported before Time Entries."
            )

        self._validated_cache["projects"].add(project_id)
        logger.debug(f"Validated project reference: {project_id}")

    async def validate_payment_references(self, row_data: dict[str, Any]) -> None:
        """Validate all FK references in a Payment record.

        Args:
            row_data: The payment row data

        Raises:
            FKValidationError: If any FK validation fails
        """
        # Validate invoice reference (required for payments)
        invoice_id = row_data.get("InvoiceID") or row_data.get("invoice_id")
        if invoice_id:
            await self.validate_invoice_exists(invoice_id)

        # Validate account reference (optional)
        account_id = row_data.get("AccountID") or row_data.get("account_id")
        if account_id:
            await self.validate_account_exists(account_id)

    async def validate_invoice_references(self, row_data: dict[str, Any]) -> None:
        """Validate all FK references in an Invoice record.

        Args:
            row_data: The invoice row data

        Raises:
            FKValidationError: If any FK validation fails
        """
        # Extract contact reference from Contact object or direct field
        contact_data = row_data.get("Contact") or row_data.get("contact")
        if isinstance(contact_data, dict):
            contact_id = contact_data.get("ContactID")
        elif isinstance(contact_data, str):
            # JSON string - parse it
            import json

            try:
                contact_obj = json.loads(contact_data)
                contact_id = contact_obj.get("ContactID") if isinstance(contact_obj, dict) else None
            except (json.JSONDecodeError, AttributeError):
                contact_id = None
        else:
            contact_id = row_data.get("ContactID") or row_data.get("contact_id")

        if contact_id:
            await self.validate_contact_exists(contact_id)

    async def validate_asset_references(self, row_data: dict[str, Any]) -> None:
        """Validate all FK references in an Asset record.

        Args:
            row_data: The asset row data

        Raises:
            FKValidationError: If any FK validation fails
        """
        asset_type_id = row_data.get("assetTypeId") or row_data.get("asset_type_id")
        if asset_type_id:
            await self.validate_asset_type_exists(asset_type_id)

    async def validate_project_references(self, row_data: dict[str, Any]) -> None:
        """Validate all FK references in a Project record.

        Args:
            row_data: The project row data

        Raises:
            FKValidationError: If any FK validation fails
        """
        contact_id = row_data.get("contactId") or row_data.get("contact_id")
        if contact_id:
            await self.validate_contact_exists(contact_id, "contactId")

    async def validate_time_entry_references(self, row_data: dict[str, Any]) -> None:
        """Validate all FK references in a Time Entry record.

        Args:
            row_data: The time entry row data

        Raises:
            FKValidationError: If any FK validation fails
        """
        project_id = row_data.get("projectId") or row_data.get("project_id")
        if project_id:
            await self.validate_project_exists(project_id)

    async def validate_purchase_order_references(self, row_data: dict[str, Any]) -> None:
        """Validate all FK references in a Purchase Order record.

        Args:
            row_data: The purchase order row data

        Raises:
            FKValidationError: If any FK validation fails
        """
        # Extract contact reference
        contact_data = row_data.get("Contact") or row_data.get("contact")
        if isinstance(contact_data, dict):
            contact_id = contact_data.get("ContactID")
        elif isinstance(contact_data, str):
            import json

            try:
                contact_obj = json.loads(contact_data)
                contact_id = contact_obj.get("ContactID") if isinstance(contact_obj, dict) else None
            except (json.JSONDecodeError, AttributeError):
                contact_id = None
        else:
            contact_id = row_data.get("ContactID") or row_data.get("contact_id")

        if contact_id:
            await self.validate_contact_exists(contact_id)

    async def validate_quote_references(self, row_data: dict[str, Any]) -> None:
        """Validate all FK references in a Quote record.

        Args:
            row_data: The quote row data

        Raises:
            FKValidationError: If any FK validation fails
        """
        # Extract contact reference
        contact_data = row_data.get("Contact") or row_data.get("contact")
        if isinstance(contact_data, dict):
            contact_id = contact_data.get("ContactID")
        elif isinstance(contact_data, str):
            import json

            try:
                contact_obj = json.loads(contact_data)
                contact_id = contact_obj.get("ContactID") if isinstance(contact_obj, dict) else None
            except (json.JSONDecodeError, AttributeError):
                contact_id = None
        else:
            contact_id = row_data.get("ContactID") or row_data.get("contact_id")

        if contact_id:
            await self.validate_contact_exists(contact_id)

    async def validate_credit_note_references(self, row_data: dict[str, Any]) -> None:
        """Validate all FK references in a Credit Note record.

        Args:
            row_data: The credit note row data

        Raises:
            FKValidationError: If any FK validation fails
        """
        # Extract contact reference
        contact_data = row_data.get("Contact") or row_data.get("contact")
        if isinstance(contact_data, dict):
            contact_id = contact_data.get("ContactID")
        elif isinstance(contact_data, str):
            import json

            try:
                contact_obj = json.loads(contact_data)
                contact_id = contact_obj.get("ContactID") if isinstance(contact_obj, dict) else None
            except (json.JSONDecodeError, AttributeError):
                contact_id = None
        else:
            contact_id = row_data.get("ContactID") or row_data.get("contact_id")

        if contact_id:
            await self.validate_contact_exists(contact_id)

    async def validate_overpayment_references(self, row_data: dict[str, Any]) -> None:
        """Validate all FK references in an Overpayment record.

        Args:
            row_data: The overpayment row data

        Raises:
            FKValidationError: If any FK validation fails
        """
        # Extract contact reference
        contact_data = row_data.get("Contact") or row_data.get("contact")
        if isinstance(contact_data, dict):
            contact_id = contact_data.get("ContactID")
        elif isinstance(contact_data, str):
            import json

            try:
                contact_obj = json.loads(contact_data)
                contact_id = contact_obj.get("ContactID") if isinstance(contact_obj, dict) else None
            except (json.JSONDecodeError, AttributeError):
                contact_id = None
        else:
            contact_id = row_data.get("ContactID") or row_data.get("contact_id")

        if contact_id:
            await self.validate_contact_exists(contact_id)

    async def validate_prepayment_references(self, row_data: dict[str, Any]) -> None:
        """Validate all FK references in a Prepayment record.

        Args:
            row_data: The prepayment row data

        Raises:
            FKValidationError: If any FK validation fails
        """
        # Extract contact reference
        contact_data = row_data.get("Contact") or row_data.get("contact")
        if isinstance(contact_data, dict):
            contact_id = contact_data.get("ContactID")
        elif isinstance(contact_data, str):
            import json

            try:
                contact_obj = json.loads(contact_data)
                contact_id = contact_obj.get("ContactID") if isinstance(contact_obj, dict) else None
            except (json.JSONDecodeError, AttributeError):
                contact_id = None
        else:
            contact_id = row_data.get("ContactID") or row_data.get("contact_id")

        if contact_id:
            await self.validate_contact_exists(contact_id)


# Entity-specific validation mapping
FK_VALIDATORS = {
    "payments": "validate_payment_references",
    "invoices": "validate_invoice_references",
    "assets": "validate_asset_references",
    "projects": "validate_project_references",
    "time_entries": "validate_time_entry_references",
    "purchase_orders": "validate_purchase_order_references",
    "quotes": "validate_quote_references",
    "credit_notes": "validate_credit_note_references",
    "overpayments": "validate_overpayment_references",
    "prepayments": "validate_prepayment_references",
    # Foundation entities don't need FK validation
    # accounts, contacts, asset_types: no FKs
    # bank_transactions, bank_transfers, journals: complex nested FKs, skip for now
    # budgets: may reference accounts but optional
    # files, folders, associations: no FKs to core entities
}

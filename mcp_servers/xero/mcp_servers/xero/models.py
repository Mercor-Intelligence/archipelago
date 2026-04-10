"""Xero API Pydantic models.

This module contains all Pydantic models for Xero API data structures.
Consolidated from the schemas/ directory for simpler imports and better organization.
"""

from __future__ import annotations

from decimal import Decimal
from enum import Enum

from mcp_schema import GeminiBaseModel as BaseModel
from pydantic import ConfigDict, Field, field_validator, model_validator

# =============================================================================
# Common Models
# =============================================================================


class MetaDict(BaseModel):
    """Metadata model for Xero API responses.

    Supports flexible metadata structure with required fields and extra properties.
    """

    mode: str = "online"
    provider: str
    calledAt: str

    model_config = ConfigDict(populate_by_name=True, extra="allow")


# =============================================================================
# Account Models
# =============================================================================


class Account(BaseModel):
    """Account model from Xero API.

    Reference: Xero Accounts API - Account Schema
    """

    AccountID: str = Field(..., description="Unique identifier (GUID)")
    Name: str = Field(..., description="Account name")
    Type: str = Field(..., description="Account type (e.g., BANK, REVENUE, EXPENSE)")
    Code: str | None = Field(None, description="Account code")
    Status: str | None = Field(None, description="Account status (e.g., ACTIVE)")
    CurrencyCode: str | None = Field(None, description="Account currency code")
    Description: str | None = Field(None, description="Account description")
    Class: str | None = Field(None, description="Account class (e.g., ASSET, LIABILITY)")
    SystemAccount: str | None = Field(None, description="System account identifier")
    BankAccountNumber: str | None = Field(None, description="Bank account number")
    OpeningBalance: Decimal | None = Field(
        None,
        description="Opening balance posted when master data is loaded (debit positive)",
    )
    ReportingCode: str | None = Field(None, description="Reporting code")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetAccountsResponse(BaseModel):
    """Response model for getting accounts.

    Note: Using uppercase 'Accounts' key to match Xero API format.
    """

    Accounts: list[Account] = Field(..., alias="Accounts", description="List of accounts")
    meta: dict | MetaDict = Field(..., description="Response metadata including pagination")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


# =============================================================================
# Bank Transaction Models
# =============================================================================


class BankTransactionType(str, Enum):
    """Bank transaction type codes.

    Reference: XER-14_BankTransactions_API_Reference.md - BankTransactionType section
    """

    RECEIVE = "RECEIVE"  # Receive money
    SPEND = "SPEND"  # Spend money
    RECEIVE_OVERPAYMENT = "RECEIVE-OVERPAYMENT"  # Customer overpayment
    SPEND_OVERPAYMENT = "SPEND-OVERPAYMENT"  # Supplier overpayment
    RECEIVE_PREPAYMENT = "RECEIVE-PREPAYMENT"  # Customer prepayment
    SPEND_PREPAYMENT = "SPEND-PREPAYMENT"  # Supplier prepayment
    RECEIVE_TRANSFER = "RECEIVE-TRANSFER"  # Transfer (GET only)
    SPEND_TRANSFER = "SPEND-TRANSFER"  # Transfer (GET only)


class BankTransactionStatus(str, Enum):
    """Bank transaction status codes.

    Reference: XER-14_BankTransactions_API_Reference.md - BankTransactionStatus section
    """

    AUTHORISED = "AUTHORISED"  # Authorized transaction
    DELETED = "DELETED"  # Deleted transaction
    VOIDED = "VOIDED"  # Voided transaction


class LineAmountTypes(str, Enum):
    """Line amount tax treatment.

    Reference: XER-14_BankTransactions_API_Reference.md - LineAmountTypes section
    """

    EXCLUSIVE = "Exclusive"  # Line amounts exclude tax (default)
    INCLUSIVE = "Inclusive"  # Line amounts include tax
    NOTAX = "NoTax"  # Line items have no tax


class ContactSummary(BaseModel):
    """Contact summary (returned in bank transaction collections).

    Reference: XER-14_BankTransactions_API_Reference.md - Contact Schema section
    """

    ContactID: str = Field(..., description="Unique identifier (GUID)")
    Name: str = Field(..., description="Full contact/organization name")


class BankAccountSummary(BaseModel):
    """Bank account summary.

    Reference: XER-14_BankTransactions_API_Reference.md - BankAccount Schema section

    Note: Only accounts with Type=BANK are valid for bank transactions.
    """

    AccountID: str = Field(..., description="Account identifier (GUID)")
    Code: str = Field(..., description="Account code")
    Name: str | None = Field(None, description="Account name")
    CurrencyCode: str | None = Field(None, description="Account currency")


class BankTransactionLineItem(BaseModel):
    """Bank transaction line item.

    Reference: XER-14_BankTransactions_API_Reference.md - LineItem Schema section
    """

    LineItemID: str | None = Field(None, description="Xero-generated unique identifier (GUID)")
    Description: str = Field(..., min_length=1, description="Line item description (min 1 char)")
    Quantity: Decimal = Field(..., description="Quantity (must be > 0)")
    UnitAmount: Decimal = Field(
        ..., description="Unit price (2 or 4 decimal places, controlled by unitdp)"
    )
    LineAmount: Decimal = Field(
        ..., description="Total line amount: Quantity × UnitAmount × ((100 - DiscountRate) / 100)"
    )
    AccountCode: str | None = Field(None, description="Account code reference")
    AccountID: str | None = Field(None, description="Account identifier (GUID)")
    ItemCode: str | None = Field(None, description="Item code reference (SPEND/RECEIVE only)")
    TaxType: str = Field(..., description="Tax type code (e.g., OUTPUT, INPUT, NONE)")
    TaxAmount: Decimal = Field(..., description="Auto-calculated tax amount")
    DiscountRate: Decimal | None = Field(None, description="Discount percentage (0-100)")
    DiscountAmount: Decimal | None = Field(None, description="Discount amount")

    model_config = ConfigDict(use_enum_values=True, populate_by_name=True)


class BankTransaction(BaseModel):
    """Bank transaction model with totals consistency validation.

    Reference: XER-14_BankTransactions_API_Reference.md - BankTransaction Schema section

    Important: All monetary fields use Decimal type to avoid floating-point
    precision issues. Totals are validated to ensure Σ(LineAmount) == SubTotal ±0.01
    """

    # Core identifiers
    BankTransactionID: str = Field(..., description="Xero-generated unique identifier (GUID)")
    Type: BankTransactionType = Field(..., description="Transaction type")
    Status: BankTransactionStatus = Field(..., description="Transaction status")

    # Related entities
    Contact: ContactSummary = Field(..., description="Contact/supplier/customer")
    BankAccount: BankAccountSummary = Field(
        ..., description="Bank account used (must be Type=BANK)"
    )

    # Date and reconciliation
    Date: str = Field(..., description="Transaction date (YYYY-MM-DD or /Date(...)/ format)")
    IsReconciled: bool = Field(..., description="Whether transaction is reconciled")

    # Line items and tax treatment
    line_amount_types: LineAmountTypes | None = Field(
        default=LineAmountTypes.EXCLUSIVE,
        alias="LineAmountTypes",
        description="Tax treatment (default: Exclusive)",
    )
    LineItems: list[BankTransactionLineItem] = Field(
        ..., min_length=1, description="Array of line items (minimum 1 required)"
    )

    # Amounts (all required, use Decimal for precision)
    SubTotal: Decimal = Field(..., description="Total excluding taxes (sum of line amounts)")
    TotalTax: Decimal = Field(..., description="Total tax amount")
    Total: Decimal = Field(..., description="Grand total (SubTotal + TotalTax)")

    # Optional fields
    Reference: str | None = Field(None, description="Reference text (SPEND/RECEIVE only)")
    CurrencyCode: str | None = Field("USD", description="Currency code (e.g., USD, NZD, GBP)")
    CurrencyRate: Decimal | None = Field(None, description="Exchange rate for non-base currency")
    Url: str | None = Field(None, description="URL link to source document")
    UpdatedDateUTC: str | None = Field(
        None, description="Last modified timestamp (UTC, Xero date format)"
    )
    HasAttachments: bool | None = Field(None, description="Whether transaction has attachments")
    PrepaymentID: str | None = Field(None, description="Prepayment ID (if Type is *-PREPAYMENT)")
    OverpaymentID: str | None = Field(None, description="Overpayment ID (if Type is *-OVERPAYMENT)")

    model_config = ConfigDict(use_enum_values=True, populate_by_name=True)

    @model_validator(mode="after")
    def validate_totals_consistency(self) -> BankTransaction:
        """Validate that sum of line amounts matches SubTotal within ±0.01.

        This validation runs when LineItems are present. Following PR #3 pattern,
        we log warnings instead of raising errors to avoid blocking production use.

        Reference: XER_Guidelines.md - Validation Standards section
        """
        if self.LineItems:
            from loguru import logger

            line_total = sum(item.LineAmount for item in self.LineItems)
            expected_total = self.SubTotal

            # Allow ±0.01 tolerance for rounding differences
            if abs(line_total - expected_total) > Decimal("0.01"):
                logger.warning(
                    f"BankTransaction {self.BankTransactionID}: Line items total ({line_total}) "
                    f"does not match SubTotal ({expected_total}). "
                    f"Difference: {abs(line_total - expected_total)}"
                )

        return self


class GetBankTransactionsRequest(BaseModel):
    """Request model for getting bank transactions.

    Reference: XER-14_BankTransactions_API_Reference.md - Query Parameters section
    """

    where: str | None = Field(
        None,
        description="""Xero OData-style filter expression.

FILTERABLE FIELDS: Type (RECEIVE/SPEND), Status, Date, BankAccount.AccountID

EXAMPLES:
- Type=="RECEIVE" (deposits only)
- Type=="SPEND" (withdrawals only)
- Status=="AUTHORISED" AND Date>=DateTime(2024,1,1)""",
        examples=['Type=="RECEIVE"', 'Status=="AUTHORISED"', "Date>=DateTime(2024,1,1)"],
    )
    unitdp: int | None = Field(
        None,
        description="""Decimal places for unit amounts in line items.

Values:
- 2 (default): Standard currency precision (e.g., $10.50)
- 4: Extended precision for commodities, forex, or high-volume pricing

Use 4 when trading commodities or processing high-volume low-value items.""",
        examples=[2, 4],
    )
    page: int = Field(
        1,
        ge=1,
        description="Page number for pagination (1-indexed). ~100 items per page.",
    )

    @field_validator("unitdp")
    @classmethod
    def validate_unitdp(cls, v: int | None) -> int | None:
        """Validate that unitdp is either 2 or 4.

        Xero API only accepts 2 or 4 decimal places for unit amounts.

        Args:
            v: The unitdp value to validate

        Returns:
            The validated unitdp value

        Raises:
            ValueError: If unitdp is not 2 or 4
        """
        if v is not None and v not in {2, 4}:
            raise ValueError("unitdp must be 2 or 4")
        return v

    model_config = ConfigDict(use_enum_values=True)


class GetBankTransactionsResponse(BaseModel):
    """Response model for getting bank transactions.

    Note: Using uppercase 'BankTransactions' key to match Xero API format.
    """

    BankTransactions: list[BankTransaction] = Field(
        ..., alias="BankTransactions", description="List of bank transactions"
    )
    meta: dict = Field(..., description="Response metadata including pagination")

    model_config = ConfigDict(use_enum_values=True, populate_by_name=True)


# =============================================================================
# Contact Models
# =============================================================================


class Contact(BaseModel):
    """Contact model from Xero API.

    Reference: Xero Contacts API - Contact Schema
    """

    ContactID: str = Field(..., description="Unique identifier (GUID)")
    Name: str = Field(..., description="Contact name")
    EmailAddress: str | None = Field(None, description="Email address")
    FirstName: str | None = Field(None, description="First name")
    LastName: str | None = Field(None, description="Last name")
    ContactStatus: str | None = Field(None, description="Contact status (e.g., ACTIVE)")
    DefaultCurrency: str | None = Field(None, description="Default currency code")
    IsCustomer: bool | None = Field(None, description="Whether contact is a customer")
    IsSupplier: bool | None = Field(None, description="Whether contact is a supplier")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetContactsResponse(BaseModel):
    """Response model for getting contacts.

    Note: Using uppercase 'Contacts' key to match Xero API format.
    """

    Contacts: list[Contact] = Field(..., alias="Contacts", description="List of contacts")
    meta: dict | MetaDict = Field(..., description="Response metadata including pagination")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


# =============================================================================
# Invoice Models
# =============================================================================


class InvoiceLineItem(BaseModel):
    """Invoice line item model.

    Reference: Xero Invoices API - LineItem Schema
    """

    Description: str | None = Field(None, description="Line item description")
    LineAmount: Decimal = Field(..., description="Total line amount")
    Quantity: Decimal | None = Field(None, description="Quantity")
    UnitAmount: Decimal | None = Field(None, description="Unit price")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Invoice(BaseModel):
    """Invoice model from Xero API.

    Reference: Xero Invoices API - Invoice Schema

    Important: All monetary fields use Decimal type to avoid floating-point
    precision issues.
    """

    InvoiceID: str = Field(..., description="Unique identifier (GUID)")
    Type: str = Field(..., description="Invoice type (e.g., ACCREC, ACCPAY)")
    Total: Decimal = Field(..., description="Grand total")
    LineItems: list[InvoiceLineItem] = Field(..., description="Array of line items")
    InvoiceNumber: str | None = Field(None, description="Invoice number")
    Status: str | None = Field(None, description="Invoice status (e.g., AUTHORISED, PAID)")
    Date: str | None = Field(None, description="Invoice date (YYYY-MM-DD or /Date(...)/ format)")
    DueDate: str | None = Field(None, description="Due date (YYYY-MM-DD or /Date(...)/ format)")
    CurrencyCode: str | None = Field(None, description="Currency code")
    SubTotal: Decimal | None = Field(None, description="Total excluding taxes")
    TotalTax: Decimal | None = Field(None, description="Total tax amount")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetInvoicesResponse(BaseModel):
    """Response model for getting invoices.

    Note: Using uppercase 'Invoices' key to match Xero API format.
    """

    Invoices: list[Invoice] = Field(..., alias="Invoices", description="List of invoices")
    meta: dict | MetaDict = Field(..., description="Response metadata including pagination")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


# =============================================================================
# Payment Models
# =============================================================================


class Payment(BaseModel):
    """Payment model from Xero API.

    Reference: Xero Payments API - Payment Schema

    Important: All monetary fields use Decimal type to avoid floating-point
    precision issues.
    """

    PaymentID: str = Field(..., description="Unique identifier (GUID)")
    Amount: Decimal = Field(..., description="Payment amount")
    Invoice: dict | None = Field(None, description="Associated invoice")
    InvoiceID: str | None = Field(None, description="Invoice identifier")
    CurrencyRate: Decimal | None = Field(None, description="Exchange rate")
    CurrencyCode: str | None = Field(None, description="Currency code (e.g., USD)")
    Date: str | None = Field(None, description="Payment date (YYYY-MM-DD or /Date(...)/ format)")
    Status: str | None = Field(None, description="Payment status (e.g., AUTHORISED)")
    Reference: str | None = Field(None, description="Payment reference")
    PaymentType: str | None = Field(None, description="Payment type (e.g., ACCRECPAYMENT)")
    AccountID: str | None = Field(None, description="Bank account used for payment")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetPaymentsResponse(BaseModel):
    """Response model for getting payments.

    Note: Using uppercase 'Payments' key to match Xero API format.
    """

    Payments: list[Payment] = Field(..., alias="Payments", description="List of payments")
    meta: dict | MetaDict = Field(..., description="Response metadata including pagination")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


# =============================================================================
# Report Models
# =============================================================================


class ReportCell(BaseModel):
    """Report cell model.

    Reference: Xero Reports API - Cell Schema
    """

    Value: str | Decimal | None = Field(None, description="Cell value")
    Attributes: dict | None = Field(None, description="Cell attributes")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class ReportRow(BaseModel):
    """Report row model.

    Reference: Xero Reports API - Row Schema
    """

    RowType: str = Field(..., description="Row type (e.g., Header, Section, Row)")
    Title: str | None = Field(None, description="Row title")
    Cells: list[ReportCell] | None = Field(None, description="Array of cells")
    Rows: list[ReportRow] | None = Field(None, description="Nested rows")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Report(BaseModel):
    """Report model from Xero API.

    Reference: Xero Reports API - Report Schema
    """

    ReportID: str | None = Field(None, description="Report identifier")
    ReportName: str = Field(..., description="Report name")
    ReportDate: str | None = Field(None, description="Report date")
    Rows: list[ReportRow] = Field(..., description="Array of report rows")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetReportsResponse(BaseModel):
    """Response model for getting reports.

    Note: Using uppercase 'Reports' key to match Xero API format.
    """

    Reports: list[Report] = Field(..., alias="Reports", description="List of reports")
    meta: dict | MetaDict = Field(..., description="Response metadata including report period")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


# =============================================================================
# Tool Input Models
# =============================================================================


class GetAccountsInput(BaseModel):
    """Input model for get_accounts tool."""

    where: str | None = Field(
        None,
        description="""Xero OData-style filter expression.

SYNTAX:
- Equality: FieldName=="Value" (strings need double-quotes)
- Combining conditions: Use AND for multiple conditions (e.g., Status=="ACTIVE" AND Type=="BANK")
- Date comparison: Date>=DateTime(2024,1,15) (no zero-padding)

FILTERABLE FIELDS: Type, Status, Class, Code, Name, SystemAccount

EXAMPLES:
- Status=="ACTIVE" (active accounts only)
- Type=="BANK" AND Status=="ACTIVE" (active bank accounts)""",
    )
    order: str | None = Field(None, description='Order expression (e.g., "Code ASC", "Name DESC")')
    page: int | None = Field(
        None,
        description="Page number for pagination (1-indexed). ~100 items per page.",
    )

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetContactsInput(BaseModel):
    """Input model for get_contacts tool."""

    ids: str | None = Field(
        None,
        description="""Comma-separated UUIDs to fetch specific contacts.

Format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx (standard UUID v4)
Example: 'a1b2c3d4-e5f6-7890-abcd-ef1234567890,b2c3d4e5-f6a7-8901-bcde-f23456789012'""",
    )
    where: str | None = Field(
        None,
        description="""Xero OData-style filter expression.

FILTERABLE FIELDS: Name, ContactStatus, EmailAddress, IsCustomer, IsSupplier

EXAMPLES:
- ContactStatus=="ACTIVE" (active contacts only)
- Name.Contains("Acme") (contacts with "Acme" in name)
- IsCustomer==true (customers only)""",
    )
    include_archived: bool = Field(
        False,
        description="""Include archived contacts in results.

- false (default): Returns only ACTIVE contacts
- true: Returns BOTH active AND archived contacts""",
    )
    order: str | None = Field(None, description='Order expression (e.g., "Name ASC")')
    page: int | None = Field(
        None,
        description="Page number for pagination (1-indexed). ~100 items per page.",
    )

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetInvoicesInput(BaseModel):
    """Input model for get_invoices tool."""

    ids: str | None = Field(
        None,
        description="""Comma-separated UUIDs to fetch specific invoices.

Format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx (standard UUID v4)
Example: 'a1b2c3d4-e5f6-7890-abcd-ef1234567890'""",
    )
    statuses: str | None = Field(
        None,
        description="""Comma-separated status filter (case-insensitive).

Valid values: DRAFT, SUBMITTED, AUTHORISED, PAID, VOIDED
Example: 'DRAFT,AUTHORISED' returns unpaid invoices""",
    )
    where: str | None = Field(
        None,
        description="""Xero OData-style filter expression.

FILTERABLE FIELDS: Type (ACCREC/ACCPAY), Status, Date, DueDate, Contact.ContactID

EXAMPLES:
- Type=="ACCREC" (customer invoices only)
- Type=="ACCPAY" (supplier bills only)
- Date>=DateTime(2024,1,1) (invoices from 2024)""",
    )
    page: int | None = Field(
        None,
        description="Page number for pagination (1-indexed). ~100 items per page.",
    )

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetPaymentsInput(BaseModel):
    """Input model for get_payments tool."""

    where: str | None = Field(None, description="Filter expression")
    page: int | None = Field(None, description="Page number for pagination (1-indexed)")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetReportBalanceSheetInput(BaseModel):
    """Input model for get_report_balance_sheet tool."""

    date: str = Field(
        ...,
        description="""Report date in YYYY-MM-DD format.
Shows balances as of end-of-day on this date.
Example: '2024-12-31' for year-end balance sheet""",
    )
    periods: int | None = Field(
        None,
        description="Number of comparison periods to include (e.g., 3 for quarterly comparison)",
    )
    timeframe: str | None = Field(
        None,
        description="""Period granularity for multi-period reports.

Valid values: 'MONTH', 'QUARTER', or 'YEAR'
Used with periods parameter for side-by-side comparison.""",
    )
    tracking_categories: str | None = Field(
        None,
        description="""Filter by tracking categories (cost centers/departments).

Format: Comma-separated tracking option UUIDs
Example: 'a1b2c3d4-e5f6-7890-abcd-ef1234567890'

Leave empty to aggregate all tracking categories.""",
    )

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetReportProfitAndLossInput(BaseModel):
    """Input model for get_report_profit_and_loss tool."""

    from_date: str = Field(
        ...,
        description="""Period start date in YYYY-MM-DD format (inclusive).
Example: '2024-01-01' for full year starting Jan 1""",
    )
    to_date: str = Field(
        ...,
        description="""Period end date in YYYY-MM-DD format (inclusive).
Example: '2024-12-31' for full year ending Dec 31""",
    )
    periods: int | None = Field(
        None,
        description="Number of comparison periods to include (e.g., 12 for monthly breakdown)",
    )
    timeframe: str | None = Field(
        None,
        description="""Period granularity for multi-period reports.

Valid values: 'MONTH', 'QUARTER', or 'YEAR'
Used with periods parameter for side-by-side comparison.""",
    )
    tracking_categories: str | None = Field(
        None,
        description="""Filter by tracking categories (cost centers/departments).

Format: Comma-separated tracking option UUIDs
Leave empty to aggregate all tracking categories.""",
    )

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class ResetStateInput(BaseModel):
    """Input model for reset_state tool."""

    model_config = ConfigDict(populate_by_name=True, extra="allow")


# =============================================================================
# Phase 2 Tool Input Models
# =============================================================================


class GetReportAgedReceivablesInput(BaseModel):
    """Input model for get_report_aged_receivables tool.

    Reference: Xero Reports API - Aged Receivables By Contact
    """

    contact_id: str = Field(
        ...,
        min_length=1,
        description="Contact UUID (required) e.g. 5040915e-8ce7-4177-8d08-fde416232f18",
    )
    date: str | None = Field(
        None,
        description="Shows payments up to this date (YYYY-MM-DD). Defaults to end of current month",
    )
    from_date: str | None = Field(
        None, description="Show all receivable invoices from this date for contact (YYYY-MM-DD)"
    )
    to_date: str | None = Field(
        None, description="Show all receivable invoices to this date for contact (YYYY-MM-DD)"
    )

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetReportAgedPayablesInput(BaseModel):
    """Input model for get_report_aged_payables tool.

    Reference: Xero Reports API - Aged Payables By Contact
    """

    contact_id: str = Field(
        ...,
        min_length=1,
        description="Contact UUID (required) e.g. 5040915e-8ce7-4177-8d08-fde416232f18",
    )
    date: str | None = Field(
        None,
        description="Shows payments up to this date (YYYY-MM-DD). Defaults to end of current month",
    )
    from_date: str | None = Field(
        None, description="Show all payable bills from this date for contact (YYYY-MM-DD)"
    )
    to_date: str | None = Field(
        None, description="Show all payable bills to this date for contact (YYYY-MM-DD)"
    )

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetBudgetSummaryInput(BaseModel):
    """Input model for get_budget_summary tool.

    Reference: Xero Reports API - Budget Summary
    """

    date: str | None = Field(
        None,
        description="Report date in YYYY-MM-DD format. Example: '2024-04-30'",
    )
    periods: int | None = Field(
        None,
        ge=1,
        le=12,
        description="Number of periods to compare (1-12). Example: 3 for quarterly comparison",
    )
    timeframe: int | None = Field(
        None,
        description="""Period size as integer:
- 1 = month
- 3 = quarter
- 12 = year

Example: periods=3, timeframe=1 shows last 3 months.""",
    )

    @field_validator("timeframe")
    @classmethod
    def validate_timeframe(cls, v: int | None) -> int | None:
        """Validate that timeframe is 1, 3, or 12."""
        if v is not None and v not in {1, 3, 12}:
            raise ValueError("timeframe must be 1 (month), 3 (quarter), or 12 (year)")
        return v

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetBudgetsInput(BaseModel):
    """Input model for get_budgets tool.

    Reference: Xero Budgets API
    Note: No parameters required for this endpoint.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetReportExecutiveSummaryInput(BaseModel):
    """Input model for get_report_executive_summary tool.

    Reference: Xero Reports API - Executive Summary
    """

    date: str = Field(..., description="Report date (YYYY-MM-DD)")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetJournalsInput(BaseModel):
    """Input model for get_journals tool.

    Reference: Xero Journals API
    """

    offset: int | None = Field(
        None,
        ge=0,
        description="""Starting journal number for sequential retrieval.

Xero journals are numbered sequentially (1, 2, 3, ...).
This parameter specifies the journal NUMBER to start from, not a record count.

Pagination approach:
1. First call: offset=0 (or omit) - returns journals starting from #1
2. Note highest JournalNumber in response (e.g., 100)
3. Next call: offset=100 - returns journals #101+
4. Repeat until empty response""",
    )
    payments_only: bool | None = Field(
        None,
        description="If true, returns only payment-related journal entries.",
    )

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetBankTransfersInput(BaseModel):
    """Input model for get_bank_transfers tool.

    Reference: Xero BankTransfers API
    """

    where: str | None = Field(None, description="Filter expression")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetQuotesInput(BaseModel):
    """Input model for get_quotes tool.

    Reference: Xero Quotes API
    """

    ids: str | None = Field(
        None,
        description="""Comma-separated UUIDs to fetch specific quotes.
Format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx""",
    )
    where: str | None = Field(
        None,
        description="""Xero OData-style filter expression.
Example: Status=="SENT" AND Date>=DateTime(2024,1,1)""",
    )
    page: int | None = Field(None, ge=1, description="Page number (1-indexed, ~100 per page)")
    statuses: str | None = Field(
        None,
        description="""Comma-separated status filter (case-insensitive).

Valid values: DRAFT, SENT, ACCEPTED, DECLINED, INVOICED
Example: 'SENT,ACCEPTED' returns quotes pending customer decision""",
    )

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetPurchaseOrdersInput(BaseModel):
    """Input model for get_purchase_orders tool.

    Reference: Xero PurchaseOrders API
    """

    ids: str | None = Field(
        None,
        description="""Comma-separated UUIDs to fetch specific purchase orders.
Format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx""",
    )
    where: str | None = Field(
        None,
        description="""Xero OData-style filter expression.
Example: Status=="AUTHORISED" AND Date>=DateTime(2024,1,1)""",
    )
    page: int | None = Field(None, ge=1, description="Page number (1-indexed, ~100 per page)")
    statuses: str | None = Field(
        None,
        description="""Comma-separated status filter (case-insensitive).

Valid values: DRAFT, SUBMITTED, AUTHORISED, BILLED, DELETED
Example: 'DRAFT,SUBMITTED' returns POs awaiting approval""",
    )

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetCreditNotesInput(BaseModel):
    """Input model for get_credit_notes tool.

    Reference: Xero CreditNotes API
    """

    ids: str | None = Field(None, description="Comma-separated credit note IDs")
    where: str | None = Field(None, description="Filter expression")
    page: int | None = Field(None, ge=1, description="Page number")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetPrepaymentsInput(BaseModel):
    """Input model for get_prepayments tool.

    Reference: Xero Prepayments API
    """

    where: str | None = Field(None, description="Filter expression")
    page: int | None = Field(None, ge=1, description="Page number")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetOverpaymentsInput(BaseModel):
    """Input model for get_overpayments tool.

    Reference: Xero Overpayments API
    """

    where: str | None = Field(None, description="Filter expression")
    page: int | None = Field(None, ge=1, description="Page number")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetAssetsInput(BaseModel):
    """Input model for get_assets tool.

    Reference: Xero Assets API
    """

    status: str | None = Field(
        None,
        description="""Filter by asset status. CASE-SENSITIVE values:
- 'Draft': Setup incomplete, not yet in use
- 'Registered': Active asset, being depreciated
- 'Disposed': Sold, scrapped, or written off

Note: Must use exact case shown (first letter uppercase).""",
    )
    page: int | None = Field(None, ge=1, description="Page number (1-indexed)")
    page_size: int | None = Field(None, ge=1, le=200, description="Items per page (max 200)")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetAssetTypesInput(BaseModel):
    """Input model for get_asset_types tool.

    Reference: Xero Asset Types API
    Note: No parameters required for this endpoint.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetFilesInput(BaseModel):
    """Input model for get_files tool.

    Reference: Xero Files API
    """

    page: int | None = Field(None, ge=1, description="Page number")
    page_size: int | None = Field(None, ge=1, le=100, description="Page size (max 100)")
    sort: str | None = Field(None, description="Sort field: Name, Size, CreatedDateUtc")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetFoldersInput(BaseModel):
    """Input model for get_folders tool.

    Reference: Xero Files API - Folders
    Note: No parameters required for this endpoint.
    """

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetAssociationsInput(BaseModel):
    """Input model for get_associations tool.

    Reference: Xero Files API - Associations
    """

    file_id: str = Field(..., description="File UUID to get associations for")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetProjectsInput(BaseModel):
    """Input model for get_projects tool.

    Reference: Xero Projects API
    """

    page: int | None = Field(None, ge=1, description="Page number (1-indexed)")
    page_size: int | None = Field(None, ge=1, description="Items per page")
    contact_id: str | None = Field(
        None,
        description="""Filter by contact UUID (customer/client).
Format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx""",
    )
    states: str | None = Field(
        None,
        description="""Comma-separated project states (case-insensitive).

Valid values: INPROGRESS, CLOSED
Example: 'INPROGRESS' for active projects only""",
    )

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetProjectTimeInput(BaseModel):
    """Input model for get_project_time tool.

    Reference: Xero Projects API - Time Entries
    """

    project_id: str = Field(..., description="Project UUID to get time entries for")
    page: int | None = Field(None, ge=1, description="Page number")
    page_size: int | None = Field(None, ge=1, description="Page size")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


# =============================================================================
# Phase 2 Entity Models
# =============================================================================


class TrackingCategory(BaseModel):
    """Tracking category for budgets.

    Reference: Xero Budgets API - Tracking Schema
    """

    TrackingCategoryID: str = Field(..., description="Unique identifier (GUID)")
    Name: str = Field(..., description="Tracking category name")
    Option: str | None = Field(None, description="Selected option")
    Options: list[dict] = Field(default_factory=list, description="Available options")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class BudgetLine(BaseModel):
    """Budget line item with account and period amounts.

    Reference: Xero Budgets API - BudgetLine Schema
    """

    AccountID: str | None = Field(None, description="Account identifier (GUID)")
    AccountCode: str | None = Field(None, description="Account code")
    BudgetBalances: list[dict] = Field(default_factory=list, description="Period budget amounts")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Budget(BaseModel):
    """Budget entity with tracking categories.

    Reference: Xero Budgets API - Budget Schema
    """

    BudgetID: str = Field(..., description="Unique identifier (GUID)")
    Type: str = Field(..., description="Budget type (TRACKING, OVERALL)")
    Description: str | None = Field(None, description="Budget description")
    UpdatedDateUTC: str | None = Field(None, description="Last modified timestamp")
    Tracking: list[TrackingCategory] = Field(
        default_factory=list, description="Tracking category assignments"
    )
    BudgetLines: list[BudgetLine] = Field(default_factory=list, description="Budget line items")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class JournalLine(BaseModel):
    """Journal line item with account and amount.

    Reference: Xero Journals API - JournalLine Schema
    """

    JournalLineID: str = Field(..., description="Unique identifier (GUID)")
    AccountID: str = Field(..., description="Account identifier (GUID)")
    AccountCode: str = Field(..., description="Account code")
    AccountType: str = Field(..., description="Account type (e.g., CURRLIAB, BANK)")
    AccountName: str = Field(..., description="Account name")
    Description: str | None = Field(None, description="Line description")
    NetAmount: Decimal = Field(..., description="Net amount (positive=debit, negative=credit)")
    GrossAmount: Decimal = Field(..., description="Gross amount including tax")
    TaxAmount: Decimal = Field(..., description="Tax amount")
    TaxType: str | None = Field(None, description="Tax type code")
    TaxName: str | None = Field(None, description="Tax type name")
    TrackingCategories: list[dict] = Field(
        default_factory=list, description="Tracking category assignments"
    )

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Journal(BaseModel):
    """Journal entry with balanced lines.

    Reference: Xero Journals API - Journal Schema
    """

    JournalID: str = Field(..., description="Unique identifier (GUID)")
    JournalDate: str = Field(..., description="Journal date")
    JournalNumber: int = Field(..., description="Sequential journal number")
    CreatedDateUTC: str | None = Field(None, description="Created timestamp")
    Reference: str | None = Field(None, description="Reference text")
    SourceID: str | None = Field(None, description="Source transaction ID")
    SourceType: str | None = Field(None, description="Source type (e.g., ACCREC, ACCPAY)")
    JournalLines: list[JournalLine] = Field(
        ..., description="Journal line items (debits and credits)"
    )

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class BankAccountRef(BaseModel):
    """Bank account reference for transfers.

    Reference: Xero BankTransfers API - BankAccount Schema
    """

    AccountID: str = Field(..., description="Account identifier (GUID)")
    Name: str | None = Field(None, description="Account name")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class BankTransfer(BaseModel):
    """Bank transfer between accounts.

    Reference: Xero BankTransfers API - BankTransfer Schema
    """

    BankTransferID: str = Field(..., description="Unique identifier (GUID)")
    CreatedDateUTCString: str | None = Field(None, description="Created date string")
    CreatedDateUTC: str | None = Field(None, description="Created timestamp")
    DateString: str | None = Field(None, description="Transfer date string")
    Date: str = Field(..., description="Transfer date")
    FromBankAccount: BankAccountRef = Field(..., description="Source bank account")
    ToBankAccount: BankAccountRef = Field(..., description="Destination bank account")
    Amount: Decimal = Field(..., description="Transfer amount")
    FromBankTransactionID: str | None = Field(None, description="Source transaction ID")
    ToBankTransactionID: str | None = Field(None, description="Destination transaction ID")
    FromIsReconciled: bool | None = Field(None, description="Source side reconciled")
    ToIsReconciled: bool | None = Field(None, description="Destination side reconciled")
    Reference: str | None = Field(None, description="Reference text")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class QuoteLineItem(BaseModel):
    """Quote line item.

    Reference: Xero Quotes API - LineItem Schema
    """

    LineItemID: str | None = Field(None, description="Unique identifier (GUID)")
    Description: str | None = Field(None, description="Line item description")
    Quantity: Decimal | None = Field(None, description="Quantity")
    UnitAmount: Decimal | None = Field(None, description="Unit price")
    LineAmount: Decimal = Field(..., description="Total line amount")
    AccountCode: str | None = Field(None, description="Account code")
    TaxType: str | None = Field(None, description="Tax type code")
    TaxAmount: Decimal | None = Field(None, description="Tax amount")
    DiscountRate: Decimal | None = Field(None, description="Discount percentage")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Quote(BaseModel):
    """Sales quote/estimate.

    Reference: Xero Quotes API - Quote Schema
    """

    QuoteID: str = Field(..., description="Unique identifier (GUID)")
    QuoteNumber: str | None = Field(None, description="Quote number")
    Reference: str | None = Field(None, description="Reference text")
    Status: str = Field(..., description="Status: DRAFT, SENT, ACCEPTED, DECLINED")
    Contact: ContactSummary = Field(..., description="Contact")
    Date: str | None = Field(None, description="Quote date")
    ExpiryDate: str | None = Field(None, description="Expiry date")
    LineItems: list[QuoteLineItem] = Field(default_factory=list, description="Line items")
    SubTotal: Decimal | None = Field(None, description="Total excluding taxes")
    TotalTax: Decimal | None = Field(None, description="Total tax amount")
    Total: Decimal | None = Field(None, description="Grand total")
    CurrencyCode: str | None = Field(None, description="Currency code")
    Title: str | None = Field(None, description="Quote title")
    Summary: str | None = Field(None, description="Quote summary")
    Terms: str | None = Field(None, description="Terms and conditions")
    LineAmountTypes: str | None = Field(None, description="Tax treatment")
    UpdatedDateUTC: str | None = Field(None, description="Last modified timestamp")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class PurchaseOrderLineItem(BaseModel):
    """Purchase order line item.

    Reference: Xero PurchaseOrders API - LineItem Schema
    """

    LineItemID: str | None = Field(None, description="Unique identifier (GUID)")
    Description: str | None = Field(None, description="Line item description")
    Quantity: Decimal | None = Field(None, description="Quantity")
    UnitAmount: Decimal | None = Field(None, description="Unit price")
    LineAmount: Decimal = Field(..., description="Total line amount")
    AccountCode: str | None = Field(None, description="Account code")
    TaxType: str | None = Field(None, description="Tax type code")
    TaxAmount: Decimal | None = Field(None, description="Tax amount")
    ItemCode: str | None = Field(None, description="Item code")
    DiscountRate: Decimal | None = Field(None, description="Discount percentage")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class PurchaseOrder(BaseModel):
    """Purchase order.

    Reference: Xero PurchaseOrders API - PurchaseOrder Schema
    """

    PurchaseOrderID: str = Field(..., description="Unique identifier (GUID)")
    PurchaseOrderNumber: str | None = Field(None, description="PO number")
    DateString: str | None = Field(None, description="Date string")
    Date: str | None = Field(None, description="PO date")
    DeliveryDateString: str | None = Field(None, description="Delivery date string")
    DeliveryDate: str | None = Field(None, description="Delivery date")
    DeliveryAddress: str | None = Field(None, description="Delivery address")
    AttentionTo: str | None = Field(None, description="Attention to")
    Telephone: str | None = Field(None, description="Contact telephone")
    DeliveryInstructions: str | None = Field(None, description="Delivery instructions")
    IsDiscounted: bool | None = Field(None, description="Has discounts")
    Reference: str | None = Field(None, description="Reference text")
    Type: str | None = Field(None, description="Type (PURCHASEORDER)")
    CurrencyRate: Decimal | None = Field(None, description="Exchange rate")
    CurrencyCode: str | None = Field(None, description="Currency code")
    Contact: ContactSummary = Field(..., description="Supplier contact")
    BrandingThemeID: str | None = Field(None, description="Branding theme ID")
    Status: str = Field(..., description="Status: DRAFT, SUBMITTED, AUTHORISED, BILLED, DELETED")
    LineAmountTypes: str | None = Field(None, description="Tax treatment")
    LineItems: list[PurchaseOrderLineItem] = Field(default_factory=list, description="Line items")
    SubTotal: Decimal | None = Field(None, description="Total excluding taxes")
    TotalTax: Decimal | None = Field(None, description="Total tax amount")
    Total: Decimal | None = Field(None, description="Grand total")
    UpdatedDateUTC: str | None = Field(None, description="Last modified timestamp")
    ExpectedArrivalDateString: str | None = Field(None, description="Expected arrival date string")
    ExpectedArrivalDate: str | None = Field(None, description="Expected arrival date")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Allocation(BaseModel):
    """Allocation of credit/prepayment/overpayment to invoice.

    Reference: Xero Allocations Schema
    """

    AllocationID: str | None = Field(None, description="Unique identifier (GUID)")
    Amount: Decimal = Field(..., description="Allocated amount")
    Date: str | None = Field(None, description="Allocation date")
    DateString: str | None = Field(None, description="Date string")
    Invoice: dict | None = Field(None, description="Allocated invoice reference")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class CreditNoteLineItem(BaseModel):
    """Credit note line item.

    Reference: Xero CreditNotes API - LineItem Schema
    """

    LineItemID: str | None = Field(None, description="Unique identifier (GUID)")
    Description: str | None = Field(None, description="Line item description")
    Quantity: Decimal | None = Field(None, description="Quantity")
    UnitAmount: Decimal | None = Field(None, description="Unit price")
    LineAmount: Decimal = Field(..., description="Total line amount")
    AccountCode: str | None = Field(None, description="Account code")
    TaxType: str | None = Field(None, description="Tax type code")
    TaxAmount: Decimal | None = Field(None, description="Tax amount")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class CreditNote(BaseModel):
    """Credit note for returns/adjustments.

    Reference: Xero CreditNotes API - CreditNote Schema
    """

    CreditNoteID: str = Field(..., description="Unique identifier (GUID)")
    CreditNoteNumber: str | None = Field(None, description="Credit note number")
    Contact: ContactSummary = Field(..., description="Contact")
    DateString: str | None = Field(None, description="Date string")
    Date: str | None = Field(None, description="Credit note date")
    Status: str = Field(..., description="Status: DRAFT, SUBMITTED, AUTHORISED, PAID, VOIDED")
    LineAmountTypes: str | None = Field(None, description="Tax treatment")
    SubTotal: Decimal | None = Field(None, description="Total excluding taxes")
    TotalTax: Decimal | None = Field(None, description="Total tax amount")
    Total: Decimal | None = Field(None, description="Grand total")
    UpdatedDateUTC: str | None = Field(None, description="Last modified timestamp")
    CurrencyCode: str | None = Field(None, description="Currency code")
    FullyPaidOnDate: str | None = Field(None, description="Fully paid date")
    Type: str = Field(..., description="Type: ACCRECCREDIT, ACCPAYCREDIT")
    CurrencyRate: Decimal | None = Field(None, description="Exchange rate")
    RemainingCredit: Decimal | None = Field(None, description="Remaining credit amount")
    Allocations: list[Allocation] = Field(default_factory=list, description="Invoice allocations")
    LineItems: list[CreditNoteLineItem] = Field(default_factory=list, description="Line items")
    HasAttachments: bool | None = Field(None, description="Has attachments")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Prepayment(BaseModel):
    """Prepayment record.

    Reference: Xero Prepayments API - Prepayment Schema
    """

    PrepaymentID: str = Field(..., description="Unique identifier (GUID)")
    Contact: ContactSummary = Field(..., description="Contact")
    Date: str | None = Field(None, description="Prepayment date")
    Status: str = Field(..., description="Status: AUTHORISED, PAID, VOIDED")
    LineAmountTypes: str | None = Field(None, description="Tax treatment")
    SubTotal: Decimal | None = Field(None, description="Total excluding taxes")
    TotalTax: Decimal | None = Field(None, description="Total tax amount")
    Total: Decimal | None = Field(None, description="Grand total")
    UpdatedDateUTC: str | None = Field(None, description="Last modified timestamp")
    CurrencyCode: str | None = Field(None, description="Currency code")
    FullyPaidOnDate: str | None = Field(None, description="Fully paid date")
    Type: str = Field(..., description="Type: RECEIVE-PREPAYMENT, SPEND-PREPAYMENT")
    CurrencyRate: Decimal | None = Field(None, description="Exchange rate")
    RemainingCredit: Decimal | None = Field(None, description="Remaining credit amount")
    Allocations: list[Allocation] = Field(default_factory=list, description="Invoice allocations")
    HasAttachments: bool | None = Field(None, description="Has attachments")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Overpayment(BaseModel):
    """Overpayment record.

    Reference: Xero Overpayments API - Overpayment Schema
    """

    OverpaymentID: str = Field(..., description="Unique identifier (GUID)")
    Contact: ContactSummary = Field(..., description="Contact")
    DateString: str | None = Field(None, description="Date string")
    Date: str | None = Field(None, description="Overpayment date")
    Status: str = Field(..., description="Status: AUTHORISED, PAID, VOIDED")
    LineAmountTypes: str | None = Field(None, description="Tax treatment")
    SubTotal: Decimal | None = Field(None, description="Total excluding taxes")
    TotalTax: Decimal | None = Field(None, description="Total tax amount")
    Total: Decimal | None = Field(None, description="Grand total")
    UpdatedDateUTC: str | None = Field(None, description="Last modified timestamp")
    CurrencyCode: str | None = Field(None, description="Currency code")
    Type: str = Field(..., description="Type: RECEIVE-OVERPAYMENT, SPEND-OVERPAYMENT")
    CurrencyRate: Decimal | None = Field(None, description="Exchange rate")
    RemainingCredit: Decimal | None = Field(None, description="Remaining credit amount")
    Allocations: list[Allocation] = Field(default_factory=list, description="Invoice allocations")
    HasAttachments: bool | None = Field(None, description="Has attachments")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class BookDepreciationSetting(BaseModel):
    """Asset depreciation settings.

    Reference: Xero Assets API - BookDepreciationSetting Schema
    """

    depreciationMethod: str | None = Field(
        None,
        description="Method: StraightLine, DiminishingValue100, DiminishingValue150, DiminishingValue200",
    )
    averagingMethod: str | None = Field(None, description="Averaging method: ActualDays, FullMonth")
    depreciationRate: Decimal | None = Field(None, description="Depreciation rate (percentage)")
    depreciationCalculationMethod: str | None = Field(None, description="Calculation method")
    effectiveLifeYears: int | None = Field(None, description="Effective life in years")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class BookDepreciationDetail(BaseModel):
    """Asset depreciation details.

    Reference: Xero Assets API - BookDepreciationDetail Schema
    """

    currentCapitalGain: Decimal | None = Field(None, description="Current capital gain")
    currentGainLoss: Decimal | None = Field(None, description="Current gain/loss")
    depreciationStartDate: str | None = Field(None, description="Depreciation start date")
    costLimit: Decimal | None = Field(None, description="Cost limit")
    residualValue: Decimal | None = Field(None, description="Residual value")
    priorAccumDepreciationAmount: Decimal | None = Field(
        None, description="Prior accumulated depreciation"
    )
    currentAccumDepreciationAmount: Decimal | None = Field(
        None, description="Current accumulated depreciation"
    )

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Asset(BaseModel):
    """Fixed asset.

    Reference: Xero Assets API - Asset Schema
    """

    assetId: str = Field(..., description="Unique identifier (GUID)")
    assetName: str = Field(..., description="Asset name")
    assetNumber: str | None = Field(None, description="Asset number (e.g., FA-0001)")
    purchaseDate: str | None = Field(None, description="Purchase date")
    purchasePrice: Decimal | None = Field(None, description="Purchase price")
    disposalPrice: Decimal | None = Field(None, description="Disposal price")
    assetStatus: str = Field(..., description="Status: Draft, Registered, Disposed")
    bookDepreciationSetting: BookDepreciationSetting | None = Field(
        None, description="Depreciation settings"
    )
    bookDepreciationDetail: BookDepreciationDetail | None = Field(
        None, description="Depreciation details"
    )
    canRollback: bool | None = Field(None, description="Can rollback status")
    accountingBookValue: Decimal | None = Field(None, description="Book value")
    serialNumber: str | None = Field(None, description="Serial number")
    warrantyExpiryDate: str | None = Field(None, description="Warranty expiry date")
    assetTypeId: str | None = Field(None, description="Asset type ID")
    description: str | None = Field(None, description="Asset description")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class AssetType(BaseModel):
    """Asset type with depreciation settings.

    Reference: Xero Assets API - AssetType Schema
    """

    assetTypeId: str = Field(..., description="Unique identifier (GUID)")
    assetTypeName: str = Field(..., description="Asset type name")
    fixedAssetAccountId: str | None = Field(None, description="Fixed asset account ID")
    depreciationExpenseAccountId: str | None = Field(
        None, description="Depreciation expense account ID"
    )
    accumulatedDepreciationAccountId: str | None = Field(
        None, description="Accumulated depreciation account ID"
    )
    bookDepreciationSetting: BookDepreciationSetting | None = Field(
        None, description="Default depreciation settings"
    )
    locks: int | None = Field(None, description="Number of assets using this type")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class FileUser(BaseModel):
    """File user information.

    Reference: Xero Files API - User Schema
    """

    Id: str = Field(..., description="User ID (GUID)")
    Name: str = Field(..., description="User email/name")
    FirstName: str | None = Field(None, description="First name")
    LastName: str | None = Field(None, description="Last name")
    FullName: str | None = Field(None, description="Full name")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class File(BaseModel):
    """File metadata.

    Reference: Xero Files API - File Schema
    """

    Id: str = Field(..., description="Unique identifier (GUID)")
    Name: str = Field(..., description="File name")
    MimeType: str | None = Field(None, description="MIME type")
    Size: int | None = Field(None, description="File size in bytes")
    CreatedDateUtc: str | None = Field(None, description="Created timestamp")
    UpdatedDateUtc: str | None = Field(None, description="Updated timestamp")
    User: FileUser | None = Field(None, description="User who uploaded file")
    FolderId: str | None = Field(None, description="Parent folder ID")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Folder(BaseModel):
    """File folder.

    Reference: Xero Files API - Folder Schema
    """

    Id: str = Field(..., description="Unique identifier (GUID)")
    Name: str = Field(..., description="Folder name")
    FileCount: int | None = Field(None, description="Number of files in folder")
    IsInbox: bool | None = Field(None, description="Is inbox folder")
    Email: str | None = Field(None, description="Email address for inbox (inbox only)")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Association(BaseModel):
    """File association to Xero object.

    Reference: Xero Files API - Association Schema
    """

    FileId: str = Field(..., description="File ID (GUID)")
    ObjectId: str = Field(..., description="Linked object ID (GUID)")
    ObjectType: str = Field(..., description="Object type (Invoice, Contact, etc.)")
    ObjectGroup: str | None = Field(None, description="Object group (Account, Contact, etc.)")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class CurrencyAmount(BaseModel):
    """Currency amount for projects.

    Reference: Xero Projects API - Amount Schema
    """

    currency: str = Field(..., description="Currency code")
    value: Decimal = Field(..., description="Amount value")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Project(BaseModel):
    """Project for time tracking.

    Reference: Xero Projects API - Project Schema
    """

    projectId: str = Field(..., description="Unique identifier (GUID)")
    contactId: str | None = Field(None, description="Associated contact ID")
    name: str = Field(..., description="Project name")
    currencyCode: str | None = Field(None, description="Currency code")
    minutesLogged: int | None = Field(None, description="Total minutes logged")
    totalTaskAmount: CurrencyAmount | None = Field(None, description="Total task amount")
    totalExpenseAmount: CurrencyAmount | None = Field(None, description="Total expense amount")
    minutesToBeInvoiced: int | None = Field(None, description="Minutes not yet invoiced")
    estimate: CurrencyAmount | None = Field(None, description="Project estimate")
    status: str = Field(..., description="Status: INPROGRESS, CLOSED")
    deadlineUtc: str | None = Field(None, description="Project deadline")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class TimeEntry(BaseModel):
    """Project time entry.

    Reference: Xero Projects API - TimeEntry Schema
    """

    timeEntryId: str = Field(..., description="Unique identifier (GUID)")
    userId: str | None = Field(None, description="User ID who logged time")
    projectId: str = Field(..., description="Project ID")
    taskId: str | None = Field(None, description="Task ID")
    dateUtc: str | None = Field(None, description="Entry date")
    duration: int = Field(..., description="Duration in minutes")
    description: str | None = Field(None, description="Time entry description")
    status: str | None = Field(None, description="Status: ACTIVE, LOCKED")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class Pagination(BaseModel):
    """Pagination metadata for paginated responses.

    Reference: Xero Assets/Projects API pagination
    """

    page: int = Field(..., description="Current page number")
    pageSize: int = Field(..., description="Items per page")
    pageCount: int | None = Field(None, description="Total number of pages")
    itemCount: int | None = Field(None, description="Total number of items")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


# =============================================================================
# Phase 2 Response Models
# =============================================================================


class GetBudgetsResponse(BaseModel):
    """Response model for getting budgets.

    Note: Using uppercase 'Budgets' key to match Xero API format.
    """

    Budgets: list[Budget] = Field(..., alias="Budgets", description="List of budgets")
    meta: dict | MetaDict = Field(..., description="Response metadata")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetJournalsResponse(BaseModel):
    """Response model for getting journals.

    Note: Using uppercase 'Journals' key to match Xero API format.
    """

    Journals: list[Journal] = Field(..., alias="Journals", description="List of journals")
    meta: dict | MetaDict = Field(..., description="Response metadata")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetBankTransfersResponse(BaseModel):
    """Response model for getting bank transfers.

    Note: Using uppercase 'BankTransfers' key to match Xero API format.
    """

    BankTransfers: list[BankTransfer] = Field(
        ..., alias="BankTransfers", description="List of bank transfers"
    )
    meta: dict | MetaDict = Field(..., description="Response metadata")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetQuotesResponse(BaseModel):
    """Response model for getting quotes.

    Note: Using uppercase 'Quotes' key to match Xero API format.
    """

    Quotes: list[Quote] = Field(..., alias="Quotes", description="List of quotes")
    meta: dict | MetaDict = Field(..., description="Response metadata")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetPurchaseOrdersResponse(BaseModel):
    """Response model for getting purchase orders.

    Note: Using uppercase 'PurchaseOrders' key to match Xero API format.
    """

    PurchaseOrders: list[PurchaseOrder] = Field(
        ..., alias="PurchaseOrders", description="List of purchase orders"
    )
    meta: dict | MetaDict = Field(..., description="Response metadata")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetCreditNotesResponse(BaseModel):
    """Response model for getting credit notes.

    Note: Using uppercase 'CreditNotes' key to match Xero API format.
    """

    CreditNotes: list[CreditNote] = Field(
        ..., alias="CreditNotes", description="List of credit notes"
    )
    meta: dict | MetaDict = Field(..., description="Response metadata")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetPrepaymentsResponse(BaseModel):
    """Response model for getting prepayments.

    Note: Using uppercase 'Prepayments' key to match Xero API format.
    """

    Prepayments: list[Prepayment] = Field(
        ..., alias="Prepayments", description="List of prepayments"
    )
    meta: dict | MetaDict = Field(..., description="Response metadata")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetOverpaymentsResponse(BaseModel):
    """Response model for getting overpayments.

    Note: Using uppercase 'Overpayments' key to match Xero API format.
    """

    Overpayments: list[Overpayment] = Field(
        ..., alias="Overpayments", description="List of overpayments"
    )
    meta: dict | MetaDict = Field(..., description="Response metadata")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetAssetsResponse(BaseModel):
    """Response model for getting assets.

    Note: Assets API uses lowercase keys and different structure.
    """

    pagination: Pagination = Field(..., description="Pagination metadata")
    items: list[Asset] = Field(..., description="List of assets")
    meta: dict | MetaDict = Field(..., description="Response metadata")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetAssetTypesResponse(BaseModel):
    """Response model for getting asset types.

    Note: Asset types API returns array directly; this wraps it for consistency.
    """

    AssetTypes: list[AssetType] = Field(..., alias="AssetTypes", description="List of asset types")
    meta: dict | MetaDict = Field(..., description="Response metadata")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetFilesResponse(BaseModel):
    """Response model for getting files.

    Note: Files API uses TotalCount, Page, PerPage, Items structure.
    """

    TotalCount: int = Field(..., description="Total number of files")
    Page: int = Field(..., description="Current page number")
    PerPage: int = Field(..., description="Items per page")
    Items: list[File] = Field(..., description="List of files")
    meta: dict | MetaDict = Field(..., description="Response metadata")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetFoldersResponse(BaseModel):
    """Response model for getting folders.

    Note: Folders API returns array directly; this wraps it for consistency.
    """

    Folders: list[Folder] = Field(..., alias="Folders", description="List of folders")
    meta: dict | MetaDict = Field(..., description="Response metadata")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetAssociationsResponse(BaseModel):
    """Response model for getting associations.

    Note: Associations API returns array directly; this wraps it for consistency.
    """

    Associations: list[Association] = Field(
        ..., alias="Associations", description="List of associations"
    )
    meta: dict | MetaDict = Field(..., description="Response metadata")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetProjectsResponse(BaseModel):
    """Response model for getting projects.

    Note: Projects API uses pagination and items structure.
    """

    pagination: Pagination = Field(..., description="Pagination metadata")
    items: list[Project] = Field(..., description="List of projects")
    meta: dict | MetaDict = Field(..., description="Response metadata")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


class GetProjectTimeResponse(BaseModel):
    """Response model for getting project time entries.

    Note: Projects Time API uses pagination and items structure.
    """

    pagination: Pagination = Field(..., description="Pagination metadata")
    items: list[TimeEntry] = Field(..., description="List of time entries")
    meta: dict | MetaDict = Field(..., description="Response metadata")

    model_config = ConfigDict(populate_by_name=True, extra="allow")


# =============================================================================
# CSV Upload Models
# =============================================================================


class UploadCSVInput(BaseModel):
    """Input model for CSV upload tools."""

    csv_content: str = Field(
        ...,
        description="""CSV content with headers.

Supports dot notation for nested fields (e.g., Contact.ContactID, Invoice.InvoiceID).
All dates should use YYYY-MM-DD format.
Numeric values should not include currency symbols.""",
    )
    merge_mode: str = Field(
        default="replace",
        description="""How to handle existing data when uploading.

'append' (default):
- New records (ID not found): INSERT
- Existing records (ID matches): UPDATE all fields
- Records not in CSV: KEPT unchanged

'replace':
- ALL existing records of this type: DELETED first
- Then all CSV rows: INSERTED
- Use with caution - clears historical data""",
    )

    @field_validator("merge_mode")
    @classmethod
    def validate_merge_mode(cls, v: str) -> str:
        """Validate merge mode."""
        if v not in ("append", "replace"):
            raise ValueError("merge_mode must be 'append' or 'replace'")
        return v


class UploadCSVResponse(BaseModel):
    """Response model for CSV upload tools."""

    success: bool = Field(..., description="Whether the upload was successful")
    message: str = Field(..., description="Status message")
    rows_added: int = Field(..., description="Number of new rows added")
    rows_updated: int = Field(..., description="Number of existing rows updated")
    total_rows: int = Field(..., description="Total number of rows in dataset after merge")


# =============================================================================
# Exports
# =============================================================================

__all__ = [
    # Common
    "MetaDict",
    # Accounts
    "Account",
    "GetAccountsResponse",
    "GetAccountsInput",
    # Bank Transactions
    "BankAccountSummary",
    "BankTransaction",
    "BankTransactionLineItem",
    "BankTransactionStatus",
    "BankTransactionType",
    "ContactSummary",
    "GetBankTransactionsRequest",
    "GetBankTransactionsResponse",
    "LineAmountTypes",
    # Contacts
    "Contact",
    "GetContactsResponse",
    "GetContactsInput",
    # Invoices
    "Invoice",
    "InvoiceLineItem",
    "GetInvoicesResponse",
    "GetInvoicesInput",
    # Payments
    "Payment",
    "GetPaymentsResponse",
    "GetPaymentsInput",
    # Reports
    "Report",
    "ReportCell",
    "ReportRow",
    "GetReportsResponse",
    "GetReportBalanceSheetInput",
    "GetReportProfitAndLossInput",
    # State Management
    "ResetStateInput",
    # CSV Upload
    "UploadCSVInput",
    "UploadCSVResponse",
    # =============================================================================
    # Phase 2 Exports
    # =============================================================================
    # Phase 2 Input Models
    "GetReportAgedReceivablesInput",
    "GetReportAgedPayablesInput",
    "GetBudgetSummaryInput",
    "GetBudgetsInput",
    "GetReportExecutiveSummaryInput",
    "GetJournalsInput",
    "GetBankTransfersInput",
    "GetQuotesInput",
    "GetPurchaseOrdersInput",
    "GetCreditNotesInput",
    "GetPrepaymentsInput",
    "GetOverpaymentsInput",
    "GetAssetsInput",
    "GetAssetTypesInput",
    "GetFilesInput",
    "GetFoldersInput",
    "GetAssociationsInput",
    "GetProjectsInput",
    "GetProjectTimeInput",
    # Phase 2 Entity Models
    "TrackingCategory",
    "BudgetLine",
    "Budget",
    "JournalLine",
    "Journal",
    "BankAccountRef",
    "BankTransfer",
    "QuoteLineItem",
    "Quote",
    "PurchaseOrderLineItem",
    "PurchaseOrder",
    "Allocation",
    "CreditNoteLineItem",
    "CreditNote",
    "Prepayment",
    "Overpayment",
    "BookDepreciationSetting",
    "BookDepreciationDetail",
    "Asset",
    "AssetType",
    "FileUser",
    "File",
    "Folder",
    "Association",
    "CurrencyAmount",
    "Project",
    "TimeEntry",
    "Pagination",
    # Phase 2 Response Models
    "GetBudgetsResponse",
    "GetJournalsResponse",
    "GetBankTransfersResponse",
    "GetQuotesResponse",
    "GetPurchaseOrdersResponse",
    "GetCreditNotesResponse",
    "GetPrepaymentsResponse",
    "GetOverpaymentsResponse",
    "GetAssetsResponse",
    "GetAssetTypesResponse",
    "GetFilesResponse",
    "GetFoldersResponse",
    "GetAssociationsResponse",
    "GetProjectsResponse",
    "GetProjectTimeResponse",
]

"""Meta tools that consolidate Xero API operations into action-based interfaces.

This module reduces the number of exposed MCP tools from 28 to 8 by grouping
related operations under domain-based meta tools with an `action` parameter.

Final Tool Count: 7 meta tools + 1 schema tool = 8 total tools

Pattern:
    Instead of: xero_GetAccounts, xero_GetContacts, xero_GetInvoices, ...
    Use: xero_entities(action="accounts", ...), xero_transactions(action="invoices", ...)

All tools support action="help" to discover available actions and required parameters.
"""

from typing import Any, Literal

from mcp_schema import GeminiBaseModel, OutputBaseModel
from pydantic import Field

from mcp_servers.xero.tools.xero_tools import get_provider

# =============================================================================
# VALIDATION CONSTANTS
# =============================================================================

# Valid status/state values for each entity type
INVOICE_STATUSES = frozenset({"DRAFT", "SUBMITTED", "AUTHORISED", "PAID", "VOIDED"})
QUOTE_STATUSES = frozenset({"DRAFT", "SENT", "ACCEPTED", "DECLINED", "INVOICED"})
PURCHASE_ORDER_STATUSES = frozenset({"DRAFT", "SUBMITTED", "AUTHORISED", "BILLED", "DELETED"})
PROJECT_STATES = frozenset({"INPROGRESS", "CLOSED"})
VALID_UNITDP = frozenset({2, 4})


def _validate_enum_values(values: list[str], valid_set: frozenset[str], field_name: str) -> None:
    """Validate that all values are in the valid set.

    Args:
        values: List of values to validate
        valid_set: Set of valid values
        field_name: Name of the field for error messages

    Raises:
        ValueError: If any value is not in the valid set
    """
    invalid = [v for v in values if v not in valid_set]
    if invalid:
        raise ValueError(
            f"Invalid {field_name}: {', '.join(invalid)}. "
            f"Valid options: {', '.join(sorted(valid_set))}"
        )


# =============================================================================
# HELP RESPONSE MODEL
# =============================================================================


class HelpResponse(OutputBaseModel):
    """Standard help response for all tools."""

    tool_name: str
    description: str
    actions: dict[str, dict[str, Any]]


# =============================================================================
# META TOOL INPUT MODELS
# =============================================================================


class EntitiesInput(GeminiBaseModel):
    """Input for entities meta tool (accounts, contacts)."""

    action: Literal["help", "accounts", "contacts"] = Field(
        ..., description="Action: 'help', 'accounts', 'contacts'"
    )
    # For accounts
    where: str | None = Field(
        None,
        description="""Xero OData-style filter expression.

SYNTAX:
- Equality: FieldName=="Value" (strings need double-quotes)
- Inequality: FieldName!="Value"
- Comparison: FieldName>=Value, FieldName<=Value, FieldName>Value, FieldName<Value
- Date comparison: Date>=DateTime(2024,1,15) (note: no zero-padding, use 2024,1,15 not 2024,01,15)
- Combining conditions: Use && for AND (e.g., Status=="ACTIVE"&&Type=="BANK")
- Note: OR is NOT supported in Xero's filter syntax

FILTERABLE FIELDS FOR ACCOUNTS:
Type, Status, Class, Code, Name, SystemAccount

FILTERABLE FIELDS FOR CONTACTS:
Name, ContactStatus, EmailAddress, IsCustomer, IsSupplier

EXAMPLES:
- Status=="ACTIVE" (active accounts only)
- Type=="BANK" (bank accounts only)
- Status=="ACTIVE"&&Type=="REVENUE" (active revenue accounts)
- Name.Contains("Acme") (contacts with "Acme" in name)""",
    )
    order: str | None = Field(None, description="Sort order (e.g., 'Name ASC')")
    # For contacts
    ids: str | None = Field(
        None,
        description="""Comma-separated UUIDs to fetch specific records.

Format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx (standard UUID v4)
Example: 'a1b2c3d4-e5f6-7890-abcd-ef1234567890,b2c3d4e5-f6a7-8901-bcde-f23456789012'

Spaces around commas are automatically trimmed.
Omit parameter to return all records (paginated).""",
    )
    include_archived: bool = Field(
        default=False,
        description="""Control whether archived (inactive) contacts appear in results.

- false (default): Returns only ACTIVE contacts
- true: Returns BOTH active AND archived contacts

Use true when:
- Generating historical reports
- Looking up contacts from old invoices
- Auditing all contacts including inactive ones""",
    )
    # Pagination
    page: int | None = Field(
        None,
        ge=1,
        description="""Page number for paginated results (1-indexed, starts at 1).

Default: 1 (first page) when omitted
Items per page: ~100 records

To paginate through all records:
1. Start with page=1 (or omit for default)
2. If returned items count equals ~100, request page=2
3. Continue until returned items count < 100 or empty array""",
    )


class TransactionsInput(GeminiBaseModel):
    """Input for transactions meta tool."""

    action: Literal[
        "help",
        "invoices",
        "payments",
        "bank_transactions",
        "journals",
        "bank_transfers",
        "credit_notes",
        "prepayments",
        "overpayments",
        "quotes",
        "purchase_orders",
    ] = Field(..., description="Action to perform")
    # Common filters
    where: str | None = Field(
        None,
        description="""Xero OData-style filter expression.

SYNTAX:
- Equality: FieldName=="Value" (strings need double-quotes)
- Inequality: FieldName!="Value"
- Comparison: FieldName>=Value, FieldName<=Value
- Date comparison: Date>=DateTime(2024,1,15) (no zero-padding)
- Combining conditions: Use && for AND
- Note: OR is NOT supported

FILTERABLE FIELDS BY ACTION:
- invoices: Type (ACCREC/ACCPAY), Status, Date, DueDate, Contact.ContactID
- bank_transactions: Type (RECEIVE/SPEND), Status, Date, BankAccount.AccountID
- payments: Date, Amount, PaymentType
- bank_transfers: Date, Amount
- credit_notes: Type, Status, Date
- prepayments/overpayments: Type, Status, Date

EXAMPLES:
- Type=="ACCREC" (customer invoices only)
- Status=="AUTHORISED"&&Date>=DateTime(2024,1,1) (authorized since Jan 2024)
- Type=="RECEIVE" (bank deposits only)""",
    )
    ids: str | None = Field(
        None,
        description="""Comma-separated UUIDs to fetch specific records.

Format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx (standard UUID v4)
Example: 'a1b2c3d4-e5f6-7890-abcd-ef1234567890,b2c3d4e5-f6a7-8901-bcde-f23456789012'

Spaces around commas are automatically trimmed.
Omit parameter to return all records (paginated).""",
    )
    statuses: str | None = Field(
        None,
        description="""Comma-separated status filter. Valid values vary by action:

- invoices: DRAFT, SUBMITTED, AUTHORISED, PAID, VOIDED
- quotes: DRAFT, SENT, ACCEPTED, DECLINED, INVOICED
- purchase_orders: DRAFT, SUBMITTED, AUTHORISED, BILLED, DELETED
- credit_notes: DRAFT, SUBMITTED, AUTHORISED, PAID, VOIDED

Values are case-insensitive (converted to uppercase).
Example: 'DRAFT,AUTHORISED' returns unpaid invoices ready for collection.""",
    )
    # Pagination
    page: int | None = Field(
        None,
        ge=1,
        description="""Page number for paginated results (1-indexed, starts at 1).

Default: 1 (first page) when omitted
Items per page: ~100 records (exact count varies by endpoint)

To paginate through all records:
1. Start with page=1 (or omit for default)
2. If returned items count equals ~100, request page=2
3. Continue until returned items count < 100 or empty array""",
    )
    # For bank_transactions
    unitdp: int | None = Field(
        None,
        description="""Decimal places for unit amounts in line items.

Values:
- 2 (default): Standard currency precision (e.g., $10.50)
- 4: Extended precision for commodities, forex, or high-volume pricing

Use 4 decimal places when:
- Trading commodities with fractional pricing
- Dealing with foreign exchange transactions
- Processing high-volume, low-value items (e.g., $0.0025 per unit)

Most accounting scenarios use 2 (the default).""",
    )
    # For journals
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
4. Repeat until empty response

Note: This differs from page-based pagination used by other endpoints.""",
    )
    payments_only: bool | None = Field(
        None, description="If true, returns only payment-related journal entries."
    )


class ReportsInput(GeminiBaseModel):
    """Input for reports meta tool."""

    action: Literal[
        "help",
        "balance_sheet",
        "profit_loss",
        "aged_receivables",
        "aged_payables",
        "budget_summary",
        "budgets",
        "executive_summary",
    ] = Field(..., description="Report type to generate")
    # Date parameters
    date: str | None = Field(
        None,
        description="""Single reference date in YYYY-MM-DD format.

REQUIRED for:
- balance_sheet: Shows balances as of end-of-day on this date
- executive_summary: Calculates KPIs through this date

OPTIONAL for:
- aged_receivables, aged_payables: Defaults to end of current month

NOT USED for:
- profit_loss: Use from_date and to_date instead
- budget_summary, budgets: Optional

Example: '2024-12-31' for year-end report""",
    )
    from_date: str | None = Field(
        None,
        description="""Period start date in YYYY-MM-DD format (inclusive).

REQUIRED for:
- profit_loss: First day of reporting period

OPTIONAL for:
- aged_receivables, aged_payables: Filters invoices by issue date

Example: '2024-01-01' for full year starting Jan 1""",
    )
    to_date: str | None = Field(
        None,
        description="""Period end date in YYYY-MM-DD format (inclusive).

REQUIRED for:
- profit_loss: Last day of reporting period

OPTIONAL for:
- aged_receivables, aged_payables: Filters invoices by issue date (defaults to today)

Example: '2024-12-31' for full year ending Dec 31""",
    )
    # Period parameters
    periods: int | None = Field(
        None, description="Number of comparison periods. For variance reports."
    )
    timeframe: str | None = Field(
        None,
        description="""Comparison period granularity for multi-period reports.

Valid values: 'MONTH', 'QUARTER', or 'YEAR'
- MONTH: Compare by calendar month
- QUARTER: Compare by 3-month quarters
- YEAR: Compare by fiscal/calendar year

Used with 'periods' parameter to show multiple periods side-by-side.
Example: periods=3, timeframe='MONTH' shows last 3 months.

Default: 'MONTH'""",
    )
    # Other
    tracking_categories: str | None = Field(
        None,
        description="""Filter report by Xero tracking categories (dimensions for cost center/department reporting).

Tracking categories are custom dimensions configured in Xero for segmented reporting
(e.g., "Region", "Department", "Project").

Format: Comma-separated tracking option UUIDs
Example: 'a1b2c3d4-e5f6-7890-abcd-ef1234567890,b2c3d4e5-f6a7-8901-bcde-f23456789012'

To find tracking option IDs:
1. Tracking categories and options are returned with account data
2. Check the TrackingCategories field in account responses

Leave empty to aggregate all tracking categories in the report.""",
    )
    contact_id: str | None = Field(
        None,
        description="""Contact UUID (REQUIRED for aged_receivables and aged_payables reports).

To find contact IDs:
1. Use xero_entities(action="contacts") to list all contacts
2. Filter by name using where='Name.Contains("Customer Name")'
3. Use the ContactID from the results

Format: UUID (e.g., '5040915e-8ce7-4177-8d08-fde416232f18')""",
    )


class AssetsInput(GeminiBaseModel):
    """Input for assets meta tool."""

    action: Literal["help", "list", "types"] = Field(
        ..., description="Action: 'help', 'list', 'types'"
    )
    # For list
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


class FilesInput(GeminiBaseModel):
    """Input for files meta tool."""

    action: Literal["help", "list", "folders", "associations"] = Field(
        ..., description="Action: 'help', 'list', 'folders', 'associations'"
    )
    # For list
    page: int | None = Field(None, ge=1, description="Page number (1-indexed)")
    page_size: int | None = Field(None, ge=1, le=100, description="Items per page (max 100)")
    sort: str | None = Field(
        None, description="Sort field: 'Name', 'Size', or 'CreatedDateUtc'. Example: 'Name'"
    )
    # For associations
    file_id: str | None = Field(
        None,
        description="""File UUID. REQUIRED for 'associations' action.

Format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx (standard UUID v4)

To find file IDs:
1. Use xero_files(action="list") to list all files
2. Use the Id field from the results""",
    )


class AdminInput(GeminiBaseModel):
    """Input for admin meta tool."""

    action: Literal["help", "projects", "project_time", "reset_state", "server_info"] = Field(
        ..., description="Action to perform"
    )
    # For projects
    contact_id: str | None = Field(
        None,
        description="""Contact UUID to filter projects by customer/client.

Format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx (standard UUID v4)
Example: '5040915e-8ce7-4177-8d08-fde416232f18'

To find contact IDs:
1. Use xero_entities(action="contacts") to list all contacts
2. Use the ContactID from the results""",
    )
    states: str | None = Field(
        None,
        description="""Comma-separated project states (case-insensitive, converted to uppercase).

Valid values:
- INPROGRESS: Active projects currently being worked on
- CLOSED: Completed or archived projects

Example: 'INPROGRESS' for only active projects""",
    )
    page: int | None = Field(None, ge=1, description="Page number (1-indexed)")
    page_size: int | None = Field(None, ge=1, le=100, description="Items per page (max 100)")
    # For project_time
    project_id: str | None = Field(
        None,
        description="""Project UUID. REQUIRED for project_time action.

Format: xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx (standard UUID v4)

To find project IDs:
1. Use xero_admin(action="projects") to list all projects
2. Use the projectId from the results""",
    )


class DataInput(GeminiBaseModel):
    """Input for data management meta tool (CSV uploads)."""

    action: Literal[
        "help",
        "upload_accounts",
        "upload_contacts",
        "upload_invoices",
        "upload_payments",
        "upload_bank_transactions",
        "upload_purchase_orders",
        "upload_journals",
    ] = Field(..., description="Action: 'help' or 'upload_<entity_type>'")
    csv_content: str | None = Field(
        None, description="CSV content with headers (required for upload actions)"
    )
    merge_mode: Literal["append", "replace"] = Field(
        default="append",
        description="""How to handle existing data when uploading CSV.

'append' (default):
- New records (ID not found): INSERT
- Existing records (ID matches): UPDATE all fields
- Records not in CSV: KEPT unchanged

'replace':
- ALL existing records of this type: DELETED first
- Then all CSV rows: INSERTED
- Use with caution - clears historical data""",
    )


class SchemaInput(GeminiBaseModel):
    """Input for schema introspection tool."""

    tool: Literal[
        "xero_entities",
        "xero_transactions",
        "xero_reports",
        "xero_assets",
        "xero_files",
        "xero_admin",
        "xero_data",
    ] = Field(..., description="Tool name to get schema for")
    action: str | None = Field(
        None, description="The operation to perform. REQUIRED. Call with action='help' first."
    )


# =============================================================================
# META TOOL OUTPUT MODELS
# =============================================================================


class EntitiesOutput(OutputBaseModel):
    """Output for entities meta tool."""

    action: str
    help: HelpResponse | None = None
    data: dict[str, Any] | None = None


class TransactionsOutput(OutputBaseModel):
    """Output for transactions meta tool."""

    action: str
    help: HelpResponse | None = None
    data: dict[str, Any] | None = None


class ReportsOutput(OutputBaseModel):
    """Output for reports meta tool."""

    action: str
    help: HelpResponse | None = None
    data: dict[str, Any] | None = None


class AssetsOutput(OutputBaseModel):
    """Output for assets meta tool."""

    action: str
    help: HelpResponse | None = None
    data: dict[str, Any] | None = None


class FilesOutput(OutputBaseModel):
    """Output for files meta tool."""

    action: str
    help: HelpResponse | None = None
    data: dict[str, Any] | None = None


class AdminOutput(OutputBaseModel):
    """Output for admin meta tool."""

    action: str
    help: HelpResponse | None = None
    data: dict[str, Any] | None = None
    success: bool | None = None
    message: str | None = None


class DataOutput(OutputBaseModel):
    """Output for data management meta tool."""

    action: str
    help: HelpResponse | None = None
    success: bool | None = None
    message: str | None = None
    rows_added: int | None = None
    rows_updated: int | None = None
    total_rows: int | None = None


class SchemaOutput(OutputBaseModel):
    """Output for schema introspection tool."""

    tool: str
    action: str | None = None
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]


# =============================================================================
# HELP DEFINITIONS
# =============================================================================

ENTITIES_HELP = HelpResponse(
    tool_name="xero_entities",
    description="Retrieve Xero master data entities (accounts, contacts).",
    actions={
        "accounts": {
            "description": "Get chart of accounts with optional filtering",
            "required_params": [],
            "optional_params": ["where", "order", "page"],
        },
        "contacts": {
            "description": "Get contacts (customers/suppliers)",
            "required_params": [],
            "optional_params": ["ids", "where", "include_archived", "page"],
        },
    },
)

TRANSACTIONS_HELP = HelpResponse(
    tool_name="xero_transactions",
    description="Retrieve Xero transactional data.",
    actions={
        "invoices": {
            "description": "Get AR/AP invoices",
            "required_params": [],
            "optional_params": ["ids", "statuses", "where", "page"],
        },
        "payments": {
            "description": "Get payments linking invoices to bank transactions",
            "required_params": [],
            "optional_params": ["where", "page"],
        },
        "bank_transactions": {
            "description": "Get bank transactions (deposits, withdrawals)",
            "required_params": [],
            "optional_params": ["where", "unitdp", "page"],
        },
        "journals": {
            "description": "Get manual journal entries",
            "required_params": [],
            "optional_params": ["offset", "payments_only"],
        },
        "bank_transfers": {
            "description": "Get inter-account transfers",
            "required_params": [],
            "optional_params": ["where"],
        },
        "credit_notes": {
            "description": "Get credit notes",
            "required_params": [],
            "optional_params": ["ids", "where", "page"],
        },
        "prepayments": {
            "description": "Get prepayment records",
            "required_params": [],
            "optional_params": ["where", "page"],
        },
        "overpayments": {
            "description": "Get overpayment records",
            "required_params": [],
            "optional_params": ["where", "page"],
        },
        "quotes": {
            "description": "Get sales quotes/estimates",
            "required_params": [],
            "optional_params": ["ids", "statuses", "where", "page"],
        },
        "purchase_orders": {
            "description": "Get purchase orders",
            "required_params": [],
            "optional_params": ["ids", "statuses", "where", "page"],
        },
    },
)

REPORTS_HELP = HelpResponse(
    tool_name="xero_reports",
    description="Generate Xero financial reports.",
    actions={
        "balance_sheet": {
            "description": "Get balance sheet as of a specific date",
            "required_params": ["date"],
            "optional_params": ["periods", "timeframe", "tracking_categories"],
        },
        "profit_loss": {
            "description": "Get profit and loss statement for a period",
            "required_params": ["from_date", "to_date"],
            "optional_params": ["periods", "timeframe", "tracking_categories"],
        },
        "aged_receivables": {
            "description": "Get aged receivables by contact",
            "required_params": ["contact_id"],
            "optional_params": ["date", "from_date", "to_date"],
        },
        "aged_payables": {
            "description": "Get aged payables by contact",
            "required_params": ["contact_id"],
            "optional_params": ["date", "from_date", "to_date"],
        },
        "budget_summary": {
            "description": "Get budget vs actual comparison",
            "required_params": [],
            "optional_params": ["date", "periods", "timeframe"],
        },
        "budgets": {
            "description": "Get budget entities with tracking categories",
            "required_params": [],
            "optional_params": [],
        },
        "executive_summary": {
            "description": "Get executive summary with KPIs and trends",
            "required_params": ["date"],
            "optional_params": [],
        },
    },
)

ASSETS_HELP = HelpResponse(
    tool_name="xero_assets",
    description="Manage Xero fixed assets.",
    actions={
        "list": {
            "description": "Get fixed assets",
            "required_params": [],
            "optional_params": ["status", "page", "page_size"],
        },
        "types": {
            "description": "Get asset types with depreciation settings",
            "required_params": [],
            "optional_params": [],
        },
    },
)

FILES_HELP = HelpResponse(
    tool_name="xero_files",
    description="Access Xero file storage.",
    actions={
        "list": {
            "description": "Get file metadata",
            "required_params": [],
            "optional_params": ["page", "page_size", "sort"],
        },
        "folders": {
            "description": "Get folder metadata",
            "required_params": [],
            "optional_params": [],
        },
        "associations": {
            "description": "Get file associations",
            "required_params": ["file_id"],
            "optional_params": [],
        },
    },
)

ADMIN_HELP = HelpResponse(
    tool_name="xero_admin",
    description="Administrative operations and project management.",
    actions={
        "projects": {
            "description": "Get projects",
            "required_params": [],
            "optional_params": ["contact_id", "states", "page", "page_size"],
        },
        "project_time": {
            "description": "Get time entries for a project",
            "required_params": ["project_id"],
            "optional_params": ["page", "page_size"],
        },
        "reset_state": {
            "description": "Reset database state (offline mode only)",
            "required_params": [],
            "optional_params": [],
        },
        "server_info": {
            "description": "Get server status and information",
            "required_params": [],
            "optional_params": [],
        },
    },
)

DATA_HELP = HelpResponse(
    tool_name="xero_data",
    description="Upload and manage Xero data via CSV (offline mode only).",
    actions={
        "upload_accounts": {
            "description": "Upload chart of accounts from CSV with accounting equation validation.",
            "required_params": ["csv_content"],
            "optional_params": ["merge_mode"],
            "required_columns": ["AccountID"],
            "recommended_columns": [
                "Code",
                "Name",
                "Type",
                "Status",
                "Class",
                "OpeningBalance",
            ],
            "validation_rules": [
                "AccountID must be unique - duplicates trigger update (append) or error (replace)",
                "If OpeningBalance provided: Assets must equal Liabilities + Equity (within $0.01)",
                "Type must be valid: BANK, CURRENT, CURRLIAB, DEPRECIATN, DIRECTCOSTS, EQUITY, "
                "EXPENSE, FIXED, INVENTORY, LIABILITY, NONCURRENT, OTHERINCOME, OVERHEADS, "
                "PREPAYMENT, REVENUE, SALES, TERMLIAB, PAYGLIABILITY, SUPERANNUATIONEXPENSE, "
                "SUPERANNUATIONLIABILITY, WAGESEXPENSE",
                "Status must be: ACTIVE or ARCHIVED",
            ],
            "error_cases": [
                "Missing AccountID column: Returns error with available column names",
                "Empty AccountID values: Rows skipped, success reported with skip count",
                "Accounting equation fails: Upload rejected before any data changes",
            ],
            "example_row": "AccountID,Code,Name,Type,Status,Class,OpeningBalance\n"
            "acc-001,200,Sales,REVENUE,ACTIVE,REVENUE,0",
        },
        "upload_contacts": {
            "description": "Upload contacts (customers/suppliers) from CSV",
            "required_params": ["csv_content"],
            "optional_params": ["merge_mode"],
            "required_columns": ["ContactID"],
            "recommended_columns": [
                "Name",
                "FirstName",
                "LastName",
                "EmailAddress",
                "ContactStatus",
            ],
            "validation_rules": [
                "ContactID must be unique",
                "ContactStatus must be: ACTIVE or ARCHIVED",
            ],
            "error_cases": [
                "Missing ContactID column: Returns error with available column names",
                "Empty ContactID values: Rows skipped, success reported with skip count",
            ],
            "example_row": "ContactID,Name,EmailAddress,ContactStatus\n"
            "con-001,Acme Corp,contact@acme.com,ACTIVE",
        },
        "upload_invoices": {
            "description": "Upload AR/AP invoices from CSV",
            "required_params": ["csv_content"],
            "optional_params": ["merge_mode"],
            "required_columns": ["InvoiceID"],
            "recommended_columns": [
                "InvoiceNumber",
                "Type",
                "Status",
                "Contact.ContactID",
                "Date",
                "DueDate",
                "Total",
                "AmountDue",
            ],
            "validation_rules": [
                "InvoiceID must be unique",
                "Type must be: ACCREC (customer invoice) or ACCPAY (supplier bill)",
                "Status must be: DRAFT, SUBMITTED, AUTHORISED, PAID, or VOIDED",
                "Contact.ContactID must reference existing contact (in append mode)",
                "Date format: YYYY-MM-DD",
            ],
            "error_cases": [
                "Missing InvoiceID column: Returns error with available column names",
                "Invalid Contact.ContactID reference: Row skipped, other rows processed",
            ],
            "example_row": "InvoiceID,InvoiceNumber,Type,Status,Contact.ContactID,Date,Total\n"
            "inv-001,INV-0001,ACCREC,AUTHORISED,con-001,2024-01-15,1000.00",
        },
        "upload_payments": {
            "description": "Upload payments from CSV",
            "required_params": ["csv_content"],
            "optional_params": ["merge_mode"],
            "required_columns": ["PaymentID"],
            "recommended_columns": ["Invoice.InvoiceID", "Amount", "Date", "PaymentType"],
            "validation_rules": [
                "PaymentID must be unique",
                "Invoice.InvoiceID must reference existing invoice (in append mode)",
                "Amount must be positive number",
                "Date format: YYYY-MM-DD",
            ],
            "error_cases": [
                "Invalid Invoice.InvoiceID reference: Row skipped, other rows processed",
            ],
            "example_row": "PaymentID,Invoice.InvoiceID,Amount,Date\npay-001,inv-001,500.00,2024-01-20",
        },
        "upload_bank_transactions": {
            "description": "Upload bank transactions from CSV",
            "required_params": ["csv_content"],
            "optional_params": ["merge_mode"],
            "required_columns": ["BankTransactionID"],
            "recommended_columns": [
                "Type",
                "Contact.ContactID",
                "Date",
                "Status",
                "Total",
                "BankAccount.AccountID",
            ],
            "validation_rules": [
                "BankTransactionID must be unique",
                "Type must be: RECEIVE (deposit) or SPEND (withdrawal)",
                "Status must be: AUTHORISED, DELETED, or VOIDED",
                "BankAccount.AccountID must reference a BANK type account",
                "Date format: YYYY-MM-DD",
            ],
            "error_cases": [
                "Invalid BankAccount.AccountID reference: Row skipped",
                "Invalid Contact.ContactID reference: Row skipped",
            ],
            "example_row": "BankTransactionID,Type,Date,Total,Status\n"
            "bt-001,RECEIVE,2024-01-15,500.00,AUTHORISED",
        },
        "upload_purchase_orders": {
            "description": "Upload purchase orders from CSV",
            "required_params": ["csv_content"],
            "optional_params": ["merge_mode"],
            "required_columns": ["PurchaseOrderID"],
            "recommended_columns": [
                "PurchaseOrderNumber",
                "Status",
                "Contact.ContactID",
                "Date",
                "DeliveryDate",
                "Total",
            ],
            "validation_rules": [
                "PurchaseOrderID must be unique",
                "Status must be: DRAFT, SUBMITTED, AUTHORISED, BILLED, or DELETED",
                "Contact.ContactID must reference existing supplier contact",
                "Date format: YYYY-MM-DD",
            ],
            "error_cases": [
                "Invalid Contact.ContactID reference: Row skipped",
            ],
            "example_row": "PurchaseOrderID,PurchaseOrderNumber,Status,Date,Total\n"
            "po-001,PO-0001,AUTHORISED,2024-01-15,2500.00",
        },
        "upload_journals": {
            "description": "Upload manual journal entries from CSV",
            "required_params": ["csv_content"],
            "optional_params": ["merge_mode"],
            "required_columns": ["JournalID"],
            "recommended_columns": ["JournalNumber", "JournalDate", "Reference", "SourceType"],
            "validation_rules": [
                "JournalID must be unique",
                "Journal entries must balance (debits = credits, within $0.01)",
                "All AccountID references in journal lines must exist",
                "JournalDate format: YYYY-MM-DD",
            ],
            "error_cases": [
                "Journal doesn't balance: Upload rejected with difference amount shown",
                "Invalid AccountID in journal lines: Row skipped",
            ],
            "example_row": "JournalID,JournalNumber,JournalDate,Reference\n"
            "jnl-001,1001,2024-01-31,Monthly accrual",
        },
    },
)


# =============================================================================
# SCHEMA DEFINITIONS FOR INTROSPECTION
# =============================================================================

TOOL_SCHEMAS = {
    "xero_entities": {"input": EntitiesInput, "output": EntitiesOutput},
    "xero_transactions": {"input": TransactionsInput, "output": TransactionsOutput},
    "xero_reports": {"input": ReportsInput, "output": ReportsOutput},
    "xero_assets": {"input": AssetsInput, "output": AssetsOutput},
    "xero_files": {"input": FilesInput, "output": FilesOutput},
    "xero_admin": {"input": AdminInput, "output": AdminOutput},
    "xero_data": {"input": DataInput, "output": DataOutput},
}


# =============================================================================
# META TOOL IMPLEMENTATIONS
# =============================================================================


async def xero_entities(request: EntitiesInput) -> EntitiesOutput:
    """Retrieve Xero master data entities (accounts, contacts).

    Examples:
        # Get all active bank accounts
        xero_entities(action="accounts", where='Type=="BANK"&&Status=="ACTIVE"')

        # Get accounts sorted by code
        xero_entities(action="accounts", order="Code ASC")

        # Get contacts with "Acme" in name
        xero_entities(action="contacts", where='Name.Contains("Acme")')

        # Get all contacts including archived
        xero_entities(action="contacts", include_archived=True)

        # See available actions and parameters
        xero_entities(action="help")

    Returns:
        EntitiesOutput with:
        - action: The action that was performed
        - data: Dict containing entity array (Accounts or Contacts) and meta
        - help: HelpResponse (only when action="help")

    Raises:
        ValueError: If page < 1
        RuntimeError: If provider not initialized
    """
    match request.action:
        case "help":
            return EntitiesOutput(action="help", help=ENTITIES_HELP)

        case "accounts":
            provider = get_provider()
            data = await provider.get_accounts(
                where=request.where, order=request.order, page=request.page
            )
            return EntitiesOutput(action="accounts", data=data)

        case "contacts":
            provider = get_provider()
            ids_list = (
                [id.strip() for id in request.ids.split(",") if id.strip()] if request.ids else None
            )
            data = await provider.get_contacts(
                ids=ids_list,
                where=request.where,
                include_archived=request.include_archived,
                page=request.page,
            )
            return EntitiesOutput(action="contacts", data=data)

    raise ValueError(f"Unknown action: {request.action}")


async def xero_transactions(request: TransactionsInput) -> TransactionsOutput:
    """Retrieve Xero transactional data including invoices, payments, and bank activity.

    Available actions:
    - invoices: AR (ACCREC) and AP (ACCPAY) invoices
    - payments: Payment records linking invoices to bank transactions
    - bank_transactions: Deposits (RECEIVE) and withdrawals (SPEND)
    - journals: Manual and system-generated journal entries
    - bank_transfers: Inter-account money movements
    - credit_notes: Credit memos for returns/adjustments
    - prepayments/overpayments: Advance or excess payments
    - quotes: Sales estimates (not yet invoiced)
    - purchase_orders: Orders to suppliers

    Examples:
        # Get unpaid customer invoices
        xero_transactions(action="invoices", statuses="DRAFT,AUTHORISED", where='Type=="ACCREC"')

        # Get all bank deposits from 2024
        xero_transactions(action="bank_transactions", where='Type=="RECEIVE"&&Date>=DateTime(2024,1,1)')

        # Get a specific invoice by ID
        xero_transactions(action="invoices", ids="a1b2c3d4-e5f6-7890-abcd-ef1234567890")

        # See all available actions and parameters
        xero_transactions(action="help")

    Returns:
        TransactionsOutput with:
        - action: The action that was performed
        - data: Dict containing entity array (e.g., Invoices, Payments) and meta
        - help: HelpResponse (only when action="help")

    Raises:
        ValueError: If invalid status value provided
        ValueError: If page < 1
        RuntimeError: If provider not initialized
    """
    match request.action:
        case "help":
            return TransactionsOutput(action="help", help=TRANSACTIONS_HELP)

        case "invoices":
            provider = get_provider()
            ids_list = (
                [id.strip() for id in request.ids.split(",") if id.strip()] if request.ids else None
            )
            statuses_list = None
            if request.statuses:
                statuses_list = [
                    s.strip().upper() for s in request.statuses.split(",") if s.strip()
                ]
                _validate_enum_values(statuses_list, INVOICE_STATUSES, "statuses")
            data = await provider.get_invoices(
                ids=ids_list, statuses=statuses_list, where=request.where, page=request.page
            )
            return TransactionsOutput(action="invoices", data=data)

        case "payments":
            provider = get_provider()
            data = await provider.get_payments(where=request.where, page=request.page)
            return TransactionsOutput(action="payments", data=data)

        case "bank_transactions":
            provider = get_provider()
            if request.unitdp is not None and request.unitdp not in VALID_UNITDP:
                raise ValueError("unitdp must be 2 or 4")
            data = await provider.get_bank_transactions(
                where=request.where, unitdp=request.unitdp, page=request.page
            )
            return TransactionsOutput(action="bank_transactions", data=data)

        case "journals":
            provider = get_provider()
            data = await provider.get_journals(
                offset=request.offset, payments_only=request.payments_only
            )
            return TransactionsOutput(action="journals", data=data)

        case "bank_transfers":
            provider = get_provider()
            data = await provider.get_bank_transfers(where=request.where)
            return TransactionsOutput(action="bank_transfers", data=data)

        case "credit_notes":
            provider = get_provider()
            ids_list = (
                [id.strip() for id in request.ids.split(",") if id.strip()] if request.ids else None
            )
            data = await provider.get_credit_notes(
                ids=ids_list, where=request.where, page=request.page
            )
            return TransactionsOutput(action="credit_notes", data=data)

        case "prepayments":
            provider = get_provider()
            data = await provider.get_prepayments(where=request.where, page=request.page)
            return TransactionsOutput(action="prepayments", data=data)

        case "overpayments":
            provider = get_provider()
            data = await provider.get_overpayments(where=request.where, page=request.page)
            return TransactionsOutput(action="overpayments", data=data)

        case "quotes":
            provider = get_provider()
            ids_list = (
                [id.strip() for id in request.ids.split(",") if id.strip()] if request.ids else None
            )
            statuses_list = None
            if request.statuses:
                statuses_list = [
                    s.strip().upper() for s in request.statuses.split(",") if s.strip()
                ]
                _validate_enum_values(statuses_list, QUOTE_STATUSES, "statuses")
            data = await provider.get_quotes(
                ids=ids_list, statuses=statuses_list, where=request.where, page=request.page
            )
            return TransactionsOutput(action="quotes", data=data)

        case "purchase_orders":
            provider = get_provider()
            ids_list = (
                [id.strip() for id in request.ids.split(",") if id.strip()] if request.ids else None
            )
            statuses_list = None
            if request.statuses:
                statuses_list = [
                    s.strip().upper() for s in request.statuses.split(",") if s.strip()
                ]
                _validate_enum_values(statuses_list, PURCHASE_ORDER_STATUSES, "statuses")
            data = await provider.get_purchase_orders(
                ids=ids_list, statuses=statuses_list, where=request.where, page=request.page
            )
            return TransactionsOutput(action="purchase_orders", data=data)

    raise ValueError(f"Unknown action: {request.action}")


async def xero_reports(request: ReportsInput) -> ReportsOutput:
    """Generate Xero financial reports.

    Available reports:
    - balance_sheet: Asset, liability, and equity balances at a point in time
    - profit_loss: Revenue and expenses over a date range
    - aged_receivables: Outstanding customer invoices by age
    - aged_payables: Outstanding supplier bills by age
    - budget_summary: Budget vs actual comparison
    - budgets: Budget definitions with tracking categories
    - executive_summary: KPIs and financial health metrics

    Examples:
        # Get balance sheet as of Dec 31, 2024
        xero_reports(action="balance_sheet", date="2024-12-31")

        # Get P&L for full year 2024
        xero_reports(action="profit_loss", from_date="2024-01-01", to_date="2024-12-31")

        # Get aged receivables for a specific customer
        xero_reports(action="aged_receivables", contact_id="5040915e-8ce7-4177-8d08-fde416232f18")

        # Get budget vs actual by month for 3 periods
        xero_reports(action="budget_summary", periods=3, timeframe="MONTH")

        # See all available reports and parameters
        xero_reports(action="help")

    Returns:
        ReportsOutput with:
        - action: The report type that was generated
        - data: Dict containing report data (Reports array) and meta
        - help: HelpResponse (only when action="help")

    Raises:
        ValueError: If required date parameters missing for report type
        RuntimeError: If provider not initialized
    """
    match request.action:
        case "help":
            return ReportsOutput(action="help", help=REPORTS_HELP)

        case "balance_sheet":
            provider = get_provider()
            if not request.date:
                raise ValueError("'date' is required for balance_sheet")
            tracking_list = (
                [c.strip() for c in request.tracking_categories.split(",") if c.strip()]
                if request.tracking_categories
                else None
            )
            data = await provider.get_report_balance_sheet(
                date=request.date,
                periods=request.periods,
                timeframe=request.timeframe,
                tracking_categories=tracking_list,
            )
            return ReportsOutput(action="balance_sheet", data=data)

        case "profit_loss":
            provider = get_provider()
            if not request.from_date or not request.to_date:
                raise ValueError("'from_date' and 'to_date' are required for profit_loss")
            tracking_list = (
                [c.strip() for c in request.tracking_categories.split(",") if c.strip()]
                if request.tracking_categories
                else None
            )
            data = await provider.get_report_profit_and_loss(
                from_date=request.from_date,
                to_date=request.to_date,
                periods=request.periods,
                timeframe=request.timeframe,
                tracking_categories=tracking_list,
            )
            return ReportsOutput(action="profit_loss", data=data)

        case "aged_receivables":
            provider = get_provider()
            if not request.contact_id:
                raise ValueError("'contact_id' is required for aged_receivables")
            data = await provider.get_report_aged_receivables(
                contact_id=request.contact_id,
                date=request.date,
                from_date=request.from_date,
                to_date=request.to_date,
            )
            return ReportsOutput(action="aged_receivables", data=data)

        case "aged_payables":
            provider = get_provider()
            if not request.contact_id:
                raise ValueError("'contact_id' is required for aged_payables")
            data = await provider.get_report_aged_payables(
                contact_id=request.contact_id,
                date=request.date,
                from_date=request.from_date,
                to_date=request.to_date,
            )
            return ReportsOutput(action="aged_payables", data=data)

        case "budget_summary":
            provider = get_provider()
            # Map string timeframes to their numeric equivalents
            timeframe_map = {"MONTH": 1, "QUARTER": 3, "YEAR": 12}
            timeframe_value = None
            if request.timeframe:
                upper_tf = request.timeframe.strip().upper()
                if upper_tf in timeframe_map:
                    timeframe_value = timeframe_map[upper_tf]
                else:
                    # Fall back to integer parsing for numeric input
                    timeframe_value = int(request.timeframe)
            data = await provider.get_budget_summary(
                date=request.date,
                periods=request.periods,
                timeframe=timeframe_value,
            )
            return ReportsOutput(action="budget_summary", data=data)

        case "budgets":
            provider = get_provider()
            data = await provider.get_budgets()
            return ReportsOutput(action="budgets", data=data)

        case "executive_summary":
            provider = get_provider()
            if not request.date:
                raise ValueError("'date' is required for executive_summary")
            data = await provider.get_report_executive_summary(date=request.date)
            return ReportsOutput(action="executive_summary", data=data)

    raise ValueError(f"Unknown action: {request.action}")


async def xero_assets(request: AssetsInput) -> AssetsOutput:
    """Manage Xero fixed assets.

    Available actions:
    - list: Get fixed assets with optional status filter
    - types: Get asset types with depreciation settings

    Examples:
        # Get all registered (active) assets
        xero_assets(action="list", status="Registered")

        # Get all assets with pagination
        xero_assets(action="list", page=1, page_size=50)

        # Get all asset types and their depreciation settings
        xero_assets(action="types")

        # See all available actions and parameters
        xero_assets(action="help")

    Returns:
        AssetsOutput with:
        - action: The action that was performed
        - data: Dict containing items array and pagination info
        - help: HelpResponse (only when action="help")

    Raises:
        ValueError: If page < 1
        RuntimeError: If provider not initialized
    """
    match request.action:
        case "help":
            return AssetsOutput(action="help", help=ASSETS_HELP)

        case "list":
            provider = get_provider()
            data = await provider.get_assets(
                status=request.status, page=request.page, page_size=request.page_size
            )
            return AssetsOutput(action="list", data=data)

        case "types":
            provider = get_provider()
            data = await provider.get_asset_types()
            return AssetsOutput(action="types", data=data)

    raise ValueError(f"Unknown action: {request.action}")


async def xero_files(request: FilesInput) -> FilesOutput:
    """Access Xero file storage.

    Available actions:
    - list: Get file metadata with optional sorting
    - folders: Get folder structure
    - associations: Get file-to-object associations (requires file_id)

    Examples:
        # Get all files sorted by name
        xero_files(action="list", sort="Name")

        # Get files with pagination
        xero_files(action="list", page=1, page_size=50)

        # Get all folders
        xero_files(action="folders")

        # Get associations for a specific file
        xero_files(action="associations", file_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890")

        # See all available actions and parameters
        xero_files(action="help")

    Returns:
        FilesOutput with:
        - action: The action that was performed
        - data: Dict containing items/folders/associations and pagination info
        - help: HelpResponse (only when action="help")

    Raises:
        ValueError: If file_id missing for associations action
        RuntimeError: If provider not initialized
    """
    match request.action:
        case "help":
            return FilesOutput(action="help", help=FILES_HELP)

        case "list":
            provider = get_provider()
            data = await provider.get_files(
                page=request.page, page_size=request.page_size, sort=request.sort
            )
            return FilesOutput(action="list", data=data)

        case "folders":
            provider = get_provider()
            data = await provider.get_folders()
            return FilesOutput(action="folders", data=data)

        case "associations":
            if not request.file_id:
                raise ValueError("'file_id' is required for associations")
            provider = get_provider()
            data = await provider.get_associations(file_id=request.file_id)
            return FilesOutput(action="associations", data=data)

    raise ValueError(f"Unknown action: {request.action}")


async def xero_admin(request: AdminInput) -> AdminOutput:
    """Administrative operations and project management.

    Available actions:
    - projects: Get projects with optional contact/state filters
    - project_time: Get time entries for a specific project
    - reset_state: Reset database (offline mode only)
    - server_info: Get server status and configuration

    Examples:
        # Get all active projects
        xero_admin(action="projects", states="INPROGRESS")

        # Get projects for a specific customer
        xero_admin(action="projects", contact_id="5040915e-8ce7-4177-8d08-fde416232f18")

        # Get time entries for a project
        xero_admin(action="project_time", project_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890")

        # Get server info
        xero_admin(action="server_info")

        # Reset database (offline mode only)
        xero_admin(action="reset_state")

        # See all available actions and parameters
        xero_admin(action="help")

    Returns:
        AdminOutput with:
        - action: The action that was performed
        - data: Dict containing items/projects and pagination info
        - success/message: For reset_state action
        - help: HelpResponse (only when action="help")

    Raises:
        ValueError: If project_id missing for project_time action
        RuntimeError: If provider not initialized
    """
    match request.action:
        case "help":
            return AdminOutput(action="help", help=ADMIN_HELP)

        case "projects":
            provider = get_provider()
            states_list = None
            if request.states:
                states_list = [s.strip().upper() for s in request.states.split(",") if s.strip()]
                _validate_enum_values(states_list, PROJECT_STATES, "states")
            data = await provider.get_projects(
                page=request.page,
                page_size=request.page_size,
                contact_id=request.contact_id,
                states=states_list,
            )
            return AdminOutput(action="projects", data=data)

        case "project_time":
            if not request.project_id:
                raise ValueError("'project_id' is required for project_time")
            provider = get_provider()
            data = await provider.get_project_time(
                project_id=request.project_id, page=request.page, page_size=request.page_size
            )
            return AdminOutput(action="project_time", data=data)

        case "reset_state":
            from mcp_servers.xero.providers.offline._base import OfflineProviderBase

            provider = get_provider()
            if not isinstance(provider, OfflineProviderBase):
                return AdminOutput(
                    action="reset_state",
                    success=False,
                    message="reset_state is only available in offline mode",
                )
            try:
                from mcp_servers.xero.db.session import drop_db, init_db

                await drop_db()
                await init_db()
                return AdminOutput(
                    action="reset_state",
                    success=True,
                    message="Database reset successfully. All records cleared.",
                )
            except Exception as e:
                return AdminOutput(
                    action="reset_state", success=False, message=f"Failed to reset database: {e}"
                )

        case "server_info":
            from mcp_servers.xero.config import Config

            provider = get_provider()
            config = Config()
            return AdminOutput(
                action="server_info",
                data={
                    "name": "Xero MCP",
                    "version": "0.4.0",
                    "mode": config.mode.value,
                    "provider": provider.__class__.__name__,
                    "tools_available": 8,
                    "pattern": "Meta-tools with action parameter",
                },
            )

    raise ValueError(f"Unknown action: {request.action}")


async def xero_data(request: DataInput) -> DataOutput:
    """Upload and manage Xero data via CSV (offline mode only).

    Available actions:
    - upload_accounts: Upload chart of accounts
    - upload_contacts: Upload customers/suppliers
    - upload_invoices: Upload AR/AP invoices
    - upload_payments: Upload payments
    - upload_bank_transactions: Upload bank transactions
    - upload_purchase_orders: Upload purchase orders
    - upload_journals: Upload journal entries

    Examples:
        # Upload accounts (replace mode - clears existing)
        xero_data(action="upload_accounts", csv_content="AccountID,Code,Name,Type\\nacc-001,200,Sales,REVENUE", merge_mode="replace")

        # Upload contacts (append mode - adds/updates)
        xero_data(action="upload_contacts", csv_content="ContactID,Name\\ncon-001,Acme Corp", merge_mode="append")

        # See required columns and validation rules
        xero_data(action="help")

    Returns:
        DataOutput with:
        - action: The action that was performed
        - success: Whether upload succeeded
        - message: Status message with details
        - rows_added, rows_updated, total_rows: Upload statistics
        - help: HelpResponse (only when action="help")

    Raises:
        ValueError: If csv_content missing for upload actions
        ValueError: If not in offline mode
    """
    from mcp_servers.xero.models import UploadCSVInput
    from mcp_servers.xero.providers.offline._base import OfflineProviderBase
    from mcp_servers.xero.tools.xero_tools import (
        upload_accounts_csv,
        upload_bank_transactions_csv,
        upload_contacts_csv,
        upload_invoices_csv,
        upload_journals_csv,
        upload_payments_csv,
        upload_purchase_orders_csv,
    )

    # Check offline mode for all upload actions
    if request.action != "help":
        provider = get_provider()
        if not isinstance(provider, OfflineProviderBase):
            return DataOutput(
                action=request.action,
                success=False,
                message="CSV upload is only available in offline mode",
            )
        if not request.csv_content:
            return DataOutput(
                action=request.action,
                success=False,
                message="csv_content is required for upload actions",
            )

    # Type narrowing: csv_content is guaranteed to be str for non-help actions
    csv_content: str = request.csv_content if request.csv_content else ""

    match request.action:
        case "help":
            return DataOutput(action="help", help=DATA_HELP)

        case "upload_accounts":
            input_data = UploadCSVInput(csv_content=csv_content, merge_mode=request.merge_mode)
            result = await upload_accounts_csv(input_data)
            return DataOutput(
                action="upload_accounts",
                success=result.success,
                message=result.message,
                rows_added=result.rows_added,
                rows_updated=result.rows_updated,
                total_rows=result.total_rows,
            )

        case "upload_contacts":
            input_data = UploadCSVInput(csv_content=csv_content, merge_mode=request.merge_mode)
            result = await upload_contacts_csv(input_data)
            return DataOutput(
                action="upload_contacts",
                success=result.success,
                message=result.message,
                rows_added=result.rows_added,
                rows_updated=result.rows_updated,
                total_rows=result.total_rows,
            )

        case "upload_invoices":
            input_data = UploadCSVInput(csv_content=csv_content, merge_mode=request.merge_mode)
            result = await upload_invoices_csv(input_data)
            return DataOutput(
                action="upload_invoices",
                success=result.success,
                message=result.message,
                rows_added=result.rows_added,
                rows_updated=result.rows_updated,
                total_rows=result.total_rows,
            )

        case "upload_payments":
            input_data = UploadCSVInput(csv_content=csv_content, merge_mode=request.merge_mode)
            result = await upload_payments_csv(input_data)
            return DataOutput(
                action="upload_payments",
                success=result.success,
                message=result.message,
                rows_added=result.rows_added,
                rows_updated=result.rows_updated,
                total_rows=result.total_rows,
            )

        case "upload_bank_transactions":
            input_data = UploadCSVInput(csv_content=csv_content, merge_mode=request.merge_mode)
            result = await upload_bank_transactions_csv(input_data)
            return DataOutput(
                action="upload_bank_transactions",
                success=result.success,
                message=result.message,
                rows_added=result.rows_added,
                rows_updated=result.rows_updated,
                total_rows=result.total_rows,
            )

        case "upload_purchase_orders":
            input_data = UploadCSVInput(csv_content=csv_content, merge_mode=request.merge_mode)
            result = await upload_purchase_orders_csv(input_data)
            return DataOutput(
                action="upload_purchase_orders",
                success=result.success,
                message=result.message,
                rows_added=result.rows_added,
                rows_updated=result.rows_updated,
                total_rows=result.total_rows,
            )

        case "upload_journals":
            input_data = UploadCSVInput(csv_content=csv_content, merge_mode=request.merge_mode)
            result = await upload_journals_csv(input_data)
            return DataOutput(
                action="upload_journals",
                success=result.success,
                message=result.message,
                rows_added=result.rows_added,
                rows_updated=result.rows_updated,
                total_rows=result.total_rows,
            )

    raise ValueError(f"Unknown action: {request.action}")


async def xero_schema(request: SchemaInput) -> SchemaOutput:
    """Get JSON schema for any Xero tool's input/output.

    Use this tool to discover the exact input parameters and output structure
    for any of the Xero meta-tools.

    Examples:
        # Get schema for xero_entities tool
        xero_schema(tool="xero_entities")

        # Get schema for xero_transactions
        xero_schema(tool="xero_transactions")

        # Get schema for xero_reports
        xero_schema(tool="xero_reports")

    Returns:
        SchemaOutput with:
        - tool: The tool name
        - input_schema: JSON Schema for the tool's input parameters
        - output_schema: JSON Schema for the tool's response structure
    """
    if request.tool not in TOOL_SCHEMAS:
        raise ValueError(f"Unknown tool: {request.tool}. Available: {list(TOOL_SCHEMAS.keys())}")

    schemas = TOOL_SCHEMAS[request.tool]
    input_schema = schemas["input"].model_json_schema()
    output_schema = schemas["output"].model_json_schema()

    return SchemaOutput(
        tool=request.tool,
        action=request.action,
        input_schema=input_schema,
        output_schema=output_schema,
    )

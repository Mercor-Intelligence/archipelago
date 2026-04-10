"""Xero MCP tools implementation."""

import csv
import io
from typing import Any, cast

from loguru import logger

from mcp_servers.xero.models import (
    GetAccountsInput,
    GetAssetsInput,
    GetAssetTypesInput,
    GetAssociationsInput,
    GetBankTransactionsRequest,
    GetBankTransfersInput,
    GetBudgetsInput,
    GetBudgetSummaryInput,
    GetContactsInput,
    GetCreditNotesInput,
    GetFilesInput,
    GetFoldersInput,
    GetInvoicesInput,
    GetJournalsInput,
    GetOverpaymentsInput,
    GetPaymentsInput,
    GetPrepaymentsInput,
    GetProjectsInput,
    GetProjectTimeInput,
    GetPurchaseOrdersInput,
    GetQuotesInput,
    GetReportAgedPayablesInput,
    GetReportAgedReceivablesInput,
    GetReportBalanceSheetInput,
    GetReportExecutiveSummaryInput,
    GetReportProfitAndLossInput,
    ResetStateInput,
    UploadCSVInput,
    UploadCSVResponse,
)
from mcp_servers.xero.providers.base import BaseProvider
from mcp_servers.xero.providers.offline._base import OfflineProviderBase
from mcp_servers.xero.utils.csv_parser import parse_csv_with_dot_notation

# Global provider instance
_provider: BaseProvider | None = None


def set_provider(provider: BaseProvider) -> None:
    """Set the global provider instance."""
    global _provider
    _provider = provider
    logger.info(f"Provider set to {provider.__class__.__name__}")


def _ensure_provider_initialized() -> None:
    """Initialize provider if not already initialized (lazy initialization)."""
    global _provider

    if _provider is not None:
        return

    # Import here to avoid circular imports
    from mcp_servers.xero.auth import OAuthManager, TokenStore
    from mcp_servers.xero.config import Config, Mode
    from mcp_servers.xero.providers import OfflineProvider, OnlineProvider

    config = Config()

    try:
        if config.mode == Mode.OFFLINE:
            logger.info("Auto-initializing offline provider")
            _provider = OfflineProvider()
        else:
            logger.info("Auto-initializing online provider")
            config.validate_online_config()
            token_store = TokenStore(config.token_storage_path)
            oauth_manager = OAuthManager(config, token_store)
            _provider = OnlineProvider(config, oauth_manager)

        logger.info(f"Provider auto-initialized: {_provider.__class__.__name__}")
    except Exception as e:
        logger.error(f"Failed to auto-initialize provider: {e}")
        raise RuntimeError(f"Provider initialization failed: {e}") from e


def get_provider() -> BaseProvider:
    """Get the global provider instance, initializing if necessary."""
    _ensure_provider_initialized()

    if _provider is None:
        raise RuntimeError("Provider not initialized. Call set_provider() first.")
    return _provider


async def get_accounts(input: GetAccountsInput) -> dict[str, Any]:
    """Get chart of accounts from Xero with optional filtering and ordering.

    Examples:
        # Get all active bank accounts
        get_accounts(where='Type=="BANK"&&Status=="ACTIVE"')

        # Get accounts sorted by code
        get_accounts(order="Code ASC")

        # Get second page of results
        get_accounts(page=2)

    Returns:
        dict with keys:
        - Accounts: List of Account objects (AccountID, Name, Type, Code, Status, Class, etc.)
        - meta: Metadata including mode, provider, calledAt

    Raises:
        ValueError: If page < 1
        RuntimeError: If provider not initialized
    """
    # Validate page number
    if input.page is not None and input.page < 1:
        raise ValueError("Page number must be >= 1")

    logger.info(f"Getting accounts (where={input.where}, order={input.order}, page={input.page})")
    provider = get_provider()
    return await provider.get_accounts(where=input.where, order=input.order, page=input.page)


async def get_contacts(input: GetContactsInput) -> dict[str, Any]:
    """Get contacts (customers/suppliers) from Xero.

    Examples:
        # Get all active contacts
        get_contacts(where='ContactStatus=="ACTIVE"')

        # Get contacts by name search
        get_contacts(where='Name.Contains("Acme")')

        # Get specific contacts by ID
        get_contacts(ids="a1b2c3d4-e5f6-7890-abcd-ef1234567890")

        # Include archived contacts
        get_contacts(include_archived=True)

    Returns:
        dict with keys:
        - Contacts: List of Contact objects (ContactID, Name, EmailAddress, etc.)
        - meta: Metadata including mode, provider, calledAt

    Raises:
        ValueError: If page < 1
        RuntimeError: If provider not initialized
    """
    # Validate page number
    if input.page is not None and input.page < 1:
        raise ValueError("Page number must be >= 1")

    logger.info(
        f"Getting contacts (ids={input.ids}, where={input.where}, "
        f"include_archived={input.include_archived}, page={input.page})"
    )
    provider = get_provider()
    ids_list = [id.strip() for id in input.ids.split(",") if id.strip()] if input.ids else None
    return await provider.get_contacts(
        ids=ids_list, where=input.where, include_archived=input.include_archived, page=input.page
    )


async def get_invoices(input: GetInvoicesInput) -> dict[str, Any]:
    """Get AR/AP invoices from Xero with line items and related data.

    Examples:
        # Get unpaid customer invoices
        get_invoices(statuses="DRAFT,AUTHORISED", where='Type=="ACCREC"')

        # Get all supplier bills
        get_invoices(where='Type=="ACCPAY"')

        # Get specific invoice by ID
        get_invoices(ids="a1b2c3d4-e5f6-7890-abcd-ef1234567890")

        # Get invoices from 2024
        get_invoices(where='Date>=DateTime(2024,1,1)')

    Returns:
        dict with keys:
        - Invoices: List of Invoice objects with line items
        - meta: Metadata including mode, provider, calledAt

    Raises:
        ValueError: If invalid status value provided
        ValueError: If page < 1
        RuntimeError: If provider not initialized
    """
    # Default page to 1 if not specified
    page = input.page if input.page is not None else 1

    # Validate page number
    if page < 1:
        raise ValueError("Page number must be >= 1")

    # Validate status enums
    VALID_STATUSES = {"DRAFT", "SUBMITTED", "AUTHORISED", "PAID", "VOIDED"}
    if input.statuses:
        statuses_list = [
            status.strip().upper() for status in input.statuses.split(",") if status.strip()
        ]
        invalid_statuses = [s for s in statuses_list if s not in VALID_STATUSES]
        if invalid_statuses:
            raise ValueError(
                f"Invalid status values: {', '.join(invalid_statuses)}. "
                f"Valid statuses are: {', '.join(sorted(VALID_STATUSES))}"
            )
    else:
        statuses_list = None

    logger.info(
        f"Getting invoices (ids={input.ids}, statuses={input.statuses}, where={input.where}, page={page})"
    )
    provider = get_provider()
    ids_list = [id.strip() for id in input.ids.split(",") if id.strip()] if input.ids else None
    return await provider.get_invoices(
        ids=ids_list, statuses=statuses_list, where=input.where, page=page
    )


async def get_bank_transactions(input: GetBankTransactionsRequest) -> dict[str, Any]:
    """Get bank transactions from Xero including deposits, withdrawals, and prepayments.

    Examples:
        # Get all bank deposits
        get_bank_transactions(where='Type=="RECEIVE"')

        # Get all bank withdrawals
        get_bank_transactions(where='Type=="SPEND"')

        # Get transactions with 4 decimal precision
        get_bank_transactions(unitdp=4)

        # Get authorized transactions from 2024
        get_bank_transactions(where='Status=="AUTHORISED"&&Date>=DateTime(2024,1,1)')

    Returns:
        dict with keys:
        - BankTransactions: List of BankTransaction objects with line items
        - meta: Metadata including mode, provider, calledAt

    Raises:
        ValueError: If unitdp not 2 or 4
        ValueError: If page < 1
        RuntimeError: If provider not initialized
    """
    # Validate page number
    if input.page is not None and input.page < 1:
        raise ValueError("Page number must be >= 1")

    # Validate unitdp parameter
    if input.unitdp is not None and input.unitdp not in {2, 4}:
        raise ValueError("unitdp must be 2 or 4")

    logger.info(
        f"Getting bank transactions (where={input.where}, unitdp={input.unitdp}, page={input.page})"
    )
    provider = get_provider()
    return await provider.get_bank_transactions(
        where=input.where, unitdp=input.unitdp, page=input.page
    )


async def get_payments(input: GetPaymentsInput) -> dict[str, Any]:
    """Get payments from Xero linking invoices to bank transactions.

    Examples:
        # Get all payments
        get_payments()

        # Get payments from 2024
        get_payments(where='Date>=DateTime(2024,1,1)')

    Returns:
        dict with keys:
        - Payments: List of Payment objects (PaymentID, Amount, Invoice, Date, etc.)
        - meta: Metadata including mode, provider, calledAt

    Raises:
        ValueError: If page < 1
        RuntimeError: If provider not initialized
    """
    # Validate page number
    if input.page is not None and input.page < 1:
        raise ValueError("Page number must be >= 1")

    logger.info(f"Getting payments (where={input.where}, page={input.page})")
    provider = get_provider()
    return await provider.get_payments(where=input.where, page=input.page)


async def get_report_balance_sheet(input: GetReportBalanceSheetInput) -> dict[str, Any]:
    """Get Balance Sheet report from Xero.

    Examples:
        # Get balance sheet as of year end
        get_report_balance_sheet(date="2024-12-31")

        # Get balance sheet with 3 quarterly comparisons
        get_report_balance_sheet(date="2024-12-31", periods=3, timeframe="QUARTER")

    Returns:
        dict with keys:
        - Reports: List containing balance sheet report with rows and cells
        - meta: Metadata including mode, provider, calledAt

    Raises:
        RuntimeError: If provider not initialized
    """
    logger.info(
        f"Getting balance sheet report (date={input.date}, periods={input.periods}, "
        f"timeframe={input.timeframe})"
    )
    provider = get_provider()
    tracking_list = (
        [cat.strip() for cat in input.tracking_categories.split(",") if cat.strip()]
        if input.tracking_categories
        else None
    )
    return await provider.get_report_balance_sheet(
        date=input.date,
        periods=input.periods,
        timeframe=input.timeframe,
        tracking_categories=tracking_list,
    )


async def get_report_profit_and_loss(input: GetReportProfitAndLossInput) -> dict[str, Any]:
    """Get Profit & Loss report from Xero.

    Examples:
        # Get P&L for full year
        get_report_profit_and_loss(from_date="2024-01-01", to_date="2024-12-31")

        # Get P&L with monthly breakdown
        get_report_profit_and_loss(from_date="2024-01-01", to_date="2024-12-31", periods=12, timeframe="MONTH")

    Returns:
        dict with keys:
        - Reports: List containing P&L report with rows and cells
        - meta: Metadata including mode, provider, calledAt

    Raises:
        RuntimeError: If provider not initialized
    """
    logger.info(
        f"Getting P&L report (from={input.from_date}, to={input.to_date}, "
        f"periods={input.periods}, timeframe={input.timeframe})"
    )
    provider = get_provider()
    tracking_list = (
        [cat.strip() for cat in input.tracking_categories.split(",") if cat.strip()]
        if input.tracking_categories
        else None
    )
    return await provider.get_report_profit_and_loss(
        from_date=input.from_date,
        to_date=input.to_date,
        periods=input.periods,
        timeframe=input.timeframe,
        tracking_categories=tracking_list,
    )


async def get_report_aged_receivables(input: GetReportAgedReceivablesInput) -> dict[str, Any]:
    """Get Aged Receivables report from Xero.

    Examples:
        # Get aged receivables for a customer
        get_report_aged_receivables(contact_id="5040915e-8ce7-4177-8d08-fde416232f18")

        # Get aged receivables as of specific date
        get_report_aged_receivables(contact_id="5040915e-8ce7-4177-8d08-fde416232f18", date="2024-12-31")

    Returns:
        dict with keys:
        - Reports: List containing aged receivables report with aging buckets
        - meta: Metadata including mode, provider, calledAt

    Raises:
        ValueError: If contact_id is missing
        RuntimeError: If provider not initialized
    """
    logger.info(
        f"Getting aged receivables report (contact_id={input.contact_id}, "
        f"date={input.date}, from_date={input.from_date}, to_date={input.to_date})"
    )
    provider = get_provider()
    return await provider.get_report_aged_receivables(
        contact_id=input.contact_id,
        date=input.date,
        from_date=input.from_date,
        to_date=input.to_date,
    )


async def get_report_aged_payables(input: GetReportAgedPayablesInput) -> dict[str, Any]:
    """Get Aged Payables report from Xero.

    Examples:
        # Get aged payables for a supplier
        get_report_aged_payables(contact_id="5040915e-8ce7-4177-8d08-fde416232f18")

        # Get aged payables as of specific date
        get_report_aged_payables(contact_id="5040915e-8ce7-4177-8d08-fde416232f18", date="2024-12-31")

    Returns:
        dict with keys:
        - Reports: List containing aged payables report with aging buckets
        - meta: Metadata including mode, provider, calledAt

    Raises:
        ValueError: If contact_id is missing
        RuntimeError: If provider not initialized
    """
    logger.info(
        f"Getting aged payables report (contact_id={input.contact_id}, "
        f"date={input.date}, from_date={input.from_date}, to_date={input.to_date})"
    )
    provider = get_provider()
    return await provider.get_report_aged_payables(
        contact_id=input.contact_id,
        date=input.date,
        from_date=input.from_date,
        to_date=input.to_date,
    )


async def get_budget_summary(input: GetBudgetSummaryInput) -> dict[str, Any]:
    """Get Budget Summary report from Xero."""
    logger.info(
        f"Getting budget summary (date={input.date}, periods={input.periods}, "
        f"timeframe={input.timeframe})"
    )
    provider = get_provider()
    return await provider.get_budget_summary(
        date=input.date,
        periods=input.periods,
        timeframe=input.timeframe,
    )


async def get_budgets(input: GetBudgetsInput) -> dict[str, Any]:
    """Get Budgets from Xero."""
    logger.info("Getting budgets")
    provider = get_provider()
    return await provider.get_budgets()


async def get_report_executive_summary(input: GetReportExecutiveSummaryInput) -> dict[str, Any]:
    """Get Executive Summary report from Xero."""
    logger.info(f"Getting executive summary (date={input.date})")
    provider = get_provider()
    return await provider.get_report_executive_summary(date=input.date)


async def get_journals(input: GetJournalsInput) -> dict[str, Any]:
    """Get Journals from Xero.

    Examples:
        # Get first batch of journals
        get_journals()

        # Get journals starting from number 100
        get_journals(offset=100)

        # Get payment-related journals only
        get_journals(payments_only=True)

    Returns:
        dict with keys:
        - Journals: List of Journal objects with JournalLines (debits/credits)
        - meta: Metadata including mode, provider, calledAt

    Note:
        Journals use offset-based pagination (by journal number), not page-based.

    Raises:
        RuntimeError: If provider not initialized
    """
    logger.info(f"Getting journals (offset={input.offset}, payments_only={input.payments_only})")
    provider = get_provider()
    return await provider.get_journals(
        offset=input.offset,
        payments_only=input.payments_only,
    )


async def get_bank_transfers(input: GetBankTransfersInput) -> dict[str, Any]:
    """Get Bank Transfers from Xero."""
    logger.info(f"Getting bank transfers (where={input.where})")
    provider = get_provider()
    return await provider.get_bank_transfers(where=input.where)


async def get_quotes(input: GetQuotesInput) -> dict[str, Any]:
    """Get Quotes from Xero.

    Examples:
        # Get all sent quotes awaiting response
        get_quotes(statuses="SENT")

        # Get quotes by ID
        get_quotes(ids="a1b2c3d4-e5f6-7890-abcd-ef1234567890")

        # Get accepted quotes from 2024
        get_quotes(statuses="ACCEPTED", where='Date>=DateTime(2024,1,1)')

    Returns:
        dict with keys:
        - Quotes: List of Quote objects with line items
        - meta: Metadata including mode, provider, calledAt

    Raises:
        ValueError: If invalid status value provided
        ValueError: If page < 1
        RuntimeError: If provider not initialized
    """
    # Validate page number
    if input.page is not None and input.page < 1:
        raise ValueError("Page number must be >= 1")

    # Validate status enums
    VALID_STATUSES = {"DRAFT", "SENT", "ACCEPTED", "DECLINED", "INVOICED"}
    if input.statuses:
        statuses_list = [s.strip().upper() for s in input.statuses.split(",") if s.strip()]
        invalid_statuses = [s for s in statuses_list if s not in VALID_STATUSES]
        if invalid_statuses:
            raise ValueError(
                f"Invalid status values: {', '.join(invalid_statuses)}. "
                f"Valid statuses are: {', '.join(sorted(VALID_STATUSES))}"
            )
    else:
        statuses_list = None

    logger.info(
        f"Getting quotes (ids={input.ids}, statuses={input.statuses}, "
        f"where={input.where}, page={input.page})"
    )
    provider = get_provider()
    ids_list = [id.strip() for id in input.ids.split(",") if id.strip()] if input.ids else None
    return await provider.get_quotes(
        ids=ids_list, statuses=statuses_list, where=input.where, page=input.page
    )


async def get_purchase_orders(input: GetPurchaseOrdersInput) -> dict[str, Any]:
    """Get Purchase Orders from Xero.

    Examples:
        # Get all authorized POs
        get_purchase_orders(statuses="AUTHORISED")

        # Get POs awaiting approval
        get_purchase_orders(statuses="DRAFT,SUBMITTED")

        # Get PO by ID
        get_purchase_orders(ids="a1b2c3d4-e5f6-7890-abcd-ef1234567890")

    Returns:
        dict with keys:
        - PurchaseOrders: List of PurchaseOrder objects with line items
        - meta: Metadata including mode, provider, calledAt

    Raises:
        ValueError: If invalid status value provided
        ValueError: If page < 1
        RuntimeError: If provider not initialized
    """
    # Validate page number
    if input.page is not None and input.page < 1:
        raise ValueError("Page number must be >= 1")

    # Validate status enums
    VALID_STATUSES = {"DRAFT", "SUBMITTED", "AUTHORISED", "BILLED", "DELETED"}
    if input.statuses:
        statuses_list = [s.strip().upper() for s in input.statuses.split(",") if s.strip()]
        invalid_statuses = [s for s in statuses_list if s not in VALID_STATUSES]
        if invalid_statuses:
            raise ValueError(
                f"Invalid status values: {', '.join(invalid_statuses)}. "
                f"Valid statuses are: {', '.join(sorted(VALID_STATUSES))}"
            )
    else:
        statuses_list = None

    logger.info(
        f"Getting purchase orders (ids={input.ids}, statuses={input.statuses}, "
        f"where={input.where}, page={input.page})"
    )
    provider = get_provider()
    ids_list = [id.strip() for id in input.ids.split(",") if id.strip()] if input.ids else None
    return await provider.get_purchase_orders(
        ids=ids_list, statuses=statuses_list, where=input.where, page=input.page
    )


async def get_credit_notes(input: GetCreditNotesInput) -> dict[str, Any]:
    """Get Credit Notes from Xero."""
    # Validate page number
    if input.page is not None and input.page < 1:
        raise ValueError("Page number must be >= 1")

    logger.info(f"Getting credit notes (ids={input.ids}, where={input.where}, page={input.page})")
    provider = get_provider()
    ids_list = [id.strip() for id in input.ids.split(",") if id.strip()] if input.ids else None
    return await provider.get_credit_notes(ids=ids_list, where=input.where, page=input.page)


async def get_prepayments(input: GetPrepaymentsInput) -> dict[str, Any]:
    """Get Prepayments from Xero."""
    # Validate page number
    if input.page is not None and input.page < 1:
        raise ValueError("Page number must be >= 1")

    logger.info(f"Getting prepayments (where={input.where}, page={input.page})")
    provider = get_provider()
    return await provider.get_prepayments(where=input.where, page=input.page)


async def get_overpayments(input: GetOverpaymentsInput) -> dict[str, Any]:
    """Get Overpayments from Xero."""
    # Validate page number
    if input.page is not None and input.page < 1:
        raise ValueError("Page number must be >= 1")

    logger.info(f"Getting overpayments (where={input.where}, page={input.page})")
    provider = get_provider()
    return await provider.get_overpayments(where=input.where, page=input.page)


async def get_assets(input: GetAssetsInput) -> dict[str, Any]:
    """Get Assets from Xero.

    Examples:
        # Get all registered (active) assets
        get_assets(status="Registered")

        # Get draft assets awaiting setup
        get_assets(status="Draft")

        # Get assets with pagination
        get_assets(page=1, page_size=50)

    Returns:
        dict with keys:
        - pagination: Pagination metadata (page, pageSize, itemCount)
        - items: List of Asset objects with depreciation details
        - meta: Metadata including mode, provider, calledAt

    Note:
        Asset status values are CASE-SENSITIVE: 'Draft', 'Registered', 'Disposed'

    Raises:
        ValueError: If page < 1
        RuntimeError: If provider not initialized
    """
    # Validate page number
    if input.page is not None and input.page < 1:
        raise ValueError("Page number must be >= 1")

    logger.info(
        f"Getting assets (status={input.status}, page={input.page}, page_size={input.page_size})"
    )
    provider = get_provider()
    return await provider.get_assets(
        status=input.status, page=input.page, page_size=input.page_size
    )


async def get_asset_types(input: GetAssetTypesInput) -> dict[str, Any]:
    """Get Asset Types from Xero."""
    logger.info("Getting asset types")
    provider = get_provider()
    return await provider.get_asset_types()


async def get_files(input: GetFilesInput) -> dict[str, Any]:
    """Get Files from Xero."""
    # Validate page number
    if input.page is not None and input.page < 1:
        raise ValueError("Page number must be >= 1")

    logger.info(
        f"Getting files (page={input.page}, page_size={input.page_size}, sort={input.sort})"
    )
    provider = get_provider()
    return await provider.get_files(page=input.page, page_size=input.page_size, sort=input.sort)


async def get_folders(input: GetFoldersInput) -> dict[str, Any]:
    """Get Folders from Xero."""
    logger.info("Getting folders")
    provider = get_provider()
    return await provider.get_folders()


async def get_associations(input: GetAssociationsInput) -> dict[str, Any]:
    """Get Associations from Xero."""
    logger.info(f"Getting associations (file_id={input.file_id})")
    provider = get_provider()
    return await provider.get_associations(file_id=input.file_id)


async def get_projects(input: GetProjectsInput) -> dict[str, Any]:
    """Get Projects from Xero.

    Examples:
        # Get all active projects
        get_projects(states="INPROGRESS")

        # Get projects for a specific customer
        get_projects(contact_id="5040915e-8ce7-4177-8d08-fde416232f18")

        # Get all projects with pagination
        get_projects(page=1, page_size=50)

    Returns:
        dict with keys:
        - pagination: Pagination metadata (page, pageSize, itemCount)
        - items: List of Project objects with time tracking info
        - meta: Metadata including mode, provider, calledAt

    Raises:
        ValueError: If invalid state value provided
        ValueError: If page < 1
        RuntimeError: If provider not initialized
    """
    # Validate page number
    if input.page is not None and input.page < 1:
        raise ValueError("Page number must be >= 1")

    # Validate state enums
    VALID_STATES = {"INPROGRESS", "CLOSED"}
    if input.states:
        states_list = [s.strip().upper() for s in input.states.split(",") if s.strip()]
        invalid_states = [s for s in states_list if s not in VALID_STATES]
        if invalid_states:
            raise ValueError(
                f"Invalid state values: {', '.join(invalid_states)}. "
                f"Valid states are: {', '.join(sorted(VALID_STATES))}"
            )
    else:
        states_list = None

    logger.info(
        f"Getting projects (contact_id={input.contact_id}, states={input.states}, "
        f"page={input.page}, page_size={input.page_size})"
    )
    provider = get_provider()
    return await provider.get_projects(
        page=input.page, page_size=input.page_size, contact_id=input.contact_id, states=states_list
    )


async def get_project_time(input: GetProjectTimeInput) -> dict[str, Any]:
    """Get Project Time Entries from Xero.

    Examples:
        # Get time entries for a project
        get_project_time(project_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890")

        # Get time entries with pagination
        get_project_time(project_id="a1b2c3d4-e5f6-7890-abcd-ef1234567890", page=1, page_size=50)

    Returns:
        dict with keys:
        - pagination: Pagination metadata (page, pageSize, itemCount)
        - items: List of TimeEntry objects (duration, description, user)
        - meta: Metadata including mode, provider, calledAt

    Raises:
        ValueError: If project_id is missing
        ValueError: If page < 1
        RuntimeError: If provider not initialized
    """
    # Validate page number
    if input.page is not None and input.page < 1:
        raise ValueError("Page number must be >= 1")

    logger.info(
        f"Getting project time (project_id={input.project_id}, "
        f"page={input.page}, page_size={input.page_size})"
    )
    provider = get_provider()
    return await provider.get_project_time(
        project_id=input.project_id, page=input.page, page_size=input.page_size
    )


# =============================================================================
# CSV Upload Tools
# =============================================================================


async def upload_accounts_csv(input: UploadCSVInput) -> UploadCSVResponse:
    """Upload accounts data from CSV with accounting equation validation.

    Examples:
        # Upload accounts (replace mode - clears existing)
        upload_accounts_csv(csv_content="AccountID,Code,Name,Type,Status\\nacc-001,200,Sales,REVENUE,ACTIVE", merge_mode="replace")

        # Upload accounts (append mode - adds/updates)
        upload_accounts_csv(csv_content="AccountID,Code,Name,Type,Status\\nacc-001,200,Sales,REVENUE,ACTIVE", merge_mode="append")

    Required columns: AccountID
    Recommended columns: Code, Name, Type, Status, Class, OpeningBalance

    Validation:
        - If OpeningBalance provided: Assets must equal Liabilities + Equity (within $0.01)
        - Type must be valid Xero account type
        - Status must be ACTIVE or ARCHIVED

    Returns:
        UploadCSVResponse with success, message, rows_added, rows_updated, total_rows

    Raises:
        ValueError: If accounting equation validation fails
        ValueError: If not in offline mode
    """
    # First, validate the accounting equation before uploading
    parsed_data = parse_csv_with_dot_notation(input.csv_content)

    if parsed_data:
        # Calculate totals by class
        total_assets = 0.0
        total_liabilities = 0.0
        total_equity = 0.0

        for row in parsed_data:
            # Get the class (supports both formats)
            account_class = (row.get("Class") or row.get("class") or "").upper()
            # Get opening balance (supports multiple field names)
            balance_str = (
                row.get("OpeningBalance")
                or row.get("opening_balance")
                or row.get("Balance")
                or row.get("BalanceUSD")
                or "0"
            )

            try:
                balance = float(balance_str) if balance_str else 0.0
            except (ValueError, TypeError):
                balance = 0.0

            if account_class == "ASSET":
                total_assets += balance
            elif account_class == "LIABILITY":
                total_liabilities += balance
            elif account_class == "EQUITY":
                total_equity += balance
            # REVENUE and EXPENSE don't affect balance sheet equation

        # Validate accounting equation: Assets = Liabilities + Equity
        liabilities_plus_equity = total_liabilities + total_equity
        difference = abs(total_assets - liabilities_plus_equity)

        # Allow small floating point tolerance (0.01)
        if difference > 0.01 and (total_assets > 0 or liabilities_plus_equity > 0):
            return UploadCSVResponse(
                success=False,
                message=(
                    f"Accounting equation validation failed: "
                    f"Assets ({total_assets:,.2f}) ≠ Liabilities ({total_liabilities:,.2f}) + "
                    f"Equity ({total_equity:,.2f}) = {liabilities_plus_equity:,.2f}. "
                    f"Difference: {difference:,.2f}. "
                    f"Please adjust opening balances so Assets = Liabilities + Equity."
                ),
                rows_added=0,
                rows_updated=0,
                total_rows=len(parsed_data),
            )

        logger.info(
            f"Account balance validation passed: Assets={total_assets:,.2f}, "
            f"Liabilities={total_liabilities:,.2f}, Equity={total_equity:,.2f}"
        )

    # Validation passed, proceed with upload
    return await _upload_csv_generic(
        input=input,
        data_key="Accounts",
        id_field="AccountID",
        entity_name="accounts",
    )


async def upload_contacts_csv(input: UploadCSVInput) -> UploadCSVResponse:
    """Upload contacts data from CSV."""
    return await _upload_csv_generic(
        input=input,
        data_key="Contacts",
        id_field="ContactID",
        entity_name="contacts",
    )


async def upload_invoices_csv(input: UploadCSVInput) -> UploadCSVResponse:
    """Upload invoices data from CSV."""
    return await _upload_csv_generic(
        input=input,
        data_key="Invoices",
        id_field="InvoiceID",
        entity_name="invoices",
    )


async def upload_bank_transactions_csv(input: UploadCSVInput) -> UploadCSVResponse:
    """Upload bank transactions data from CSV."""
    return await _upload_csv_generic(
        input=input,
        data_key="BankTransactions",
        id_field="BankTransactionID",
        entity_name="bank_transactions",
    )


async def upload_payments_csv(input: UploadCSVInput) -> UploadCSVResponse:
    """Upload payments data from CSV."""
    return await _upload_csv_generic(
        input=input,
        data_key="Payments",
        id_field="PaymentID",
        entity_name="payments",
    )


async def upload_reports_csv(input: UploadCSVInput) -> UploadCSVResponse:
    """Upload reports data from CSV."""
    # Reports are stored as a nested dict, not a list
    # This is a special case - we'll just parse and replace
    provider = get_provider()

    if not isinstance(provider, OfflineProviderBase):
        raise ValueError("CSV upload is only supported in offline mode")

    # Cast to OfflineProviderBase for type checker
    cast(OfflineProviderBase, provider)

    try:
        # Parse CSV
        parsed_data = parse_csv_with_dot_notation(input.csv_content)

        if not parsed_data:
            return UploadCSVResponse(
                success=False,
                message="No data parsed from CSV",
                rows_added=0,
                rows_updated=0,
                total_rows=0,
            )

        # TODO: Implement database-backed reports upload
        # The provider no longer has _data or write_synthetic_data()
        # Need to implement proper database storage for reports
        raise NotImplementedError(
            "CSV upload for reports is not yet implemented with database storage. "
            "Please use the template CSV files in mcp_servers/xero/templates/ "
            "and the setup_demo_data.py script instead."
        )

        logger.info(f"Uploaded reports data: {len(parsed_data)} report(s)")

        return UploadCSVResponse(
            success=True,
            message=f"Successfully uploaded {len(parsed_data)} report(s)",
            rows_added=len(parsed_data),
            rows_updated=0,
            total_rows=len(parsed_data),
        )

    except Exception as e:
        logger.error(f"Failed to upload reports CSV: {e}")
        return UploadCSVResponse(
            success=False,
            message=f"Failed to upload reports: {str(e)}",
            rows_added=0,
            rows_updated=0,
            total_rows=0,
        )


# =============================================================================
# Phase 2 CSV Upload Tools - Accounting Operations
# =============================================================================


async def upload_journals_csv(input: UploadCSVInput) -> UploadCSVResponse:
    """Upload journal entries data from CSV."""
    import json

    # Parse CSV to check format
    parsed_data = parse_csv_with_dot_notation(input.csv_content)

    if not parsed_data:
        return UploadCSVResponse(
            success=False,
            message="No data parsed from CSV",
            rows_added=0,
            rows_updated=0,
            total_rows=0,
        )

    # Detect format: check if first row has dr_cr/debit_credit column (simple format)
    first_row = parsed_data[0]
    has_dr_cr = any(
        key.lower() in ("dr_cr", "debit_credit", "dr/cr", "type") for key in first_row.keys()
    )
    has_journal_lines = any(
        key.lower() in ("journal_lines", "journallines") for key in first_row.keys()
    )

    # If simple format, convert to JSON format
    if has_dr_cr and not has_journal_lines:
        logger.info("Detected simple journal format - converting to JSON format")

        # Group rows by journal_id
        journals_by_id: dict[str, list[dict]] = {}
        journal_metadata: dict[str, dict] = {}

        for row in parsed_data:
            journal_id = row.get("JournalID") or row.get("journal_id")
            if not journal_id:
                continue

            if journal_id not in journals_by_id:
                journals_by_id[journal_id] = []
                # Store metadata from first row
                journal_metadata[journal_id] = {
                    "journal_date": row.get("JournalDate") or row.get("journal_date"),
                    "journal_number": row.get("JournalNumber") or row.get("journal_number"),
                    "reference": row.get("Reference")
                    or row.get("reference")
                    or row.get("description"),
                    "source_type": row.get("SourceType") or row.get("source_type") or "MANUAL",
                }

            # Get dr_cr value
            dr_cr = None
            for key in ("dr_cr", "debit_credit", "dr/cr", "type", "DrCr", "DebitCredit"):
                if key in row:
                    dr_cr = row[key]
                    break

            # Get amount
            amount_str = row.get("Amount") or row.get("amount") or row.get("NetAmount") or "0"
            try:
                amount = float(amount_str)
            except (ValueError, TypeError):
                amount = 0.0

            # Convert DR/CR to signed amount
            if dr_cr and dr_cr.upper() in ("CR", "CREDIT", "C"):
                amount = -abs(amount)  # Credits are negative
            elif dr_cr and dr_cr.upper() in ("DR", "DEBIT", "D"):
                amount = abs(amount)  # Debits are positive

            # Build journal line
            line = {
                "AccountID": row.get("AccountID") or row.get("account_id"),
                "NetAmount": amount,
            }

            # Add optional fields if present
            account_code = row.get("AccountCode") or row.get("account_code")
            if account_code:
                line["AccountCode"] = account_code

            description = row.get("Description") or row.get("description")
            if description:
                line["Description"] = description

            journals_by_id[journal_id].append(line)

        # Validate each journal balances to zero
        for journal_id, lines in journals_by_id.items():
            total = sum(line["NetAmount"] for line in lines)
            if abs(total) > 0.01:
                return UploadCSVResponse(
                    success=False,
                    message=(
                        f"Journal {journal_id} does not balance: "
                        f"Debits - Credits = {total:,.2f}. "
                        f"Journal entries must sum to zero."
                    ),
                    rows_added=0,
                    rows_updated=0,
                    total_rows=len(parsed_data),
                )

        # Convert to JSON format for upload
        converted_rows = []
        for journal_id, lines in journals_by_id.items():
            metadata = journal_metadata[journal_id]
            converted_rows.append(
                {
                    "journal_id": journal_id,
                    "journal_date": metadata["journal_date"],
                    "journal_number": metadata["journal_number"],
                    "reference": metadata["reference"],
                    "source_type": metadata["source_type"],
                    "journal_lines": json.dumps(lines),
                }
            )

        # Rebuild CSV content from converted rows
        if converted_rows:
            headers = list(converted_rows[0].keys())
            csv_lines = [",".join(headers)]
            for row in converted_rows:
                values = []
                for h in headers:
                    val = row.get(h) or ""
                    # Quote values containing commas, quotes, or newlines
                    if isinstance(val, str) and (
                        "," in val or '"' in val or "\n" in val or "\r" in val
                    ):
                        val = '"' + val.replace('"', '""') + '"'
                    values.append(str(val) if val else "")
                csv_lines.append(",".join(values))
            input.csv_content = "\n".join(csv_lines)

        logger.info(
            f"Converted {len(parsed_data)} simple rows to {len(converted_rows)} journal entries"
        )

    return await _upload_csv_generic(
        input=input,
        data_key="Journals",
        id_field="JournalID",
        entity_name="journals",
    )


async def upload_purchase_orders_csv(input: UploadCSVInput) -> UploadCSVResponse:
    """Upload purchase orders data from CSV."""
    return await _upload_csv_generic(
        input=input,
        data_key="PurchaseOrders",
        id_field="PurchaseOrderID",
        entity_name="purchase_orders",
    )


async def upload_quotes_csv(input: UploadCSVInput) -> UploadCSVResponse:
    """Upload sales quotes/estimates data from CSV."""
    return await _upload_csv_generic(
        input=input,
        data_key="Quotes",
        id_field="QuoteID",
        entity_name="quotes",
    )


async def upload_credit_notes_csv(input: UploadCSVInput) -> UploadCSVResponse:
    """Upload credit notes data from CSV."""
    return await _upload_csv_generic(
        input=input,
        data_key="CreditNotes",
        id_field="CreditNoteID",
        entity_name="credit_notes",
    )


async def upload_bank_transfers_csv(input: UploadCSVInput) -> UploadCSVResponse:
    """Upload inter-account bank transfers data from CSV."""
    return await _upload_csv_generic(
        input=input,
        data_key="BankTransfers",
        id_field="BankTransferID",
        entity_name="bank_transfers",
    )


async def upload_overpayments_csv(input: UploadCSVInput) -> UploadCSVResponse:
    """Upload customer/supplier overpayments data from CSV."""
    return await _upload_csv_generic(
        input=input,
        data_key="Overpayments",
        id_field="OverpaymentID",
        entity_name="overpayments",
    )


async def upload_prepayments_csv(input: UploadCSVInput) -> UploadCSVResponse:
    """Upload customer/supplier prepayments data from CSV."""
    return await _upload_csv_generic(
        input=input,
        data_key="Prepayments",
        id_field="PrepaymentID",
        entity_name="prepayments",
    )


async def upload_budgets_csv(input: UploadCSVInput) -> UploadCSVResponse:
    """Upload accounting budgets data from CSV."""
    return await _upload_csv_generic(
        input=input,
        data_key="Budgets",
        id_field="BudgetID",
        entity_name="budgets",
    )


# =============================================================================
# Phase 2 CSV Upload Tools - Assets API
# =============================================================================


async def upload_assets_csv(input: UploadCSVInput) -> UploadCSVResponse:
    """Upload fixed assets data from CSV (Assets API)."""
    return await _upload_csv_generic(
        input=input,
        data_key="Assets",
        id_field="assetId",
        entity_name="assets",
    )


async def upload_asset_types_csv(input: UploadCSVInput) -> UploadCSVResponse:
    """Upload asset type definitions from CSV (Assets API)."""
    return await _upload_csv_generic(
        input=input,
        data_key="AssetTypes",
        id_field="assetTypeId",
        entity_name="asset_types",
    )


# =============================================================================
# Phase 2 CSV Upload Tools - Projects API
# =============================================================================


async def upload_projects_csv(input: UploadCSVInput) -> UploadCSVResponse:
    """Upload projects data from CSV (Projects API)."""
    return await _upload_csv_generic(
        input=input,
        data_key="Projects",
        id_field="projectId",
        entity_name="projects",
    )


async def upload_time_entries_csv(input: UploadCSVInput) -> UploadCSVResponse:
    """Upload project time entries from CSV (Projects API)."""
    return await _upload_csv_generic(
        input=input,
        data_key="TimeEntries",
        id_field="timeEntryId",
        entity_name="time_entries",
    )


# =============================================================================
# Phase 2 CSV Upload Tools - Files API
# =============================================================================


async def upload_files_csv(input: UploadCSVInput) -> UploadCSVResponse:
    """Upload files metadata from CSV (Files API)."""
    return await _upload_csv_generic(
        input=input,
        data_key="Files",
        id_field="Id",
        entity_name="files",
    )


async def upload_folders_csv(input: UploadCSVInput) -> UploadCSVResponse:
    """Upload folder structure from CSV (Files API)."""
    return await _upload_csv_generic(
        input=input,
        data_key="Folders",
        id_field="Id",
        entity_name="folders",
    )


async def upload_associations_csv(input: UploadCSVInput) -> UploadCSVResponse:
    """Upload file-to-object associations from CSV (Files API)."""
    return await _upload_csv_generic(
        input=input,
        data_key="Associations",
        id_field="Id",
        entity_name="associations",
    )


async def _upload_csv_generic(
    input: UploadCSVInput,
    data_key: str,
    id_field: str,
    entity_name: str,
) -> UploadCSVResponse:
    """
    Generic CSV upload handler for list-based entities.

    """
    provider = get_provider()

    # Only works with offline provider
    if not isinstance(provider, OfflineProviderBase):
        raise ValueError("CSV upload is only supported in offline mode")

    # Cast to OfflineProviderBase for type checker
    cast(OfflineProviderBase, provider)

    try:
        # Parse CSV
        parsed_data = parse_csv_with_dot_notation(input.csv_content)

        if not parsed_data:
            return UploadCSVResponse(
                success=False,
                message="No data parsed from CSV",
                rows_added=0,
                rows_updated=0,
                total_rows=0,
            )

        # Import database models and session
        from sqlalchemy import delete, select

        from mcp_servers.xero.db.models import (
            Account,
            Asset,
            AssetType,
            Association,
            BankTransaction,
            BankTransfer,
            Budget,
            Contact,
            CreditNote,
            File,
            Folder,
            Invoice,
            Journal,
            Overpayment,
            Payment,
            Prepayment,
            Project,
            PurchaseOrder,
            Quote,
            TimeEntry,
        )
        from mcp_servers.xero.db.session import async_session

        # Map entity names to their database models
        model_map = {
            # Phase 1 models
            "accounts": Account,
            "contacts": Contact,
            "invoices": Invoice,
            "payments": Payment,
            "bank_transactions": BankTransaction,
            # Phase 2 models - Accounting Operations
            "journals": Journal,
            "purchase_orders": PurchaseOrder,
            "quotes": Quote,
            "credit_notes": CreditNote,
            "bank_transfers": BankTransfer,
            "overpayments": Overpayment,
            "prepayments": Prepayment,
            "budgets": Budget,
            # Phase 2 models - Assets API
            "assets": Asset,
            "asset_types": AssetType,
            # Phase 2 models - Projects API
            "projects": Project,
            "time_entries": TimeEntry,
            # Phase 2 models - Files API
            "files": File,
            "folders": Folder,
            "associations": Association,
        }

        # Map entity names to their ID field names (lowercase for from_dict)
        id_field_map = {
            # Phase 1 models
            "accounts": "account_id",
            "contacts": "contact_id",
            "invoices": "invoice_id",
            "payments": "payment_id",
            "bank_transactions": "bank_transaction_id",
            # Phase 2 models - Accounting Operations
            "journals": "journal_id",
            "purchase_orders": "purchase_order_id",
            "quotes": "quote_id",
            "credit_notes": "credit_note_id",
            "bank_transfers": "bank_transfer_id",
            "overpayments": "overpayment_id",
            "prepayments": "prepayment_id",
            "budgets": "budget_id",
            # Phase 2 models - Assets API (camelCase)
            "assets": "asset_id",
            "asset_types": "asset_type_id",
            # Phase 2 models - Projects API (camelCase)
            "projects": "project_id",
            "time_entries": "time_entry_id",
            # Phase 2 models - Files API
            "files": "file_id",
            "folders": "folder_id",
            "associations": "association_id",
        }

        model_class = model_map.get(entity_name)
        if not model_class:
            raise ValueError(f"Unknown entity type: {entity_name}")

        id_field_name = id_field_map.get(entity_name)
        if not id_field_name:
            raise ValueError(f"Unknown ID field for entity: {entity_name}")

        rows_added = 0
        rows_updated = 0
        rows_skipped = 0

        async with async_session() as session:
            async with session.begin():
                # For replace mode, delete all existing records first
                if input.merge_mode == "replace":
                    await session.execute(delete(model_class))
                    logger.info(f"Cleared all existing {entity_name} records (replace mode)")

                # Import FK validator
                from mcp_servers.xero.utils.fk_validator import (
                    FK_VALIDATORS,
                    FKValidationError,
                    FKValidator,
                )

                # Create FK validator instance
                fk_validator = FKValidator(session)

                for row_data in parsed_data:
                    # Get the ID value from the row
                    row_id = row_data.get(id_field) or row_data.get(id_field_name)

                    if not row_id:
                        # Case-insensitive fallback for header casing mismatches
                        id_field_lower = id_field.lower()
                        for key, val in row_data.items():
                            if key.lower() == id_field_lower and val:
                                row_id = val
                                logger.warning(
                                    f"ID field '{id_field}' matched via case-insensitive "
                                    f"fallback (found key '{key}')"
                                )
                                break

                    if not row_id:
                        logger.warning(f"Skipping row without {id_field}: {row_data}")
                        rows_skipped += 1
                        continue

                    # Validate foreign key references if applicable
                    if entity_name in FK_VALIDATORS:
                        try:
                            validator_method = getattr(fk_validator, FK_VALIDATORS[entity_name])
                            await validator_method(row_data)
                        except FKValidationError as e:
                            logger.error(f"FK validation failed for {entity_name} {row_id}: {e}")
                            rows_skipped += 1
                            continue  # Skip this row, continue with next

                    # Check if record exists (for update vs insert in append mode)
                    if input.merge_mode == "append":
                        # Check if exists
                        stmt = select(model_class).where(
                            getattr(model_class, id_field_name) == row_id
                        )
                        result = await session.execute(stmt)
                        existing = result.scalar_one_or_none()

                        if existing:
                            # Update existing record by creating a new instance from dict
                            # and copying its attributes (this ensures proper JSON serialization)
                            updated_record = model_class.from_dict(row_data)

                            # Copy all attributes from the updated record to existing
                            # Use sqlalchemy.inspect() to get proper Python attribute names
                            # (e.g., "class_" for Column("class", ...))
                            from sqlalchemy import inspect as sa_inspect

                            mapper = sa_inspect(model_class)
                            for attr in mapper.column_attrs:
                                setattr(existing, attr.key, getattr(updated_record, attr.key))

                            rows_updated += 1
                        else:
                            # Add new record
                            new_record = model_class.from_dict(row_data)
                            session.add(new_record)
                            rows_added += 1
                    else:
                        # Replace mode - just add (old records already deleted)
                        new_record = model_class.from_dict(row_data)
                        session.add(new_record)
                        rows_added += 1

        total_rows = rows_added + rows_updated

        # Check if all rows were skipped due to missing ID
        if total_rows == 0 and rows_skipped > 0:
            # Get raw CSV headers from the first line (not from parsed data which drops empty values)
            csv_reader = csv.reader(io.StringIO(input.csv_content))
            raw_headers = next(csv_reader, [])
            # Distinguish between missing column vs empty values
            id_column_in_header = id_field in raw_headers or id_field_name in raw_headers
            if id_column_in_header:
                error_detail = (
                    f"The '{id_field}' column exists but all values are empty. "
                    f"Please ensure each row has a non-empty ID value."
                )
            else:
                error_detail = (
                    f"The required '{id_field}' column is missing. "
                    f"Your CSV has columns: {', '.join(raw_headers)}. "
                    f"Please add an '{id_field}' column with unique IDs for each row."
                )
            logger.error(f"All {rows_skipped} rows skipped - {error_detail}")
            return UploadCSVResponse(
                success=False,
                message=f"Upload failed: All {rows_skipped} rows skipped. {error_detail}",
                rows_added=0,
                rows_updated=0,
                total_rows=0,
            )

        logger.info(
            f"Uploaded {entity_name} CSV: {rows_added} added, "
            f"{rows_updated} updated, {rows_skipped} skipped, {total_rows} total"
        )

        # Build message with skip info if applicable
        message = f"Successfully uploaded {entity_name}: {rows_added} added, {rows_updated} updated"
        if rows_skipped > 0:
            message += f" ({rows_skipped} rows skipped - missing {id_field})"

        return UploadCSVResponse(
            success=True,
            message=message,
            rows_added=rows_added,
            rows_updated=rows_updated,
            total_rows=total_rows,
        )

    except Exception as e:
        logger.error(f"Failed to upload {entity_name} CSV: {e}")
        return UploadCSVResponse(
            success=False,
            message=f"Failed to upload {entity_name}: {str(e)}",
            rows_added=0,
            rows_updated=0,
            total_rows=0,
        )


async def reset_state(input: ResetStateInput) -> dict[str, Any]:
    """Reset database state by clearing all records.

    Examples:
        # Reset all data in offline mode
        reset_state()

    Warning:
        This permanently deletes ALL records from the database.
        Only available in offline mode.

    Returns:
        dict with keys:
        - success: Boolean indicating if reset succeeded
        - message: Status message

    Raises:
        RuntimeError: If not in offline mode
    """
    _ensure_provider_initialized()

    # Only allow reset in offline mode
    if not isinstance(_provider, OfflineProviderBase):
        return {"success": False, "message": "reset_state is only available in offline mode"}

    try:
        from mcp_servers.xero.db.session import drop_db, init_db

        # Drop and recreate all tables
        await drop_db()
        await init_db()

        logger.info("Database state reset - all tables cleared")
        return {"success": True, "message": "Database reset successfully. All records cleared."}

    except Exception as e:
        logger.error(f"Failed to reset database: {e}")
        return {"success": False, "message": f"Failed to reset database: {str(e)}"}

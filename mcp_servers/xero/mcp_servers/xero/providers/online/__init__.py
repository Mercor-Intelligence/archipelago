"""Online provider implementation using Xero REST API."""

from ...auth.oauth_manager import OAuthManager
from ...config import Config
from ..base import BaseProvider
from . import (
    accounts,
    assets,
    associations,
    bank_transactions,
    bank_transfers,
    budgets,
    contacts,
    credit_notes,
    files,
    folders,
    invoices,
    journals,
    overpayments,
    payments,
    prepayments,
    project_time,
    projects,
    purchase_orders,
    quotes,
    reports,
)
from ._base import OnlineProviderBase


class OnlineProvider(OnlineProviderBase, BaseProvider):
    """
    Online provider using live Xero REST API.

    Implements OAuth token management and HTTP caching.
    Rate limiting is handled by middleware.

    This provider is composed from individual resource modules for better
    maintainability and separation of concerns.
    """

    def __init__(self, config: Config, oauth_manager: OAuthManager, **kwargs):
        """
        Initialize online provider.

        Args:
            config: Application configuration
            oauth_manager: OAuth manager for authentication
        """
        OnlineProviderBase.__init__(self, config, oauth_manager, **kwargs)

    # Accounts resource
    get_accounts = accounts.get_accounts

    # Contacts resource
    get_contacts = contacts.get_contacts

    # Invoices resource
    get_invoices = invoices.get_invoices

    # Bank transactions resource
    get_bank_transactions = bank_transactions.get_bank_transactions

    # Payments resource
    get_payments = payments.get_payments

    # Overpayments resource
    get_overpayments = overpayments.get_overpayments

    # Reports resources
    get_report_balance_sheet = reports.get_report_balance_sheet
    get_report_profit_and_loss = reports.get_report_profit_and_loss
    get_report_aged_receivables = reports.get_report_aged_receivables
    get_report_aged_payables = reports.get_report_aged_payables
    get_report_executive_summary = reports.get_report_executive_summary
    get_budget_summary = reports.get_budget_summary

    # Budgets resource
    get_budgets = budgets.get_budgets

    # =========================================================================
    # Phase 2 Methods - Stub Implementations
    # These will be implemented in subsequent tickets
    # =========================================================================

    # Phase 2 Operations
    get_journals = journals.get_journals
    get_bank_transfers = bank_transfers.get_bank_transfers
    get_quotes = quotes.get_quotes
    get_credit_notes = credit_notes.get_credit_notes
    get_purchase_orders = purchase_orders.get_purchase_orders

    # Prepayments resource
    get_prepayments = prepayments.get_prepayments

    # Phase 2 Assets API
    get_assets = assets.get_assets
    get_asset_types = assets.get_asset_types

    # Phase 2 Files API
    get_files = files.get_files

    get_folders = folders.get_folders

    # Associations resource
    get_associations = associations.get_associations

    # Phase 2 Projects API
    get_projects = projects.get_projects
    get_project_time = project_time.get_project_time

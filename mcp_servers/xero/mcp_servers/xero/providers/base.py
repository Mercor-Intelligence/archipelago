"""Base provider interface for Xero data."""

from abc import ABC, abstractmethod
from datetime import UTC, datetime
from typing import Any


class BaseProvider(ABC):
    """Base provider interface for Xero data access."""

    @abstractmethod
    async def get_accounts(
        self, where: str | None = None, order: str | None = None, page: int | None = None
    ) -> dict[str, Any]:
        """Get chart of accounts."""
        pass

    @abstractmethod
    async def get_contacts(
        self,
        ids: list[str] | None = None,
        where: str | None = None,
        include_archived: bool = False,
        page: int | None = None,
    ) -> dict[str, Any]:
        """Get contacts (customers/suppliers)."""
        pass

    @abstractmethod
    async def get_invoices(
        self,
        ids: list[str] | None = None,
        statuses: list[str] | None = None,
        where: str | None = None,
        page: int | None = None,
    ) -> dict[str, Any]:
        """Get invoices."""
        pass

    @abstractmethod
    async def get_bank_transactions(
        self, where: str | None = None, unitdp: int | None = None, page: int | None = None
    ) -> dict[str, Any]:
        """Get bank transactions."""
        pass

    @abstractmethod
    async def get_payments(
        self, where: str | None = None, page: int | None = None
    ) -> dict[str, Any]:
        """Get payments."""
        pass

    @abstractmethod
    async def get_report_balance_sheet(
        self,
        date: str,
        periods: int | None = None,
        timeframe: str | None = None,
        tracking_categories: list[str] | None = None,
    ) -> dict[str, Any]:
        """Get balance sheet report."""
        pass

    @abstractmethod
    async def get_report_profit_and_loss(
        self,
        from_date: str,
        to_date: str,
        periods: int | None = None,
        timeframe: str | None = None,
        tracking_categories: list[str] | None = None,
    ) -> dict[str, Any]:
        """Get profit and loss report."""
        pass

    # =========================================================================
    # Phase 2 Methods - Reports
    # =========================================================================

    @abstractmethod
    async def get_report_aged_receivables(
        self,
        contact_id: str,
        date: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get aged receivables report by contact."""
        pass

    @abstractmethod
    async def get_report_aged_payables(
        self,
        contact_id: str,
        date: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> dict[str, Any]:
        """Get aged payables report by contact."""
        pass

    @abstractmethod
    async def get_budget_summary(
        self,
        date: str | None = None,
        periods: int | None = None,
        timeframe: int | None = None,
    ) -> dict[str, Any]:
        """Get budget vs actual comparison report."""
        pass

    @abstractmethod
    async def get_budgets(self) -> dict[str, Any]:
        """Get budget entities with tracking categories."""
        pass

    @abstractmethod
    async def get_report_executive_summary(self, date: str) -> dict[str, Any]:
        """Get executive summary report with KPIs and trends."""
        pass

    # =========================================================================
    # Phase 2 Methods - Operations
    # =========================================================================

    @abstractmethod
    async def get_journals(
        self,
        offset: int | None = None,
        payments_only: bool | None = None,
    ) -> dict[str, Any]:
        """Get manual journal entries."""
        pass

    @abstractmethod
    async def get_bank_transfers(
        self,
        where: str | None = None,
    ) -> dict[str, Any]:
        """Get inter-account transfers."""
        pass

    @abstractmethod
    async def get_quotes(
        self,
        ids: list[str] | None = None,
        where: str | None = None,
        page: int | None = None,
        statuses: list[str] | None = None,
    ) -> dict[str, Any]:
        """Get sales quotes/estimates."""
        pass

    @abstractmethod
    async def get_purchase_orders(
        self,
        ids: list[str] | None = None,
        where: str | None = None,
        page: int | None = None,
        statuses: list[str] | None = None,
    ) -> dict[str, Any]:
        """Get purchase orders."""
        pass

    @abstractmethod
    async def get_credit_notes(
        self,
        ids: list[str] | None = None,
        where: str | None = None,
        page: int | None = None,
    ) -> dict[str, Any]:
        """Get credit notes."""
        pass

    @abstractmethod
    async def get_prepayments(
        self,
        where: str | None = None,
        page: int | None = None,
    ) -> dict[str, Any]:
        """Get prepayment records."""
        pass

    @abstractmethod
    async def get_overpayments(
        self,
        where: str | None = None,
        page: int | None = None,
    ) -> dict[str, Any]:
        """Get overpayment records."""
        pass

    # =========================================================================
    # Phase 2 Methods - Assets API
    # =========================================================================

    @abstractmethod
    async def get_assets(
        self,
        status: str | None = None,
        page: int | None = None,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        """Get fixed assets."""
        pass

    @abstractmethod
    async def get_asset_types(self) -> dict[str, Any]:
        """Get asset types with depreciation settings."""
        pass

    # =========================================================================
    # Phase 2 Methods - Files API
    # =========================================================================

    @abstractmethod
    async def get_files(
        self,
        page: int | None = None,
        page_size: int | None = None,
        sort: str | None = None,
    ) -> dict[str, Any]:
        """Get file metadata."""
        pass

    @abstractmethod
    async def get_folders(self) -> dict[str, Any]:
        """Get folder metadata."""
        pass

    @abstractmethod
    async def get_associations(self, file_id: str) -> dict[str, Any]:
        """Get file associations."""
        pass

    # =========================================================================
    # Phase 2 Methods - Projects API
    # =========================================================================

    @abstractmethod
    async def get_projects(
        self,
        page: int | None = None,
        page_size: int | None = None,
        contact_id: str | None = None,
        states: list[str] | None = None,
    ) -> dict[str, Any]:
        """Get projects."""
        pass

    @abstractmethod
    async def get_project_time(
        self,
        project_id: str,
        page: int | None = None,
        page_size: int | None = None,
    ) -> dict[str, Any]:
        """Get time entries for a project."""
        pass

    def _add_metadata(
        self,
        data: dict[str, Any],
        provider_name: str,
        mode: str,
        report_period: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """
        Add metadata to response.

        Args:
            data: Response data
            provider_name: Provider name
            mode: Mode (online/offline)

        Returns:
            Response with metadata
        """
        called_at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
        metadata: dict[str, Any] = {
            "mode": mode,
            "provider": provider_name,
            "calledAt": called_at,
        }

        if report_period:
            metadata["reportPeriod"] = report_period

        data["meta"] = metadata
        return data

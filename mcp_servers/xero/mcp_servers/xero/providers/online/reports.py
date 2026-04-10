"""Reports resource implementation for online provider."""

from typing import Any


async def get_report_balance_sheet(
    self,
    date: str,
    periods: int | None = None,
    timeframe: str | None = None,
    tracking_categories: list[str] | None = None,
) -> dict[str, Any]:
    """Get a Balance Sheet report from the live Xero API."""
    params: dict[str, Any] = {"date": date}

    if periods is not None:
        if periods < 1:
            raise ValueError("Periods must be >= 1")
        params["periods"] = str(periods)

    if timeframe:
        params["timeframe"] = timeframe

    if tracking_categories:
        # Use comma-separated IDs as supported by Xero reports API
        params["trackingCategoryIDs"] = ",".join(tracking_categories)

    # Make request to Xero API
    response = await self._make_request("/Reports/BalanceSheet", params=params)
    report_period = {"asOfDate": date}
    return self._add_metadata(response, "xero-api", "online", report_period=report_period)


async def get_report_profit_and_loss(
    self,
    from_date: str,
    to_date: str,
    periods: int | None = None,
    timeframe: str | None = None,
    tracking_categories: list[str] | None = None,
) -> dict[str, Any]:
    """
    Get profit and loss report from Xero API.

    Args:
        from_date: Start date in YYYY-MM-DD format
        to_date: End date in YYYY-MM-DD format
        periods: Number of comparison periods (1-12)
        timeframe: Period timeframe (MONTH, QUARTER, YEAR)
        tracking_categories: List of tracking category options

    Returns:
        Profit and loss report data with metadata

    Reference: https://developer.xero.com/documentation/api/accounting/reports#profit-and-loss
    """
    # Build query parameters
    params: dict[str, Any] = {
        "fromDate": from_date,
        "toDate": to_date,
    }

    if periods is not None:
        if periods < 1:
            raise ValueError("Periods must be >= 1")
        params["periods"] = str(periods)

    if timeframe:
        params["timeframe"] = timeframe

    if tracking_categories:
        # Use comma-separated IDs as supported by Xero reports API
        params["trackingCategoryIDs"] = ",".join(tracking_categories)

    # Make request to Xero API
    response = await self._make_request("/Reports/ProfitAndLoss", params=params)
    report_period = {"fromDate": from_date, "toDate": to_date}
    return self._add_metadata(response, "xero-api", "online", report_period=report_period)


async def get_report_aged_receivables(
    self,
    contact_id: str,
    date: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    """
    Get aged receivables report by contact from Xero API.

    Retrieves AR aging report showing customer balances by aging bucket
    (Current, 30, 60, 90+ days).

    Args:
        contact_id: Contact UUID (required) - only show invoices for this contact
        date: Report date (YYYY-MM-DD) - shows payments up to this date.
              Defaults to end of current month.
        from_date: Show invoices from this date (YYYY-MM-DD)
        to_date: Show invoices to this date (YYYY-MM-DD)

    Returns:
        Aged receivables report data with metadata

    Reference: https://developer.xero.com/documentation/api/accounting/reports#aged-receivables
    """
    from calendar import monthrange
    from datetime import datetime

    # Build query parameters - contactId is required
    params: dict[str, Any] = {
        "contactId": contact_id,
    }

    # Determine report date for metadata (defaults to end of current month)
    if date:
        params["date"] = date
        report_date_str = date
    else:
        # Default to end of current month
        today = datetime.now()
        last_day = monthrange(today.year, today.month)[1]
        report_date_str = f"{today.year}-{today.month:02d}-{last_day:02d}"

    if from_date:
        params["fromDate"] = from_date

    if to_date:
        params["toDate"] = to_date

    # Make request to Xero API
    response = await self._make_request("/Reports/AgedReceivablesByContact", params=params)

    # Build report period metadata
    report_period: dict[str, Any] = {"asOfDate": report_date_str}
    if from_date:
        report_period["fromDate"] = from_date
    if to_date:
        report_period["toDate"] = to_date

    return self._add_metadata(response, "xero-api", "online", report_period=report_period)


async def get_report_aged_payables(
    self,
    contact_id: str,
    date: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
) -> dict[str, Any]:
    """
    Get aged payables report by contact from Xero API.

    Retrieves AP aging report showing supplier balances by aging bucket
    (Current, 30, 60, 90+ days).

    Args:
        contact_id: Contact UUID (required) - only show bills for this contact
        date: Report date (YYYY-MM-DD) - shows payments up to this date.
              Defaults to end of current month.
        from_date: Show bills from this date (YYYY-MM-DD)
        to_date: Show bills to this date (YYYY-MM-DD)

    Returns:
        Aged payables report data with metadata

    Reference: https://developer.xero.com/documentation/api/accounting/reports#aged-payables
    """
    from calendar import monthrange
    from datetime import datetime

    # Build query parameters - contactId is required
    params: dict[str, Any] = {
        "contactId": contact_id,
    }

    # Determine report date for metadata (defaults to end of current month)
    if date:
        params["date"] = date
        report_date_str = date
    else:
        # Default to end of current month
        today = datetime.now()
        last_day = monthrange(today.year, today.month)[1]
        report_date_str = f"{today.year}-{today.month:02d}-{last_day:02d}"

    if from_date:
        params["fromDate"] = from_date

    if to_date:
        params["toDate"] = to_date

    # Make request to Xero API
    response = await self._make_request("/Reports/AgedPayablesByContact", params=params)

    # Build report period metadata
    report_period: dict[str, Any] = {"asOfDate": report_date_str}
    if from_date:
        report_period["fromDate"] = from_date
    if to_date:
        report_period["toDate"] = to_date

    return self._add_metadata(response, "xero-api", "online", report_period=report_period)


async def get_report_executive_summary(
    self,
    date: str,
) -> dict[str, Any]:
    """
    Get executive summary report from Xero API.

    Retrieves a high-level view of the organization's financial health
    including Cash, Receivables, and Payables sections with KPIs.

    Args:
        date: Report date (YYYY-MM-DD) - required parameter

    Returns:
        Executive summary report data with metadata

    Reference: https://developer.xero.com/documentation/api/accounting/reports#executive-summary
    """
    # Build query parameters - date is required
    params: dict[str, Any] = {
        "date": date,
    }

    # Make request to Xero API
    response = await self._make_request("/Reports/ExecutiveSummary", params=params)

    # Build report period metadata
    report_period: dict[str, Any] = {"asOfDate": date}

    return self._add_metadata(response, "xero-api", "online", report_period=report_period)


async def get_budget_summary(
    self,
    date: str | None = None,
    periods: int | None = None,
    timeframe: int | None = None,
) -> dict[str, Any]:
    """
    Get budget vs actual comparison report from Xero API.

    Retrieves budget summary showing budget vs actual amounts by account,
    with variance calculations.

    Args:
        date: Report date (YYYY-MM-DD). Defaults to end of current month.
        periods: Number of periods to compare (1-12). Default is 1.
        timeframe: Period size - 1=month, 3=quarter, 12=year. Default is 1.

    Returns:
        Budget summary report data with metadata

    Reference: https://developer.xero.com/documentation/api/accounting/reports#budget-summary
    """
    from calendar import monthrange
    from datetime import datetime

    # Build query parameters
    params: dict[str, Any] = {}

    # Determine report date for metadata (defaults to end of current month)
    if date:
        params["date"] = date
        report_date_str = date
    else:
        # Default to end of current month
        today = datetime.now()
        last_day = monthrange(today.year, today.month)[1]
        report_date_str = f"{today.year}-{today.month:02d}-{last_day:02d}"

    if periods is not None:
        params["periods"] = str(periods)

    if timeframe is not None:
        params["timeframe"] = str(timeframe)

    # Make request to Xero API
    response = await self._make_request("/Reports/BudgetSummary", params=params)

    # Build report period metadata
    report_period: dict[str, Any] = {"asOfDate": report_date_str}

    return self._add_metadata(response, "xero-api", "online", report_period=report_period)

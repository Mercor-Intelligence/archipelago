"""
Financial statement tools using provider pattern.

From FMP API:

income statement
balance sheet
cash flow
metrics
ratios
growth analysis

"""

from mcp_servers.fmp_server.models import (
    CompanySymbolRequest,
    EmployeeCountRequest,
    FinancialReportRequest,
    FinancialStatementRequest,
    LargePagedRequest,
)
from mcp_servers.fmp_server.providers import get_provider


def _add_empty_data_message(result: dict, data_type: str, symbol: str, period: str) -> dict:
    """Add an informative error message when a provider returns empty data without an error."""
    if result.get("error"):
        return result
    data = result.get("data", [])
    if not data or (isinstance(data, list) and len(data) == 0):
        if not result.get("error"):
            result["error"] = (
                f"No {data_type} data available for {symbol.upper()} "
                f"(period={period}). Verify the ticker is correct and has filed financials."
            )
    return result


def _check_income_statement_integrity(statement: dict) -> list[str]:
    """Validate: revenue - costOfRevenue = grossProfit.

    FMP standardized data uses camelCase keys. Returns a list of warning
    strings for any mismatches found.
    """
    warnings: list[str] = []

    revenue = None
    cost_of_revenue = None
    gross_profit = None
    for key, val in statement.items():
        k = key.lower()
        if k in ("revenue",):
            revenue = val
        elif k in ("costofrevenue", "cost_of_revenue", "costofgoodssold"):
            cost_of_revenue = val
        elif k in ("grossprofit", "gross_profit"):
            gross_profit = val

    if revenue is not None and cost_of_revenue is not None and gross_profit is not None:
        try:
            rev = float(revenue)
            computed_gp = rev - float(cost_of_revenue)
            reported_gp = float(gross_profit)
            if rev != 0 and abs(reported_gp - computed_gp) / abs(rev) > 0.01:
                period = statement.get("date") or statement.get("period", "unknown")
                warnings.append(
                    f"Period {period}: grossProfit ({reported_gp:,.0f}) does not equal "
                    f"revenue - costOfRevenue ({computed_gp:,.0f}). "
                    f"Difference: {abs(reported_gp - computed_gp):,.0f}. "
                    "This may indicate non-standard reporting or adjustments."
                )
        except (TypeError, ValueError):
            pass

    return warnings


def _check_cash_flow_integrity(statement: dict) -> list[str]:
    """Validate: operating + investing + financing ~= netChangeInCash.

    FMP standardized data uses camelCase keys. Returns a list of warning
    strings for any mismatches found.
    """
    warnings: list[str] = []

    operating = None
    investing = None
    financing = None
    net_change = None
    for key, val in statement.items():
        k = key.lower()
        if k in (
            "netcashprovidedbyoperatingactivities",
            "operatingcashflow",
            "net_cash_provided_by_operating_activities",
        ):
            operating = val
        elif k in (
            "netcashusedforinvestingactivites",
            "netcashusedforinvestingactivities",
            "net_cash_used_for_investing_activities",
        ):
            investing = val
        elif k in (
            "netcashusedprovidedbyfinancingactivities",
            "net_cash_used_provided_by_financing_activities",
        ):
            financing = val
        elif k in (
            "netchangeincash",
            "net_change_in_cash",
        ):
            net_change = val

    if (
        operating is not None
        and investing is not None
        and financing is not None
        and net_change is not None
    ):
        try:
            computed = float(operating) + float(investing) + float(financing)
            reported = float(net_change)
            denominator = abs(reported) if reported != 0 else abs(computed)
            if denominator != 0 and abs(reported - computed) / denominator > 0.05:
                period = statement.get("date") or statement.get("period", "unknown")
                warnings.append(
                    f"Period {period}: netChangeInCash ({reported:,.0f}) does not closely match "
                    f"operating + investing + financing cash flows ({computed:,.0f}). "
                    f"Difference: {abs(reported - computed):,.0f}. "
                    "This is common due to FX effects or other adjustments."
                )
        except (TypeError, ValueError):
            pass

    return warnings


async def get_income_statement(request: FinancialStatementRequest) -> dict:
    """Access real-time income statement data for public companies, private companies, and ETFs."""
    provider = get_provider()
    result = await provider.get_income_statement(request.symbol, request.period, request.limit)
    result = _add_empty_data_message(result, "income statement", request.symbol, request.period)

    data = result.get("data")
    if data and isinstance(data, list):
        integrity_warnings: list[str] = []
        for statement in data:
            if isinstance(statement, dict):
                integrity_warnings.extend(_check_income_statement_integrity(statement))
        if integrity_warnings:
            result["_integrity_warnings"] = integrity_warnings

    return result


async def get_balance_sheet(request: FinancialStatementRequest) -> dict:
    """Access detailed balance sheet statements for publicly traded companies."""
    provider = get_provider()
    result = await provider.get_balance_sheet(request.symbol, request.period, request.limit)
    return _add_empty_data_message(result, "balance sheet", request.symbol, request.period)


async def get_cash_flow_statement(request: FinancialStatementRequest) -> dict:
    """Gain insights into a company's cash flow activities."""
    provider = get_provider()
    result = await provider.get_cash_flow_statement(request.symbol, request.period, request.limit)
    result = _add_empty_data_message(result, "cash flow statement", request.symbol, request.period)

    data = result.get("data")
    if data and isinstance(data, list):
        integrity_warnings: list[str] = []
        for statement in data:
            if isinstance(statement, dict):
                integrity_warnings.extend(_check_cash_flow_integrity(statement))
        if integrity_warnings:
            result["_integrity_warnings"] = integrity_warnings

    return result


async def get_latest_financials(request: LargePagedRequest) -> dict:
    """Get latest financial statements across all companies."""
    provider = get_provider()
    return await provider.get_latest_financials(request.page, request.limit)


async def get_income_statement_ttm(request: EmployeeCountRequest) -> dict:
    """Get trailing twelve-month income statement data."""
    provider = get_provider()
    return await provider.get_income_statement_ttm(request.symbol)


async def get_balance_sheet_ttm(request: EmployeeCountRequest) -> dict:
    """Get TTM balance sheet data."""
    provider = get_provider()
    return await provider.get_balance_sheet_ttm(request.symbol)


async def get_cash_flow_ttm(request: EmployeeCountRequest) -> dict:
    """Get TTM cash flow statement."""
    provider = get_provider()
    return await provider.get_cash_flow_ttm(request.symbol)


async def get_key_metrics(request: FinancialStatementRequest) -> dict:
    """Access key financial metrics for comprehensive company analysis."""
    provider = get_provider()
    result = await provider.get_key_metrics(request.symbol, request.period, request.limit)
    return _add_empty_data_message(result, "key metrics", request.symbol, request.period)


async def get_financial_ratios(request: FinancialStatementRequest) -> dict:
    """Gain comprehensive view of company's financial performance.

    Returns detailed ratio analysis for the specified company.
    """
    provider = get_provider()
    return await provider.get_financial_ratios(request.symbol, request.period, request.limit)


async def get_key_metrics_ttm(request: CompanySymbolRequest) -> dict:
    """Get trailing twelve-month key metrics."""
    provider = get_provider()
    result = await provider.get_key_metrics_ttm(request.symbol)
    if not result.get("error"):
        data = result.get("data", [])
        if not data or (isinstance(data, list) and len(data) == 0):
            result["error"] = (
                f"No TTM key metrics data available for {request.symbol.upper()}. "
                "Verify the ticker is correct and has recent financial data."
            )
    return result


async def get_ratios_ttm(request: CompanySymbolRequest) -> dict:
    """Get TTM financial ratios."""
    provider = get_provider()
    return await provider.get_ratios_ttm(request.symbol)


async def get_financial_scores(request: CompanySymbolRequest) -> dict:
    """Assess overall financial health with Piotroski F-Score and Altman Z-Score."""
    provider = get_provider()
    return await provider.get_financial_scores(request.symbol)


async def get_owner_earnings(request: EmployeeCountRequest) -> dict:
    """Calculate true economic earnings available to owners."""
    provider = get_provider()
    return await provider.get_owner_earnings(request.symbol, request.limit)


async def get_enterprise_values(request: FinancialStatementRequest) -> dict:
    """Access comprehensive enterprise value calculations."""
    provider = get_provider()
    return await provider.get_enterprise_values(request.symbol, request.period, request.limit)


async def get_income_growth(request: FinancialStatementRequest) -> dict:
    """Track income statement metrics growth over time."""
    provider = get_provider()
    return await provider.get_income_growth(request.symbol, request.period, request.limit)


async def get_balance_sheet_growth(request: FinancialStatementRequest) -> dict:
    """Analyze balance sheet metrics growth trends."""
    provider = get_provider()
    return await provider.get_balance_sheet_growth(request.symbol, request.period, request.limit)


async def get_cash_flow_growth(request: FinancialStatementRequest) -> dict:
    """Monitor cash flow metrics growth patterns."""
    provider = get_provider()
    return await provider.get_cash_flow_growth(request.symbol, request.period, request.limit)


async def get_financial_growth(request: FinancialStatementRequest) -> dict:
    """Access comprehensive financial growth metrics across all statements."""
    provider = get_provider()
    return await provider.get_financial_growth(request.symbol, request.period, request.limit)


async def get_revenue_by_product(request: FinancialStatementRequest) -> dict:
    """Analyze revenue breakdown by product segments."""
    provider = get_provider()
    return await provider.get_revenue_by_product(request.symbol, request.period, request.limit)


async def get_revenue_by_geography(request: FinancialStatementRequest) -> dict:
    """Understand geographic revenue distribution."""
    provider = get_provider()
    return await provider.get_revenue_by_geography(request.symbol, request.period, request.limit)


async def get_income_as_reported(request: FinancialStatementRequest) -> dict:
    """Access income statements as filed with SEC without adjustments."""
    provider = get_provider()
    return await provider.get_income_as_reported(request.symbol, request.period, request.limit)


def _check_balance_sheet_integrity(statement: dict) -> list[str]:
    """Validate the accounting identity: totalAssets = totalLiabilities + totalEquity.

    FMP as-reported data uses lowercase keys. Returns a list of warning strings
    for any mismatches found.
    """
    warnings: list[str] = []

    total_assets = None
    total_liabilities = None
    total_equity = None
    total_liab_and_equity = None
    for key, val in statement.items():
        k = key.lower()
        if k in ("totalassets", "total_assets"):
            total_assets = val
        elif k in ("totalliabilities", "total_liabilities"):
            total_liabilities = val
        elif k in (
            "totalstockholdersequity",
            "total_stockholders_equity",
            "totalequity",
            "totalshareholdersequity",
        ):
            total_equity = val
        elif k in (
            "totalliabilitiesandstockholdersequity",
            "totalliabilitiesandequity",
        ):
            total_liab_and_equity = val

    if total_assets is None:
        return warnings

    try:
        assets = float(total_assets)
        if assets == 0:
            return warnings

        # Prefer individual components; fall back to the combined field
        if total_liabilities is not None and total_equity is not None:
            computed = float(total_liabilities) + float(total_equity)
        elif total_liab_and_equity is not None:
            computed = float(total_liab_and_equity)
        else:
            return warnings

        if abs(assets - computed) / abs(assets) > 0.01:
            period = statement.get("date") or statement.get("period", "unknown")
            warnings.append(
                f"Period {period}: totalAssets ({assets:,.0f}) does not equal "
                f"totalLiabilities + totalEquity ({computed:,.0f}). "
                f"Difference: {abs(assets - computed):,.0f}. "
                "This may indicate non-standard reporting categories."
            )
    except (TypeError, ValueError):
        pass

    return warnings


async def get_balance_sheet_as_reported(request: FinancialStatementRequest) -> dict:
    """Access balance sheets as filed with SEC without adjustments."""
    provider = get_provider()
    result = await provider.get_balance_sheet_as_reported(
        request.symbol, request.period, request.limit
    )

    data = result.get("data")
    if data and isinstance(data, list):
        integrity_warnings: list[str] = []
        for statement in data:
            if isinstance(statement, dict):
                integrity_warnings.extend(_check_balance_sheet_integrity(statement))
        if integrity_warnings:
            result["_integrity_warnings"] = integrity_warnings

    return result


async def get_cash_flow_as_reported(request: FinancialStatementRequest) -> dict:
    """Access cash flow statements as filed with SEC without adjustments."""
    provider = get_provider()
    return await provider.get_cash_flow_as_reported(request.symbol, request.period, request.limit)


async def get_full_financials_as_reported(request: FinancialStatementRequest) -> dict:
    """Get complete financial statements exactly as filed with SEC."""
    provider = get_provider()
    return await provider.get_full_financials_as_reported(
        request.symbol, request.period, request.limit
    )


async def get_financial_reports_dates(request: CompanySymbolRequest) -> dict:
    """Get available financial report dates for a company."""
    provider = get_provider()
    return await provider.get_financial_reports_dates(request.symbol)


async def get_financial_report_json(request: FinancialReportRequest) -> dict:
    """Get financial report in JSON format."""
    provider = get_provider()
    return await provider.get_financial_report_json(request.symbol, request.year, request.period)


async def get_financial_report_xlsx(request: FinancialReportRequest) -> dict:
    """Get financial report download link in XLSX format."""
    provider = get_provider()
    return await provider.get_financial_report_xlsx(request.symbol, request.year, request.period)

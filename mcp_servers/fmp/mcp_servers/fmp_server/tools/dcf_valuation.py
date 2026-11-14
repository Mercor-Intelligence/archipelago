"""
DCF valuation tools using provider pattern.

From FMP API:

dcf valuation
levered dcf valuation
custom dcf valuation
custom levered dcf valuation

"""

from mcp_servers.fmp_server.models import CompanySymbolRequest, CustomDcfRequest
from mcp_servers.fmp_server.providers import get_provider


async def get_dcf_valuation(request: CompanySymbolRequest) -> dict:
    """Estimate the intrinsic value of a company with DCF valuation."""
    provider = get_provider()
    return await provider.get_dcf_valuation(request.symbol)


async def get_levered_dcf_valuation(request: CompanySymbolRequest) -> dict:
    """Analyze company's value with Levered DCF incorporating impact of debt."""
    provider = get_provider()
    return await provider.get_levered_dcf_valuation(request.symbol)


async def get_custom_dcf_valuation(request: CustomDcfRequest) -> dict:
    """Run tailored DCF analysis using custom parameters."""
    provider = get_provider()
    return await provider.get_custom_dcf_valuation(
        request.symbol,
        revenue_growth=request.revenue_growth_pct,
        ebitda_margin=request.ebitda_pct,
        cost_of_equity=request.cost_of_equity,
        terminal_growth=request.long_term_growth_rate,
        tax_rate=request.tax_rate,
        cost_of_debt=request.cost_of_debt,
        beta=request.beta,
        risk_free_rate=request.risk_free_rate,
    )


async def get_custom_levered_dcf_valuation(request: CustomDcfRequest) -> dict:
    """Run tailored Levered DCF analysis with custom parameters."""
    provider = get_provider()
    return await provider.get_custom_levered_dcf_valuation(
        request.symbol,
        revenue_growth=request.revenue_growth_pct,
        ebitda_margin=request.ebitda_pct,
        cost_of_equity=request.cost_of_equity,
        terminal_growth=request.long_term_growth_rate,
        tax_rate=request.tax_rate,
        cost_of_debt=request.cost_of_debt,
        beta=request.beta,
        risk_free_rate=request.risk_free_rate,
    )

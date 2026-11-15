"""
Earnings transcript tools using provider pattern.

From FMP API:

earnings call transcripts
earnings call transcripts dates
available transcripts
companies with transcripts

"""

from mcp_servers.fmp_server.models import (
    CompanySymbolRequest,
    EarningTranscriptRequest,
    TranscriptListRequest,
)
from mcp_servers.fmp_server.providers import get_provider


async def get_latest_earning_transcripts(request: TranscriptListRequest) -> dict:
    """Access available earnings transcripts for companies."""
    provider = get_provider()
    return await provider.get_latest_earning_transcripts(request.page or 0, request.limit)


async def get_earning_call_transcript(request: EarningTranscriptRequest) -> dict:
    """Access the full transcript of a company's earnings call."""
    provider = get_provider()
    return await provider.get_earning_call_transcript(request.symbol, request.year, request.quarter)


async def get_transcript_dates_by_symbol(request: CompanySymbolRequest) -> dict:
    """Access earnings call transcript dates for specific companies."""
    provider = get_provider()
    return await provider.get_transcript_dates_by_symbol(request.symbol)

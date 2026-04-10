"""EDGAR SEC MCP Server - Main entry point.

Exposes tools in two modes based on GUI_ENABLED environment variable:

Meta-tools mode (default, GUI_ENABLED=false):
  Optimized for LLM context efficiency - 5 consolidated meta-tools:
  - edgar_filings: Company submissions, facts, concepts, and frames (4 actions)
  - edgar_analysis: Debt schedules, equity compensation, HTML tables (3 actions)
  - edgar_documents: Fetch actual document text from SEC filings (2 actions)
  - edgar_lookup: CIK lookup and health check (2 actions)
  - edgar_schema: Tool introspection

Individual tools mode (GUI_ENABLED=true):
  Optimized for UI - 11 discrete tools with clear input/output schemas:
  - get_company_submissions, get_company_facts, get_company_concept, get_frames
  - get_debt_schedule, get_equity_compensation, extract_html_table
  - list_filing_documents, get_filing_document
  - lookup_cik, health_check
"""

import os

from fastmcp import FastMCP
from loguru import logger
from mcp_middleware import ServerConfig, run_server

SERVER_CONFIG = ServerConfig(
    name="edgar_sec",
    version="0.1.0",
    description=(
        "Access SEC EDGAR (U.S. Securities and Exchange Commission Electronic Data system) "
        "for public company filings, XBRL financial data, and document text. "
        "Covers all U.S. public companies. Rate limited to 10 requests/second by SEC. "
        "Use edgar_lookup action='health' to check current mode (online/offline)."
    ),
    pagination_key="pagination",
    native_pagination_params={"page": "page", "limit": "limit"},
)

mcp = FastMCP(
    name=SERVER_CONFIG.name,
    instructions=SERVER_CONFIG.description,
    version=SERVER_CONFIG.version,
)


def _register_tools():
    """Register all tools with the MCP server."""
    gui_enabled = os.getenv("GUI_ENABLED", "false").lower()
    use_gui_tools = gui_enabled not in ("false", "0", "no")

    if use_gui_tools:
        _register_discrete_tools()
    else:
        _register_consolidated_tools()


def _register_discrete_tools():
    """Register individual tools for UI (GUI_ENABLED=true)."""
    from config import EDGAR_OFFLINE_MODE
    from tools.documents import get_filing_document, list_filing_documents
    from tools.filings import (
        get_company_concept,
        get_company_facts,
        get_company_submissions,
        get_frames,
    )
    from tools.health import health_check
    from tools.lookup import lookup_cik

    # Always available
    mcp.tool(get_company_submissions)
    mcp.tool(get_company_facts)
    mcp.tool(get_company_concept)
    mcp.tool(get_frames)
    mcp.tool(lookup_cik)
    mcp.tool(health_check)

    # Document tools work in both online and offline modes
    mcp.tool(list_filing_documents)
    mcp.tool(get_filing_document)

    if not EDGAR_OFFLINE_MODE:
        _register_online_analysis_tools()


def _register_online_analysis_tools():
    """Register analysis tools that require online mode."""
    from tools.debt import get_debt_schedule
    from tools.equity import get_equity_compensation
    from tools.html_tables import extract_html_table

    mcp.tool(get_debt_schedule)
    mcp.tool(get_equity_compensation)
    mcp.tool(extract_html_table)


def _register_consolidated_tools():
    """Register meta-tools for LLMs (default)."""
    from tools._meta_tools import (
        edgar_analysis,
        edgar_documents,
        edgar_filings,
        edgar_lookup,
        edgar_schema,
    )

    mcp.tool(edgar_filings)
    mcp.tool(edgar_analysis)
    mcp.tool(edgar_documents)
    mcp.tool(edgar_lookup)
    mcp.tool(edgar_schema)


async def _flatten_tool_schemas():
    """Flatten registered tool schemas for Gemini compatibility.

    FastMCP may generate wrapper schemas with $defs/$ref for function parameters.
    This post-processes all tool schemas to ensure Gemini compatibility.
    """
    from mcp_schema.gemini import flatten_schema

    tools = await mcp.get_tools()
    for tool in tools.values():
        schema = getattr(tool, "parameters", None)
        if schema:
            tool.parameters = flatten_schema(schema)


def _log_startup_banner():
    """Log startup banner indicating data source and mode."""
    from config import EDGAR_OFFLINE_DATA_DIR, EDGAR_OFFLINE_MODE

    if EDGAR_OFFLINE_MODE:
        logger.info("=" * 60)
        logger.info("EDGAR SEC MCP Server - OFFLINE MODE")
        logger.info(f"Data source: local files at {EDGAR_OFFLINE_DATA_DIR}")
        logger.info(
            "Document tools: list available (primary doc from submissions), "
            "get_text requires filings/ directory"
        )
        logger.info("Analysis tools (debt, equity, html_tables): disabled")
        logger.info("=" * 60)
    else:
        logger.info("=" * 60)
        logger.info("EDGAR SEC MCP Server - ONLINE MODE")
        logger.info("Data source: SEC EDGAR API (data.sec.gov)")
        logger.info("All tools available")
        logger.info("=" * 60)


def main():
    """Entry point for the MCP server."""
    _register_tools()
    _log_startup_banner()
    run_server(mcp, config=SERVER_CONFIG)


if __name__ == "__main__":
    main()

"""Xero MCP Server - Consolidated Meta Tools Pattern.

This server exposes 8 tools (7 meta tools + 1 schema tool) instead of 28 individual tools.
Each meta tool handles multiple actions via an `action` parameter.

| Meta Tool          | Actions                                                                          |
|-------------------|----------------------------------------------------------------------------------|
| xero_entities     | accounts, contacts                                                               |
| xero_transactions | invoices, payments, bank_transactions, journals, bank_transfers, credit_notes,   |
|                   | prepayments, overpayments, quotes, purchase_orders                               |
| xero_reports      | balance_sheet, profit_loss, aged_receivables, aged_payables, budget_summary,     |
|                   | budgets, executive_summary                                                       |
| xero_assets       | list, types                                                                      |
| xero_files        | list, folders, associations                                                      |
| xero_admin        | projects, project_time, reset_state, server_info                                 |
| xero_data         | upload_accounts, upload_contacts, upload_invoices, upload_payments,              |
|                   | upload_bank_transactions, upload_purchase_orders, upload_journals                |
| xero_schema       | Returns JSON schema for any tool's input/output                                  |

All meta tools support action="help" to discover available actions and their parameters.

Benefits:
- Reduced token usage in model context (8 vs 35+ tool descriptions)
- Built-in discovery via action="help" on each tool
- Schema introspection via xero_schema tool
- Clearer domain-based organization

Tool registration is controlled by the GUI_ENABLED environment variable:
- GUI_ENABLED=false (default): 8 meta-tools for LLM agents
- GUI_ENABLED=true: 29 individual tools for UI display
"""

import asyncio
import os
import sys
from pathlib import Path

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)
from loguru import logger
from mcp_middleware.logging import LoggingMiddleware
from mcp_middleware.ratelimit import RateLimitMiddleware
from mcp_schema.gemini import flatten_schema
from middleware.validation_error_sanitizer import ValidationErrorSanitizerMiddleware

# Support all execution methods:
# 1. python main.py (direct execution from server dir)
# 2. python -m mcp_servers.xero (module execution)
# Add project root to sys.path unconditionally for absolute imports in sub-modules
project_root = Path(__file__).resolve().parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from mcp_servers.xero.auth import OAuthManager, TokenStore  # noqa: E402
from mcp_servers.xero.config import Config, Mode  # noqa: E402
from mcp_servers.xero.providers import OfflineProvider, OnlineProvider  # noqa: E402
from mcp_servers.xero.tools import xero_tools  # noqa: E402
from mcp_servers.xero.tools._meta_tools import (  # noqa: E402
    xero_admin,
    xero_assets,
    xero_data,
    xero_entities,
    xero_files,
    xero_reports,
    xero_schema,
    xero_transactions,
)

# Initialize configuration
config = Config()

# Configure logging
logger.remove()
logger.add(
    sys.stderr,
    level=config.log_level,
    format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>",
)

if config.log_file:
    logger.add(config.log_file, rotation="10 MB", level=config.log_level)

# Initialize MCP server
mcp = FastMCP(
    "XeroMCP",
    instructions="""Xero accounting software integration for reading financial data.

Provides access to:
- Chart of accounts and contacts (customers/suppliers)
- Invoices (AR/AP), payments, and bank transactions
- Financial reports (Balance Sheet, P&L, Aged Receivables/Payables)
- Fixed assets, projects, and file management

All monetary values are in the organization's base currency unless CurrencyCode specifies otherwise.
All dates use YYYY-MM-DD format.
Pagination uses 1-indexed page numbers with ~100 items per page.

NOTE: This server is READ-ONLY in online mode. Write operations (CSV upload) only work in offline/sandbox mode.""",
)

# Set up error injection middleware for Dynamic Friction testing
try:
    from mcp_middleware.injected_errors import setup_error_injection

    setup_error_injection(mcp)
except Exception:
    pass

# Add middleware
mcp.add_middleware(RateLimitMiddleware(max_calls=60, period_sec=60))
mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True))
mcp.add_middleware(RetryMiddleware(max_retries=config.max_retries))
mcp.add_middleware(LoggingMiddleware())
mcp.add_middleware(ValidationErrorSanitizerMiddleware())

# Initialize provider based on mode
logger.info(f"Initializing Xero MCP server in {config.mode.value} mode")


def initialize_provider():
    """Initialize the appropriate provider based on configuration."""
    if config.mode == Mode.OFFLINE:
        logger.info("Using offline provider with synthetic data")
        provider = OfflineProvider()
    else:
        logger.info("Using online provider with Xero API")
        # Validate online configuration
        config.validate_online_config()

        # Initialize components for online mode
        token_store = TokenStore(config.token_storage_path)
        oauth_manager = OAuthManager(config, token_store)
        provider = OnlineProvider(config, oauth_manager)

    # Set provider for tools
    xero_tools.set_provider(provider)
    logger.info("Provider initialized successfully")
    return provider


# Initialize provider before registering tools
# This runs on module import AND can be called by bridge
try:
    provider = initialize_provider()
except Exception as e:
    logger.warning(f"Provider initialization on import failed: {e}")
    provider = None

# Mutually exclusive: GUI_ENABLED gets individual tools, otherwise meta-tools
if os.getenv("GUI_ENABLED", "").lower() in ("true", "1", "yes"):
    logger.info("GUI_ENABLED=true: Registering individual tools for UI...")

    # Register all upload tools for UI
    upload_tools = [
        # Phase 1 upload tools
        xero_tools.upload_accounts_csv,
        xero_tools.upload_contacts_csv,
        xero_tools.upload_invoices_csv,
        xero_tools.upload_payments_csv,
        xero_tools.upload_bank_transactions_csv,
        xero_tools.upload_reports_csv,
        # Phase 2 upload tools - Accounting Operations
        xero_tools.upload_journals_csv,
        xero_tools.upload_purchase_orders_csv,
        xero_tools.upload_quotes_csv,
        xero_tools.upload_credit_notes_csv,
        xero_tools.upload_bank_transfers_csv,
        xero_tools.upload_overpayments_csv,
        xero_tools.upload_prepayments_csv,
        xero_tools.upload_budgets_csv,
        # Phase 2 upload tools - Assets API
        xero_tools.upload_assets_csv,
        xero_tools.upload_asset_types_csv,
        # Phase 2 upload tools - Projects API
        xero_tools.upload_projects_csv,
        xero_tools.upload_time_entries_csv,
        # Phase 2 upload tools - Files API
        xero_tools.upload_files_csv,
        xero_tools.upload_folders_csv,
        xero_tools.upload_associations_csv,
    ]

    # Register getter tools for UI
    getter_tools = [
        # Phase 1 - Core entities
        xero_tools.get_accounts,
        xero_tools.get_contacts,
        xero_tools.get_invoices,
        xero_tools.get_bank_transactions,
        xero_tools.get_payments,
        # Phase 1 - Reports
        xero_tools.get_report_balance_sheet,
        xero_tools.get_report_profit_and_loss,
        # Phase 2 - Additional reports
        xero_tools.get_report_aged_receivables,
        xero_tools.get_report_aged_payables,
        xero_tools.get_budget_summary,
        xero_tools.get_budgets,
        xero_tools.get_report_executive_summary,
        # Phase 2 - Transactions
        xero_tools.get_journals,
        xero_tools.get_bank_transfers,
        xero_tools.get_quotes,
        xero_tools.get_purchase_orders,
        xero_tools.get_credit_notes,
        xero_tools.get_prepayments,
        xero_tools.get_overpayments,
        # Phase 2 - Assets API
        xero_tools.get_assets,
        xero_tools.get_asset_types,
        # Phase 2 - Files API
        xero_tools.get_files,
        xero_tools.get_folders,
        xero_tools.get_associations,
        # Phase 2 - Projects API
        xero_tools.get_projects,
        xero_tools.get_project_time,
        # Admin
        xero_tools.reset_state,
    ]

    all_individual_tools = upload_tools + getter_tools
    for tool in all_individual_tools:
        mcp.tool(tool)

    logger.info(f"Registered {len(all_individual_tools)} individual tools for UI")
else:
    # Register consolidated meta tools (7 domain tools + 1 schema tool = 8 total)
    logger.info("Registering MCP meta tools...")

    mcp.tool(xero_entities)
    mcp.tool(xero_transactions)
    mcp.tool(xero_reports)
    mcp.tool(xero_assets)
    mcp.tool(xero_files)
    mcp.tool(xero_admin)
    mcp.tool(xero_data)
    mcp.tool(xero_schema)

    logger.info("All tools registered successfully (8 meta tools)")


async def _flatten_tool_schemas():
    """Flatten all registered tool parameter schemas for runtime compatibility."""
    for tool in (await mcp.get_tools()).values():
        params = getattr(tool, "parameters", None)
        if isinstance(params, dict):
            tool.parameters = flatten_schema(params)


_flatten_tool_schemas_task: asyncio.Task[None] | None = None


def _log_flatten_task_error(task: asyncio.Task[None]) -> None:
    """Log background flatten errors without interrupting startup."""
    if task.cancelled():
        return
    try:
        task.result()
    except Exception as exc:
        import logging

        logging.getLogger(__name__).error(
            "Background schema flattening failed: %s", exc, exc_info=True
        )


try:
    loop = asyncio.get_running_loop()
except RuntimeError:
    asyncio.run(_flatten_tool_schemas())
else:
    _flatten_tool_schemas_task = loop.create_task(_flatten_tool_schemas())
    _flatten_tool_schemas_task.add_done_callback(_log_flatten_task_error)


# Add resources
@mcp.resource("resource://xero/config")
def get_config_info() -> str:
    """Get current server configuration information."""
    return f"""Xero MCP Server Configuration:
- Mode: {config.mode.value}
- Rate Limits: {config.rate_limit_per_minute}/min, {config.rate_limit_per_day}/day
- Tenant ID: {config.xero_tenant_id or "Not configured"}
- Pattern: Meta-tools with action parameter
- Tools: 7 (6 domain + 1 schema)
"""


if __name__ == "__main__":
    logger.info("=" * 60)
    logger.info("Xero MCP Server - Meta Tools Pattern")
    logger.info("=" * 60)
    logger.info(f"Mode: {config.mode.value}")
    logger.info(f"Provider: {provider.__class__.__name__ if provider else 'Not initialized'}")
    logger.info("Tools: 7 (6 domain meta tools + 1 schema tool)")
    logger.info("=" * 60)

    # Run the server with configurable transport
    transport = os.environ.get("MCP_TRANSPORT", "http").lower()
    if transport == "http":
        port = int(os.environ.get("MCP_PORT", "5000"))
        logger.info(f"Server: HTTP transport on 0.0.0.0:{port}")
        mcp.run(transport="http", host="0.0.0.0", port=port)
    else:
        logger.info("Server: stdio transport")
        mcp.run(transport="stdio")

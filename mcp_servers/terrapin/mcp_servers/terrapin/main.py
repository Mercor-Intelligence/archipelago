"""Terrapin MCP Server with offline mode support.

Exposes tools in two modes based on GUI_ENABLED environment variable:

Meta-tools mode (default, GUI_ENABLED=false):
  Optimized for LLM context efficiency - 2 consolidated meta-tools:
  - terrapin_bonds: Government and corporate bond operations (6 actions)
  - terrapin_schema: Tool introspection

Individual tools mode (GUI_ENABLED=true):
  Optimized for UI - 6 discrete tools with clear input/output schemas:
  - search_bonds, get_bond_reference_data, get_bond_pricing_latest,
    get_bond_pricing_history, get_bond_cashflows, get_inflation_factors

Note: Municipal bond tools are NOT currently implemented. The models for muni bonds
exist in models.py but the tools are not registered or implemented.

Offline mode is activated when:
- --offline true flag is passed

When API_KEY is present but invalid, an error is thrown (not offline mode).

Usage:
    TERRAPIN_API_KEY=your_key python main.py  # online mode (requires valid API key)
    python main.py --offline true  # offline mode
"""

import os
import sys
from pathlib import Path

# Add packages directory to path for mcp_middleware
sys.path.insert(0, str(Path(__file__).parent.parent.parent / "packages" / "mcp_middleware"))


from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)
from loguru import logger
from mcp_middleware.injected_errors import setup_error_injection
from mcp_schema import flatten_schema

# Support all execution methods:
# 1. python main.py (direct execution)
# 2. python -m mcp_servers.terrapin (module execution)
# 3. import main (RLS wrapper from same directory)
try:
    from .middleware.logging import LoggingMiddleware
    from .middleware.validation_error_sanitizer import ValidationErrorSanitizerMiddleware
    from .utils.api_client import InvalidAPIKeyError, validate_api_key
    from .utils.config import OFFLINE_MODE, TERRAPIN_API_KEY
    from .utils.db import DB_PATH, get_connection
except ImportError:
    from middleware.logging import LoggingMiddleware
    from middleware.validation_error_sanitizer import ValidationErrorSanitizerMiddleware
    from utils.api_client import InvalidAPIKeyError, validate_api_key
    from utils.config import OFFLINE_MODE, TERRAPIN_API_KEY
    from utils.db import DB_PATH, get_connection


def validate_startup():
    """Validate startup conditions.

    - In online mode with API key: validate the key
    - In offline mode: check fixtures database exists and has data
    """
    if not OFFLINE_MODE:
        if not TERRAPIN_API_KEY:
            logger.error("TERRAPIN_API_KEY is unset")
            sys.exit(1)
        logger.info("Validating API key...")
        try:
            if validate_api_key():
                logger.info("API key validated successfully")
            else:
                logger.warning("Could not validate API key (network issue or server unavailable)")
        except InvalidAPIKeyError as e:
            logger.error(f"Invalid API key: {e}")
            sys.exit(1)
    elif OFFLINE_MODE:
        if not DB_PATH.exists():
            logger.warning("Fixtures database not found")
            logger.warning("Queries will return empty results until fixtures are populated")
            logger.warning("Run download_fixtures to populate the database")
        else:
            conn = get_connection()
            bond_count = conn.execute("SELECT COUNT(*) FROM bonds").fetchone()[0]
            muni_count = conn.execute("SELECT COUNT(*) FROM muni_bonds").fetchone()[0]
            if bond_count == 0 and muni_count == 0:
                logger.warning("Fixtures database is empty")
                logger.warning("Run download_fixtures to populate the database")
            else:
                logger.info(f"Fixtures: {bond_count} bonds, {muni_count} municipal bonds")


validate_startup()

mcp = FastMCP(
    "terrapin-server",
    instructions=(
        "Access to Terrapin Finance bond data: government and corporate bonds "
        "(search, reference, pricing, cash flows, inflation factors) and US municipal bonds "
        "(search, reference, pricing, yield-from-price, cash flows). Use for bond research, "
        "portfolio construction, and yield/price analysis without a live Terrapin subscription."
    ),
)
# Set up error injection middleware for Dynamic Friction testing
setup_error_injection(mcp)

mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True))
mcp.add_middleware(RetryMiddleware())
mcp.add_middleware(LoggingMiddleware())
mcp.add_middleware(ValidationErrorSanitizerMiddleware())


def register_discrete_tools():
    """Register individual tools for UI (GUI_ENABLED=true)."""
    try:
        from .tools._bonds import (
            get_bond_cashflows,
            get_bond_pricing_history,
            get_bond_pricing_latest,
            get_bond_reference_data,
            get_inflation_factors,
            search_bonds,
        )
    except ImportError:
        from tools._bonds import (
            get_bond_cashflows,
            get_bond_pricing_history,
            get_bond_pricing_latest,
            get_bond_reference_data,
            get_inflation_factors,
            search_bonds,
        )

    # Government/Corporate bonds
    mcp.tool(search_bonds)
    mcp.tool(get_bond_reference_data)
    mcp.tool(get_bond_pricing_latest)
    mcp.tool(get_bond_pricing_history)
    mcp.tool(get_bond_cashflows)
    mcp.tool(get_inflation_factors)


def register_consolidated_tools():
    """Register meta-tools for LLMs (default)."""
    try:
        from .tools._meta_tools import terrapin_bonds, terrapin_schema
    except ImportError:
        from tools._meta_tools import terrapin_bonds, terrapin_schema

    mcp.tool(terrapin_bonds)
    mcp.tool(terrapin_schema)


if os.getenv("GUI_ENABLED", "false").lower() == "true":
    register_discrete_tools()
else:
    register_consolidated_tools()


def _apply_gemini_compatible_input_schemas() -> None:
    """Rewrite FastMCP tool input schemas to Gemini-compatible JSON Schema.

    FastMCP builds input schemas via Pydantic's TypeAdapter, which can emit
    ``$defs`` / ``$ref`` (and related constructs) even when argument models
    subclass ``GeminiBaseModel``. The ``mcp_schema`` package documents that
    Gemini Function Calling requires flattened schemas; we apply ``flatten_schema``
    to each registered tool's parameters so ``list_tools`` / introspection match
    that contract.
    """
    tm = mcp._tool_manager
    for key in list(tm._tools.keys()):
        tool = tm._tools[key]
        params = tool.parameters
        if not isinstance(params, dict):
            continue
        flat = flatten_schema(dict(params))
        tm._tools[key] = tool.model_copy(update={"parameters": flat})


async def _flatten_tool_schemas() -> None:
    """Optional hook for tests; also safe to call multiple times."""
    _apply_gemini_compatible_input_schemas()


_apply_gemini_compatible_input_schemas()

if __name__ == "__main__":
    transport = os.getenv("MCP_TRANSPORT", "http").lower()
    if transport == "http":
        port = int(os.getenv("MCP_PORT", "5000"))
        mcp.run(transport="http", host="0.0.0.0", port=port)
    else:
        mcp.run(transport="stdio")

"""Legacy config module - imports from the new config module for backward compatibility.

This module is kept for backward compatibility with existing tool imports.
New code should import from `mcp_servers.fmp_server.config` directly.
"""

# Import from the new config module (relative import from parent package)
from mcp_servers.fmp_server.config import Config, Mode, config, get_config

# Backward compatibility exports
# These are used by existing tools and api_client.py
FMP_API_KEY = config.fmp_api_key
FMP_API_BASE_URL = config.fmp_api_base_url
DEFAULT_LIST_LIMIT = config.default_list_limit
MAX_LIST_LIMIT = config.max_list_limit


def apply_limit(limit: int | None) -> int:
    """Apply default limit if None, cap at MAX_LIST_LIMIT.

    This is a wrapper around config.apply_limit for backward compatibility.
    """
    return config.apply_limit(limit)


# Re-export new config components for convenience
__all__ = [
    # New config components
    "Config",
    "Mode",
    "config",
    "get_config",
    # Legacy exports
    "FMP_API_KEY",
    "FMP_API_BASE_URL",
    "DEFAULT_LIST_LIMIT",
    "MAX_LIST_LIMIT",
    "apply_limit",
]

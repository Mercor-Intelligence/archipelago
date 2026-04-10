"""Configuration module for Terrapin server with offline mode support.

Offline mode is activated when:
- TERRAPIN_OFFLINE=1 environment variable is set
- --offline true flag is passed

TERRAPIN_OFFLINE=0 explicitly disables offline mode.
Any other value for TERRAPIN_OFFLINE raises an error.
"""

import os
import sys
from pathlib import Path

from loguru import logger

TERRAPIN_API_KEY = os.getenv("TERRAPIN_API_KEY")
TERRAPIN_API_BASE_URL = os.getenv("TERRAPIN_API_BASE_URL") or "https://terrapinfinance.com/api/v1"

FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"


def _parse_cli_flags() -> dict:
    """Parse CLI flags for offline mode detection."""
    flags = {
        "offline": False,
    }

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--offline":
            if i + 1 < len(args) and args[i + 1].lower() == "true":
                flags["offline"] = True
                i += 1
        i += 1

    return flags


def _determine_offline_mode() -> bool:
    """Determine if offline mode should be active.

    Returns True if:
    - TERRAPIN_OFFLINE=1 environment variable is set
    - --offline true flag is passed

    Raises ValueError if TERRAPIN_OFFLINE is set to anything other than 0 or 1.
    """
    # Check environment variable first
    env_value = os.getenv("TERRAPIN_OFFLINE")
    match env_value:
        case "1":
            return True
        case "0":
            return False
        case None:
            pass  # Fall through to CLI flag check
        case _:
            raise ValueError(f"Invalid TERRAPIN_OFFLINE value: '{env_value}'. Must be '0' or '1'.")

    # Then check CLI flag
    cli_flags = _parse_cli_flags()
    return cli_flags["offline"]


# Determine offline mode at module load time
if OFFLINE_MODE := _determine_offline_mode():
    logger.warning("TERRAPIN SERVER RUNNING IN OFFLINE MODE")
    logger.warning("All queries will use local fixture data")
    logger.warning(f"Fixtures directory: {FIXTURES_DIR}")
else:
    logger.info("Terrapin server running in online mode (API access enabled)")

"""Configuration for EDGAR SEC server."""

import os
import subprocess
from pathlib import Path

# EDGAR API configuration
EDGAR_API_BASE_URL = os.getenv("EDGAR_API_BASE_URL", "https://data.sec.gov")
EDGAR_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
EDGAR_ARCHIVES_BASE_URL = "https://www.sec.gov/Archives/edgar/data"

# API path templates (relative to EDGAR_API_BASE_URL)
EDGAR_SUBMISSIONS_PATH = "/submissions/CIK{cik}.json"
EDGAR_COMPANY_FACTS_PATH = "/api/xbrl/companyfacts/CIK{cik}.json"
EDGAR_COMPANY_CONCEPT_PATH = "/api/xbrl/companyconcept/CIK{cik}/{taxonomy}/{tag}.json"
EDGAR_FRAMES_PATH = "/api/xbrl/frames/{taxonomy}/{tag}/{unit}/{period}.json"
EDGAR_USER_AGENT = os.getenv(
    "EDGAR_USER_AGENT",
    "DefaultUser default@email.com",  # Must be overridden
)

# Offline mode configuration
EDGAR_OFFLINE_MODE = os.getenv("EDGAR_OFFLINE_MODE", "false").lower() == "true"

# Response size limits (to prevent context window overflow)
# Default: 32KB max response size (safe for most LLM context windows)
MAX_RESPONSE_SIZE_BYTES = int(os.getenv("EDGAR_MAX_RESPONSE_SIZE", "32768"))


def _get_repo_root() -> Path:
    """Get repository root using git, with fallback to path traversal."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=True,
            cwd=Path(__file__).parent,
        )
        return Path(result.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Fallback if git isn't available or we're not in a repo
        return Path(__file__).parent.parent.parent


REPO_ROOT = _get_repo_root()

# Offline data directory - extracted via: python -m zipfile -e data/edgar_offline.zip ./offline_data
# The zip contains edgar_offline/ as root, so data ends up at ./offline_data/edgar_offline/
EDGAR_OFFLINE_DATA_DIR = REPO_ROOT / "offline_data" / "edgar_offline"

# Validate user agent only if not in offline mode and not during UI generation
if not EDGAR_OFFLINE_MODE and not os.getenv("MCP_UI_GEN") and "DefaultUser" in EDGAR_USER_AGENT:
    raise ValueError(
        "EDGAR_USER_AGENT environment variable must be set with valid "
        "contact info per SEC requirements. "
        "Format: 'CompanyName contact@email.com'. "
        "See: https://www.sec.gov/os/webmaster-faq#code-support"
    )

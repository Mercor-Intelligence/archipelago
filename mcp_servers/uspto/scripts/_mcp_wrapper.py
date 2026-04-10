"""
MCP Wrapper Script

This wrapper redirects stdout to stderr during imports to prevent logging
from corrupting the MCP protocol communication (which uses stdout for JSON-RPC).
"""

import os
import runpy
import sys

# Add current working directory to sys.path so relative imports work
# (e.g., 'from middleware.auth import setup_auth')
cwd = os.getcwd()
if cwd not in sys.path:
    sys.path.insert(0, cwd)

_real_stdout = sys.stdout
sys.stdout = sys.stderr

from loguru import logger  # noqa: E402

logger.remove()
logger.add(sys.stderr, format="{time} | {level} | {message}", level="DEBUG")

sys.stdout = _real_stdout
runpy.run_path("main.py", run_name="__main__")

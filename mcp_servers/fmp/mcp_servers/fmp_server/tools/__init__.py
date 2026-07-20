"""FMP Server Tools Package."""

import sys
from pathlib import Path

# Add parent directory (fmp_server) to sys.path so imports like
# "from utils..." and "from models..." work when this package is imported
_current_dir = Path(__file__).parent
_server_dir = _current_dir.parent
if str(_server_dir) not in sys.path:
    sys.path.insert(0, str(_server_dir))

# This file makes the tools directory importable as a Python package
# The UI generator needs to import this module to discover tools

#!/usr/bin/env python3
"""Detect UI drift between generated and current UI files.

Implementation: mercor-mcp-shared/mcp_scripts/detect_ui_drift.py
"""

import sys

from mcp_scripts import detect_ui_drift

sys.modules[__name__] = detect_ui_drift

if __name__ == "__main__":
    sys.exit(detect_ui_drift.main())

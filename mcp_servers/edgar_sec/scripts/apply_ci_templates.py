#!/usr/bin/env python
"""Apply standard CI templates to an existing MCP server project.

Implementation: mercor-mcp-shared/mcp_scripts/apply_ci_templates.py
"""

import sys

from mcp_scripts import apply_ci_templates

sys.modules[__name__] = apply_ci_templates

if __name__ == "__main__":
    sys.exit(apply_ci_templates.main())

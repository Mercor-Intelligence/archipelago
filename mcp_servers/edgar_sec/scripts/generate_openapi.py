#!/usr/bin/env python
"""Generate OpenAPI documentation from Pydantic models.

Implementation: mercor-mcp-shared/mcp_scripts/generate_openapi.py
"""

import sys

from mcp_scripts import generate_openapi

sys.modules[__name__] = generate_openapi

if __name__ == "__main__":
    sys.exit(generate_openapi.main())

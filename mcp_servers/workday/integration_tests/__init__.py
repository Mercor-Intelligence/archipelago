"""Integration tests for Workday MCP server.

These tests run against the REST bridge and verify all tools work correctly.
NOT meant to run as part of CI/CD - run manually with:

    uv run pytest integration_tests/ -v

Prerequisites:
    1. Start REST bridge in another terminal: uv run mcp-ui -s workday --no-open
    2. Wait for migrations to complete
    3. Run tests
"""

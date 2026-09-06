# mercor-mcp-shared

Shared framework for building MCP (Model Context Protocol) servers.

## Overview

This repository contains shared code used across multiple MCP server applications:

- **packages/** - Reusable Python packages (mcp_auth, mcp_cache, mcp_middleware, mcp_testing)
- **ui_generator/** - Auto-generate web UIs from MCP server definitions
- **mcp_scripts/** - Build tools, validation scripts, database utilities
- **templates/** - Server scaffolding templates

## Installation

### As a dependency in another project

```toml
# In pyproject.toml
[project]
dependencies = [
    "mercor-mcp-shared[database,rest]",
]

[tool.uv.sources]
# Use local path (subtree or sibling directory)
mercor-mcp-shared = { path = "packages/mercor-mcp-shared" }
```

### For development on this repo

```bash
uv sync
```

## Packages

| Package | Description |
|---------|-------------|
| `mcp-auth` | Authentication middleware, JWT tokens, OAuth PKCE. Auth controlled via `ENABLE_AUTH`/`DISABLE_AUTH` env vars (disabled by default). |
| `mcp-cache` | HTTP caching middleware with ETag/Last-Modified |
| `mcp-middleware` | Rate limiting, latency tracking, logging, `run_server()` for transport configuration |
| `mcp-testing` | Testing framework for MCP servers |

## CLI Tools

After installation, these CLI commands are available:

```bash
mcp-ui-gen --help    # Generate UI from MCP server
mcp-ui --help        # Run local UI development server
```

## Scripts

Scripts are available as a package (`mcp_scripts`) for use in wrapper files:

```python
# In your app's scripts/create_mcp_server.py
import sys
from mcp_scripts import create_mcp_server

if __name__ == "__main__":
    sys.exit(create_mcp_server.main())
```

## Development

```bash
# Run linting
uv run ruff check .

# Run tests
uv run pytest

# Format code
uv run ruff format .
```

## Migrating Existing Repositories

If you have an existing repository with copied shared code (the "classic" structure), you can migrate to use this shared dependency as a git subtree instead.

### Claude-Assisted Migration

Tell Claude Code:

```
I want to migrate this repository to use mercor-mcp-shared as a local path dependency via git subtree.

First, add mercor-mcp-shared as a git subtree:
git subtree add --prefix=packages/mercor-mcp-shared https://github.com/Mercor-Intelligence/mercor-mcp-shared.git main --squash

Then follow the migration instructions in:
packages/mercor-mcp-shared/CLAUDE.md
```

Claude will:
1. Analyze differences between local and shared code
2. Generate wrapper scripts for shared modules
3. Update CI workflows
4. Update pyproject.toml to use path dependency

### Manual Migration

See [CLAUDE.md](CLAUDE.md) for detailed step-by-step instructions.

## Managing the Subtree

When you make changes to this shared repository, consumer repos can manage updates using the built-in subtree commands:

```bash
# Pull updates from mercor-mcp-shared (squashed into single commit)
uv run shared-pull [branch]  # defaults to main

# Push local changes back to mercor-mcp-shared (preserves full commit history)
uv run shared-push <branch>

# Switch to a different branch (clean replacement, not a merge)
uv run shared-switch <branch>

# Manually refresh package after changes (auto-runs after pull)
uv run shared-refresh
```

See [CLAUDE.md](CLAUDE.md) for detailed subtree management instructions.

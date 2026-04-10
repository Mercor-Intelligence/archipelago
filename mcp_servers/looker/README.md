# Looker MCP Server

A Python-based framework for rapidly developing Model Context Protocol (MCP) servers


## ArCo — Configuring Your App for Archipelago and RL Studio

### What is Archipelago?

RL Studio uses **[Archipelago](https://github.com/Mercor-Intelligence/archipelago)**, Mercor's open-source harness for running and evaluating AI agents against RL environments

Your MCP server runs inside an Archipelago environment, where AI agents connect to it via the MCP protocol to complete tasks.

### What is ArCo?

**ArCo** (short for **Archipelago Config**) is the configuration system for deploying your MCP server to Archipelago. It consists of two files that tell Archipelago how to build and run your application.

### Configuration Files

| File | Purpose |
|------|---------|
| `mise.toml` | **How to build and run your app** — lifecycle tasks (install, build, start, test) |
| `arco.toml` | **What infrastructure your app needs** — environment variables, secrets, runtime settings |

### Why ArCo?

Archipelago is deployed to multiple environments with different infrastructure requirements (Docker, Kubernetes, custom orchestrators). Rather than writing Dockerfiles or K8s manifests directly, you declare *what your app needs* in these config files, and RL Studio generates the appropriate deployment artifacts for each proprietary customer "target consumer".

You as a Mercor expert only need to write `mise.toml` and `arco.toml`, we write Dockerfiles, K8s manifests, etc. for you. 

### Mise: The Task Runner

**[Mise](https://mise.jdx.dev/)** is required for development. Install it first:

```bash
curl https://mise.run | sh
```

Mise is a polyglot tool manager -- it reads `mise.toml` and automatically installs the correct versions of Python, uv, and any other tools your project needs. You don't need to install Python or uv yourself.

**Run tasks with mise instead of calling tools directly:**

| Instead of... | Run... |
|---------------|--------|
| `uv sync --all-extras` | `mise run install` |
| `pytest` | `mise run test` |
| `uv run python main.py` | `mise run start` |
| `ruff check .` | `mise run lint` |

### Lifecycle Tasks (`mise.toml`)

The `mise.toml` file defines how to build and run your application:

```toml
[tools]
python = "3.13"
uv = "0.6.10"

[env]
_.python.venv = { path = ".venv", create = true }

[tasks.install]
description = "Install dependencies"
run = "uv sync --all-extras"

[tasks.build]
description = "Build the project"
run = "echo 'No build step required'"

[tasks.start]
description = "Start the MCP server"
run = "uv run python main.py"
depends = ["install"]

[tasks.test]
run = "pytest"

[tasks.lint]
run = "ruff check ."

[tasks.format]
run = "ruff format ."

[tasks.typecheck]
run = "basedpyright"
```

### Infrastructure Config (`arco.toml`)

The `arco.toml` file declares what infrastructure your app needs:

```toml
[arco]
source = "foundry_app"
name = "my-server"
version = "0.1.0"
env_base = "standard"

# Runtime environment: baked into container
[arco.env.runtime]
APP_FS_ROOT = "/filesystem"
INTERNET_ENABLED = "false"

# User-configurable parameters (shown in RL Studio UI)
[arco.env.runtime.schema.INTERNET_ENABLED]
type = "bool"
label = "Internet access"
description = "Allow the MCP server to make outbound network requests"

# Secrets: injected at runtime, never baked
[arco.secrets.host]
GITHUB_TOKEN = "RLS_GITHUB_READ_TOKEN"
```

### Environment Variable Matrix

ArCo uses a 2x3 matrix for environment variables:

| | Host (build orchestration) | Build (container build) | Runtime (container execution) |
|---|---|---|---|
| **Config** | `[arco.env.host]` | `[arco.env.build]` | `[arco.env.runtime]` |
| **Secret** | `[arco.secrets.host]` | `[arco.secrets.build]` | `[arco.secrets.runtime]` |

- **Config** values can be baked into containers
- **Secret** values are always injected at runtime, never baked into images

### Environment Variables: Local vs Production

**Important:** Environment variables must be set in two places — one for local development, one for production. This is current tech debt we're working to simplify.

| File | Purpose | When it's used |
|------|---------|----------------|
| `mise.toml` `[env]` | Local development | When you run `mise run start` locally |
| `arco.toml` `[arco.env.*]` | Production | When RL Studio deploys your container |

**How mise works:** Mise functions like [direnv](https://direnv.net/) — when you `cd` into a directory with a `mise.toml`, it automatically loads environment variables and activates the correct tool versions (Python, uv, etc.). You don't need to manually source anything.

**The rule:** If you add an environment variable, add it to **both files**:

```toml
# mise.toml — for local development
[env]
MY_NEW_VAR = "local_value"
```

```toml
# arco.toml — for production
[arco.env.runtime]
MY_NEW_VAR = "production_value"
```

**Do NOT use `.env` files.** The `mise.toml` + `arco.toml` system replaces `.env` entirely. These are the only two files you need for environment variable management.

### ArCo Environment Stages: host, build, runtime

Unlike `mise.toml` which has a single flat `[env]` section, ArCo separates environment variables into three stages based on *when* they're needed in the deployment pipeline. You must specify the correct stage for each variable.

| Stage | When Used | How It's Consumed | Example Variables |
|-------|-----------|-------------------|-------------------|
| `[arco.env.host]` | Before container build | Read by RL Studio orchestration layer | `REPO_URL`, `REPO_BRANCH`, `REPO_PATH` |
| `[arco.env.build]` | During `docker build` | Exported before install/build commands | `UV_COMPILE_BYTECODE`, `CFLAGS` |
| `[arco.env.runtime]` | When container runs | Baked into Dockerfile as `ENV` | `APP_FS_ROOT`, `INTERNET_ENABLED` |

**Stage Details:**

**Host Stage** (`[arco.env.host]`) — Used by RL Studio's build orchestrator (the "Report Engine") before any Docker commands. These variables tell RL Studio *how to fetch your code*:
- `REPO_URL` — Git repository to clone
- `REPO_BRANCH` — Branch to checkout (optional)
- `REPO_PATH` — Subdirectory containing your app (optional)

These are **never** injected into your container — they're consumed by infrastructure.

**Build Stage** (`[arco.env.build]`) — Available during `docker build` when running your `install` and `build` tasks. Exported as shell variables (via `export VAR=value`) before each command. Use for:
- Compiler flags (`CFLAGS`, `LDFLAGS`)
- Build-time feature toggles (`INSTALL_MEDICINE=true`)
- Package manager configuration (`UV_COMPILE_BYTECODE=1`)

These are **not** baked into the final image as `ENV` — they only exist during build.

**Runtime Stage** (`[arco.env.runtime]`) — Baked into the Dockerfile as `ENV` directives and available when your container runs. This is where most of your app configuration goes:
- `APP_FS_ROOT` — Filesystem root for your app
- `INTERNET_ENABLED` — Network policy flag
- `HAS_STATE` / `STATE_LOCATION` — Stateful app configuration
- Any custom app configuration

**Why the separation matters:** 
- Security: Host/build secrets don't leak into the final container image
- Performance: Build-time vars don't bloat the runtime environment
- Clarity: RL Studio knows exactly which vars to use at each pipeline stage

**Mapping mise.toml to arco.toml:** In local development, `mise.toml` simulates all three stages at once. When adding a new variable, consider which stage it belongs to:

```toml
# mise.toml — flat, everything available locally
[env]
APP_FS_ROOT = "/filesystem"
MY_API_URL = "http://localhost:8000"
```

```toml
# arco.toml — staged for production
[arco.env.runtime]
APP_FS_ROOT = "/filesystem"
MY_API_URL = "https://api.production.com"
```

### Secrets

Use `[arco.secrets.*]` for sensitive values like API keys, tokens, and passwords. Secrets are:
- **Never baked** into Docker images (excluded from Dockerfiles)
- **Masked** in logs and UI
- **Resolved at runtime** from AWS Secrets Manager by the MCP Core team's infrastructure

```toml
# arco.toml
[arco.secrets.runtime]
API_KEY = true              # Secret name matches env var name
DATABASE_URL = "db_password" # Custom secret name in AWS
```

**For local development:** Create a `mise.local.toml` file (gitignored) to set secret values:

```toml
# mise.local.toml — gitignored, never committed
[env]
API_KEY = "your-dev-api-key"
DATABASE_URL = "postgresql://localhost/devdb"
```

**To add a new secret:** Contact the MCP Core team. They will add the secret to AWS Secrets Manager and configure RL Studio to inject it at runtime.

### CI/CD Integration

This repository includes GitHub Actions for ArCo validation:

- **`arco-validate.yml`** — Validates your config on every PR
- **`foundry-service-sync.yml`** — Syncs your config to RL Studio on release

### Keeping Config Updated

| If you... | Update this |
|-----------|-------------|
| Changed install/build/run commands | `[tasks.*]` in `mise.toml` |
| Added a new environment variable | `[env]` in `mise.toml` AND `[arco.env.runtime]` in `arco.toml` |
| Need a new secret | `[arco.secrets.*]` in `arco.toml` |
| Want users to configure a variable | Add `[arco.env.runtime.schema.*]` |

---


## Tools (Default Mode)

These are the individual tools available by default:

### 1. `list_lookml_models`

List all available LookML models.

**Parameters:** None (returns all models)

---

### 2. `get_explore`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `explore` | - Available dimensions (groupable fields) - Available measures (aggregations) - Join relationships between tables - Field metadata (labels, types, descriptions)

    This is the primary tool for understanding:
    - "What fields are available?"
    - "How are these tables joined?"
    - "What can I query from this explore?"
    """

    model: str | _required_ | Model name |

---

### 3. `list_views`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `for` | - Understanding the data model structure - Discovering which tables are available in an Explore - Seeing join relationships between tables
"""

    model: str | _required_ | Model name |

---

### 4. `generate_lookml`

Generate LookML view and model files from CSV data.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `ctx` | Context | _required_ | - |
| `model_name` | str[str] | 'seeded_data' | - |
| `connection` | str[str] | '@{database_connection}' | - |

---

### 5. `get_generated_lookml`

Get the generated LookML content for a specific view.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `ctx` | Context | _required_ | - |
| `view_name` | str[str] | _required_ | - |

---

### 6. `list_available_views`

List all views that can be generated from CSV data.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `ctx` | Context | _required_ | - |

---

### 7. `deploy_lookml`

Deploy generated LookML to Looker via Git.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `ctx` | Context | _required_ | - |
| `model_name` | str[str] | 'seeded_data' | - |
| `connection` | str[str] | '@{database_connection}' | - |
| `trigger_looker_deploy` | bool[bool] | True | - |

---

### 8. `list_folders`

List all folders containing Looks and Dashboards.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `parent_id` | string | No | Parent folder ID. Omit for root folders |

---

### 9. `list_explores`

List available explores for a model.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `model` | string | Yes | LookML model name |

---

### 10. `list_fields`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `model` | str | _required_ | LookML model name |

---

### 11. `list_looks`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `for` | - Discovering existing query patterns - Finding queries by topic - Getting query IDs for execution
"""

    folder_id: str? | null | Filter by folder ID |

| `title` | str? | null | Search by title (case-insensitive) |

---

### 12. `get_look`

Get a Look by ID.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `look_id` | string | Yes | Look ID to retrieve |

---

### 13. `create_look`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `title` | str | _required_ | Look title |
| `query_id` | str | _required_ | Query ID for the Look |
| `folder_id` | str | _required_ | Folder to save the Look in |

---

### 14. `run_look`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `cases` | - Execute saved Looks without knowing query details - Run pre-configured reports - Access curated data views
"""

    look_id: int | str | _required_ | Look ID to execute |

---

### 15. `run_look_pdf`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `cases` | - Generate printable reports from saved Looks - Create PDF exports for email distribution - Archive Look results as documents - Enable LLM analysis via pdfs_read_image tool

    The PDF includes the Look's visualization rendered at the specified
    dimensions with optional page formatting.
    """

    look_id: int | str | _required_ | Look ID to render as PDF |

| `width` | int | 800 | PDF width in pixels |

---

### 16. `looker_update_look`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `look_id` | str | _required_ | Look ID to update |
| `title` | str? | null | New title |
| `description` | str? | null | New description |

---

### 17. `looker_delete_look`

Delete a Look.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `look_id` | string | Yes | Look ID to delete |

---

### 18. `looker_search_looks`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `title` | str? | null | Filter by title (contains) |
| `folder_id` | str? | null | Filter by folder ID |

---

### 19. `looker_render_look`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `look_id` | str | _required_ | Look ID to render |
| `format` | str | "png" | Output format (pdf or png) |
| `width` | int? | null | Width in pixels |

---

### 20. `list_dashboards`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `for` | - Discovering existing dashboard patterns - Finding dashboards by topic - Understanding available metrics
"""

    search: str? | null | Search dashboards by title (case-insensitive) |

| `folder_id` | str? | null | Filter by folder ID |
| `sorts` | list[str]? | null | Sort fields (e.g., [ |

---

### 21. `get_dashboard`

Get a Dashboard by ID.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `dashboard_id` | string | Yes | Dashboard ID to retrieve |

---

### 22. `create_dashboard`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `title` | str | _required_ | Dashboard title |
| `folder_id` | str | _required_ | Folder to save the Dashboard in (required) |

---

### 23. `add_tile_to_dashboard`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `dashboard_id` | str | _required_ | Dashboard ID |
| `query_id` | str? | null | Query ID for the tile |
| `look_id` | str? | null | Look ID for the tile (alternative to query_id) |
| `title` | str? | null | Tile title |
| `type` | str | "vis" | Tile type (vis, text, etc.) |

---

### 24. `run_dashboard`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `dashboard_id` | int | str | _required_ | Dashboard ID to execute |

---

### 25. `looker_reorder_dashboard_tiles`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `dashboard_id` | str | _required_ | Dashboard ID |

---

### 26. `looker_delete_tile`

Delete a tile from a dashboard.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `dashboard_id` | string | Yes | Dashboard ID containing the tile |
| `tile_id` | string | Yes | Tile ID to delete |

---

### 27. `looker_delete_dashboard`

Delete a dashboard.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `dashboard_id` | string | Yes | Dashboard ID to delete |

---

### 28. `looker_search_dashboards`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `title` | str? | null | Filter by title (contains) |
| `folder_id` | str? | null | Filter by folder ID |

---

### 29. `looker_export_dashboard_pdf`

Export a dashboard as PDF.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `dashboard_id` | string | Yes | Dashboard ID to export |
| `output_path` | string | No | Output file path |

---

### 30. `looker_export_dashboard_png`

Export a dashboard as PNG.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `dashboard_id` | string | Yes | Dashboard ID to export |
| `output_path` | string | No | Output file path |

---

### 31. `create_query`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `model` | str | _required_ | Model name |
| `view` | str | _required_ | Explore/view name |
| `fields` | list[str | _required_ | Fields to include (dimensions and measures) |
| `filters` | list[QueryFilter | null | Filters to apply |
| `sorts` | list[str | null | Sort order |
| `limit` | int | 5000 | Row limit (default 5000, max 5000) |
| `vis_config` | VisConfig? | null | Visualization configuration for chart rendering. |

---

### 32. `looker_create_query`

Create a new query.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `model` | string | Yes | LookML model name |
| `view` | string | Yes | LookML view name |
| `fields` | array[string] | Yes | Fields to include |
| `filters` | object | No | Query filters |
| `sorts` | array[string] | No | Sort order |
| `limit` | integer | No | Row limit |

---

### 33. `looker_get_query`

Get a query by ID.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `query_id` | string | Yes | Query ID to retrieve |

---

### 34. `run_query_inline`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `model` | str | _required_ | Model name |
| `view` | str | _required_ | Explore/view name |
| `fields` | list[str | _required_ | Fields to include |
| `filters` | list[QueryFilter | null | Filters to apply |
| `sorts` | list[str | null | Sort order |
| `limit` | int | 5000 | Row limit (default 5000, max 5000) |
| `dynamic_fields` | list[TableCalculation | null | Table calculations to apply to query results. |

---

### 35. `run_query_by_id`

Execute a query by ID.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `query_id` | string | Yes | Query ID to run |
| `result_format` | string | No | Output format (json, csv, etc.) |

---

### 36. `looker_run_query_json`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `model` | str | _required_ | Model name |
| `view` | str | _required_ | Explore/view name |
| `fields` | list[str | _required_ | Fields to include |
| `filters` | list[QueryFilter | null | Filters to apply |
| `sorts` | list[str | null | Sort order |
| `limit` | int | 5000 | Row limit (default 5000, max 5000) |
| `dynamic_fields` | list[TableCalculation | null | Table calculations to apply to query results. |

---

### 37. `looker_run_query_csv`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `model` | str | _required_ | Model name |
| `view` | str | _required_ | Explore/view name |
| `fields` | list[str | _required_ | Fields to include |
| `filters` | list[QueryFilter | null | Filters to apply |
| `sorts` | list[str | null | Sort order |
| `limit` | int | 5000 | Row limit (default 5000, max 5000) |
| `dynamic_fields` | list[TableCalculation | null | Table calculations to apply to query results. |

---

### 38. `run_query_png`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `cases` | - Generate chart images for reports - Create visualizations for dashboards - Export query results as images for sharing

    Supported chart types:
    - looker_column (default)
    - looker_bar
    - looker_line
    - looker_pie
    - looker_area
    - looker_scatter
    - single_value
    - table
    """

    query_id: int | str | _required_ | Query ID to execute and visualize |

| `width` | int | 800 | Image width in pixels |

---

### 39. `export_query`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `for` | - Downloading query results for external analysis - Sharing data with other tools - Creating data exports for reporting
"""

    query_id: int | str | _required_ | Query ID to export |

| `format` | ExportFormat | null | Export format: json (structured data) or csv (comma-separated) |

---

### 40. `run_sql_query`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `for` | - Advanced SQL analysis - Testing and debugging queries - Exploring database schema directly - Quick data exploration without creating LookML
"""

    connection: str | _required_ | Database connection name |

| `sql` | str | _required_ | SQL query to execute |
| `limit` | int | 5000 | Maximum rows to return (default 5000, max 5000) |

---

### 41. `looker_download_rendered_file`

Download a rendered file.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `render_task_id` | string | Yes | Render task ID |
| `output_path` | string | No | Output file path |

---

### 42. `health_check`

Check Looker API health status.

**Parameters:** None (returns health status)

---

## Consolidated Tools

When using consolidated mode, these meta-tools combine multiple operations:

### 1. `looker_lookml`

LookML discovery and management.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | LOOKML_ACTIONS | Ellipsis | Action: 'help', 'list_models', 'get_explore', 'list_views', 'generate', 'get_generated', 'list_av... |
| `model` | string? | null | LookML model name. REQUIRED for queries. |
| `explore` | string? | null | LookML explore name. REQUIRED with model for queries. |
| `model_name` | string? | null | Name for generated LookML model |
| `connection` | string? | null | Database connection name |
| `view_name` | string? | null | View name to get LookML for |
| `trigger_looker_deploy` | boolean | true | Trigger Looker deploy after Git push |

---

### 2. `looker_content`

Content discovery - folders, search, explores, and fields.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | CONTENT_ACTIONS | Ellipsis | Action: 'help', 'list_folders', 'search', 'list_explores', 'list_fields' |
| `parent_id` | string? | null | Parent folder ID. Required for nested content. Omit for root. |
| `query` | string? | null | Search text. Matches names, descriptions. Case-insensitive. |
| `content_type` | string? | null | Content type filter (look, dashboard) |
| `model` | string? | null | LookML model name. REQUIRED for queries. |
| `explore` | string? | null | LookML explore name. REQUIRED with model for queries. |

---

### 3. `looker_queries`

Query creation, execution, and export.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | QUERY_ACTIONS | Ellipsis | Action: 'help', 'create', 'run_inline', 'run_by_id', 'run_png', 'export', 'sql' |
| `model` | string? | null | LookML model name. REQUIRED for queries. |
| `view` | string? | null | View name within explore. REQUIRED for field selection. |
| `fields` | array[string]? | null | Fields to query or include in response. |
| `filters` | object[string, string]? | null | Query filters |
| `sorts` | array[string]? | null | Sort order specification. |
| `limit` | integer? | null | Max results to return. Typical range: 1-100. |
| `query_id` | string? | null | Query ID. REQUIRED for query operations. |
| `chart_type` | string? | null | Chart type filter. Optional. |
| `format` | string? | null | Export format (json, csv) |
| `sql` | string? | null | SQL query (SELECT only). |
| `connection` | string? | null | Database connection name |

---

### 4. `looker_looks`

Look management - list, get, create, run, and render.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | LOOK_ACTIONS | Ellipsis | Action: 'help', 'list', 'get', 'create', 'run', 'render_pdf' |
| `folder_id` | string? | null | Folder ID. REQUIRED for folder operations. |
| `look_id` | string? | null | Look ID. REQUIRED for look operations. |
| `title` | string? | null | Title for the entity. REQUIRED for create. |
| `query_id` | string? | null | Query ID. REQUIRED for query operations. |
| `description` | string? | null | Detailed description. Optional but recommended. |
| `limit` | integer? | null | Max results to return. Typical range: 1-100. |

---

### 5. `looker_dashboards`

Dashboard management - list, get, create, add tiles, and export.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | DASHBOARD_ACTIONS | Ellipsis | Action: 'help', 'list', 'get', 'create', 'add_tile', 'export_pdf', 'export_png', 'download_render' |
| `folder_id` | string? | null | Folder ID. REQUIRED for folder operations. |
| `dashboard_id` | string? | null | Dashboard ID. REQUIRED for dashboard operations. |
| `title` | string? | null | Title for the entity. REQUIRED for create. |
| `description` | string? | null | Detailed description. Optional but recommended. |
| `query_id` | string? | null | Query ID. REQUIRED for query operations. |
| `look_id` | string? | null | Look ID. REQUIRED for look operations. |
| `tile_title` | string? | null | Tile title. Optional. |
| `tile_type` | string | `"vis"` | Tile type (vis, text) |
| `chart_type` | string? | null | Chart type filter. Optional. |
| `width` | integer? | null | Width in pixels. Optional for export. |
| `height` | integer? | null | Height in pixels. Optional for export. |
| `render_task_id` | string? | null | Render task ID |

---

### 6. `looker_admin`

Server administration and health check.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | ADMIN_ACTIONS | Ellipsis | The operation to perform. REQUIRED. Call with action='help' first. |

---

### 7. `looker_schema`

Get JSON schema for any Looker tool's input/output.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `tool` | string | Ellipsis | Tool name for schema lookup. |
| `action` | string? | null | Optional: filter to show schema for specific action |

---

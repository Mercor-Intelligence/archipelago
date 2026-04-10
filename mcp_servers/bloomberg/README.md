# Bloomberg MCP Server

FastAPI wrapped in FastMCP servers with shared utilities


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


## Tools

### 1. `reference_data`

Get current quotes and reference data for securities.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `securities` | array[string] | _required_ | List of securities in Bloomberg format. |
| `fields` | array[string] | _required_ | List of Bloomberg field mnemonics to retrieve. |

---

### 2. `historical_data`

Get historical OHLCV data for securities.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `securities` | array[string] | _required_ | List of securities in Bloomberg format. |
| `fields` | array[string] | _required_ | List of Bloomberg field mnemonics for historical data. |
| `start_date` | string | _required_ | Start date in ISO format (e.g., "2025-11-01T00:00:00Z") |
| `end_date` | string | _required_ | End date in ISO format (e.g., "2025-11-07T00:00:00Z") |

---

### 3. `intraday_bars`

Get intraday OHLCV bar data at various intervals.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `security` | string | _required_ | Single security in Bloomberg format. |
| `start_datetime` | string | _required_ | Start datetime in ISO format (e.g., "2025-11-25T09:30:00Z") |
| `end_datetime` | string | _required_ | End datetime in ISO format (e.g., "2025-11-25T16:00:00Z") |
| `interval` | integer | 60 | Bar interval in minutes (e.g., 1, 5, 15, 60) |

---

### 4. `intraday_ticks`

Get intraday tick-level data.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `security` | string | _required_ | Single security in Bloomberg format. |
| `start_datetime` | string | _required_ | Start datetime in ISO format (e.g., "2025-11-25T09:30:00Z") |
| `end_datetime` | string | _required_ | End datetime in ISO format (e.g., "2025-11-25T16:00:00Z") |
| `event_types` | array[string] | _required_ | Event types to retrieve (e.g., ["TRADE", "BID", "ASK"]) |

---

### 5. `equity_screening`

Screen equities by criteria (sector, market cap, etc.).

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `screen_name` | string | `"Custom Screen"` | Name of the equity screen |
| `sector` | ValidSector? | null | Sector filter for screening. Must be one of the exact lowercase values below. |
| `market_cap_min` | number? | null | Minimum market cap in millions USD |
| `market_cap_max` | number? | null | Maximum market cap in millions USD |

---

### 6. `list_symbols`

List all symbols available in the offline database.

---

### 7. `data_status`

Get database status showing date ranges and row counts for each data type.

---

### 8. `download_symbol`

Download CSV data for a single symbol.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | string | _required_ | Stock ticker symbol (e.g., "AAPL") |
| `data_type` | string | `"historical"` | Type of data: "historical" for daily OHLCV, or intraday like "intraday_5min", "intraday_15min", "... |
| `start_date` | string? | null | Optional start date filter (e.g., "2024-01-01") |
| `end_date` | string? | null | Optional end date filter (e.g., "2024-12-31") |

---

### 9. `bloomberg_discover`

Discover available Bloomberg tools and their capabilities.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `tool_name` | string? | null | Optional: Get detailed info for a specific tool. Leave empty to list all tools. |

---

## Individual Tools (GUI Mode)

When `GUI_ENABLED=true`, these individual tools are available instead of the consolidated meta-tools above:

### 1. `data_status`

Get database status showing date ranges and row counts for each data type.

**Parameters:** None (no parameters required)

---

### 2. `download_symbol`

Download CSV data for a single symbol.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | Yes | Stock ticker symbol (e.g., "AAPL") |
| `data_type` | string | No | Type of data: "historical" for daily OHLCV, or intraday like "intraday_5min". Default: "historical" |
| `start_date` | string | No | Optional start date filter (e.g., "2024-01-01") |
| `end_date` | string | No | Optional end date filter (e.g., "2024-12-31") |

---

### 3. `equity_screening`

Screen equities by criteria (sector, market cap, etc.).

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `screen_name` | string | No | Name of the equity screen. Default: "Custom Screen" |
| `sector` | string | No | Sector filter (e.g., "technology", "healthcare", "financial") |
| `min_market_cap` | number | No | Minimum market cap filter |
| `max_market_cap` | number | No | Maximum market cap filter |
| `min_pe_ratio` | number | No | Minimum P/E ratio filter |
| `max_pe_ratio` | number | No | Maximum P/E ratio filter |

---

### 4. `historical_data`

Get historical OHLCV data for securities.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `securities` | array[string] | Yes | List of securities in Bloomberg format (e.g., ["AAPL US Equity"]) |
| `fields` | array[string] | Yes | List of Bloomberg field mnemonics (e.g., ["PX_LAST", "VOLUME"]) |
| `start_date` | string | Yes | Start date in YYYY-MM-DD format |
| `end_date` | string | Yes | End date in YYYY-MM-DD format |
| `periodicity` | string | No | Data frequency: "DAILY", "WEEKLY", "MONTHLY". Default: "DAILY" |

---

### 5. `intraday_bars`

Get intraday OHLCV bar data at various intervals.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `security` | string | Yes | Single security in Bloomberg format (e.g., "AAPL US Equity") |
| `start_datetime` | string | Yes | Start datetime in ISO format (e.g., "2025-11-25T09:30:00Z") |
| `end_datetime` | string | Yes | End datetime in ISO format (e.g., "2025-11-25T16:00:00Z") |
| `interval` | integer | No | Bar interval in minutes (1-1440). Default: 60 |

---

### 6. `intraday_ticks`

Get intraday tick-level data.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `security` | string | Yes | Single security in Bloomberg format (e.g., "AAPL US Equity") |
| `start_datetime` | string | Yes | Start datetime in ISO format |
| `end_datetime` | string | Yes | End datetime in ISO format |
| `event_types` | array[string] | No | Event types to retrieve. Default: ["TRADE"] |

---

### 7. `list_symbols`

List all symbols available in the offline database.

**Parameters:** None (no parameters required)

---

### 8. `reference_data`

Get current quotes and reference data for securities.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `securities` | array[string] | Yes | List of securities in Bloomberg format (e.g., ["AAPL US Equity", "MSFT US Equity"]) |
| `fields` | array[string] | Yes | List of Bloomberg field mnemonics (e.g., ["PX_LAST", "BID", "ASK", "VOLUME"]) |

---

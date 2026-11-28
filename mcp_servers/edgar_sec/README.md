# Edgarsec MCP Server

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

### 1. `get_company_submissions`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `cik` | str? | null | Company CIK number (e.g., |
| `ticker` | str? | null | Stock ticker symbol (e.g., |
| `name` | str? | null | Company name (e.g., |
| `limit` | int? | 20 | Max number of filings to return per page (max: 50) |
| `page` | int? | 1 | Page number (1-indexed) for pagination |
| `form_types` | list[str]? | null | Filter by SEC form types (e.g., [ |

---

### 2. `get_company_facts`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `cik` | str? | null | Company CIK number |
| `ticker` | str? | null | Stock ticker symbol (e.g., |
| `name` | str? | null | Company name (e.g., |

---

### 3. `get_company_concept`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `cik` | str? | null | Company CIK number |
| `ticker` | str? | null | Stock ticker symbol (e.g., |
| `name` | str? | null | Company name (e.g., |
| `taxonomy` | str | _required_ | XBRL taxonomy (e.g., |

---

### 4. `get_frames`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `taxonomy` | str | _required_ | XBRL taxonomy (e.g., |
| `tag` | str | _required_ | XBRL tag (e.g., |
| `unit` | str | _required_ | Unit of measure (e.g., |

---

### 5. `lookup_cik`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `ticker` | str? | null | Stock ticker symbol (e.g., |

---

### 6. `health_check`

No description available.

**Parameters:** None

---

### 7. `list_filing_documents`

List all documents in a SEC filing (primary document and exhibits).

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `cik` | str? | null | 10-digit zero-padded CIK |
| `ticker` | str? | null | Stock ticker symbol (e.g., 'AAPL') |
| `name` | str? | null | Company name (e.g., 'Apple Inc') |
| `filing_accession` | str | _required_ | Filing accession number (e.g., '0000320193-23-000106') |

---

### 8. `get_filing_document`

Get the text content of a specific document from a SEC filing.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `cik` | str? | null | 10-digit zero-padded CIK |
| `ticker` | str? | null | Stock ticker symbol (e.g., 'AAPL') |
| `name` | str? | null | Company name (e.g., 'Apple Inc') |
| `filing_accession` | str | _required_ | Filing accession number (e.g., '0000320193-23-000106') |
| `document` | str? | 'primary' | 'primary' for main filing, or specific filename from list_filing_documents |

---

## Consolidated Tools

When using consolidated mode, these meta-tools combine multiple operations:

### 1. `edgar_filings`

SEC EDGAR company filings, facts, and XBRL data.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'submissions', 'facts', 'concept', 'frames'] | _required_ | Operation to perform |
| `cik` | string? | null | 10-digit zero-padded CIK. ONE OF cik/ticker/name REQUIRED for submissions, facts, concept. |
| `ticker` | string? | null | Stock ticker (e.g., 'AAPL'). ONE OF cik/ticker/name REQUIRED for submissions, facts, concept. |
| `name` | string? | null | Company name for fuzzy search. ONE OF cik/ticker/name REQUIRED for submissions, facts, concept. |
| `limit` | integer? | 20 | Max results to return. Typical range: 1-100. |
| `page` | integer? | 1 | Page number (1-indexed). Use with limit for pagination. |
| `form_types` | array[string]? | null | Filter by SEC form types (e.g., ['10-K', '10-Q']). Common: 10-K (annual), 10-Q (quarterly), 8-K (... |
| `summary_only` | boolean | false | If True, return only summary statistics without full filing details. Use to discover what filings... |
| `limit_concepts` | integer? | 50 | Max concepts per taxonomy (max 100) |
| `taxonomy` | string? | null | XBRL taxonomy (e.g., 'us-gaap') |
| `tag` | string? | null | XBRL tag (e.g., 'Revenue') |
| `unit` | string? | null | Unit of measure (e.g., 'USD') |
| `period` | string? | null | Reporting period. Format varies. Check action help. |

---

### 2. `edgar_analysis`

Extract structured data from SEC filings.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'debt_schedule', 'equity_compensation', 'html_table'] | _required_ | Operation to perform |
| `cik` | string? | null | 10-digit zero-padded CIK. ONE OF cik/ticker/name REQUIRED for all analysis actions. |
| `ticker` | string? | null | Stock ticker (e.g., 'AAPL'). ONE OF cik/ticker/name REQUIRED for all analysis actions. |
| `name` | string? | null | Company name. ONE OF cik/ticker/name REQUIRED for all analysis actions. |
| `filing_accession` | string? | null | Filing accession number (e.g., '0000320193-23-000106'). REQUIRED for all analysis actions. |
| `table_keyword` | string? | null | Keyword to search in table headers. REQUIRED for html_table action. |

---

### 3. `edgar_documents`

Fetch raw document text from SEC filings (8-K exhibits, merger agreements, contracts, etc.).

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'list', 'get_text'] | _required_ | Operation to perform |
| `cik` | string? | null | 10-digit zero-padded CIK. ONE OF cik/ticker/name REQUIRED. |
| `ticker` | string? | null | Stock ticker (e.g., 'AAPL'). ONE OF cik/ticker/name REQUIRED. |
| `name` | string? | null | Company name for fuzzy search. ONE OF cik/ticker/name REQUIRED. |
| `filing_accession` | string | _required_ | Filing accession number (e.g., '0000320193-23-000106'). |
| `document` | string? | 'primary' | 'primary' for main filing, or specific filename from list action. |

---

### 4. `edgar_lookup`

Look up company CIK and check server health.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'cik', 'health'] | _required_ | Operation to perform |
| `ticker` | string? | null | Stock ticker (e.g., 'AAPL'). ONE OF ticker/name REQUIRED for cik action. |
| `name` | string? | null | Company name for fuzzy search. ONE OF ticker/name REQUIRED for cik action. |

---

### 5. `edgar_schema`

Get JSON schema for EDGAR SEC tools.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `tool_name` | string? | null | Tool name to get schema for. If None, lists all tools. |

---
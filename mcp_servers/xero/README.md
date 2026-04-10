# Xero MCP Server

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
XERO_DATABASE_URL = "db_password" # Custom secret name in AWS
```

**For local development:** Create a `mise.local.toml` file (gitignored) to set secret values:

```toml
# mise.local.toml — gitignored, never committed
[env]
API_KEY = "your-dev-api-key"
XERO_DATABASE_URL = "postgresql://localhost/devdb"
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

### 1. `upload_accounts_csv`

Upload accounts data from CSV with accounting equation validation.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `csv_content` | string | Yes | CSV content with headers |
| `merge_mode` | string | No | "append" to add/update or "replace" to replace all. Default: "replace" |

---

### 2. `upload_contacts_csv`

Upload contacts data from CSV.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `csv_content` | string | Yes | CSV content with headers |
| `merge_mode` | string | No | "append" to add/update or "replace" to replace all. Default: "replace" |

---

### 3. `upload_invoices_csv`

Upload invoices data from CSV.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `csv_content` | string | Yes | CSV content with headers |
| `merge_mode` | string | No | "append" to add/update or "replace" to replace all. Default: "replace" |

---

### 4. `upload_payments_csv`

Upload payments data from CSV.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `csv_content` | string | Yes | CSV content with headers |
| `merge_mode` | string | No | "append" to add/update or "replace" to replace all. Default: "replace" |

---

### 5. `upload_bank_transactions_csv`

Upload bank transactions data from CSV.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `csv_content` | string | Yes | CSV content with headers |
| `merge_mode` | string | No | "append" to add/update or "replace" to replace all. Default: "replace" |

---

### 6. `upload_reports_csv`

Upload reports data from CSV.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `csv_content` | string | Yes | CSV content with headers |
| `merge_mode` | string | No | "append" to add/update or "replace" to replace all. Default: "replace" |

---

### 7. `upload_journals_csv`

Upload journal entries data from CSV.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `csv_content` | string | Yes | CSV content with headers |
| `merge_mode` | string | No | "append" to add/update or "replace" to replace all. Default: "replace" |

---

### 8. `upload_purchase_orders_csv`

Upload purchase orders data from CSV.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `csv_content` | string | Yes | CSV content with headers |
| `merge_mode` | string | No | "append" to add/update or "replace" to replace all. Default: "replace" |

---

### 9. `upload_quotes_csv`

Upload sales quotes/estimates data from CSV.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `csv_content` | string | Yes | CSV content with headers |
| `merge_mode` | string | No | "append" to add/update or "replace" to replace all. Default: "replace" |

---

### 10. `upload_credit_notes_csv`

Upload credit notes data from CSV.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `csv_content` | string | Yes | CSV content with headers |
| `merge_mode` | string | No | "append" to add/update or "replace" to replace all. Default: "replace" |

---

### 11. `upload_bank_transfers_csv`

Upload inter-account bank transfers data from CSV.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `csv_content` | string | Yes | CSV content with headers |
| `merge_mode` | string | No | "append" to add/update or "replace" to replace all. Default: "replace" |

---

### 12. `upload_overpayments_csv`

Upload customer/supplier overpayments data from CSV.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `csv_content` | string | Yes | CSV content with headers |
| `merge_mode` | string | No | "append" to add/update or "replace" to replace all. Default: "replace" |

---

### 13. `upload_prepayments_csv`

Upload customer/supplier prepayments data from CSV.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `csv_content` | string | Yes | CSV content with headers |
| `merge_mode` | string | No | "append" to add/update or "replace" to replace all. Default: "replace" |

---

### 14. `upload_budgets_csv`

Upload accounting budgets data from CSV.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `csv_content` | string | Yes | CSV content with headers |
| `merge_mode` | string | No | "append" to add/update or "replace" to replace all. Default: "replace" |

---

### 15. `upload_assets_csv`

Upload fixed assets data from CSV.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `csv_content` | string | Yes | CSV content with headers |
| `merge_mode` | string | No | "append" to add/update or "replace" to replace all. Default: "replace" |

---

### 16. `upload_asset_types_csv`

Upload asset type definitions from CSV.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `csv_content` | string | Yes | CSV content with headers |
| `merge_mode` | string | No | "append" to add/update or "replace" to replace all. Default: "replace" |

---

### 17. `upload_projects_csv`

Upload projects data from CSV.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `csv_content` | string | Yes | CSV content with headers |
| `merge_mode` | string | No | "append" to add/update or "replace" to replace all. Default: "replace" |

---

### 18. `upload_time_entries_csv`

Upload time entries data from CSV.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `csv_content` | string | Yes | CSV content with headers |
| `merge_mode` | string | No | "append" to add/update or "replace" to replace all. Default: "replace" |

---

### 19. `upload_files_csv`

Upload files metadata from CSV.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `csv_content` | string | Yes | CSV content with headers |
| `merge_mode` | string | No | "append" to add/update or "replace" to replace all. Default: "replace" |

---

### 20. `upload_folders_csv`

Upload folders data from CSV.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `csv_content` | string | Yes | CSV content with headers |
| `merge_mode` | string | No | "append" to add/update or "replace" to replace all. Default: "replace" |

---

### 21. `upload_associations_csv`

Upload file associations data from CSV.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `csv_content` | string | Yes | CSV content with headers |
| `merge_mode` | string | No | "append" to add/update or "replace" to replace all. Default: "replace" |

---

### 22. `get_accounts`

Get chart of accounts from Xero with optional filtering and ordering.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `where` | string | No | Filter expression (e.g., Status=="ACTIVE") |
| `order` | string | No | Order expression (e.g., "Code ASC") |
| `page` | integer | No | Page number for pagination (1-indexed) |

---

### 23. `get_contacts`

Get contacts (customers/suppliers) from Xero.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `ids` | string | No | Comma-separated list of Contact IDs |
| `where` | string | No | Filter expression (e.g., ContactStatus=="ACTIVE") |
| `include_archived` | boolean | No | Include archived contacts. Default: false |
| `order` | string | No | Order expression (e.g., "Name ASC") |
| `page` | integer | No | Page number for pagination (1-indexed) |

---

### 24. `get_invoices`

Get AR/AP invoices from Xero with line items and related data.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `ids` | string | No | Comma-separated list of Invoice IDs |
| `statuses` | string | No | Comma-separated list of statuses |
| `where` | string | No | Filter expression |
| `page` | integer | No | Page number for pagination (1-indexed) |

---

### 25. `get_bank_transactions`

Get bank transactions from Xero.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `where` | string | No | Filter expression |
| `page` | integer | No | Page number for pagination (1-indexed) |

---

### 26. `get_payments`

Get payments from Xero.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `where` | string | No | Filter expression |
| `page` | integer | No | Page number for pagination (1-indexed) |

---

### 27. `get_report_balance_sheet`

Generate balance sheet report.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `date` | string | Yes | Report date (YYYY-MM-DD) |
| `periods` | integer | No | Number of periods to include |
| `timeframe` | string | No | Timeframe (MONTH, QUARTER, YEAR) |
| `tracking_categories` | string | No | Comma-separated tracking category options |

---

### 28. `get_report_profit_and_loss`

Generate profit and loss report.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `from_date` | string | Yes | Start date (YYYY-MM-DD) |
| `to_date` | string | Yes | End date (YYYY-MM-DD) |
| `periods` | integer | No | Number of periods to include |
| `timeframe` | string | No | Timeframe (MONTH, QUARTER, YEAR) |
| `tracking_categories` | string | No | Comma-separated tracking category options |

---

### 29. `get_report_aged_receivables`

Get aged receivables report for a contact.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `contact_id` | string | Yes | Contact UUID |
| `date` | string | No | Shows payments up to this date (YYYY-MM-DD) |
| `from_date` | string | No | Start date for aging |

---

### 30. `get_report_aged_payables`

Get aged payables report for a contact.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `contact_id` | string | Yes | Contact UUID |
| `date` | string | No | Shows payments up to this date (YYYY-MM-DD) |
| `from_date` | string | No | Start date for aging |

---

### 31. `get_budget_summary`

Get budget summary data.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `date` | string | No | Budget date (YYYY-MM-DD) |
| `period` | integer | No | Number of periods |
| `timeframe` | string | No | Timeframe (MONTH, QUARTER, YEAR) |

---

### 32. `get_budgets`

Get budgets from Xero.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `where` | string | No | Filter expression |
| `page` | integer | No | Page number for pagination (1-indexed) |

---

### 33. `get_report_executive_summary`

Get executive summary report.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `date` | string | No | Report date (YYYY-MM-DD) |

---

### 34. `get_journals`

Get journals from Xero.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `where` | string | No | Filter expression |
| `page` | integer | No | Page number for pagination (1-indexed) |

---

### 35. `get_bank_transfers`

Get bank transfers from Xero.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `where` | string | No | Filter expression |
| `page` | integer | No | Page number for pagination (1-indexed) |

---

### 36. `get_quotes`

Get quotes from Xero.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `where` | string | No | Filter expression |
| `page` | integer | No | Page number for pagination (1-indexed) |

---

### 37. `get_purchase_orders`

Get purchase orders from Xero.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `where` | string | No | Filter expression |
| `page` | integer | No | Page number for pagination (1-indexed) |

---

### 38. `get_credit_notes`

Get credit notes from Xero.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `where` | string | No | Filter expression |
| `page` | integer | No | Page number for pagination (1-indexed) |

---

### 39. `get_prepayments`

Get prepayments from Xero.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `where` | string | No | Filter expression |
| `page` | integer | No | Page number for pagination (1-indexed) |

---

### 40. `get_overpayments`

Get overpayments from Xero.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `where` | string | No | Filter expression |
| `page` | integer | No | Page number for pagination (1-indexed) |

---

### 41. `get_assets`

Get fixed assets from Xero.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `status` | string | No | Filter by asset status |
| `page` | integer | No | Page number for pagination |

---

### 42. `get_asset_types`

Get asset types from Xero.

**Parameters:** None (returns all asset types)

---

### 43. `get_files`

Get files from Xero.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `page` | integer | No | Page number for pagination |

---

### 44. `get_folders`

Get folders from Xero.

**Parameters:** None (returns all folders)

---

### 45. `get_associations`

Get file associations from Xero.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `object_id` | string | No | Filter by associated object ID |
| `page` | integer | No | Page number for pagination |

---

### 46. `get_projects`

Get projects from Xero.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `page` | integer | No | Page number for pagination |

---

### 47. `get_project_time`

Get project time entries from Xero.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `project_id` | string | No | Filter by project ID |
| `page` | integer | No | Page number for pagination |

---

### 48. `reset_state`

Reset the server state to initial values.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `confirm` | boolean | No | Confirmation flag. Default: false |

---

## Consolidated Tools

When using consolidated mode, these meta-tools combine multiple operations:

### 1. `xero_entities`

Retrieve Xero master data entities.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'accounts', 'contacts'] | Ellipsis | Action: 'help', 'accounts', 'contacts' |
| `where` | string? | null | Filter expression (e.g., 'Type=="BANK"') |
| `order` | string? | null | Sort order (e.g., 'Name ASC') |
| `ids` | string? | null | Comma-separated IDs to fetch specific records. |
| `include_archived` | boolean | false | If true, includes archived records. Default: false. |
| `page` | integer? | null | Page number (1-indexed) |

---

### 2. `xero_transactions`

Retrieve Xero transactional data.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'invoices', 'payments', 'bank_transactions', 'journals', 'bank_transfers', 'credit_notes', 'prepayments', 'overpayments', 'quotes', 'purchase_orders'] | Ellipsis | Action to perform |
| `where` | string? | null | Filter expression |
| `ids` | string? | null | Comma-separated IDs to fetch specific records. |
| `statuses` | string? | null | Comma-separated statuses (e.g., 'DRAFT,AUTHORISED') |
| `page` | integer? | null | Page number (1-indexed) |
| `unitdp` | integer? | null | Decimal places: 2 (default) or 4. |
| `offset` | integer? | null | Records to skip. Use: offset = (page-1) \* limit. |
| `payments_only` | boolean? | null | If true, returns only payment-related entries. |

---

### 3. `xero_reports`

Generate Xero financial reports.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'balance_sheet', 'profit_loss', 'aged_receivables', 'aged_payables', 'budget_summary', 'budgets', 'executive_summary'] | Ellipsis | Report type to generate |
| `date` | string? | null | Target date (YYYY-MM-DD). REQUIRED for point-in-time operations. |
| `from_date` | string? | null | Start date (YYYY-MM-DD). Beginning of date range. |
| `to_date` | string? | null | End date (YYYY-MM-DD). Defaults to today if omitted. |
| `periods` | integer? | null | Number of comparison periods. For variance reports. |
| `timeframe` | string? | null | Period granularity: MONTH, QUARTER, or YEAR. |
| `tracking_categories` | string? | null | Comma-separated tracking category IDs |
| `contact_id` | string? | null | Contact ID (required for aged_receivables/aged_payables) |

---

### 4. `xero_assets`

Manage Xero fixed assets.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'list', 'types'] | Ellipsis | Action: 'help', 'list', 'types' |
| `status` | string? | null | Filter by status. Use action=help to see valid values. |
| `page` | integer? | null | Page number |
| `page_size` | integer? | null | Items per page (max 200) |

---

### 5. `xero_files`

Access Xero file storage.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'list', 'folders', 'associations'] | Ellipsis | Action: 'help', 'list', 'folders', 'associations' |
| `page` | integer? | null | Page number |
| `page_size` | integer? | null | Items per page |
| `sort` | string? | null | Sort field name. |
| `file_id` | string? | null | File UUID. REQUIRED for file operations. |

---

### 6. `xero_admin`

Administrative operations and project management.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'projects', 'project_time', 'reset_state', 'server_info'] | Ellipsis | Action to perform |
| `contact_id` | string? | null | Contact UUID. REQUIRED for contact-specific operations. |
| `states` | string? | null | Comma-separated project states |
| `page` | integer? | null | Page number |
| `page_size` | integer? | null | Items per page |
| `project_id` | string? | null | Project identifier. REQUIRED for project_time action. |

---

### 7. `xero_data`

Upload and manage Xero data via CSV (offline mode only).

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'upload_accounts', 'upload_contacts', 'upload_invoices', 'upload_payments', 'upload_bank_transactions', 'upload_purchase_orders', 'upload_journals'] | Ellipsis | Action: 'help' or 'upload\_<entity_type>' |
| `csv_content` | string? | null | CSV content with headers (required for upload actions) |
| `merge_mode` | enum['append', 'replace'] | `"append"` | Merge mode: 'append' to add/update records, 'replace' to clear and reload |

---

### 8. `xero_schema`

Get JSON schema for any Xero tool's input/output.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `tool` | enum['xero_entities', 'xero_transactions', 'xero_reports', 'xero_assets', 'xero_files', 'xero_admin', 'xero_data'] | Ellipsis | Tool name to get schema for |
| `action` | string? | null | The operation to perform. REQUIRED. Call with action='help' first. |

---

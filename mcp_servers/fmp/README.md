# Fmp MCP Server

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

### 1. `get_analyst_estimates`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `period` | str | _required_ | Frequency of data: |

---

### 2. `get_ratings_snapshot`

Quickly assess financial health and performance.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | Yes | Stock ticker symbol (e.g., "AAPL") |

---

### 3. `get_ratings_historical`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |

---

### 4. `get_price_target_summary`

Gain insights into analysts' expectations for stock prices.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | Yes | Stock ticker symbol (e.g., "AAPL") |

---

### 5. `get_price_target_consensus`

Access analysts' consensus price targets.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | Yes | Stock ticker symbol (e.g., "AAPL") |

---

### 6. `get_price_target_news`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |

---

### 7. `get_price_target_latest_news`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `page` | int? | null | Page number (0-indexed, first page is 0) |

---

### 8. `get_stock_grades`

Get stock analyst grades and recommendations.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | Yes | Stock ticker symbol (e.g., "AAPL") |

---

### 9. `get_grades_historical`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |

---

### 10. `get_grades_consensus`

Get consensus analyst grades.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | Yes | Stock ticker symbol (e.g., "AAPL") |

---

### 11. `get_grade_news`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |

---

### 12. `get_grade_latest_news`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `page` | int? | null | Page number (0-indexed, first page is 0) |

---

### 13. `get_historical_price_light`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `from_date` | str? | null | Start date in YYYY-MM-DD format (e.g., |
| `to_date` | str? | null | End date in YYYY-MM-DD format (e.g., |

---

### 14. `get_historical_price_full`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `from_date` | str? | null | Start date in YYYY-MM-DD format (e.g., |
| `to_date` | str? | null | End date in YYYY-MM-DD format (e.g., |

---

### 15. `get_historical_price_unadjusted`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `from_date` | str? | null | Start date in YYYY-MM-DD format (e.g., |
| `to_date` | str? | null | End date in YYYY-MM-DD format (e.g., |

---

### 16. `get_historical_price_dividend_adjusted`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `from_date` | str? | null | Start date in YYYY-MM-DD format (e.g., |
| `to_date` | str? | null | End date in YYYY-MM-DD format (e.g., |

---

### 17. `get_intraday_1min`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `from_date` | str? | null | Start date in YYYY-MM-DD format (e.g., |
| `to_date` | str? | null | End date in YYYY-MM-DD format (e.g., |

---

### 18. `get_intraday_5min`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `from_date` | str? | null | Start date in YYYY-MM-DD format (e.g., |
| `to_date` | str? | null | End date in YYYY-MM-DD format (e.g., |

---

### 19. `get_intraday_15min`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `from_date` | str? | null | Start date in YYYY-MM-DD format (e.g., |
| `to_date` | str? | null | End date in YYYY-MM-DD format (e.g., |

---

### 20. `get_intraday_30min`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `from_date` | str? | null | Start date in YYYY-MM-DD format (e.g., |
| `to_date` | str? | null | End date in YYYY-MM-DD format (e.g., |

---

### 21. `get_intraday_1hour`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `from_date` | str? | null | Start date in YYYY-MM-DD format (e.g., |
| `to_date` | str? | null | End date in YYYY-MM-DD format (e.g., |

---

### 22. `get_intraday_4hour`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `from_date` | str? | null | Start date in YYYY-MM-DD format (e.g., |
| `to_date` | str? | null | End date in YYYY-MM-DD format (e.g., |

---

### 23. `get_commodities_list`

Get list of available commodities.

**Parameters:** None (returns all available commodities)

---

### 24. `get_company_profile`

Access detailed company profile data.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | Yes | Stock ticker symbol (e.g., "AAPL") |

---

### 25. `get_profile_by_cik`

Get company profile by CIK number.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `cik` | string | Yes | SEC CIK number |

---

### 26. `get_company_notes`

Get company notes and filings.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | Yes | Stock ticker symbol (e.g., "AAPL") |

---

### 27. `get_stock_peers`

Identify and compare companies within the same sector.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | Yes | Stock ticker symbol (e.g., "AAPL") |

---

### 28. `get_delisted_companies`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `page` | int | 0 | Page number (0-indexed, first page is 0) |

---

### 29. `get_employee_count`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |

---

### 30. `get_historical_employee_count`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |

---

### 31. `get_market_cap`

Retrieve the market capitalization for a specific company.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | Yes | Stock ticker symbol (e.g., "AAPL") |

---

### 32. `get_batch_market_cap`

Get market capitalization for multiple symbols.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbols` | string | Yes | Comma-separated list of ticker symbols |

---

### 33. `get_historical_market_cap`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `limit` | int? | null | Maximum number of data points to return (default: 100) |
| `from_date` | str? | null | Start date in YYYY-MM-DD format (e.g., |

---

### 34. `get_shares_float`

Get shares float for a company.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | Yes | Stock ticker symbol (e.g., "AAPL") |

---

### 35. `get_all_shares_float`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `page` | int | 0 | Page number (0-indexed, first page is 0) |

---

### 36. `get_latest_mergers_acquisitions`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `page` | int | 0 | Page number (0-indexed, first page is 0) |

---

### 37. `search_ma`

Search mergers and acquisitions.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `name` | string | No | Company name to search |

---

### 38. `get_company_executives`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol |

---

### 39. `get_executive_compensation`

Get executive compensation data.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | Yes | Stock ticker symbol (e.g., "AAPL") |

---

### 40. `get_executive_comp_benchmark`

Get executive compensation benchmarks.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `year` | integer | No | Year for benchmark data |

---

### 41. `search_by_symbol`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `query` | str | _required_ | Stock symbol or partial symbol (e.g., |
| `limit` | int? | null | Maximum number of results (default: 50, max: 100) |

---

### 42. `search_by_company_name`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `query` | str | _required_ | Company name or partial name (e.g., |
| `limit` | int? | null | Maximum number of results (default: 50, max: 100) |

---

### 43. `search_by_cik`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `cik` | str | _required_ | CIK (Central Index Key) - SEC |

---

### 44. `search_by_cusip`

Search companies by CUSIP identifier.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `cusip` | string | Yes | CUSIP identifier |

---

### 45. `search_by_isin`

Search companies by ISIN identifier.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `isin` | string | Yes | ISIN identifier |

---

### 46. `screen_stocks`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `market_cap_more_than` | float? | null | Minimum market cap in millions (e.g., 1000.0 for $1 billion market cap) |
| `market_cap_lower_than` | float? | null | Maximum market cap in millions (e.g., 5000.0 for $5 billion market cap) |
| `price_more_than` | float? | null | Minimum stock price in USD (e.g., 10.50) |
| `price_lower_than` | float? | null | Maximum stock price in USD (e.g., 100.00) |
| `beta_more_than` | float? | null | Minimum beta value (e.g., 1.0 for stocks with market-level volatility) |
| `beta_lower_than` | float? | null | Maximum beta value (e.g., 0.5 for low-volatility stocks) |
| `volume_more_than` | int? | null | Minimum average trading volume (e.g., 1000000 for 1M shares) |
| `volume_lower_than` | int? | null | Maximum average trading volume |
| `dividend_more_than` | float? | null | Minimum dividend yield percentage (e.g., 2.0 for 2%) |
| `dividend_lower_than` | float? | null | Maximum dividend yield percentage |
| `is_etf` | bool? | null | Filter for ETFs only (true) or exclude ETFs (false) |
| `is_fund` | bool? | null | Filter for funds only (true) or exclude funds (false) |
| `is_actively_trading` | bool? | null | Filter for actively trading stocks only |
| `sector` | str? | null | Sector filter (e.g., |
| `industry` | str? | null | Industry filter (e.g., |
| `country` | str? | null | Country code (e.g., |
| `exchange` | str? | null | Exchange (e.g., |

---

### 47. `find_exchange_listings`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `exchange` | str | _required_ | Exchange code to find listings for (e.g., |

---

### 48. `get_house_disclosure`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `page` | int | 0 | Page number (0-indexed, first page is 0) |

---

### 49. `get_senate_disclosure`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `page` | int | 0 | Page number (0-indexed, first page is 0) |

---

### 50. `get_senate_trades`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `page` | int | 0 | Page number (0-indexed, first page is 0) |

---

### 51. `get_house_trades`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `page` | int | 0 | Page number (0-indexed, first page is 0) |

---

### 52. `get_cryptocurrency_list`

Get list of available cryptocurrencies.

**Parameters:** None (returns all available cryptocurrencies)

---

### 53. `get_dcf_valuation`

Get DCF (Discounted Cash Flow) valuation.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | Yes | Stock ticker symbol (e.g., "AAPL") |

---

### 54. `get_levered_dcf_valuation`

Get levered DCF valuation.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | Yes | Stock ticker symbol (e.g., "AAPL") |

---

### 55. `get_custom_dcf_valuation`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `revenue_growth_pct` | float? | null | Expected revenue growth rate as decimal (e.g., 0.10 for 10%) |
| `ebitda_pct` | float? | null | EBITDA margin as decimal (e.g., 0.25 for 25%) |
| `tax_rate` | float? | null | Corporate tax rate as decimal (e.g., 0.21 for 21%) |
| `long_term_growth_rate` | float? | null | Terminal growth rate as decimal (e.g., 0.025 for 2.5%) |
| `cost_of_debt` | float? | null | Cost of debt as decimal (e.g., 0.05 for 5%) |
| `cost_of_equity` | float? | null | Cost of equity as decimal (e.g., 0.10 for 10%) |
| `beta` | float? | null | Stock beta relative to market (e.g., 1.2 means 20% more volatile) |

---

### 56. `get_custom_levered_dcf_valuation`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `revenue_growth_pct` | float? | null | Expected revenue growth rate as decimal (e.g., 0.10 for 10%) |
| `ebitda_pct` | float? | null | EBITDA margin as decimal (e.g., 0.25 for 25%) |
| `tax_rate` | float? | null | Corporate tax rate as decimal (e.g., 0.21 for 21%) |
| `long_term_growth_rate` | float? | null | Terminal growth rate as decimal (e.g., 0.025 for 2.5%) |
| `cost_of_debt` | float? | null | Cost of debt as decimal (e.g., 0.05 for 5%) |
| `cost_of_equity` | float? | null | Cost of equity as decimal (e.g., 0.10 for 10%) |
| `beta` | float? | null | Stock beta relative to market (e.g., 1.2 means 20% more volatile) |

---

### 57. `get_company_dividends`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |

---

### 58. `get_dividends_calendar`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `from_date` | str? | null | Start date (YYYY-MM-DD) |
| `to_date` | str? | null | End date (YYYY-MM-DD, max 90-day range) |

---

### 59. `get_company_earnings`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |

---

### 60. `get_earnings_calendar`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `from_date` | str? | null | Start date (YYYY-MM-DD) |
| `to_date` | str? | null | End date (YYYY-MM-DD, max 90-day range) |

---

### 61. `get_ipos_calendar`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `from_date` | str? | null | Start date (YYYY-MM-DD) |
| `to_date` | str? | null | End date (YYYY-MM-DD, max 90-day range) |

---

### 62. `get_ipos_disclosure`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `from_date` | str? | null | Start date (YYYY-MM-DD) |

---

### 63. `get_ipos_prospectus`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `from_date` | str? | null | Start date (YYYY-MM-DD) |

---

### 64. `get_stock_splits`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |

---

### 65. `get_splits_calendar`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `from_date` | str? | null | Start date (YYYY-MM-DD) |
| `to_date` | str? | null | End date (YYYY-MM-DD, max 90-day range) |

---

### 66. `get_latest_earning_transcripts`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `limit` | int? | null | Maximum results per page (default: 20) |

---

### 67. `get_earning_call_transcript`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `year` | str | _required_ | Fiscal year as 4-digit string (e.g., |
| `quarter` | str | _required_ | Fiscal quarter as single digit: |

---

### 68. `get_transcript_dates_by_symbol`

Get available transcript dates for a symbol.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | Yes | Stock ticker symbol (e.g., "AAPL") |

---

### 69. `get_treasury_rates`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `from_date` | str? | null | Start date (YYYY-MM-DD) |

---

### 70. `get_economic_indicators`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `name` | str | _required_ | Indicator name (e.g., |
| `from_date` | str? | null | Start date (YYYY-MM-DD) |

---

### 71. `get_economic_calendar`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `from_date` | str? | null | Start date (YYYY-MM-DD) |
| `to_date` | str? | null | End date (YYYY-MM-DD, max 90-day range) |

---

### 72. `get_market_risk_premium`

Get market risk premium data.

**Parameters:** None (returns current market risk premium)

---

### 73. `get_etf_holdings`

Get ETF holdings.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | Yes | ETF ticker symbol (e.g., "SPY") |

---

### 74. `get_etf_info`

Get ETF information.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | Yes | ETF ticker symbol (e.g., "SPY") |

---

### 75. `get_etf_country_weightings`

Get ETF country weightings.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | Yes | ETF ticker symbol (e.g., "SPY") |

---

### 76. `get_etf_asset_exposure`

Get ETF asset exposure breakdown.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | Yes | ETF ticker symbol (e.g., "SPY") |

---

### 77. `get_etf_sector_weightings`

Get ETF sector weightings.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | Yes | ETF ticker symbol (e.g., "SPY") |

---

### 78. `get_fund_disclosure_holders_latest`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |

---

### 79. `get_fund_disclosure`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Fund/ETF ticker symbol in uppercase (e.g., |
| `year` | str | _required_ | Fiscal year as 4-digit string (e.g., |
| `quarter` | str | _required_ | Fiscal quarter as single digit: |

---

### 80. `search_fund_disclosure_by_name`

Search fund disclosures by name.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `name` | string | Yes | Fund name to search |

---

### 81. `get_fund_disclosure_dates`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Fund/ETF symbol |

---

### 82. `get_income_statement`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `period` | str | _required_ | Frequency of data: |

---

### 83. `get_balance_sheet`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `period` | str | _required_ | Frequency of data: |

---

### 84. `get_cash_flow_statement`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `period` | str | _required_ | Frequency of data: |

---

### 85. `get_latest_financials`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `page` | int | 0 | Page number (0-indexed, first page is 0) |

---

### 86. `get_income_statement_ttm`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |

---

### 87. `get_balance_sheet_ttm`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |

---

### 88. `get_cash_flow_ttm`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |

---

### 89. `get_key_metrics`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `period` | str | _required_ | Frequency of data: |

---

### 90. `get_financial_ratios`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `period` | str | _required_ | Frequency of data: |

---

### 91. `get_key_metrics_ttm`

Get trailing twelve months key metrics.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | Yes | Stock ticker symbol (e.g., "AAPL") |

---

### 92. `get_ratios_ttm`

Get trailing twelve months financial ratios.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | Yes | Stock ticker symbol (e.g., "AAPL") |

---

### 93. `get_financial_scores`

Get financial scores (Altman Z-Score, Piotroski Score).

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | Yes | Stock ticker symbol (e.g., "AAPL") |

---

### 94. `get_owner_earnings`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |

---

### 95. `get_enterprise_values`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `period` | str | _required_ | Frequency of data: |

---

### 96. `get_income_growth`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `period` | str | _required_ | Frequency of data: |

---

### 97. `get_balance_sheet_growth`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `period` | str | _required_ | Frequency of data: |

---

### 98. `get_cash_flow_growth`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `period` | str | _required_ | Frequency of data: |

---

### 99. `get_financial_growth`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `period` | str | _required_ | Frequency of data: |

---

### 100. `get_revenue_by_product`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `period` | str | _required_ | Frequency of data: |

---

### 101. `get_revenue_by_geography`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `period` | str | _required_ | Frequency of data: |

---

### 102. `get_income_as_reported`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `period` | str | _required_ | Frequency of data: |

---

### 103. `get_balance_sheet_as_reported`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `period` | str | _required_ | Frequency of data: |

---

### 104. `get_cash_flow_as_reported`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `period` | str | _required_ | Frequency of data: |

---

### 105. `get_full_financials_as_reported`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `period` | str | _required_ | Frequency of data: |

---

### 106. `get_financial_reports_dates`

Get available financial report dates for a company.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | Yes | Stock ticker symbol (e.g., "AAPL") |

---

### 107. `get_financial_report_json`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `year` | str | _required_ | Fiscal year as 4-digit string (e.g., |

---

### 108. `get_financial_report_xlsx`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `year` | str | _required_ | Fiscal year as 4-digit string (e.g., |

---

### 109. `get_forex_currency_pairs`

Get list of available forex currency pairs.

**Parameters:** None (returns all forex pairs)

---

### 110. `get_index_list`

Get list of available market indices.

**Parameters:** None (returns all indices)

---

### 111. `get_sp500_constituents`

Get S&P 500 index constituents.

**Parameters:** None (returns current constituents)

---

### 112. `get_nasdaq_constituents`

Get NASDAQ index constituents.

**Parameters:** None (returns current constituents)

---

### 113. `get_dowjones_constituents`

Get Dow Jones index constituents.

**Parameters:** None (returns current constituents)

---

### 114. `get_historical_sp500`

Get historical S&P 500 constituent changes.

**Parameters:** None (returns historical changes)

---

### 115. `get_historical_nasdaq`

Get historical NASDAQ constituent changes.

**Parameters:** None (returns historical changes)

---

### 116. `get_historical_dowjones`

Get historical Dow Jones constituent changes.

**Parameters:** None (returns historical changes)

---

### 117. `get_exchange_market_hours`

Get market hours for a specific exchange.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `exchange` | string | Yes | Exchange code (e.g., "NYSE") |

---

### 118. `get_holidays_by_exchange`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `exchange` | str | _required_ | Exchange code (e.g., |
| `from_date` | str? | null | Start date (YYYY-MM-DD) |

---

### 119. `get_all_exchange_market_hours`

Get market hours for all exchanges.

**Parameters:** None (returns all exchange hours)

---

### 120. `get_sector_performance_snapshot`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `date` | str | _required_ | Date in YYYY-MM-DD format |
| `exchange` | str? | null | Exchange filter (e.g., |

---

### 121. `get_industry_performance_snapshot`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `date` | str | _required_ | Date in YYYY-MM-DD format |
| `exchange` | str? | null | Exchange filter (e.g., |

---

### 122. `get_historical_sector_performance`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `sector` | str | _required_ | Sector name (e.g., |
| `exchange` | str? | null | Exchange filter (e.g., |
| `from_date` | str? | null | Start date (YYYY-MM-DD) |

---

### 123. `get_historical_industry_performance`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `industry` | str | _required_ | Industry name (e.g., |
| `exchange` | str? | null | Exchange filter (e.g., |
| `from_date` | str? | null | Start date (YYYY-MM-DD) |

---

### 124. `get_sector_pe_snapshot`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `date` | str | _required_ | Date in YYYY-MM-DD format |
| `exchange` | str? | null | Exchange filter (e.g., |

---

### 125. `get_industry_pe_snapshot`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `date` | str | _required_ | Date in YYYY-MM-DD format |
| `exchange` | str? | null | Exchange filter (e.g., |

---

### 126. `get_historical_sector_pe`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `sector` | str | _required_ | Sector name (e.g., |
| `exchange` | str? | null | Exchange filter (e.g., |
| `from_date` | str? | null | Start date (YYYY-MM-DD) |

---

### 127. `get_historical_industry_pe`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `industry` | str | _required_ | Industry name (e.g., |
| `exchange` | str? | null | Exchange filter (e.g., |
| `from_date` | str? | null | Start date (YYYY-MM-DD) |

---

### 128. `get_biggest_gainers`

Get stocks with biggest gains today.

**Parameters:** None (returns top gainers)

---

### 129. `get_biggest_losers`

Get stocks with biggest losses today.

**Parameters:** None (returns top losers)

---

### 130. `get_most_actives`

Get most actively traded stocks today.

**Parameters:** None (returns most active stocks)

---

### 131. `get_fmp_articles`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `page` | int? | null | Page number (default: 0) |

---

### 132. `get_general_news_latest`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `from_date` | str? | null | Start date (YYYY-MM-DD) |
| `to_date` | str? | null | End date (YYYY-MM-DD) |
| `page` | int? | null | Page number |

---

### 133. `get_press_releases_latest`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `from_date` | str? | null | Start date (YYYY-MM-DD) |
| `to_date` | str? | null | End date (YYYY-MM-DD) |
| `page` | int? | null | Page number |

---

### 134. `get_stock_news_latest`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `from_date` | str? | null | Start date (YYYY-MM-DD) |
| `to_date` | str? | null | End date (YYYY-MM-DD) |
| `page` | int? | null | Page number |

---

### 135. `get_crypto_news_latest`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `from_date` | str? | null | Start date (YYYY-MM-DD) |
| `to_date` | str? | null | End date (YYYY-MM-DD) |
| `page` | int? | null | Page number |

---

### 136. `get_forex_news_latest`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `from_date` | str? | null | Start date (YYYY-MM-DD) |
| `to_date` | str? | null | End date (YYYY-MM-DD) |
| `page` | int? | null | Page number |

---

### 137. `search_press_releases_by_symbol`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbols` | str | _required_ | Symbol(s) to search for (e.g., |
| `from_date` | str? | null | Start date (YYYY-MM-DD) |
| `to_date` | str? | null | End date (YYYY-MM-DD) |
| `page` | int? | null | Page number |

---

### 138. `search_stock_news_by_symbol`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbols` | str | _required_ | Symbol(s) to search for (e.g., |
| `from_date` | str? | null | Start date (YYYY-MM-DD) |
| `to_date` | str? | null | End date (YYYY-MM-DD) |
| `page` | int? | null | Page number |

---

### 139. `search_crypto_news_by_symbol`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbols` | str | _required_ | Symbol(s) to search for (e.g., |
| `from_date` | str? | null | Start date (YYYY-MM-DD) |
| `to_date` | str? | null | End date (YYYY-MM-DD) |
| `page` | int? | null | Page number |

---

### 140. `search_forex_news_by_symbol`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbols` | str | _required_ | Symbol(s) to search for (e.g., |
| `from_date` | str? | null | Start date (YYYY-MM-DD) |
| `to_date` | str? | null | End date (YYYY-MM-DD) |
| `page` | int? | null | Page number |

---

### 141. `get_stock_quote`

Get real-time stock quote.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | Yes | Stock ticker symbol (e.g., "AAPL") |

---

### 142. `get_stock_quote_short`

Get abbreviated stock quote.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | Yes | Stock ticker symbol (e.g., "AAPL") |

---

### 143. `get_aftermarket_trade`

Get aftermarket trade data.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | Yes | Stock ticker symbol (e.g., "AAPL") |

---

### 144. `get_aftermarket_quote`

Get aftermarket quote data.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | Yes | Stock ticker symbol (e.g., "AAPL") |

---

### 145. `get_stock_price_change`

Get stock price change data.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbol` | string | Yes | Stock ticker symbol (e.g., "AAPL") |

---

### 146. `get_batch_stock_quotes`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbols` | str | _required_ | Comma-separated stock ticker symbols (e.g., |

---

### 147. `get_batch_stock_quotes_short`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbols` | str | _required_ | Comma-separated stock ticker symbols (e.g., |

---

### 148. `get_batch_aftermarket_trades`

Get batch aftermarket trade data for multiple symbols.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `symbols` | string | Yes | Comma-separated list of stock ticker symbols (e.g., "AAPL,MSFT,GOOGL") |

---

### 149. `get_batch_aftermarket_quotes`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbols` | str | _required_ | Comma-separated stock ticker symbols (e.g., |

---

### 150. `get_exchange_stock_quotes`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `exchange` | str | _required_ | Exchange code (e.g., |
| `short` | bool? | null | Return short format (default: false) |

---

### 151. `get_all_mutualfund_quotes`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `short` | bool? | null | Return short format (default: false) |

---

### 152. `get_all_etf_quotes`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `short` | bool? | null | Return short format (default: false) |

---

### 153. `get_all_commodity_quotes`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `short` | bool? | null | Return short format (default: false) |

---

### 154. `get_all_crypto_quotes`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `short` | bool? | null | Return short format (default: false) |

---

### 155. `get_all_forex_quotes`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `short` | bool? | null | Return short format (default: false) |

---

### 156. `get_all_index_quotes`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `short` | bool? | null | Return short format (default: false) |

---

### 157. `get_institutional_ownership`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `page` | int | 0 | Page number (0-indexed, first page is 0) |
| `limit` | int? | null | Maximum number of results to return (default: 50, max: 100) |

---

### 158. `get_8k_filings`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `page` | int | 0 | Page number (0-indexed, first page is 0) |
| `limit` | int? | null | Maximum number of results to return (default: 50, max: 100) |

---

### 159. `get_all_stock_symbols`

Get comprehensive list of available stock symbols from global exchanges.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `limit` | int | No | Maximum number of results (default: 50, max: 100) |

---

### 160. `get_stocks_with_financials`

Get list of companies that have financial statements available.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `limit` | int | No | Maximum number of results (default: 50, max: 100) |

---

### 161. `get_cik_database`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `page` | int | 0 | Page number (0-indexed, first page is 0) |

---

### 162. `get_recent_symbol_changes`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `invalid` | bool? | null | Filter to show only invalid symbols |

---

### 163. `get_all_etfs`

Get complete list of Exchange Traded Funds (ETFs) with ticker symbols and fund names.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `limit` | int | No | Maximum number of results (default: 50, max: 100) |

---

### 164. `get_actively_trading_stocks`

Get list of actively trading companies currently being traded on public exchanges.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `limit` | int | No | Maximum number of results (default: 50, max: 100) |

---

### 165. `get_companies_with_transcripts`

Get list of companies that have earnings call transcripts available.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `limit` | int | No | Maximum number of results (default: 50, max: 100) |

---

### 166. `get_supported_exchanges`

Get complete list of all supported stock exchanges worldwide.

**Parameters:** None

---

### 167. `get_all_sectors`

Get list of all industry sectors.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `limit` | int | No | Maximum number of results (default: 50, max: 100) |

---

### 168. `get_all_industries`

Get comprehensive list of all industries where stocks are available.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `limit` | int | No | Maximum number of results (default: 50, max: 100) |

---

### 169. `get_all_countries`

Get list of all countries where stock symbols are available.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `limit` | int | No | Maximum number of results (default: 50, max: 100) |

---

### 170. `get_sma`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `periodLength` | int | _required_ | Number of periods for the indicator (e.g., 14 for RSI, 20 for SMA) |
| `timeframe` | str | _required_ | Chart timeframe: |
| `from_date` | str? | null | Start date in YYYY-MM-DD format (e.g., |

---

### 171. `get_ema`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `periodLength` | int | _required_ | Number of periods for the indicator (e.g., 14 for RSI, 20 for SMA) |
| `timeframe` | str | _required_ | Chart timeframe: |
| `from_date` | str? | null | Start date in YYYY-MM-DD format (e.g., |

---

### 172. `get_wma`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `periodLength` | int | _required_ | Number of periods for the indicator (e.g., 14 for RSI, 20 for SMA) |
| `timeframe` | str | _required_ | Chart timeframe: |
| `from_date` | str? | null | Start date in YYYY-MM-DD format (e.g., |

---

### 173. `get_dema`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `periodLength` | int | _required_ | Number of periods for the indicator (e.g., 14 for RSI, 20 for SMA) |
| `timeframe` | str | _required_ | Chart timeframe: |
| `from_date` | str? | null | Start date in YYYY-MM-DD format (e.g., |

---

### 174. `get_tema`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `periodLength` | int | _required_ | Number of periods for the indicator (e.g., 14 for RSI, 20 for SMA) |
| `timeframe` | str | _required_ | Chart timeframe: |
| `from_date` | str? | null | Start date in YYYY-MM-DD format (e.g., |

---

### 175. `get_rsi`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `periodLength` | int | _required_ | Number of periods for the indicator (e.g., 14 for RSI, 20 for SMA) |
| `timeframe` | str | _required_ | Chart timeframe: |
| `from_date` | str? | null | Start date in YYYY-MM-DD format (e.g., |

---

### 176. `get_standard_deviation`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `periodLength` | int | _required_ | Number of periods for the indicator (e.g., 14 for RSI, 20 for SMA) |
| `timeframe` | str | _required_ | Chart timeframe: |
| `from_date` | str? | null | Start date in YYYY-MM-DD format (e.g., |

---

### 177. `get_williams`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `periodLength` | int | _required_ | Number of periods for the indicator (e.g., 14 for RSI, 20 for SMA) |
| `timeframe` | str | _required_ | Chart timeframe: |
| `from_date` | str? | null | Start date in YYYY-MM-DD format (e.g., |

---

### 178. `get_adx`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `symbol` | str | _required_ | Stock ticker symbol in uppercase (e.g., |
| `periodLength` | int | _required_ | Number of periods for the indicator (e.g., 14 for RSI, 20 for SMA) |
| `timeframe` | str | _required_ | Chart timeframe: |
| `from_date` | str? | null | Start date in YYYY-MM-DD format (e.g., |

---

## Consolidated Tools

When using consolidated mode, these meta-tools combine multiple operations:

### 1. `fmp_analyst`

Analyst ratings, estimates, grades, and price targets.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'estimates', 'ratings_snapshot', 'ratings_historical', 'price_target_summary', 'price_target_consensus', 'price_target_news', 'price_target_latest', 'grades', 'grades_historical', 'grades_consensus', 'grade_news', 'grade_latest'] | Ellipsis | Action to perform |
| `symbol` | string? | null | Stock ticker symbol (e.g., "AAPL", "MSFT"). |
| `period` | string? | null | Reporting period. REQUIRED for "estimates" action. |
| `limit` | integer? | null | Max results to return. Typical range: 1-100. |
| `page` | integer? | null | Page number (0-indexed). Use with limit for pagination. |

---

### 2. `fmp_prices`

Stock quotes, historical prices, and intraday data.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'quote', 'quote_short', 'price_change', 'aftermarket_quote', 'aftermarket_trade', 'batch_quotes', 'batch_quotes_short', 'batch_aftermarket_trades', 'batch_aftermarket_quotes', 'exchange_quotes', 'historical_light', 'historical_full', 'historical_unadjusted', 'historical_dividend_adjusted', 'intraday_1min', 'intraday_5min', 'intraday_15min', 'intraday_30min', 'intraday_1hour', 'intraday_4hour'] | Ellipsis | Action to perform |
| `symbol` | string? | null | Single stock ticker symbol. REQUIRED for most actions. |
| `symbols` | string? | null | Comma-separated stock symbols. ONLY for batch\_\* actions. |
| `exchange` | string? | null | Exchange code for exchange_quotes action. Values: "NASDAQ", "NYSE", "AMEX" |
| `from_date` | string? | null | Start date (YYYY-MM-DD). Beginning of date range. |
| `to_date` | string? | null | End date (YYYY-MM-DD). Defaults to today if omitted. |
| `limit` | integer? | null | Max results to return. Typical range: 1-100. |
| `short` | boolean? | null | If true, returns condensed format. |

---

### 3. `fmp_company`

Company profiles, search, and stock directory.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'profile', 'notes', 'executives', 'peers', 'executive_compensation', 'compensation_benchmark', 'share_float', 'employee_count', 'grades', 'revenue_geography', 'revenue_product', 'search_symbol', 'search_name', 'search_cik', 'search_cusip', 'search_isin', 'screener', 'list_exchange', 'list_symbols', 'list_tradeable', 'list_etf', 'list_sp500', 'list_nasdaq', 'list_dow', 'list_index', 'list_delisted', 'list_cik', 'list_statement_symbols'] | Ellipsis | Action to perform |
| `symbol` | string? | null | Stock ticker symbol (e.g., "AAPL", "MSFT"). |
| `query` | string? | null | Search text for search_symbol, search_name actions. |
| `exchange` | string? | null | Exchange code. Values: "NASDAQ", "NYSE", "AMEX", "LSE", "TSX", etc. |
| `sector` | string? | null | Sector filter for screener action. |
| `industry` | string? | null | Industry filter for screener (e.g., "Software", "Biotechnology", "Banks"). |
| `country` | string? | null | ISO country code for screener. Values: "US", "CA", "GB", "DE", "FR". |
| `market_cap_min` | number? | null | Minimum market cap in millions USD for screener (e.g., 1000 = $1B). |
| `market_cap_max` | number? | null | Maximum market cap in millions USD for screener (e.g., 10000 = $10B). |
| `limit` | integer? | null | Max results to return. Typical range: 1-100. |
| `year` | integer? | null | Four-digit year (e.g., 2024). REQUIRED for annual reports. |

---

### 4. `fmp_financials`

Financial statements, valuations, earnings, and dividends.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'income_statement', 'balance_sheet', 'cash_flow', 'income_growth', 'balance_growth', 'cash_flow_growth', 'financial_growth', 'key_metrics', 'key_metrics_ttm', 'ratios', 'ratios_ttm', 'financial_score', 'owner_earnings', 'enterprise_value', 'dcf', 'levered_dcf', 'dividend_historical', 'dividend_calendar', 'splits_historical', 'splits_calendar', 'earnings_calendar', 'earnings_historical', 'transcript', 'transcript_dates'] | Ellipsis | Action to perform |
| `symbol` | string? | null | Stock ticker symbol (e.g., "AAPL", "MSFT"). |
| `symbols` | string? | null | Comma-separated symbols. ONLY used for batch operations. |
| `period` | string? | null | Reporting period. REQUIRED for financial statements. |
| `limit` | integer? | null | Max results to return. Typical range: 1-100. |
| `year` | integer? | null | Four-digit year (e.g., 2024). REQUIRED for transcript action. |
| `quarter` | integer? | null | Quarter number (1, 2, 3, or 4). REQUIRED for transcript action. |
| `from_date` | string? | null | Start date (YYYY-MM-DD). For calendar actions. |
| `to_date` | string? | null | End date (YYYY-MM-DD). Defaults to today if omitted. |

---

### 5. `fmp_market`

Market performance, indexes, and economic data.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'gainers', 'losers', 'most_active', 'sector_performance', 'sector_historical', 'industry_performance', 'industry_historical', 'market_hours', 'exchange_hours', 'exchange_holidays', 'index_list', 'sp500_constituents', 'nasdaq_constituents', 'dow_constituents', 'treasury_rates', 'economic_indicators', 'economic_calendar', 'market_risk_premium'] | Ellipsis | Action to perform |
| `sector` | string? | null | Sector name for sector_performance/sector_historical. |
| `industry` | string? | null | Specific industry within sector (e.g., "Software", "Biotechnology"). |
| `symbol` | string? | null | Index symbol (if applicable) |
| `indicator` | string? | null | Economic indicator name for economic_indicators action. |
| `date` | string? | null | Target date (YYYY-MM-DD). REQUIRED for sector/industry performance. |
| `from_date` | string? | null | Start date (YYYY-MM-DD). For historical ranges. |
| `to_date` | string? | null | End date (YYYY-MM-DD). Defaults to today if omitted. |
| `exchange` | string? | null | Exchange for exchange_hours/holidays. Values: "NASDAQ", "NYSE", "LSE". |

---

### 6. `fmp_assets`

ETFs, mutual funds, commodities, crypto, and forex.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'etf_list', 'etf_profile', 'etf_holdings', 'etf_sector_weightings', 'etf_country_weightings', 'etf_exposure', 'mutual_fund_search', 'fund_disclosure', 'crypto_list', 'commodity_list', 'forex_list', 'all_etf_quotes', 'all_mutualfund_quotes', 'all_commodity_quotes', 'all_crypto_quotes', 'all_forex_quotes', 'all_index_quotes', 'ipo_calendar'] | Ellipsis | Action to perform |
| `symbol` | string? | null | Asset symbol |
| `query` | string? | null | Search text. Matches names, descriptions. Case-insensitive. |
| `limit` | integer? | null | Max results to return. Typical range: 1-100. |
| `short` | boolean? | null | If true, returns condensed format. |
| `from_date` | string? | null | Start date (YYYY-MM-DD). Beginning of date range. |
| `to_date` | string? | null | End date (YYYY-MM-DD). Defaults to today if omitted. |
| `year` | string? | null | Four-digit year (e.g., 2024). REQUIRED for annual reports. |
| `quarter` | string? | null | Quarter for fund disclosure (Q1, Q2, Q3, Q4) |

---

### 7. `fmp_news`

Financial news and press releases.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'stock_news', 'forex_news', 'crypto_news', 'general_news', 'press_releases', 'press_releases_by_symbol'] | Ellipsis | Action to perform |
| `symbol` | string? | null | Stock symbol |
| `symbols` | string? | null | Comma-separated symbols |
| `page` | integer? | null | Page number (1-indexed). Use with limit for pagination. |
| `limit` | integer? | null | Max results to return. Typical range: 1-100. |
| `from_date` | string? | null | Start date (YYYY-MM-DD). Beginning of date range. |
| `to_date` | string? | null | End date (YYYY-MM-DD). Defaults to today if omitted. |

---

### 8. `fmp_government`

Congressional trading and SEC filings data.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'house_disclosure', 'senate_disclosure', 'house_trades', 'senate_trades', 'institutional_ownership', 'filings_8k'] | Ellipsis | Action to perform |
| `symbol` | string? | null | Stock ticker symbol (e.g., "AAPL", "MSFT"). |
| `page` | integer? | null | Page number (0-indexed). Use with limit for pagination. |
| `limit` | integer? | null | Max results to return. Typical range: 1-100. |

---

### 9. `fmp_technical`

Technical analysis indicators.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'sma', 'ema', 'wma', 'dema', 'tema', 'williams', 'rsi', 'adx', 'standard_deviation'] | Ellipsis | Action to perform |
| `symbol` | string? | null | Stock symbol |
| `period` | integer | 14 | Reporting period. Format varies. Check action help. |
| `interval` | string | `"1day"` | Time interval (e.g., 1day, 1hour, 5min) |

---

### 10. `fmp_schema`

Introspect tool schemas for discovery.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `tool_name` | string? | null | Tool name to get schema for. If None, lists all tools. |

---

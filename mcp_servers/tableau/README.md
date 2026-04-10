# Tableau MCP Server

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

### 1. `tableau_list_sites`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `page_number` | int | 1 | Page number (1-indexed) |

---

### 2. `tableau_create_user`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `site_id` | str | _required_ | Site identifier - UUID v4 format (36-character string). User will be created within this site. Must  |
| `name` | str | _required_ | Username - 1-255 characters, must be unique within the site. Used for login and identification. Cann |
| `email` | EmailStr? | null | Optional email address for notifications - valid email format, up to 255 characters. Only supported  |

---

### 3. `tableau_list_users`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `site_id` | str | _required_ | Site identifier - UUID v4 format (36-character string). Returns only users within this site. |
| `page_number` | int | 1 | Page number for pagination - integer starting at 1 (first page). Must be >= 1. |

---

### 4. `tableau_get_user`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `site_id` | str | _required_ | Site identifier - UUID v4 format (36-character string). Used to validate user belongs to this site. |

---

### 5. `tableau_update_user`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `updated` | - name: Username (must be unique per site)
    - email: Email address for notifications
    - site_role: User's site role (must be one of VALID_SITE_ROLES)
    """

    site_id: str | _required_ | Site identifier - UUID v4 format (36-character string). Used to validate user belongs to this site. |
| `user_id` | str | _required_ | User identifier to update - UUID v4 format (36-character string). Must exist in the users table for  |
| `name` | str? | null | Optional new username - 1-255 characters, must be unique within the site. If null, username remains  |
| `full_name` | str? | null | Optional full display name - string for user |
| `email` | EmailStr? | null | Optional notification email address - valid email format up to 255 characters. Only supported in Tab |
| `password` | str? | null | Optional new password for user authentication - string meeting password requirements. If null, passw |
| `site_role` | str? | null | Optional new site role - must be one of the 8 valid VALID_SITE_ROLES values: |
| `auth_setting` | str? | null | Optional authentication method - string specifying how user authenticates (e.g., |
| `identity_pool_name` | str? | null | Optional identity pool name - string for Tableau Server identity pools (on-premise only). Not suppor |

---

### 6. `tableau_delete_user`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `Note` | Tableau blocks deletion if user owns content unless map_assets_to is provided.
    """

    site_id: str | _required_ | Site identifier - UUID v4 format (36-character string). Used to validate user belongs to this site. |
| `user_id` | str | _required_ | User identifier to delete - UUID v4 format (36-character string). Must exist. If user owns content a |

---

### 7. `tableau_create_project`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `site_id` | str | _required_ | Site identifier - UUID v4 format (36-character string with hyphens, e.g., |
| `name` | str | _required_ | Project name - 1-255 characters, human-readable identifier for the project |
| `description` | str | "" | Optional project description - free-text field to describe the project |
| `parent_project_id` | str? | null | Optional parent project identifier - UUID v4 format (36-character string). If provided, creates nest |

---

### 8. `tableau_list_projects`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `site_id` | str | _required_ | Site identifier - UUID v4 format (36-character string). Filters projects to those within this site. |
| `parent_project_id` | str? | null | Optional filter by parent project - UUID v4 format (36-character string). If provided, returns only  |
| `page_number` | int | 1 | Page number for pagination - integer starting at 1 (first page). Must be >= 1. |

---

### 9. `tableau_get_project`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `site_id` | str | _required_ | Site identifier - UUID v4 format (36-character string). Used to validate project belongs to this sit |

---

### 10. `tableau_update_project`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `site_id` | str | _required_ | Site identifier - UUID v4 format (36-character string). Used to validate project belongs to this sit |
| `project_id` | str | _required_ | Project identifier to update - UUID v4 format (36-character string). Must exist in the projects tabl |
| `name` | str? | null | Optional new project name - 1-255 characters. If null, name remains unchanged. If provided, must not |

---

### 11. `tableau_delete_project`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `site_id` | str | _required_ | Site identifier - UUID v4 format (36-character string). Used to validate project belongs to this sit |

---

### 12. `tableau_create_workbook`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `site_id` | str | _required_ | Site identifier - UUID v4 format (36-character string). Validates that project and owner belong to t |
| `name` | str | _required_ | Workbook name - 1-255 characters, human-readable identifier for the workbook |
| `project_id` | str | _required_ | Project identifier where workbook will be published - UUID v4 format (36-character string). Must ref |
| `owner_id` | str | _required_ | Workbook owner identifier - UUID v4 format (36-character string). Must reference an existing user. O |
| `description` | str | "" | Optional workbook description - free-text field to describe the workbook |

---

### 13. `tableau_list_workbooks`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `site_id` | str | _required_ | Site identifier - UUID v4 format (36-character string). Filters workbooks to those within this site. |
| `project_id` | str? | null | Optional filter by project - UUID v4 format (36-character string). If provided, returns only workboo |
| `owner_id` | str? | null | Optional filter by owner - UUID v4 format (36-character string). If provided, returns only workbooks |
| `page_number` | int | 1 | Page number for pagination - integer starting at 1 (first page). Must be >= 1. |

---

### 14. `tableau_get_workbook`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `site_id` | str | _required_ | Site identifier - UUID v4 format (36-character string). Used to validate workbook belongs to this si |

---

### 15. `tableau_update_workbook`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `site_id` | str | _required_ | Site identifier - UUID v4 format (36-character string). Used to validate workbook belongs to this si |
| `workbook_id` | str | _required_ | Workbook identifier to update - UUID v4 format (36-character string). Must exist in the workbooks ta |
| `name` | str? | null | Optional new workbook name - 1-255 characters. If null, name remains unchanged. If provided, must no |

---

### 16. `tableau_delete_workbook`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `site_id` | str | _required_ | Site identifier - UUID v4 format (36-character string). Used to validate workbook belongs to this si |

---

### 17. `tableau_publish_workbook`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `Types` | - .twb: Unpackaged workbook (XML only, requires external data connections)
        - .twbx: Packaged workbook (includes embedded data extracts and visualizations)
            Supports modern Hyper (.hyper) data extracts, legacy Excel (.xlsx, .xls),
            and CSV files bundled within the package.

    Two ways to provide the file:
        1. file_path: Local filesystem path to the .twb/.twbx file (recommended)
        2. file_content_base64: Base64-encoded file content (for API uploads)

    If both are provided, file_path takes precedence.
    """

    site_id: str | _required_ | Site identifier - UUID v4 format. Must exist in sites table. |
| `name` | str | _required_ | Workbook name - 1-255 characters, human-readable identifier. |
| `project_id` | str | _required_ | Project identifier where workbook will be published - UUID v4 format. |
| `file_path` | str? | null | Filename of the task input file to publish (e.g., |
| `file_content_base64` | str? | null | Upload a .twb or .twbx file from your computer. The file will be base64-encoded automatically. Use t |
| `file_name` | str? | null | Original filename with extension (e.g., |
| `description` | str | "" | Optional workbook description. |
| `show_tabs` | bool | True | Show worksheet tabs in published workbook. |
| `overwrite` | bool | False | Overwrite if workbook with same name exists in project. |

---

### 18. `tableau_create_workbook_connection`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `site_id` | str | _required_ | Site identifier - UUID v4 format (36-character string). Validates that both workbook and datasource  |
| `workbook_id` | str | _required_ | Workbook identifier - UUID v4 format (36-character string). Must reference an existing workbook in t |

---

### 19. `tableau_list_workbook_connections`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `site_id` | str | _required_ | Site identifier - UUID v4 format (36-character string). Used to validate workbook belongs to this si |

---

### 20. `tableau_delete_workbook_connection`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `site_id` | str | _required_ | Site identifier - UUID v4 format (36-character string). Used to validate workbook belongs to this si |
| `workbook_id` | str | _required_ | Workbook identifier - UUID v4 format (36-character string). Used to verify the connection belongs to |

---

### 21. `tableau_list_views`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `site_id` | str | _required_ | Site identifier |
| `workbook_id` | str? | null | Filter by workbook |
| `page_number` | int | 1 | Page number (1-indexed) |

---

### 22. `tableau_get_view`

Get a specific view by ID including name, type, and content URL.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `site_id` | string | Yes | Site identifier (UUID v4 format) |
| `view_id` | string | Yes | View identifier (UUID v4 format) |

---

### 23. `tableau_get_view_metadata`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `site_id` | str | _required_ | Site identifier - UUID v4 format |
| `view_id` | str | _required_ | View identifier - UUID v4 format |
| `include_sample_values` | bool | True | Include sample values for each field |

---

### 24. `tableau_query_view_data`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `site_id` | str
    view_id: str
    max_age: int? | null | Maximum age of cached data in minutes |

---

### 25. `tableau_query_view_data_to_file`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `site_id` | str
    view_id: str
    max_age: int? | null | Maximum age of cached data in minutes |

---

### 26. `tableau_query_view_image`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `site_id` | str
    view_id: str
    resolution: str | "standard" | Image resolution: |
| `max_age` | int? | null | Maximum age of cached image in minutes |

---

### 27. `tableau_create_datasource`

Create a new datasource in a project.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `site_id` | string | Yes | Site identifier (UUID v4 format) |
| `name` | string | Yes | Datasource name (1-255 characters) |
| `project_id` | string | Yes | Project identifier where datasource will be published |
| `owner_id` | string | Yes | Datasource owner identifier |
| `connection_type` | string | Yes | Connection type (e.g., "postgres", "mysql", "excel") |
| `description` | string | No | Datasource description |

---

### 28. `tableau_list_datasources`

List datasources with optional project filtering and pagination.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `site_id` | string | Yes | Site identifier (UUID v4 format) |
| `project_id` | string | No | Optional filter by project |
| `page_number` | integer | No | Page number for pagination. Default: 1 |
| `page_size` | integer | No | Number of items per page (1-1000). Default: 100 |

---

### 29. `tableau_get_datasource`

Get a datasource by ID.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `site_id` | string | Yes | Site identifier (UUID v4 format) |
| `datasource_id` | string | Yes | Datasource identifier to retrieve |

---

### 30. `tableau_update_datasource`

Update datasource name, description, or connection type.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `site_id` | string | Yes | Site identifier (UUID v4 format) |
| `datasource_id` | string | Yes | Datasource identifier to update |
| `name` | string | No | New datasource name (1-255 characters) |
| `description` | string | No | New datasource description |
| `connection_type` | string | No | New connection type |

---

### 31. `tableau_delete_datasource`

Delete a datasource.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `site_id` | string | Yes | Site identifier (UUID v4 format) |
| `datasource_id` | string | Yes | Datasource identifier to delete |

---

### 32. `tableau_create_group`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `site_id` | str | _required_ | Site identifier - UUID v4 format (36-character string). Group will be created within this site conte |
| `name` | str | _required_ | Group name - 1-255 characters, must be unique across all sites (not just within site). Used for iden |

---

### 33. `tableau_list_groups`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `site_id` | str | _required_ | Site identifier - UUID v4 format (36-character string). Returns groups accessible within this site c |
| `page_number` | int | 1 | Page number for pagination - integer starting at 1 (first page). Must be >= 1. |

---

### 34. `tableau_add_user_to_group`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `site_id` | str | _required_ | Site identifier - UUID v4 format (36-character string). Used to validate both group and user belong  |
| `group_id` | str | _required_ | Group identifier - UUID v4 format (36-character string). Must reference an existing group. User will |

---

### 35. `tableau_remove_user_from_group`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `site_id` | str | _required_ | Site identifier - UUID v4 format (36-character string). Used to validate both group and user belong  |
| `group_id` | str | _required_ | Group identifier - UUID v4 format (36-character string). Must reference an existing group. User will |

---

### 36. `tableau_grant_permission`

Grant a permission on a resource to a user or group (idempotent).

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `site_id` | string | Yes | Site identifier (UUID v4 format) |
| `resource_type` | string | Yes | Resource type: "project", "workbook", or "datasource" |
| `resource_id` | string | Yes | Resource identifier |
| `grantee_type` | string | Yes | Grantee type: "user" or "group" |
| `grantee_id` | string | Yes | User or group identifier |
| `capability` | string | Yes | Permission capability to grant |
| `mode` | string | Yes | Permission mode: "Allow" or "Deny" |

---

### 37. `tableau_list_permissions`

List all permissions for a resource.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `site_id` | string | Yes | Site identifier (UUID v4 format) |
| `resource_type` | string | Yes | Resource type: "project", "workbook", or "datasource" |
| `resource_id` | string | Yes | Resource identifier |

---

### 38. `tableau_revoke_permission`

Revoke a permission from a resource.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `site_id` | string | Yes | Site identifier (UUID v4 format) |
| `resource_type` | string | Yes | Resource type: "project", "workbook", or "datasource" |
| `resource_id` | string | Yes | Resource identifier |
| `grantee_type` | string | Yes | Grantee type: "user" or "group" |
| `grantee_id` | string | Yes | User or group identifier |
| `capability` | string | Yes | Permission capability to revoke |

---

## Consolidated Tools

When using consolidated mode, these meta-tools combine multiple operations:

### 1. `tableau_admin`

Manage Tableau sites and permissions.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'list_sites', 'grant_permission', 'list_permissions', 'revoke_permission'] | Ellipsis | Action to perform |
| `page_number` | integer | 1 | Page number (1-indexed) |
| `page_size` | integer | 100 | Items per page |
| `site_id` | string? | null | Site ID (required for permission actions) |
| `resource_type` | string? | null | Resource type: 'project', 'workbook', or 'datasource' |
| `resource_id` | string? | null | Resource ID (workbook, view, datasource). For permissions. |
| `grantee_type` | string? | null | 'user' or 'group' |
| `grantee_id` | string? | null | User or group ID receiving permissions. REQUIRED. |
| `capability` | string? | null | 'Read', 'Write', or 'ChangePermissions' |
| `mode` | string? | null | 'Allow' or 'Deny' |

---

### 2. `tableau_users`

Manage Tableau users.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'create', 'list', 'get', 'update', 'delete'] | Ellipsis | Action: 'help', 'create', 'list', 'get', 'update', 'delete' |
| `site_id` | string? | null | Site ID (required except for help) |
| `user_id` | string? | null | User ID (required for get/update/delete) |
| `name` | string? | null | Display name. REQUIRED for create. Used in search results. |
| `email` | string? | null | Email address. REQUIRED for user operations. |
| `site_role` | string? | null | Site role (required for create) |
| `full_name` | string? | null | Persons full name. REQUIRED for user creation. |
| `password` | string? | null | Account password. REQUIRED for user creation. |
| `auth_setting` | string? | null | Authentication method |
| `map_assets_to` | string? | null | Transfer content to user ID on delete |
| `page_number` | integer | 1 |  |
| `page_size` | integer | 100 |  |

---

### 3. `tableau_projects`

Manage Tableau projects.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'create', 'list', 'get', 'update', 'delete'] | Ellipsis | Action: 'help', 'create', 'list', 'get', 'update', 'delete' |
| `site_id` | string? | null | Site ID (required except for help) |
| `project_id` | string? | null | Project identifier. REQUIRED for project_time action. |
| `name` | string? | null | Display name. REQUIRED for create. Used in search results. |
| `description` | string? | null | Detailed description. Optional but recommended. |
| `parent_project_id` | string? | null | Parent project ID |
| `owner_id` | string? | null | Owner user ID (required for create) |
| `page_number` | integer | 1 |  |
| `page_size` | integer | 100 |  |

---

### 4. `tableau_workbooks`

Manage Tableau workbooks and connections.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'create', 'list', 'get', 'update', 'delete', 'publish', 'connect', 'list_connections', 'disconnect'] | Ellipsis | Action to perform |
| `site_id` | string? | null | Site ID (required except for help) |
| `workbook_id` | string? | null | Workbook ID (required for get/update/delete/connect/list_connections/disconnect) |
| `name` | string? | null | Display name. REQUIRED for create. Used in search results. |
| `description` | string? | null | Detailed description. Optional but recommended. |
| `project_id` | string? | null | Project identifier. REQUIRED for project_time action. |
| `owner_id` | string? | null | Owner user ID (required for create) |
| `file_reference` | string? | null | File path or reference |
| `file_path` | string? | null | Full file path. REQUIRED for file operations. |
| `file_content_base64` | string? | null | Base64-encoded file content for upload. |
| `file_name` | string? | null | Filename with extension. REQUIRED for create/save. |
| `show_tabs` | boolean | true | Show worksheet tabs |
| `overwrite` | boolean | false | Overwrite existing workbook |
| `datasource_id` | string? | null | Datasource ID (required for connect) |
| `connection_id` | string? | null | Connection ID (required for disconnect) |
| `page_number` | integer | 1 |  |
| `page_size` | integer | 100 |  |

---

### 5. `tableau_views`

Query Tableau views (read-only).

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'list', 'get', 'metadata', 'query', 'query_to_file', 'image'] | Ellipsis | Action: 'help', 'list', 'get', 'metadata', 'query', 'query_to_file', 'image' |
| `site_id` | string? | null | Site ID (required except for help) |
| `view_id` | string? | null | View ID (required for get/metadata/query/query_to_file/image) |
| `workbook_id` | string? | null | Filter by workbook |
| `page_number` | integer | 1 |  |
| `page_size` | integer | 100 |  |
| `max_age` | integer? | null | Max cache age in minutes |
| `filters` | object[string, string]? | null | View filters |
| `include_sample_values` | boolean | true |  |
| `sample_value_limit` | integer | 5 |  |
| `resolution` | string | `"standard"` | 'standard' or 'high' |

---

### 6. `tableau_datasources`

Manage Tableau datasources.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'create', 'list', 'get', 'update', 'delete'] | Ellipsis | Action: 'help', 'create', 'list', 'get', 'update', 'delete' |
| `site_id` | string? | null | Site ID (required except for help) |
| `datasource_id` | string? | null | Datasource ID (required for get/update/delete) |
| `name` | string? | null | Display name. REQUIRED for create. Used in search results. |
| `description` | string? | null | Detailed description. Optional but recommended. |
| `project_id` | string? | null | Project identifier. REQUIRED for project_time action. |
| `owner_id` | string? | null | Owner user ID (required for create) |
| `connection_type` | string? | null | Connection type (required for create) |
| `page_number` | integer | 1 |  |
| `page_size` | integer | 100 |  |

---

### 7. `tableau_groups`

Manage Tableau groups and memberships.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | enum['help', 'create', 'list', 'add_user', 'remove_user'] | Ellipsis | Action: 'help', 'create', 'list', 'add_user', 'remove_user' |
| `site_id` | string? | null | Site ID (required except for help) |
| `group_id` | string? | null | Group ID (required for add_user/remove_user) |
| `user_id` | string? | null | User ID (required for add_user/remove_user) |
| `name` | string? | null | Display name. REQUIRED for create. Used in search results. |
| `description` | string? | null | Detailed description. Optional but recommended. |
| `page_number` | integer | 1 |  |
| `page_size` | integer | 100 |  |

---

### 8. `tableau_schema`

Get JSON schema for any Tableau tool's input/output.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `tool` | enum['tableau_admin', 'tableau_users', 'tableau_projects', 'tableau_workbooks', 'tableau_views', 'tableau_datasources', 'tableau_groups'] | Ellipsis | Tool name to get schema for |
| `action` | string? | null | The operation to perform. REQUIRED. Call with action='help' first. |

---
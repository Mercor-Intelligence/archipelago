# Workday MCP Server

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

### 1. `strip`

Utility function for string manipulation.

**Parameters:** None (no parameters required)

---

### 2. `workday_hire_worker`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `worker_id` | str | _required_ | Unique worker identifier |
| `job_profile_id` | str | _required_ | Job profile ID |
| `org_id` | str | _required_ | Supervisory organization ID |
| `cost_center_id` | str | _required_ | Cost center ID |
| `location_id` | str? | null | Location ID |
| `position_id` | str? | null | Position ID to fill |
| `fte` | float | 1.0 | Full-time equivalent |
| `hire_date` | str | _required_ | Hire date (YYYY-MM-DD) |

---

### 3. `workday_get_worker`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `worker_id` | str | _required_ | Worker ID to retrieve |

---

### 4. `workday_list_workers`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `page_size` | int | 100 | Results per page |
| `page_number` | int | 1 | Page number |
| `org_id` | str? | null | Filter by organization |
| `cost_center_id` | str? | null | Filter by cost center |
| `employment_status` | Literal["Active", "Terminated", "Leave"]? | null | Filter by employment status. Options: Active, Terminated, Leave |

---

### 5. `workday_transfer_worker`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `worker_id` | str | _required_ | Worker ID to transfer |
| `new_org_id` | str? | null | New supervisory organization ID |
| `new_cost_center_id` | str? | null | New cost center ID |
| `new_job_profile_id` | str? | null | New job profile ID (promotion/demotion) |
| `new_position_id` | str? | null | New position ID |
| `new_fte` | float? | null | New FTE |
| `transfer_date` | str | _required_ | Transfer effective date (YYYY-MM-DD) |

---

### 6. `workday_terminate_worker`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `worker_id` | str | _required_ | Worker ID to terminate |
| `termination_date` | str | _required_ | Termination date (YYYY-MM-DD) |

---

### 7. `workday_get_position`

Retrieve detailed information about a position.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `position_id` | string | Yes | Position ID to retrieve |

---

### 8. `workday_create_position`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `position_id` | str | _required_ | Unique position identifier |
| `job_profile_id` | str | _required_ | Job profile ID |
| `org_id` | str | _required_ | Supervisory organization ID |
| `fte` | float | 1.0 | FTE allocation |

---

### 9. `workday_list_positions`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `page_size` | int | 100 | Results per page |
| `page_number` | int | 1 | Page number |
| `org_id` | str? | null | Filter by organization |
| `status` | Literal["open", "filled", "closed"]? | null | Filter by position status. Options: open, filled, closed |

---

### 10. `workday_close_position`

Close a position, marking it as unavailable for hiring.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `position_id` | string | Yes | Position ID to close |

---

### 11. `workday_get_org`

Retrieve detailed information about a supervisory organization.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `org_id` | string | Yes | Organization ID to retrieve |

---

### 12. `workday_list_orgs`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `page_size` | int | 100 | Results per page |
| `page_number` | int | 1 | Page number |
| `parent_org_id` | str? | null | Filter by parent organization |
| `org_type` | Literal["Supervisory", "Cost_Center", "Location"]? | null | Filter by organization type. Options: Supervisory, Cost_Center, Location |

---

### 13. `workday_get_org_hierarchy`

Retrieve organization hierarchy as nested tree structure.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `root_org_id` | string | Yes | Root organization ID for hierarchy |

---

### 14. `workday_create_org`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `org_id` | str | _required_ | Organization ID |
| `org_name` | str | _required_ | Organization name |
| `org_type` | Literal["Supervisory", "Cost_Center", "Location" | "Supervisory" | Organization type. Options: Supervisory, Cost_Center, Location |
| `parent_org_id` | str? | null | Parent organization ID |

---

### 15. `workday_create_cost_center`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `cost_center_id` | str | _required_ | Unique cost center identifier |
| `cost_center_name` | str | _required_ | Cost center display name |

---

### 16. `workday_create_location`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `location_id` | str | _required_ | Unique location identifier |
| `location_name` | str | _required_ | Location display name |
| `city` | str? | null | City name. Improves tax accuracy for multi-zone cities. |

---

### 17. `workday_get_job_profile`

Retrieve a job profile by ID.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `job_profile_id` | string | Yes | Job profile ID to retrieve |

---

### 18. `workday_list_job_profiles`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `page_size` | int | 100 | Results per page |
| `page_number` | int | 1 | Page number |

---

### 19. `workday_create_job_profile`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `job_profile_id` | str | _required_ | Unique job profile identifier |
| `title` | str | _required_ | Title for the entity. REQUIRED for create. |
| `job_family` | str | _required_ | Job family (e.g., Engineering, Sales) |

---

### 20. `workday_report_workforce_roster`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `org_id` | str? | null | Filter by organization |
| `cost_center_id` | str? | null | Filter by cost center |
| `employment_status` | Literal["Active", "Terminated", "Leave"]? | null | Filter by employment status. Options: Active, Terminated, Leave |
| `as_of_date` | str? | null | Point-in-time roster (YYYY-MM-DD) |
| `page_size` | int | 1000 | Results per page |
| `page_number` | int | 1 | Page number |

---

### 21. `workday_report_headcount`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `start_date` | str | _required_ | Range start (YYYY-MM-DD). REQUIRED for date-bounded queries. |
| `end_date` | str | _required_ | Range end (YYYY-MM-DD). REQUIRED for date-bounded queries. |
| `group_by` | Literal["org_id", "cost_center_id" | "org_id" | Grouping dimension. Options: org_id, cost_center_id |

---

### 22. `workday_report_movements`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `start_date` | str | _required_ | Range start (YYYY-MM-DD). REQUIRED for date-bounded queries. |
| `end_date` | str | _required_ | Range end (YYYY-MM-DD). REQUIRED for date-bounded queries. |
| `event_type` | Literal["hire", "termination", "transfer"]? | null | Filter by event type. Options: hire, termination, transfer |
| `org_id` | str? | null | Filter by organization ID (to_org_id or from_org_id) |
| `page_size` | int | 1000 | Results per page |

---

### 23. `workday_report_positions`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `org_id` | str? | null | Filter by organization |
| `status` | Literal["open", "filled", "closed"]? | null | Filter by position status. Options: open, filled, closed |
| `job_profile_id` | str? | null | Filter by job profile |
| `page_size` | int | 1000 | Results per page |

---

### 24. `workday_report_org_hierarchy`

Generate organization hierarchy report with flattened structure.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `root_org_id` | string | Yes | Root organization ID for report |
| `max_depth` | integer | No | Maximum depth to traverse |

---

### 25. `workday_exception_request`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `case_id` | str | _required_ | Case ID |
| `milestone_type` | Literal["screening", "work*authorization", "documents", "approvals" | \_required* | Milestone requiring exception. |
| `reason` | str | _required_ | Explanation for the action. REQUIRED for adjustments. |
| `affected_policy_refs` | list[str | null | Policies being excepted |

---

### 26. `workday_exception_approve`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `exception_id` | str | _required_ | Exception ID to approve |
| `approval_status` | Literal["approved", "denied" | _required_ | Approval decision. Options: approved, denied |
| `approval_notes` | str | _required_ | Mandatory notes explaining decision |

---

### 27. `workday_audit_get_history`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `case_id` | str | _required_ | Case ID |
| `action_type` | str? | null | Filter by action type |
| `actor_persona` | str? | null | Filter by actor |
| `start_date` | str? | null | Range start (YYYY-MM-DD). REQUIRED for date-bounded queries. |

---

### 28. `workday_create_case`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `case_id` | str | _required_ | Unique case identifier (e.g., CASE-001) |
| `candidate_id` | str | _required_ | Candidate identifier from ATS |
| `requisition_id` | str? | null | Requisition/job opening ID |
| `role` | str | _required_ | Job role/title |
| `country` | str | _required_ | ISO country code. Default: US. |
| `employment_type` | Literal["full_time", "part_time", "contractor" | "full_time" | Employment type. Options: full_time, part_time, contractor |
| `owner_persona` | Literal[
"pre_onboarding_coordinator", "hr_admin", "hr_business_partner", "hiring_manager", "auditor"
| "pre_onboarding_coordinator" | Assigned owner persona |
| `proposed_start_date` | str? | null | Initial proposed start date (YYYY-MM-DD) |
| `due_date` | str? | null | Target completion date (YYYY-MM-DD) |

---

### 29. `workday_get_case`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `case_id` | str | _required_ | Case ID to retrieve |
| `include_tasks` | bool | True | Include associated tasks |

---

### 30. `workday_update_case`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `case_id` | str | _required_ | Case ID to update |
| `new_status` | Literal["open", "in*progress", "pending_approval", "resolved", "closed" | \_required* | New case status. Options: open, in*progress, pending_approval, resolved, closed |
| `rationale` | str | \_required* | Reason for status change |

---

### 31. `workday_assign_owner_case`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `case_id` | str | _required_ | Case ID |
| `new_owner_persona` | Literal[
"pre*onboarding_coordinator", "hr_admin", "hr_business_partner", "hiring_manager", "auditor"
| \_required* | New owner persona. |
| `rationale` | str | _required_ | Reason for reassignment |

---

### 32. `workday_search_case`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `status` | Literal["open", "in_progress", "pending_approval", "resolved", "closed"]? | null | Filter by case status. Options: open, in_progress, pending_approval, resolved, closed |
| `owner_persona` | (
Literal[
"pre_onboarding_coordinator",
"hr_admin",
"hr_business_partner",
"hiring_manager",
"auditor",
]
?
) | null | Filter by owner persona |
| `country` | str? | null | ISO country code. Default: US. |
| `role` | str? | null | Filter by role |
| `due_date_before` | str? | null | Due date before (YYYY-MM-DD) |
| `due_date_after` | str? | null | Due date after (YYYY-MM-DD) |
| `page_size` | int | 50 | Results per page |

---

### 33. `workday_snapshot_case`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `case_id` | str | _required_ | Case ID |

---

### 34. `workday_milestones_list`

List all milestones for a pre-onboarding case.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `case_id` | string | Yes | Case ID to list milestones for |

---

### 35. `workday_milestones_update`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `case_id` | str | _required_ | Case ID |
| `milestone_type` | Literal["screening", "work*authorization", "documents", "approvals" | \_required* | Milestone type to update. Options: screening, work*authorization, documents, approvals |
| `new_status` | Literal["pending", "in_progress", "completed", "waived", "blocked" | \_required* | New milestone status. Options: pending, in_progress, completed, waived, blocked |
| `evidence_link` | str? | null | URL or reference to evidence |
| `notes` | str? | null | Additional notes. Useful for audit trail. |

---

### 36. `workday_tasks_create`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `case_id` | str | _required_ | Case ID |
| `milestone_type` | str? | null | Optional milestone to link |
| `title` | str | _required_ | Title for the entity. REQUIRED for create. |
| `owner_persona` | str | _required_ | Task owner persona |
| `due_date` | str? | null | Task due date (YYYY-MM-DD) |

---

### 37. `workday_tasks_update`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `task_id` | str | _required_ | Task ID to update |
| `new_status` | Literal["pending", "in_progress", "completed", "cancelled"]? | null | New task status. Options: pending, in_progress, completed, cancelled |
| `new_owner_persona` | (
Literal[
"pre_onboarding_coordinator",
"hr_admin",
"hr_business_partner",
"hiring_manager",
"auditor",
]
?
) | null | New owner persona |
| `notes` | str? | null | Additional notes. Useful for audit trail. |

---

### 38. `workday_health_check`

Check server health status including database connectivity and uptime.

**Parameters:** None (no parameters required)

---

### 39. `workday_hcm_read_context`

Retrieve HCM context for a case including onboarding status and start dates.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `case_id` | string | Yes | Case ID to read context for |

---

### 40. `workday_hcm_read_position`

Retrieve position context with policy-derived requirements for a case.

**Parameters:**
| Name | Type | Required | Description |
|------|------|----------|-------------|
| `case_id` | string | Yes | Case ID to read position for |

---

### 41. `workday_hcm_confirm_start_date`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `case_id` | str | _required_ | Case ID |
| `confirmed_start_date` | str | _required_ | Start date to confirm (YYYY-MM-DD) |
| `policy_refs` | list[str | _required_ | Policy IDs justifying this decision |
| `evidence_links` | list[str | _required_ | Evidence supporting the confirmation |
| `rationale` | str | _required_ | Free-text rationale for the decision |

---

### 42. `workday_hcm_update_readiness`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `case_id` | str | _required_ | Case ID |
| `onboarding_readiness` | bool | _required_ | Readiness flag value |
| `policy_refs` | list[str | _required_ | Policy IDs justifying this update |
| `evidence_links` | list[str | _required_ | Evidence supporting the update |
| `rationale` | str | _required_ | Rationale for the update |

---

### 43. `workday_policies_get_applicable`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `country` | str | _required_ | ISO country code. Default: US. |
| `role` | str? | null | Role/job title filter |
| `employment_type` | Literal["full_time", "part_time", "contractor"]? | null | Employment type filter. Options: full_time, part_time, contractor |

---

### 44. `workday_policies_attach_to_case`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `case_id` | str | _required_ | Case ID |
| `policy_ids` | list[str | _required_ | Policy IDs to attach |
| `decision_context` | str | _required_ | Why these policies are relevant |

---

### 45. `workday_policies_create`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `policy_id` | str | _required_ | Unique policy identifier (e.g., POLICY-US-LEAD-TIME) |
| `country` | str | _required_ | ISO country code. Default: US. |
| `policy_type` | Literal["prerequisites", "lead*times", "payroll_cutoffs", "constraints" | \_required* | Type of policy. Options: prerequisites, lead*times, payroll_cutoffs, constraints |
| `content` | dict | \_required* | Content data. Format depends on action. |
| `effective_date` | str | _required_ | Date change takes effect (YYYY-MM-DD). REQUIRED. |
| `version` | str | _required_ | Policy version (e.g., 1.0) |
| `role` | str? | null | Applicable role (null = all roles) |
| `employment_type` | str? | null | Applicable employment type (null = all) |

---

### 46. `workday_policies_create_payroll_cutoff`

No description available.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `cutoff_id` | str | _required_ | Unique cutoff identifier (e.g., CUTOFF-US-001) |
| `country` | str | _required_ | ISO country code. Default: US. |
| `cutoff_day_of_month` | int | _required_ | Day of month for cutoff (1-31) |
| `processing_days` | int | _required_ | Processing days before cutoff |

---

## Consolidated Tools

When using consolidated mode, these meta-tools combine multiple operations:

### 1. `workday_workers`

Manage worker lifecycle in Workday HCM.

---

### 2. `workday_positions`

Manage positions in Workday HCM.

---

### 3. `workday_organizations`

Manage organizations, cost centers, and locations in Workday HCM.

---

### 4. `workday_job_profiles`

Manage job profiles in Workday HCM.

---

### 5. `workday_cases`

Manage pre-onboarding cases in Workday HCM.

---

### 6. `workday_hcm`

Read and write HCM context for pre-onboarding cases.

---

### 7. `workday_milestones`

Manage milestones for pre-onboarding cases.

---

### 8. `workday_tasks`

Manage tasks for pre-onboarding cases.

---

### 9. `workday_policies`

Manage policies and payroll cutoffs for pre-onboarding.

---

### 10. `workday_reports`

Generate various reports from Workday HCM data.

---

### 11. `workday_exceptions`

Request and approve exceptions for pre-onboarding cases.

---

### 12. `workday_audit`

Retrieve audit history for pre-onboarding cases.

---

### 13. `workday_system`

System utilities: health check, server info, and schema introspection.

---

### 14. `register_meta_tools`

Register all meta-tools with the MCP server.

---

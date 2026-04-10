# MCP Server Development Framework

A Python-based framework for rapidly developing Model Context Protocol (MCP) servers with spec-driven test-driven development, built-in middleware, and automatic API documentation generation.


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


## Overview

This repository provides a scaffolding system for creating MCP servers using the FastMCP framework. The architecture includes middleware for error handling, retry logic, and logging, along with utility decorators for async operations and concurrency control.

### What is MCP?

**Model Context Protocol (MCP)** is an open protocol that lets AI assistants (like Claude) connect to external tools and data sources. It provides a standardized way for AI to fetch data from APIs, access local files or databases, and control external services. This framework helps you build MCP servers that can expose your custom tools to AI assistants. Once built, a server can be connected to any MCP-compatible client (e.g., Claude Desktop).

## Prerequisites

- Python 3.10 or higher
- [uv](https://docs.astral.sh/uv/) for package management

## Quick Start (5 Minutes)

### 1. Install uv (Python package manager)

```bash
# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

### 2. Clone this template repo

```bash
git clone <your-fork-of-this-repo>
cd mercor-mcp
```

### 3. Install dependencies

```bash
uv sync --all-extras
```

This creates a virtual environment (`.venv`) and installs all dependencies from the lock file.

### 4. Create your first MCP server

```bash
# Spec-driven TDD mode (recommended)
uv run python scripts/create_mcp_server.py weather --with-models

# With database support
uv run python scripts/create_mcp_server.py weather --with-models --with-database

# Standard mode (manual test writing)
uv run python scripts/create_mcp_server.py weather
```

### 5. Start developing!

Follow the printed TDD workflow instructions.

## Two Development Modes

This framework supports two development approaches:

1. **Spec-Driven TDD** (`--with-models`): Define your API with Pydantic models, auto-generate tests, implement via TDD
2. **Standard Mode**: Traditional development with manual test writing

---

## Spec-Driven Development (Recommended)

### The 60-Second Test-Driven Development Loop

Here's the complete cycle - you'll run this loop for every feature you add:

```bash
# Step 1: Create a new MCP server
uv run python scripts/create_mcp_server.py weather --with-models

# Step 2: Define what your API should do
# Open: mcp_servers/weather/models.py
# Write: WeatherRequest and WeatherResponse classes (your spec)

# Step 3: Run tests → RED (they should fail - this is good!)
uv run pytest tests/test_weather.py -v
# Tests fail because you haven't implemented the logic yet

# Step 4: Make tests pass → GREEN
# Open: mcp_servers/weather/tools/weather.py
# Write: Your implementation code to match the spec

# Step 5: Run tests again → GREEN ✅
uv run pytest tests/test_weather.py -v
# Tests pass! Your implementation matches your spec

# Step 6: Refactor & add more features
# Repeat steps 2-5 for each new feature
```

### Why Spec-Driven?

Tests, API docs, and input/output validation automatically happen from the spec. This tight feedback loop allows quick iteration. It also enables autocomplete and type checking in the IDE.

### Example: Building a Weather API

**Step 1: Generate with models**

```bash
uv run python scripts/create_mcp_server.py weather --with-models
```

**Step 2: Define your spec in `models.py`**

```python
# mcp_servers/weather/models.py
from pydantic import BaseModel, Field

class WeatherRequest(BaseModel):
    city: str = Field(..., description="City name")
    units: str = Field("fahrenheit", description="Temperature units")

class WeatherResponse(BaseModel):
    temperature: float = Field(..., description="Current temperature")
    conditions: str = Field(..., description="Weather conditions")
    humidity: int = Field(..., description="Humidity percentage")
```

**Step 3: Run tests (RED - they fail)**

```bash
uv run pytest tests/test_weather.py -v
# FAILED - NotImplementedError or validation errors
```

**Step 4: Implement in `tools/weather.py` (GREEN - make them pass)**

```python
# mcp_servers/weather/tools/weather.py
@make_async_background
def weather(request: WeatherRequest) -> WeatherResponse:
    # Call actual weather API
    data = fetch_weather_api(request.city, request.units)

    return WeatherResponse(
        temperature=data['temp'],
        conditions=data['conditions'],
        humidity=data['humidity']
    )
```

**Step 5: Tests pass! ✅**

```bash
uv run pytest tests/test_weather.py -v
# All tests passed!
```

**Step 6: Add more features (repeat loop)**

Update `models.py` → Run tests (RED) → Implement (GREEN) → Refactor

---

## Adding Custom Tests (Beyond Auto-Generated)

The auto-generated tests validate your Pydantic schema, but you'll want to add tests for:
- Edge cases (empty strings, large numbers, special characters)
- Business logic (specific calculations, error conditions)
- Integration points (API calls, database queries)

### How to Add Tests

Simply add more test methods to your test class in `tests/test_<server_name>.py`:

```python
# tests/test_weather.py

class TestWeatherTool:
    """Test suite for weather with Pydantic validation."""

    # These 3 tests are auto-generated ✅
    async def test_basic_functionality(self): ...
    async def test_validates_request_schema(self): ...
    async def test_response_matches_schema(self): ...

    # ADD YOUR CUSTOM TESTS HERE ⬇️

    @pytest.mark.asyncio
    async def test_handles_invalid_city(self):
        """Test graceful handling of non-existent cities."""
        request = WeatherRequest(city="InvalidCityXYZ123")
        response = await weather(request)
        # Add assertions for how you want to handle this
        assert response is not None

    @pytest.mark.asyncio
    async def test_temperature_units_conversion(self):
        """Test that fahrenheit and celsius return different values."""
        request_f = WeatherRequest(city="San Francisco", units="fahrenheit")
        request_c = WeatherRequest(city="San Francisco", units="celsius")

        response_f = await weather(request_f)
        response_c = await weather(request_c)

        # Same city, different units → different temps
        assert response_f.temperature != response_c.temperature

    @pytest.mark.asyncio
    async def test_humidity_range(self):
        """Test that humidity is always 0-100."""
        request = WeatherRequest(city="San Francisco")
        response = await weather(request)

        assert 0 <= response.humidity <= 100
```

### TDD for New Features

**Scenario:** You want to add a 5-day forecast feature.

**Step 1: Update your spec** (`models.py`)
```python
class WeatherOutput(BaseModel):
    temperature: float
    conditions: str
    humidity: int
    forecast: list[str] = Field(..., description="5-day forecast")  # NEW
```

**Step 2: Add a test** (`tests/test_weather.py`)
```python
@pytest.mark.asyncio
async def test_returns_5_day_forecast(self):
    """Test that forecast has 5 days."""
    request = WeatherRequest(city="San Francisco")
    response = await weather(request)

    assert len(response.forecast) == 5
    assert all(isinstance(day, str) for day in response.forecast)
```

**Step 3: Run test (RED)**
```bash
uv run pytest tests/test_weather.py::TestWeatherTool::test_returns_5_day_forecast -v
# FAILED - forecast field missing
```

**Step 4: Implement (GREEN)**
```python
# Add forecast logic to tools/weather.py
return WeatherOutput(
    temperature=temp,
    conditions=conditions,
    humidity=humidity,
    forecast=[day['weatherDesc'] for day in response['forecast'][:5]]  # NEW
)
```

**Step 5: Test passes!**

---

## Creating a New MCP Server

### Step 1: Generate Boilerplate

Use the provided script to generate a new MCP server.

**Standard Mode:**

```bash
uv run python scripts/create_mcp_server.py <server_name>
```

**Spec-Driven Mode (with Pydantic models):**

```bash
uv run python scripts/create_mcp_server.py <server_name> --with-models
```

**Examples:**

```bash
# Standard mode
uv run python scripts/create_mcp_server.py weather_api

# Spec-driven with Pydantic (recommended for TDD)
uv run python scripts/create_mcp_server.py weather_api --with-models

# Works with any naming style
uv run python scripts/create_mcp_server.py "Content Analyzer" --with-models
```

The script will automatically:
- Convert the name to snake_case for directory and file names
- Create the directory structure under `mcp_servers/<server_name>/`
- Generate boilerplate files including main.py, tool files, middleware, and utilities
- Create a corresponding test file under `tests/`

**Generated Structure:**

```
mcp_servers/<server_name>/
├── main.py                    # Server entry point
├── middleware/
│   └── logging.py            # Logging middleware
├── utils/
│   └── decorators.py         # Utility decorators
├── tools/
│   └── <server_name>.py      # Tool implementation
└── pyrightconfig.json        # Type checking configuration

tests/
└── <server_name>.py          # Test file
```

### Step 2: Implement Tool Logic

Navigate to `mcp_servers/<server_name>/tools/` and update the generated tool file(s). Each file in the tools directory represents a tool that will be registered with the MCP server.

**Basic Tool Structure:**

```python
from loguru import logger
from utils.decorators import make_async_background

@make_async_background
def my_tool(input_param: str) -> str:
    """
    Tool description and purpose.

    Args:
        input_param: Description of the parameter

    Returns:
        Description of return value
    """
    logger.info(f"Processing input: {input_param}")

    # Implement your tool logic here
    result = process_data(input_param)

    return result
```

**Adding Multiple Tools:**

One server can have multiple tools (endpoints). To add additional tools:

1. Create a new Python file in the `tools/` directory:

```bash
# Create new tool file
touch mcp_servers/<server_name>/tools/list_items.py
```

2. Implement the tool following the structure above

3. Register the tool in `main.py` (just follow the comments):

```python
from tools.<server_name> import <server_name>
from tools.list_items import list_items  # ADD

# Register tools
mcp.tool(<server_name>)
mcp.tool(list_items)  # ADD
```

**Example: Building a CRUD API**

```python
# mcp_servers/groups_api/main.py
from tools.create_group import create_group
from tools.list_groups import list_groups
from tools.update_group import update_group
from tools.delete_group import delete_group

mcp.tool(create_group)
mcp.tool(list_groups)
mcp.tool(update_group)
mcp.tool(delete_group)
```

Each tool gets its own:
- `tools/<tool_name>.py` - Implementation
- `models/<tool_name>.py` - Pydantic schemas (if using `--with-models`)
- `tests/test_<tool_name>.py` - Tests

---

## Database Support

Add SQLAlchemy + Alembic for persistence with the `--with-database` flag.

### Create Server with Database

```bash
uv run python scripts/create_mcp_server.py myapi --with-models --with-database

# Install database dependencies
uv sync --extra database
```

**Generated structure:**
```
mcp_servers/myapi/
├── models.py              # Pydantic API schemas
├── db/
│   ├── models.py          # SQLAlchemy ORM models
│   ├── session.py         # Database session management
│   └── migrations/        # Alembic migrations
│       ├── env.py
│       └── versions/
├── repositories/
│   └── base.py            # Repository pattern (CRUD operations)
├── alembic.ini            # Migration configuration
└── tools/
```

### Database Workflow

**1. Define your database schema** (`db/models.py`):
```python
from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from db.models import Base

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True)
```

**2. Create migration:**
```bash
cd mcp_servers/myapi
uv run alembic revision --autogenerate -m "Add users table"
uv run alembic upgrade head
```

**3. Use in your tool:**
```python
from db.session import get_session
from db.models import User
from repositories.base import BaseRepository

@make_async_background
async def create_user(request: CreateUserInput) -> CreateUserOutput:
    async with get_session() as session:
        repo = BaseRepository(User, session)
        user = await repo.create(name=request.name, email=request.email)
        return CreateUserOutput(id=user.id, name=user.name)
```

**Key concepts:**
- **API schemas** (`models.py`) - What users see (Pydantic)
- **DB models** (`db/models.py`) - How data is stored (SQLAlchemy)
- **Repository pattern** - Testable data access layer
- **Migrations** - Version control for database schema

---

## Environment Configuration

Add type-safe configuration with `--with-config`.

### Create Server with Config

```bash
uv run python scripts/create_mcp_server.py myapi --with-models --with-config

# Install config dependencies
uv sync --extra config
```

**Generated:**
```
mcp_servers/myapi/
├── config.py         # Pydantic settings class
└── .env.example      # Template (copy to .env)
```

### Configuration Workflow

**1. Define your settings** (`config.py`):
```python
from pydantic import Field
from pydantic_settings import BaseSettings

class MyapiSettings(BaseSettings):
    api_key: str = Field(..., description="External API key")
    database_url: str = Field("sqlite+aiosqlite:///./data.db")
    debug: bool = Field(False)
```

**2. Set environment variables** (`.env`):
```bash
API_KEY=sk-1234567890
DEBUG=true
DATABASE_URL=postgresql://user:pass@localhost/db
```

**3. Use in your tools:**
```python
from config import settings

@make_async_background
def my_tool(request: Input) -> Output:
    if settings.debug:
        logger.debug(f"API key: {settings.api_key[:8]}...")
    # Use settings throughout your code
```

---

## Authentication Middleware

Add bearer token authentication with `--with-auth`.

### Create Server with Auth

```bash
uv run python scripts/create_mcp_server.py myapi --with-models --with-auth
```

**Generated:**
```
mcp_servers/myapi/
└── middleware/
    ├── logging.py
    └── auth.py      # Bearer token auth template
```

### Auth Implementation

The generated `middleware/auth.py` is a **template** with TODOs. You need to implement:

**1. Token extraction** (depends on your transport):
```python
def _extract_token(self, context: MiddlewareContext) -> str | None:
    # Example for HTTP:
    # headers = context.request.headers
    # auth_header = headers.get("Authorization", "")
    # if auth_header.startswith("Bearer "):
    #     return auth_header[7:]
    return None  # TODO: Implement
```

**2. Token validation** (your auth logic):
```python
def _validate_token(self, token: str) -> bool:
    # Option A: Check database
    # async with get_session() as session:
    #     user = await session.get(User, token=token)
    #     return user is not None

    # Option B: Validate JWT
    # try:
    #     jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    #     return True
    # except:
    #     return False

    return len(token) > 0  # TODO: Replace
```

**3. Register in main.py:**
```python
from middleware.auth import BearerAuthMiddleware

mcp.add_middleware(BearerAuthMiddleware())  # Add before other middleware
```

This can be customized for any auth strategy!

---

**Available Decorators:**

- `@make_async_background`: Converts a synchronous function to run in a background thread
- `@with_retry(max_retries=3, base_backoff=1.5, jitter=1.0)`: Adds retry logic with exponential backoff
- `@with_concurrency_limit(max_concurrency=10)`: Limits concurrent executions

### Step 3: Update Tests

Update the generated test file in `tests/<server_name>.py` to validate your tool implementations.

**Test Structure:**

```python
import asyncio
import sys
from pathlib import Path
import pytest

# Add the server directory to the path
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp_servers" / "<server_name>"))

from tools.my_tool import my_tool

class TestMyTool:
    """Unit tests for the my_tool MCP tool."""

    @pytest.mark.asyncio
    async def test_basic_functionality(self):
        """Test basic tool functionality."""
        result = await my_tool("test_input")
        assert result is not None
        assert isinstance(result, str)

    @pytest.mark.asyncio
    async def test_edge_cases(self):
        """Test edge cases and error handling."""
        result = await my_tool("")
        assert result is not None
```

**Running Tests:**

```bash
# Run all tests
uv run pytest tests/ -v

# Run tests for a specific server
uv run pytest tests/test_<server_name>.py -v

# Run a specific test
uv run pytest tests/test_<server_name>.py::TestClass::test_method -v
```

**Best Practices for Testing:**

- Test both successful execution and error conditions
- Validate return types and data structures
- Test concurrent execution if your tool will handle multiple requests
- Mock external dependencies to ensure unit tests are isolated
- Test async behavior explicitly

## Running an MCP Server

### Start the Server

After implementing your tools and tests, run the server:

```bash
cd mcp_servers/<server_name>
uv run python main.py
```

The server will start and listen for MCP protocol requests.

## Project Structure

```
.
├── mcp_servers/              # All MCP server implementations
│   └── <server_name>/
│       ├── main.py           # Server entry point with middleware configuration
│       ├── middleware/       # Custom middleware implementations
│       ├── utils/            # Utility functions and decorators
│       └── tools/            # Tool implementations (one per file)
├── scripts/
│   └── create_mcp_server.py  # Server generation script
├── tests/                    # Test files (one per server)
└── pyproject.toml            # Project configuration and dependencies
```

## Architecture

### Middleware Stack

The framework includes three middleware layers by default:

1. **ErrorHandlingMiddleware**: Catches and formats exceptions, optionally includes tracebacks
2. **RetryMiddleware**: Automatically retries failed operations
3. **LoggingMiddleware**: Logs all requests and responses for debugging

Middleware executes in the order added to the server configuration.

### Async Execution Model

All tools are expected to be async functions. The `@make_async_background` decorator allows you to write synchronous code that will be executed in a background thread pool, preventing blocking of the async event loop.

## Example: Spider-Man Quote Server

An example implementation is provided in `mcp_servers/spider_man_quote/` which demonstrates:

- Simple tool implementation returning random data
- Proper async decoration
- Comprehensive test coverage including randomness validation and concurrent execution

Review this example to understand the expected patterns and practices.

## Development Workflow

1. **Generate**: `uv run python scripts/create_mcp_server.py <server_name>`
2. **Implement**: Edit `mcp_servers/<server_name>/tools/<server_name>.py`
3. **Test**: `uv run pytest tests/<server_name>.py -v`
4. **Lint**: `uv run ruff check . --fix && uv run ruff format .`
5. **Commit**: Pre-commit hooks run automatically

## Acceptance Testing with mcp-testing

For comprehensive acceptance testing that matches live API behavior, this repository includes the **mcp-testing** framework. This implements the **Mercor acceptance testing requirements** (Nov 13, 2025):

✅ Obtain API key and capture live responses
✅ Generate exhaustive success/error test cases automatically
✅ Create pytest acceptance tests from fixtures
✅ Validate MCP tools match real API behavior

### Quick Start (5 Minutes)

**Step 1: Install dependencies**
```bash
uv sync
```

**Step 2: Run interactive setup (handles everything)**
```bash
uv run mcp-setup-tests
```

This interactive wizard will:
- Prompt for your API URL
- Prompt for API token (securely hidden)
- Prompt for endpoints to test
- Create `.env` file automatically
- Generate **~16 test cases per endpoint** (success, errors, edge cases)
- Create `tests/test_api_tool_acceptance.py` with pytest tests

**Step 3: Map your MCP tools to endpoints**
```python
# Edit tests/test_api_tool_acceptance.py
# The generated file has a router with TODOs for each endpoint
# Import your tools and map them in the if/elif structure:

# Example (multiple tools - FactSet/ADP pattern):
from mcp_servers.your_server.tools import get_users, get_posts
if endpoint.startswith('/users'):
    return await get_users(method, endpoint, params, data)
elif endpoint.startswith('/posts'):
    return await get_posts(method, endpoint, params, data)

# OR (single router tool - QuickBooks pattern):
from mcp_servers.your_server.tools import your_api_router
return await your_api_router(method, endpoint, params, data)
```

**Step 4: Run tests and implement until they pass**
```bash
uv run pytest tests/test_api_tool_acceptance.py -v
# Implement your tools in mcp_servers/your_server/tools/
# Re-run tests until all pass ✅
```

**Alternative: Advanced users can use CLI directly**
```bash
# Skip interactive prompts, provide all args
uv run python -m mcp_testing.auto_testing \
  --api-url https://api.your-service.com/v1 \
  --token-env YOUR_API_TOKEN \
  --endpoints /endpoint1,/endpoint2
```

**Full documentation:** See [packages/mcp_testing/README.md](packages/mcp_testing/README.md)

### Common Commands

```bash
# Generate new server
uv run python scripts/create_mcp_server.py my_server

# Run tests
uv run pytest tests/ -v

# Lint and format
uv run ruff check . --fix
uv run ruff format .

# Run server
cd mcp_servers/<server_name>
uv run python main.py

# Pre-commit (runs automatically on commit)
uv run pre-commit run --all-files
```

## Troubleshooting

**Import Errors:**

If you encounter import errors when running tests, ensure the path manipulation at the top of your test file is correct:

```python
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp_servers" / "<server_name>"))
```

**Async Warnings:**

If you see warnings about coroutines not being awaited, ensure all async functions are properly awaited:

```python
result = await my_async_function()  # Correct
result = my_async_function()        # Incorrect - returns a coroutine object
```

**Type Checking:**

Each server includes a `pyrightconfig.json` for type checking. Run type checks with:

```bash
uv run pyright mcp_servers/<server_name>/
```

## Contributing

1. Use the generation script: `uv run python scripts/create_mcp_server.py <name>`
2. Follow patterns from `mcp_servers/spider_man_quote/`
3. Write comprehensive tests with good coverage
4. Use type hints for all functions
5. Run `uv run ruff check . --fix && uv run ruff format .` before committing
6. Ensure tests pass: `uv run pytest tests/ -v`

Pre-commit hooks will automatically enforce linting and formatting standards.

---

## Complete Example: Building a Weather API with TDD

Here's a detailed walkthrough of building a real weather API from scratch using spec-driven TDD.

### Step 1: Generate the server

```bash
uv run python scripts/create_mcp_server.py weather --with-models
```

### Step 2: Define your API contract

Edit `mcp_servers/weather/models.py`:

```python
from pydantic import BaseModel, Field

class WeatherRequest(BaseModel):
    """Get weather for a city."""
    city: str = Field(..., description="City name", examples=["San Francisco"])
    units: str = Field("fahrenheit", description="Temperature units")

class WeatherResponse(BaseModel):
    """Weather data response."""
    temperature: float = Field(..., description="Current temperature")
    conditions: str = Field(..., description="Weather description", examples=["Sunny"])
    humidity: int = Field(..., ge=0, le=100, description="Humidity percentage")
```

### Step 3: Run tests (RED)

Edit `mcp_servers/tests/test_weather.py` to reflect new schema:

```python
 @pytest.mark.asyncio
    async def test_basic_functionality(self):
        """Test that tool returns valid response for valid input."""
        # Arrange: Create a valid request
        request = WeatherRequest(city="San Francisco")

        # Act: Call the tool
        response = await weather(request)

        # Assert: Response matches schema
        assert isinstance(response, WeatherResponse)
        assert response.temperature is not None
  
  ...
```

Run tests:

```bash
uv run pytest tests/test_weather.py -v

# Output:
# tests/test_weather.py::TestWeatherTool::test_basic_functionality FAILED
# tests/test_weather.py::TestWeatherTool::test_validates_request_schema PASSED
# tests/test_weather.py::TestWeatherTool::test_response_matches_schema FAILED
```

### Step 4: Implement your tool (GREEN)

Edit `mcp_servers/weather/tools/weather.py`:

```python
import requests
from models import WeatherRequest, WeatherResponse
from utils.decorators import make_async_background

@make_async_background
def weather(request: WeatherRequest) -> WeatherResponse:
    """Get weather data from external API."""
    # Call weather API
    api_url = f"https://wttr.in/{request.city}?format=j1"
    response = requests.get(api_url).json()

    # Parse and validate response
    current = response['current_condition'][0]
    temp = float(current['temp_F']) if request.units == "fahrenheit" else float(current['temp_C'])

    return WeatherResponse(
        temperature=temp,
        conditions=current['weatherDesc'][0]['value'],
        humidity=int(current['humidity'])
    )
```

### Step 5: Tests still pass! ✅

```bash
uv run pytest tests/test_weather.py -v
# All 3 tests passed!
```

### Step 6: Generate API documentation

```bash
uv run python scripts/generate_openapi.py weather
# ✅ Generated OpenAPI spec: docs/weather_openapi.yaml
```

View the docs at [Swagger Editor](https://editor.swagger.io/) - paste the YAML content.

### Step 7: Add more features (repeat loop)

Want to add forecast? Update `models.py` with new fields, run tests (RED), implement (GREEN)!

---

## Common Commands (Quick Reference)

```bash
# === Setup (one time) ===
uv sync --all-extras                          # Install all dependencies
uv run pre-commit install                     # Install git hooks (optional)

# === Create servers ===
uv run python scripts/create_mcp_server.py my_server --with-models  # Spec-driven TDD (recommended)
uv run python scripts/create_mcp_server.py my_server                # Standard mode

# === Testing ===
uv run pytest tests/ -v                       # Run all tests
uv run pytest tests/test_my_server.py -v      # Run specific server tests

# === Code quality ===
uv run ruff check . --fix                     # Auto-fix linting issues
uv run ruff format .                          # Format code

# === API Documentation ===
uv run python scripts/generate_openapi.py my_server  # Generate OpenAPI docs from Pydantic models

# === Run server ===
cd mcp_servers/my_server
uv run python main.py                         # Start the MCP server
```

---

## Advanced Troubleshooting

### "ModuleNotFoundError: No module named 'tools'"

**Cause:** Missing `__init__.py` files in server directories.

**Fix:** This should be auto-created by the script. If missing, manually add them:
```bash
touch mcp_servers/<server_name>/tools/__init__.py
touch mcp_servers/<server_name>/utils/__init__.py
touch mcp_servers/<server_name>/middleware/__init__.py
```

### "Pydantic ValidationError" when running tests

**This is a good thing!** It means Pydantic is catching invalid data before it reaches your code.

**Common fixes:**
- Check your model definitions in `models.py`
- Ensure test data matches the field types (str vs int, etc.)
- Use `Field(..., description="...")` for required fields
- Use `Field(default_value, description="...")` for optional fields
- Use validators like `ge=0, le=100` for range constraints

### Tests failing with "NotImplementedError"

**This is expected in TDD!** The RED phase means tests should fail until you implement the tool.

**The TDD cycle:**
1. **RED** - Tests fail (you haven't implemented yet)
2. **GREEN** - Write just enough code to make tests pass
3. **REFACTOR** - Improve code while keeping tests green

Don't skip the RED phase - it confirms your tests actually test something!

### "uv command not found"

**Solution:** Install uv first:
```bash
# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Windows
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"
```

After installation, restart your terminal.

### Pre-commit hooks not running

**Solution:**
```bash
uv run pre-commit install
```

This installs git hooks that run automatically on `git commit`.

### "Import error" when generating OpenAPI docs

**Cause:** Server wasn't created with `--with-models` flag.

**Solution:** OpenAPI generation only works for servers with `models.py`. Create with:
```bash
uv run python scripts/create_mcp_server.py my_server --with-models
```

### Tests are collecting functions from my tools

**Fixed!** `tests/conftest.py` now filters out tool functions from test collection.

If you still see this, check that `norecursedirs = ["mcp_servers"]` is in `pyproject.toml`.

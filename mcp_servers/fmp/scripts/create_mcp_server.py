#!/usr/bin/env python3
"""
Script to generate a new MCP server from the spider_man_quote template.
Usage: python scripts/create_mcp_server.py <server_name>
Example: python scripts/create_mcp_server.py weather_api
"""

import argparse
import re
import sys
from pathlib import Path


def to_snake_case(name: str) -> str:
    """Convert a string to snake_case."""
    # Replace hyphens and spaces with underscores
    name = name.replace("-", "_").replace(" ", "_")
    # Insert underscores before uppercase letters
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", name)
    return name.lower()


def to_pascal_case(name: str) -> str:
    """Convert a string to PascalCase."""
    # Replace hyphens and underscores with spaces
    name = name.replace("-", " ").replace("_", " ")
    # Capitalize each word and join them
    return "".join(word.capitalize() for word in name.split())


def to_title_case(name: str) -> str:
    """Convert a string to Title Case."""
    # Replace hyphens and underscores with spaces
    name = name.replace("-", " ").replace("_", " ")
    # Capitalize each word
    return " ".join(word.capitalize() for word in name.split())


def get_model_class_names(pascal_case: str) -> tuple[str, str]:
    """Get Request/Response class names, handling Test* prefix to avoid pytest collection."""
    # If name starts with "Test", strip it to avoid pytest collection issues
    base = pascal_case[4:] if pascal_case.startswith("Test") else pascal_case
    return f"{base}Request", f"{base}Response"


def generate_main_py(server_name: str, snake_case_name: str, with_auth: bool = False) -> str:
    """Generate the main.py file content."""
    auth_imports = ""
    auth_setup = ""

    if with_auth:
        auth_imports = """
# Authentication imports (requires: pip install -e ../../packages/mcp_auth)
from mcp_auth import create_login_tool, require_scopes, public_tool
from middleware.auth import setup_auth
"""
        auth_setup = f"""
# Setup authentication (must be done before creating login tool)
auth_service = setup_auth(mcp, users_file="users.json")

# Authentication Tools
# Login tool for getting access tokens
login_func = create_login_tool(auth_service)

@mcp.tool(name="login_tool")
async def login_tool_wrapper(username: str, password: str) -> dict:
    \"\"\"Login with username and password to get an access token.\"\"\"
    return await login_func(username, password)


# Example: Public tool (no authentication required)
@mcp.tool()
@public_tool
async def get_server_info() -> dict:
    \"\"\"Get public server information. No authentication required.\"\"\"
    return {{
        "name": "{server_name}",
        "status": "running",
        "features": {{
            "authentication": True,
            "authorization": "RBAC with scopes",
        }},
    }}


# Example: Protected tool requiring 'read' scope
@mcp.tool()
@require_scopes("read")
async def read_data() -> dict:
    \"\"\"
    Read data from the system.

    Required scope: read
    \"\"\"
    return {{"data": ["item1", "item2", "item3"], "count": 3}}
"""

    return f"""from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)
from middleware.logging import LoggingMiddleware
from tools.{snake_case_name} import {snake_case_name}{auth_imports}

mcp = FastMCP("{server_name}")
mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True))
mcp.add_middleware(RetryMiddleware())
mcp.add_middleware(LoggingMiddleware())

# Register tools
mcp.tool({snake_case_name})
# To add more tools:
# from tools.my_new_tool import my_new_tool
# mcp.tool(my_new_tool)
{auth_setup}
if __name__ == "__main__":
    mcp.run()
"""


def generate_tool_py(snake_case_name: str, title_case_name: str) -> str:
    """Generate the tool file content."""
    return f"""import sys
from pathlib import Path

# Ensure we can import from the server directory
sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger  # noqa: I001

from utils.decorators import make_async_background


@make_async_background
def {snake_case_name}(input_param: str) -> str:
    \"\"\"
    {title_case_name} tool - implement your logic here.

    Args:
        input_param: Description of the input parameter

    Returns:
        Description of the return value
    \"\"\"
    # TODO: Implement your tool logic here
    logger.info(f"Running {snake_case_name} with input: {{input_param}}")

    result = f"Processed: {{input_param}}"
    return result
"""


def generate_logging_middleware() -> str:
    """Generate the logging middleware file content."""
    return """from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools.tool import ToolResult
from loguru import logger


class LoggingMiddleware(Middleware):
    async def on_request(self, context: MiddlewareContext, call_next: CallNext):
        fastmcp_context = context.fastmcp_context
        if not fastmcp_context:
            logger.error("No fastmcp context")
            raise ValueError("LoggingMiddleware: No fastmcp context")

        response = await call_next(context)
        if isinstance(response, ToolResult):
            logger.debug(f"{context.method} returned {response.content}")
        else:
            logger.debug(f"{context.method} returned {response}")
        return response
"""


def generate_auth_middleware() -> str:
    """Generate production-ready auth setup using mcp-auth package."""
    return """\"""Authentication setup using mcp-auth package.

This file provides helper functions for setting up authentication.
See: packages/mcp_auth/README.md for full documentation.
\"""

from pathlib import Path
from mcp_auth import AuthService, AuthGuard


def setup_auth(mcp_instance, users_file: str = "users.json"):
    \"""
    Setup authentication for the MCP server.

    Args:
        mcp_instance: The FastMCP server instance
        users_file: Path to users.json file (default: "users.json")

    Usage in main.py:
        from middleware.auth import setup_auth

        # After registering all tools
        setup_auth(mcp, users_file="users.json")
    \"""
    users_path = Path(__file__).parent.parent / users_file
    auth_service = AuthService(users_path)

    # Add AuthGuard middleware with auto-discovery
    auth_guard = AuthGuard(
        auth_service,
        mcp_instance=mcp_instance,  # Auto-discovers permissions from decorators
        public_tools=["login_tool"],  # Tools that don't require auth
        default_deny=True,  # Deny tools without explicit permissions
    )

    mcp_instance.add_middleware(auth_guard)

    return auth_service
"""


def generate_decorators() -> str:
    """Generate the decorators file content."""
    return """import asyncio
import functools
import random
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

import asyncer
from loguru import logger

_P = ParamSpec("_P")
_R = TypeVar("_R")


def make_async_background(
    fn: Callable[_P, _R],
) -> Callable[_P, Awaitable[_R]]:
    \"\"\"
    Make a function run in the background (thread) and return an awaitable.
    \"\"\"

    @functools.wraps(fn)
    async def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        return await asyncer.asyncify(fn)(*args, **kwargs)

    return wrapper


def with_retry(max_retries=3, base_backoff=1.5, jitter: float = 1.0):
    \"\"\"
    This decorator is used to retry a function if it fails.
    It will retry the function up to the specified number of times, with a backoff between attempts.
    \"\"\"

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            for attempt in range(1, max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    is_last_attempt = attempt >= max_retries
                    if is_last_attempt:
                        logger.error(
                            f"Error in {func.__name__}: {repr(e)}, after {max_retries} attempts"
                        )
                        raise

                    backoff = base_backoff * (2 ** (attempt - 1))
                    jitter_delay = random.uniform(0, jitter) if jitter > 0 else 0
                    delay = backoff + jitter_delay
                    logger.warning(f"Error in {func.__name__}: {repr(e)}")
                    await asyncio.sleep(delay)

        return wrapper

    return decorator


def with_concurrency_limit(max_concurrency: int):
    \"\"\"
    This decorator is used to limit the concurrency of a function.
    It will limit concurrent calls to the function to the specified number
    within the same event loop.
    \"\"\"

    _semaphores: dict[int, asyncio.Semaphore] = {}

    def decorator(func):
        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            loop = asyncio.get_running_loop()
            loop_id = id(loop)

            sem = _semaphores.get(loop_id)
            if sem is None:
                sem = asyncio.Semaphore(max_concurrency)
                _semaphores[loop_id] = sem

            async with sem:
                return await func(*args, **kwargs)

        return wrapper

    return decorator
"""


def generate_tools_init(snake_case_name: str) -> str:
    """Generate the tools/__init__.py file content."""
    return ""


def generate_pyrightconfig() -> str:
    """Generate the pyrightconfig.json file content."""
    return """{
  "include": [
    "."
  ],
  "extraPaths": [
    "."
  ],
  "typeCheckingMode": "standard"
}
"""


def generate_models_file(snake_case_name: str, title_case_name: str) -> str:
    """Generate the Pydantic models file content."""
    pascal = to_pascal_case(snake_case_name)
    request_class, response_class = get_model_class_names(pascal)
    return f"""\"\"\"Pydantic models for {snake_case_name}.

Define your API specification here using Pydantic models.
These models will:
1. Validate inputs/outputs automatically
2. Generate type hints for IDE support
3. Serve as documentation
4. Enable test generation

TIP: Start simple, add fields as you need them!
\"\"\"

from pydantic import BaseModel, Field


class {request_class}(BaseModel):
    \"\"\"Input specification for {snake_case_name}.

    TODO: Define your input parameters here.
    Example:
        query: str = Field(..., description="Search query")
        limit: int = Field(10, description="Max results")
    \"\"\"

    input_param: str = Field(..., description="TODO: Replace with your actual input parameters")


class {response_class}(BaseModel):
    \"\"\"Output specification for {snake_case_name}.

    TODO: Define your response structure here.
    Example:
        results: list[str] = Field(..., description="Search results")
        total: int = Field(..., description="Total count")
    \"\"\"

    result: str = Field(..., description="TODO: Replace with your actual response structure")
"""


def generate_tool_with_models(snake_case_name: str, title_case_name: str) -> str:
    """Generate tool file using Pydantic models."""
    pascal = to_pascal_case(snake_case_name)
    request_class, response_class = get_model_class_names(pascal)
    return f"""import sys
from pathlib import Path

# Ensure we can import from the server directory
sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger  # noqa: I001

from models import {request_class}, {response_class}
from utils.decorators import make_async_background


@make_async_background
def {snake_case_name}(request: {request_class}) -> {response_class}:
    \"\"\"
    {title_case_name} tool with Pydantic validation.

    This function signature is validated by Pydantic:
    - Input must match {request_class} schema
    - Output must match {response_class} schema

    TODO: Implement your logic here to make the tests pass!

    TDD Workflow:
    1. Run tests: uv run pytest tests/test_{snake_case_name}.py -v (RED - fail)
    2. Implement logic here (GREEN - pass)
    3. Refactor and improve

    Args:
        request: Validated input matching {request_class} schema

    Returns:
        Response matching {response_class} schema
    \"\"\"
    logger.info(f"Processing {snake_case_name} request: {{request}}")

    # TODO: Implement your logic here
    # For now, return a placeholder that matches the schema
    return {response_class}(
        result=f"Processed: {{request.input_param}}"
    )
"""


def generate_test_with_models(snake_case_name: str, title_case_name: str) -> str:
    """Generate test file using Pydantic models."""
    pascal = to_pascal_case(snake_case_name)
    request_class, response_class = get_model_class_names(pascal)
    return f"""\"\"\"Test-Driven Development for {snake_case_name}.

TDD WORKFLOW:
1. Run tests: uv run pytest tests/test_{snake_case_name}.py -v (RED - will fail)
2. Implement: Edit mcp_servers/{snake_case_name}/tools/{snake_case_name}.py (GREEN - pass)
3. Refactor: Improve code while keeping tests passing

TIPS:
- Update models.py to define your API spec
- Tests validate the spec automatically
- Pydantic ensures type safety
\"\"\"

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

# Add the server to path
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp_servers" / "{snake_case_name}"))

from models import {request_class}, {response_class}
from tools.{snake_case_name} import {snake_case_name}


class Test{pascal}Tool:
    \"\"\"Test suite for {snake_case_name} with Pydantic validation.\"\"\"

    @pytest.mark.asyncio
    async def test_basic_functionality(self):
        \"\"\"Test that tool returns valid response for valid input.\"\"\"
        # Arrange: Create a valid request
        request = {request_class}(input_param="test")

        # Act: Call the tool
        response = await {snake_case_name}(request)

        # Assert: Response matches schema
        assert isinstance(response, {response_class})
        assert response.result is not None

    @pytest.mark.asyncio
    async def test_validates_request_schema(self):
        \"\"\"Test that invalid requests are rejected by Pydantic.\"\"\"
        # Pydantic should reject invalid data
        with pytest.raises(ValidationError):
            {request_class}(input_param=123)  # Wrong type

    @pytest.mark.asyncio
    async def test_response_matches_schema(self):
        \"\"\"Test that response conforms to Response schema.\"\"\"
        request = {request_class}(input_param="test")
        response = await {snake_case_name}(request)

        # Validate response can be serialized/deserialized
        json_data = response.model_dump()
        validated = {response_class}.model_validate(json_data)
        assert validated == response


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
"""


def generate_test_file(snake_case_name: str, title_case_name: str) -> str:
    """Generate the test file content."""
    return f"""import sys
from pathlib import Path

import pytest

# Add the parent directory to the path so we can import the module
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp_servers" / "{snake_case_name}"))

from tools.{snake_case_name} import {snake_case_name}  # noqa: F401


class Test{to_pascal_case(snake_case_name)}:
    \"\"\"Unit tests for the {snake_case_name} MCP tool.\"\"\"

    @pytest.mark.asyncio
    async def test_not_implemented(self):
        \"\"\"Test not implemented - implement your tests here.\"\"\"
        pytest.fail("Test not implemented")


if __name__ == "__main__":
    # Allow running the tests directly
    pytest.main([__file__, "-v"])
"""


def generate_db_models(snake_case_name: str) -> str:
    """Generate SQLAlchemy database models."""
    pascal = to_pascal_case(snake_case_name)
    return f"""\"\"\"SQLAlchemy database models for {snake_case_name}.

These are your database/ORM models (separate from Pydantic API models).
\"\"\"

from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now():
    \"\"\"Get current UTC time.\"\"\"
    return datetime.now(UTC)


class Base(DeclarativeBase):
    \"\"\"Base class for all database models.\"\"\"

    pass


class {pascal}(Base):
    \"\"\"Example database model for {snake_case_name}.

    TODO: Update fields to match your database schema.
    \"\"\"

    __tablename__ = "{snake_case_name}_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utc_now, onupdate=utc_now)
"""


def generate_db_session() -> str:
    """Generate database session management."""
    return """\"""Database session management.

Provides async SQLAlchemy session for database operations.
\"""

from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from db.models import Base

# SQLite database (change to PostgreSQL/MySQL as needed)
DATABASE_URL = "sqlite+aiosqlite:///./data.db"

engine = create_async_engine(DATABASE_URL, echo=True)
AsyncSessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db():
    \"""Initialize database (create tables).\"""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@asynccontextmanager
async def get_session():
    \"""Get database session.

    Usage:
        async with get_session() as session:
            result = await session.execute(select(MyModel))
    \"""
    async with AsyncSessionLocal() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
"""


def generate_repository_base() -> str:
    """Generate base repository pattern."""
    return """\"""Base repository pattern for database operations.

Provides common CRUD operations for models.
\"""

from typing import Generic, TypeVar

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

ModelType = TypeVar("ModelType")


class BaseRepository(Generic[ModelType]):
    \"""Base repository with common database operations.\"""

    def __init__(self, model: type[ModelType], session: AsyncSession):
        self.model = model
        self.session = session

    async def create(self, **kwargs) -> ModelType:
        \"""Create a new record.\"""
        instance = self.model(**kwargs)
        self.session.add(instance)
        await self.session.flush()
        return instance

    async def get_by_id(self, id: int) -> ModelType | None:
        \"""Get record by ID.\"""
        result = await self.session.execute(
            select(self.model).where(self.model.id == id)
        )
        return result.scalar_one_or_none()

    async def list_all(self, limit: int = 100) -> list[ModelType]:
        \"""List all records.\"""
        result = await self.session.execute(select(self.model).limit(limit))
        return list(result.scalars().all())

    async def delete(self, id: int) -> bool:
        \"""Delete record by ID.\"""
        instance = await self.get_by_id(id)
        if instance:
            await self.session.delete(instance)
            await self.session.flush()
            return True
        return False
"""


def generate_alembic_ini(snake_case_name: str) -> str:
    """Generate alembic.ini configuration."""
    return f"""# Alembic migration configuration for {snake_case_name}

[alembic]
script_location = db/migrations
prepend_sys_path = .
sqlalchemy.url = sqlite+aiosqlite:///./data.db

[loggers]
keys = root,sqlalchemy,alembic

[handlers]
keys = console

[formatters]
keys = generic

[logger_root]
level = WARN
handlers = console

[logger_sqlalchemy]
level = WARN
handlers =
qualname = sqlalchemy.engine

[logger_alembic]
level = INFO
handlers =
qualname = alembic

[handler_console]
class = StreamHandler
args = (sys.stderr,)
level = NOTSET
formatter = generic

[formatter_generic]
format = %(levelname)-5.5s [%(name)s] %(message)s
"""


def generate_config(snake_case_name: str) -> str:
    """Generate config.py with pydantic-settings."""
    pascal = to_pascal_case(snake_case_name)
    return f"""\"""Configuration management for {snake_case_name}.

Uses pydantic-settings to load from environment variables or .env file.
\"""

from pydantic_settings import BaseSettings, SettingsConfigDict


class {pascal}Settings(BaseSettings):
    \"""Application settings loaded from environment.

    TODO: Add your configuration here.

    Example:
        from pydantic import Field

        api_key: str = Field(..., description="API key")
        database_url: str = Field("sqlite+aiosqlite:///./data.db")
        debug: bool = Field(False, description="Debug mode")
    \"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )


# Global settings instance (loads on import)
settings = {pascal}Settings()
"""


def generate_env_example(snake_case_name: str) -> str:
    """Generate .env.example template."""
    return f"""# Environment configuration for {snake_case_name}
# Copy this to .env and fill in your values

# Example configuration (uncomment and set):
# API_KEY=your-api-key-here
# DATABASE_URL=sqlite+aiosqlite:///./data.db
# DEBUG=false
# MAX_RETRIES=3
"""


def generate_users_json() -> str:
    """Generate users.json file for authentication."""
    return """{
  "admin": {
    "userId": "user_001",
    "password": "admin123",
    "roles": ["admin", "user"],
    "scopes": ["read", "write", "delete"]
  },
  "viewer": {
    "userId": "user_002",
    "password": "view123",
    "roles": ["viewer"],
    "scopes": ["read"]
  }
}
"""


def generate_alembic_env() -> str:
    """Generate Alembic env.py for migrations."""
    return """\"""Alembic environment configuration.\"""

import asyncio  # noqa: I001
from logging.config import fileConfig

from alembic import context
from db.models import Base
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

config = context.config
fileConfig(config.config_file_name)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    \"""Run migrations in 'offline' mode.\"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    \"""Run migrations in 'online' mode with async engine.\"""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    \"""Run migrations in 'online' mode.\"""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
"""


def create_mcp_server(
    name: str,
    base_path: Path,
    with_models: bool = False,
    with_database: bool = False,
    with_config: bool = False,
    with_auth: bool = False,
    with_ci: bool = False,
) -> None:
    """Create a new MCP server with the given name."""
    snake_case_name = to_snake_case(name)
    pascal_case_name = to_pascal_case(name)
    title_case_name = to_title_case(name)

    # Create the server directory
    server_path = base_path / "mcp_servers" / snake_case_name
    if server_path.exists():
        print(f"Error: Server '{snake_case_name}' already exists at {server_path}")
        sys.exit(1)

    # Determine mode
    features = []
    if with_models:
        features.append("Pydantic models")
    if with_database:
        features.append("database")
    if with_config:
        features.append("config")
    if with_auth:
        features.append("auth")
    if with_ci:
        features.append("CI templates")
    mode = f"with {', '.join(features)}" if features else "standard"

    print(f"Creating MCP server: {pascal_case_name} ({mode})")
    print(f"Directory: {server_path}")

    # Create directory structure
    server_path.mkdir(parents=True, exist_ok=True)
    (server_path / "middleware").mkdir(exist_ok=True)
    (server_path / "utils").mkdir(exist_ok=True)
    (server_path / "tools").mkdir(exist_ok=True)

    # Create database directories if needed
    if with_database:
        (server_path / "db").mkdir(exist_ok=True)
        (server_path / "db" / "migrations").mkdir(exist_ok=True)
        (server_path / "db" / "migrations" / "versions").mkdir(exist_ok=True)
        (server_path / "repositories").mkdir(exist_ok=True)

    # Create __init__.py files to make directories importable
    (server_path / "middleware" / "__init__.py").write_text("")
    (server_path / "utils" / "__init__.py").write_text("")
    if with_database:
        (server_path / "db" / "__init__.py").write_text("")
        (server_path / "db" / "migrations" / "__init__.py").write_text("")
        (server_path / "repositories" / "__init__.py").write_text("")

    # Don't create tools/__init__.py - treating as namespace package prevents import conflicts
    # when multiple servers are added to sys.path

    # Create main.py
    main_content = generate_main_py(pascal_case_name, snake_case_name, with_auth=with_auth)
    (server_path / "main.py").write_text(main_content)
    print("✓ Created main.py")

    # Create models.py if using Pydantic models
    if with_models:
        models_content = generate_models_file(snake_case_name, title_case_name)
        (server_path / "models.py").write_text(models_content)
        print("✓ Created models.py (define your API spec here!)")

    # Create tool file
    if with_models:
        tool_content = generate_tool_with_models(snake_case_name, title_case_name)
    else:
        tool_content = generate_tool_py(snake_case_name, title_case_name)
    (server_path / "tools" / f"{snake_case_name}.py").write_text(tool_content)
    print(f"✓ Created tools/{snake_case_name}.py")

    # Create middleware
    logging_content = generate_logging_middleware()
    (server_path / "middleware" / "logging.py").write_text(logging_content)
    print("✓ Created middleware/logging.py")

    if with_auth:
        auth_content = generate_auth_middleware()
        (server_path / "middleware" / "auth.py").write_text(auth_content)
        print("✓ Created middleware/auth.py (uses mcp-auth package)")

        # Create users.json with example users
        users_content = generate_users_json()
        (server_path / "users.json").write_text(users_content)
        print("✓ Created users.json (example users: admin, viewer)")

    # Create utils
    decorators_content = generate_decorators()
    (server_path / "utils" / "decorators.py").write_text(decorators_content)
    print("✓ Created utils/decorators.py")

    # Create pyrightconfig.json
    pyright_content = generate_pyrightconfig()
    (server_path / "pyrightconfig.json").write_text(pyright_content)
    print("✓ Created pyrightconfig.json")

    # Create config files if needed
    if with_config:
        config_content = generate_config(snake_case_name)
        (server_path / "config.py").write_text(config_content)
        print("✓ Created config.py")

        env_example_content = generate_env_example(snake_case_name)
        (server_path / ".env.example").write_text(env_example_content)
        print("✓ Created .env.example (copy to .env and configure)")

    # Create database files if needed
    if with_database:
        # DB models
        db_models_content = generate_db_models(snake_case_name)
        (server_path / "db" / "models.py").write_text(db_models_content)
        print("✓ Created db/models.py")

        # Session management
        db_session_content = generate_db_session()
        (server_path / "db" / "session.py").write_text(db_session_content)
        print("✓ Created db/session.py")

        # Repository base
        repo_content = generate_repository_base()
        (server_path / "repositories" / "base.py").write_text(repo_content)
        print("✓ Created repositories/base.py")

        # Alembic configuration
        alembic_ini_content = generate_alembic_ini(snake_case_name)
        (server_path / "alembic.ini").write_text(alembic_ini_content)
        print("✓ Created alembic.ini")

        # Alembic env.py
        alembic_env_content = generate_alembic_env()
        (server_path / "db" / "migrations" / "env.py").write_text(alembic_env_content)
        print("✓ Created db/migrations/env.py")

    # Create test file
    test_dir = base_path / "tests"
    test_dir.mkdir(exist_ok=True)
    if with_models:
        test_content = generate_test_with_models(snake_case_name, title_case_name)
    else:
        test_content = generate_test_file(snake_case_name, title_case_name)
    test_filename = f"test_{snake_case_name}.py"
    (test_dir / test_filename).write_text(test_content)
    print(f"✓ Created tests/{test_filename}")

    # Generate CI templates if requested
    if with_ci:
        templates_dir = base_path / "templates" / "ci"
        if templates_dir.exists():
            replacements = {
                "{{SERVER_NAME}}": name,
                "{{SERVER_NAME_SNAKE}}": snake_case_name,
            }

            # Generate GitHub Actions workflow
            ci_template = templates_dir / "mcp-ci.yml"
            if ci_template.exists():
                ci_content = ci_template.read_text()
                for key, value in replacements.items():
                    ci_content = ci_content.replace(key, value)
                ci_dir = base_path / ".github" / "workflows"
                ci_dir.mkdir(parents=True, exist_ok=True)
                (ci_dir / f"{snake_case_name}-ci.yml").write_text(ci_content)
                print(f"✓ Created .github/workflows/{snake_case_name}-ci.yml")

            # Generate Makefile
            makefile_template = templates_dir / "Makefile"
            if makefile_template.exists():
                makefile_content = makefile_template.read_text()
                for key, value in replacements.items():
                    makefile_content = makefile_content.replace(key, value)
                (base_path / "Makefile").write_text(makefile_content)
                print("✓ Created Makefile")

            # Generate pre-commit config
            precommit_template = templates_dir / "pre-commit-config.yaml"
            if precommit_template.exists():
                precommit_content = precommit_template.read_text()
                (base_path / ".pre-commit-config.yaml").write_text(precommit_content)
                print("✓ Created .pre-commit-config.yaml")
        else:
            print("⚠ CI templates not found - run from project root")

    print(f"\nSuccessfully created MCP server: {pascal_case_name}")

    if with_auth:
        print("\nAUTHENTICATION SETUP:")
        print("1. Install mcp-auth package:")
        print("   cd packages/mcp_auth && pip install -e . && cd ../..")
        print(f"2. Review users in mcp_servers/{snake_case_name}/users.json")
        print("3. Test login:")
        print(f"   cd mcp_servers/{snake_case_name} && python main.py")
        print("   # Use: admin/admin123 or viewer/view123")
        print("\nSee packages/mcp_auth/README.md for full documentation")
        print("See mcp_servers/auth_server/README.md for usage examples")

    if with_models:
        print("\nTDD WORKFLOW (Spec-Driven Development):")
        print(f"1. Define API spec: Edit mcp_servers/{snake_case_name}/models.py")
        print(f"2. Run tests (RED): uv run pytest tests/test_{snake_case_name}.py -v")
        tool_path = f"mcp_servers/{snake_case_name}/tools/{snake_case_name}.py"
        print(f"3. Implement tool (GREEN): Edit {tool_path}")
        print(f"4. Tests pass: uv run pytest tests/test_{snake_case_name}.py -v")
        print("5. Refactor and repeat!")
    elif not with_auth:
        print("\nNext steps:")
        tool_path = server_path / "tools" / f"{snake_case_name}.py"
        print(
            f"1. Edit {tool_path} to implement your tool logic. "
            "This might mean making more files for each tool to be called."
        )
        print(f"2. Edit tests/test_{snake_case_name}.py to implement your tests")

    if with_ci:
        print("\nCI TEMPLATES SETUP:")
        print("1. Install pre-commit hooks: pre-commit install")
        print("2. View available make targets: make help")
        print("3. Run all checks locally: make check")
        print("4. GitHub Actions will run on push to main")


def main():
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(
        description="Generate a new MCP server from template",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/create_mcp_server.py weather_api
  python scripts/create_mcp_server.py "My Cool Server"
  python scripts/create_mcp_server.py database-connector
        """,
    )
    parser.add_argument(
        "name",
        help="Name of the MCP server (will be converted to snake_case for directory/file names)",
    )
    parser.add_argument(
        "--with-models",
        action="store_true",
        help="Generate with Pydantic models for spec-driven development",
    )
    parser.add_argument(
        "--with-database",
        action="store_true",
        help="Generate with database support (SQLAlchemy + Alembic)",
    )
    parser.add_argument(
        "--with-config",
        action="store_true",
        help="Generate with environment configuration (pydantic-settings)",
    )
    parser.add_argument(
        "--with-auth",
        action="store_true",
        help="Generate with production authentication using mcp-auth package (RBAC + scopes)",
    )
    parser.add_argument(
        "--with-ci",
        action="store_true",
        help="Generate with standard CI templates (GitHub Actions, pre-commit, Makefile)",
    )

    args = parser.parse_args()

    # Get the base path (project root)
    script_path = Path(__file__).resolve()
    base_path = script_path.parent.parent

    create_mcp_server(
        args.name,
        base_path,
        with_models=args.with_models,
        with_database=args.with_database,
        with_config=args.with_config,
        with_auth=args.with_auth,
        with_ci=args.with_ci,
    )


if __name__ == "__main__":
    main()

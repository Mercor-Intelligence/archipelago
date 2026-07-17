#!/usr/bin/env python3
"""
Script to generate a new MCP server from the full template.

Usage: python scripts/create_mcp_server.py <server_name>
Example: python scripts/create_mcp_server.py weather_api

The script copies from templates/mcp_server_full/, replaces placeholders,
renames files, and removes features not requested via flags.
"""

import argparse
import shutil
import sys
from pathlib import Path

from mcp_scripts.logging_config import get_logger
from mcp_scripts.utils import to_pascal_case, to_snake_case, to_title_case

logger = get_logger(__name__)


def get_name_variants(name: str) -> dict[str, str]:
    """Get all name variants for placeholder replacement."""
    snake = to_snake_case(name)
    pascal = to_pascal_case(name)
    return {
        "__SNAKE_NAME__": snake,
        "__PASCAL_NAME__": pascal,
        "__UPPER_NAME__": snake.upper(),
        "__TITLE_NAME__": to_title_case(name),
    }


def validate_server_name(snake_name: str) -> None:
    """Validate server name is not reserved.

    Raises SystemExit if name conflicts with existing project files.
    """
    reserved = ["create_mcp_server"]
    if snake_name in reserved:
        logger.error("Name '%s' is reserved. Choose a different name.", snake_name)
        sys.exit(1)


def replace_in_content(content: str, replacements: dict[str, str]) -> str:
    """Replace all placeholders in content."""
    for placeholder, value in replacements.items():
        content = content.replace(placeholder, value)
    return content


def replace_in_filename(filename: str, replacements: dict[str, str]) -> str:
    """Replace placeholders in filename."""
    for placeholder, value in replacements.items():
        filename = filename.replace(placeholder, value)
    return filename


def copy_and_transform_template(
    template_dir: Path,
    target_dir: Path,
    replacements: dict[str, str],
    exclude_patterns: list[str],
) -> list[Path]:
    """
    Copy template directory to target, transforming content and filenames.

    Args:
        template_dir: Source template directory
        target_dir: Destination directory
        replacements: Dict of placeholder -> value replacements
        exclude_patterns: List of glob patterns to exclude

    Returns:
        List of created file paths
    """
    created_files = []

    for src_path in template_dir.rglob("*"):
        # Skip directories (they'll be created as needed)
        if src_path.is_dir():
            continue

        # Get relative path from template root
        rel_path = src_path.relative_to(template_dir)

        # Check if this file should be excluded
        should_exclude = False
        for pattern in exclude_patterns:
            # Handle root-level patterns (e.g., "./models.py" only matches "models.py" at root)
            if pattern.startswith("./"):
                if str(rel_path) == pattern[2:]:
                    should_exclude = True
                    break
            elif rel_path.match(pattern) or any(
                part == pattern.rstrip("/*") for part in rel_path.parts
            ):
                should_exclude = True
                break

        if should_exclude:
            continue

        # Transform the relative path (replace placeholders in filename)
        new_rel_path = Path(*[replace_in_filename(part, replacements) for part in rel_path.parts])
        dest_path = target_dir / new_rel_path

        # Create parent directories
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        # Read, transform, and write content
        try:
            content = src_path.read_text()
            transformed = replace_in_content(content, replacements)
            dest_path.write_text(transformed)
            created_files.append(dest_path)
        except UnicodeDecodeError:
            # Binary file, copy as-is
            shutil.copy2(src_path, dest_path)
            created_files.append(dest_path)

    return created_files


def get_exclude_patterns(
    with_models: bool,
    with_database: bool,
    with_config: bool,
    with_auth: bool,
    with_repository: bool,
) -> list[str]:
    """
    Determine which files/directories to exclude based on flags.

    The template has ALL features. We exclude what's not requested.
    """
    excludes = []

    if not with_database:
        excludes.extend(["db/*", "alembic.ini"])

    if not with_config:
        excludes.extend(["config.py", ".env.example"])

    if not with_auth:
        excludes.extend(["middleware/auth.py", "users.json"])

    if not with_repository:
        # repositories/data.py is repository-specific
        excludes.extend(["repositories/data.py", "schemas/*", "data/*"])
        if not with_database:
            # repositories/base.py is for database operations
            excludes.extend(["repositories/base.py", "repositories/__init__.py"])

    if not with_models and not with_repository:
        # Only exclude root-level models.py, not db/models.py
        excludes.append("./models.py")

    # Always exclude middleware/logging.py - we use mcp_middleware package instead
    excludes.append("middleware/logging.py")

    return excludes


def create_simplified_main(
    server_path: Path,
    snake_name: str,
    pascal_name: str,
    with_auth: bool,
    with_repository: bool,
    with_database: bool = False,
) -> None:
    """Create a simplified main.py when not all features are enabled."""
    auth_imports = ""
    auth_setup = ""
    db_imports = ""
    db_setup = ""
    lifespan_code = ""
    lifespan_arg = ""
    tool_imports = f"from tools.{snake_name} import {snake_name}"
    tool_registrations = f"mcp.tool({snake_name})"

    # Database setup (lifespan + db_tools)
    if with_database:
        db_imports = """import sys
from contextlib import asynccontextmanager
from pathlib import Path

"""
        lifespan_code = """from db.session import init_db
from mcp_middleware.db_tools import create_database_tools


@asynccontextmanager
async def lifespan(app):
    \"\"\"Initialize database on server startup.\"\"\"
    await init_db()
    yield

"""
        lifespan_arg = ", lifespan=lifespan"
        db_setup = """
# Database management tools (import_csv, export_csv, list_tables, clear_database)
create_database_tools(mcp, "db.session")
"""

    if with_auth:
        auth_imports = """
# Authentication imports (requires: pip install -e ../../packages/mcp_auth)
from mcp_auth import create_login_tool, public_tool, require_scopes

from middleware.auth import setup_auth"""
        auth_setup = f"""
# Setup authentication
auth_service = setup_auth(mcp, users_file="users.json")

login_func = create_login_tool(auth_service)


@mcp.tool(name="login_tool")
async def login_tool_wrapper(username: str, password: str) -> dict:
    \"\"\"Login with username and password to get an access token.\"\"\"
    return await login_func(username, password)


@mcp.tool()
@public_tool
async def get_server_info() -> dict:
    \"\"\"Get public server information. No authentication required.\"\"\"
    return {{"name": "{pascal_name}", "status": "running"}}


@mcp.tool()
@require_scopes("read")
async def read_data() -> dict:
    \"\"\"Read data from the system. Required scope: read\"\"\"
    return {{"data": ["item1", "item2", "item3"], "count": 3}}
"""

    if with_repository:
        tool_imports = f"from tools.{snake_name} import get_{snake_name}, list_{snake_name}"
        tool_registrations = f"""# Tool granularity: set TOOLS env var to enable specific tools
enabled_tools = os.getenv("TOOLS", "").split(",")
enabled_tools = [t.strip() for t in enabled_tools if t.strip()]

# Register tools conditionally based on TOOLS env var
if not enabled_tools or "get_{snake_name}" in enabled_tools:
    mcp.tool(get_{snake_name})

if not enabled_tools or "list_{snake_name}" in enabled_tools:
    mcp.tool(list_{snake_name})

# To add more tools with granularity:
# from tools.other_module import other_tool
# if not enabled_tools or "other_tool" in enabled_tools:
#     mcp.tool(other_tool)

# To add custom HTTP endpoints:
# @mcp.custom_route("/v1/health", methods=["GET"])
# async def health_check():
#     return {{"status": "healthy", "service": "{snake_name}"}}"""
    else:
        tool_registrations = f"""# Tool granularity: set TOOLS env var to enable specific tools
# Example: TOOLS="{snake_name},other_tool" to enable only those tools
# If TOOLS is empty or not set, all tools are enabled
enabled_tools = os.getenv("TOOLS", "").split(",")
enabled_tools = [t.strip() for t in enabled_tools if t.strip()]

# Register tools conditionally based on TOOLS env var
if not enabled_tools or "{snake_name}" in enabled_tools:
    mcp.tool({snake_name})

# To add more tools with granularity:
# from tools.my_new_tool import my_new_tool
# if not enabled_tools or "my_new_tool" in enabled_tools:
#     mcp.tool(my_new_tool)"""

    if with_repository:
        docstring = f'''"""MCP Server: {pascal_name}

This server uses the repository pattern for data access:
- Offline mode (default): Uses synthetic data from JSON files
- Online mode: Makes live API calls

Set {snake_name.upper()}_MODE=online to use live API.

Tool granularity:
- Set TOOLS env var to comma-separated list to enable specific tools
- Example: TOOLS="get_{snake_name},list_{snake_name}"
- If TOOLS is empty or not set, all tools are enabled
"""

'''
    else:
        docstring = ""

    content = f"""{docstring}{db_imports}import os

from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)

from mcp_middleware import LoggingMiddleware
{tool_imports}{auth_imports}
{lifespan_code}
mcp = FastMCP("{pascal_name}"{lifespan_arg})
mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True))
mcp.add_middleware(RetryMiddleware())
mcp.add_middleware(LoggingMiddleware(log_level="INFO"))
{db_setup}{auth_setup}
{tool_registrations}

if __name__ == "__main__":
    mcp.run()
"""
    (server_path / "main.py").write_text(content)


def create_simplified_tool(
    server_path: Path,
    snake_name: str,
    pascal_name: str,
    title_name: str,
    with_models: bool,
    with_repository: bool,
) -> None:
    """Create a simplified tool file when not all features are enabled."""
    if with_repository:
        # Repository version is already correct in template
        return

    if with_models:
        content = f'''from loguru import logger

from models import {pascal_name}Request, {pascal_name}Response


async def {snake_name}(request: {pascal_name}Request) -> {pascal_name}Response:
    """{title_name} tool - implement your logic here."""
    logger.info(f"Processing {snake_name} request: {{request}}")

    # TODO: Implement your logic here
    return {pascal_name}Response(result=f"Processed: {{request.input_param}}")
'''
    else:
        # Even without --with-models, we need Pydantic models for proper tool discovery
        # Create a basic models.py file
        models_content = f'''"""Pydantic models for {title_name} MCP server."""

from pydantic import BaseModel, Field


class {pascal_name}Input(BaseModel):
    """{title_name} tool input."""

    input_param: str = Field(..., description="Input parameter for the tool")


class {pascal_name}Output(BaseModel):
    """{title_name} tool output."""

    result: str = Field(..., description="Result of the operation")
'''
        (server_path / "models.py").write_text(models_content)

        content = f'''from loguru import logger

from models import {pascal_name}Input, {pascal_name}Output


async def {snake_name}(request: {pascal_name}Input) -> {pascal_name}Output:
    """{title_name} tool - implement your logic here."""
    # TODO: Implement your tool logic here
    logger.info(f"Running {snake_name} with input: {{request.input_param}}")

    return {pascal_name}Output(result=f"Processed: {{request.input_param}}")
'''
    tools_dir = server_path / "tools"
    tools_dir.mkdir(exist_ok=True)
    (tools_dir / f"{snake_name}.py").write_text(content)


def create_simplified_test(
    test_path: Path,
    snake_name: str,
    pascal_name: str,
    with_models: bool,
    with_repository: bool,
) -> None:
    """Create a simplified test file when not using repository pattern."""
    if with_repository:
        # Repository test imports get_ and list_ functions
        content = f'''import sys
from pathlib import Path

import pytest

# Add the parent directory to the path so we can import the module
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp_servers" / "{snake_name}"))

from tools.{snake_name} import get_{snake_name}, list_{snake_name}  # noqa: F401
from schemas.{snake_name} import {pascal_name}Input, {pascal_name}ListInput  # noqa: F401


class Test{pascal_name}:
    """Unit tests for the {snake_name} MCP tools."""

    @pytest.mark.asyncio
    async def test_get_{snake_name}(self):
        """Test get_{snake_name} function."""
        # TODO: Implement test with valid input
        pytest.fail("Test not implemented")

    @pytest.mark.asyncio
    async def test_list_{snake_name}(self):
        """Test list_{snake_name} function."""
        # TODO: Implement test with valid input
        pytest.fail("Test not implemented")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
'''
    elif with_models:
        # Models test uses Request/Response classes
        content = f'''"""Test-Driven Development for {snake_name}.

TDD WORKFLOW:
1. Run tests: uv run pytest tests/test_{snake_name}.py -v (RED - will fail)
2. Implement: Edit mcp_servers/{snake_name}/tools/{snake_name}.py (GREEN - pass)
3. Refactor: Improve code while keeping tests passing
"""

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

# Add the server to path
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp_servers" / "{snake_name}"))

from models import {pascal_name}Request, {pascal_name}Response
from tools.{snake_name} import {snake_name}


class Test{pascal_name}Tool:
    """Test suite for {snake_name} with Pydantic validation."""

    @pytest.mark.asyncio
    async def test_basic_functionality(self):
        """Test that tool returns valid response for valid input."""
        request = {pascal_name}Request(input_param="test")
        response = await {snake_name}(request)
        assert isinstance(response, {pascal_name}Response)
        assert response.result is not None

    @pytest.mark.asyncio
    async def test_validates_request_schema(self):
        """Test that invalid requests are rejected by Pydantic."""
        with pytest.raises(ValidationError):
            {pascal_name}Request(input_param=123)  # Wrong type

    @pytest.mark.asyncio
    async def test_response_matches_schema(self):
        """Test that response conforms to Response schema."""
        request = {pascal_name}Request(input_param="test")
        response = await {snake_name}(request)
        json_data = response.model_dump()
        validated = {pascal_name}Response.model_validate(json_data)
        assert validated == response


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
'''
    else:
        # Basic test with Pydantic models (generated automatically for tool discovery)
        content = f'''"""Tests for {snake_name} MCP tool.

All tools use Pydantic models for input/output to ensure proper tool discovery.
"""

import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

# Add the server to path
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp_servers" / "{snake_name}"))

from models import {pascal_name}Input, {pascal_name}Output
from tools.{snake_name} import {snake_name}


class Test{pascal_name}Tool:
    """Test suite for {snake_name} with Pydantic validation."""

    @pytest.mark.asyncio
    async def test_basic_functionality(self):
        """Test that tool returns valid response for valid input."""
        request = {pascal_name}Input(input_param="test")
        response = await {snake_name}(request)
        assert isinstance(response, {pascal_name}Output)
        assert response.result is not None

    @pytest.mark.asyncio
    async def test_validates_request_schema(self):
        """Test that invalid requests are rejected by Pydantic."""
        with pytest.raises(ValidationError):
            {pascal_name}Input(input_param=123)  # Wrong type

    @pytest.mark.asyncio
    async def test_response_matches_schema(self):
        """Test that response conforms to Response schema."""
        request = {pascal_name}Input(input_param="test")
        response = await {snake_name}(request)
        json_data = response.model_dump()
        validated = {pascal_name}Output.model_validate(json_data)
        assert validated == response


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
'''

    test_path.write_text(content)


def create_wiki_sme_guide(
    wiki_dir: Path, snake_name: str, pascal_name: str, title_name: str
) -> Path | None:
    """Create a placeholder SME Guide in the wiki directory.

    Args:
        wiki_dir: Path to wiki directory
        snake_name: Server name in snake_case
        pascal_name: Server name in PascalCase
        title_name: Server name in Title Case

    Returns:
        Path to created guide file, or None if guide already exists
    """
    wiki_dir.mkdir(parents=True, exist_ok=True)

    # Use UPPER_SNAKE for filename (e.g., TEST_SERVER-SME-Guide.md)
    filename = f"{snake_name.upper()}-SME-Guide.md"
    guide_path = wiki_dir / filename

    # Don't overwrite existing guides
    if guide_path.exists():
        return None

    content = f"""# {title_name} SME Guide

This guide documents the {title_name} MCP server tools and how to use them effectively.

---

## Overview

{title_name} is an MCP server that provides tools for [describe your server's purpose here].

## Tools

### `{snake_name}`

[Describe what this tool does and when to use it.]

**Input Parameters:**
- `input_param` (string, required): [Description of the parameter]

**Output:**
- `result` (string): [Description of the output]

**Example Usage:**
```
[Provide an example of how to use this tool]
```

---

## Common Workflows

### Workflow 1: [Name]

[Describe a common workflow using the tools]

1. Step 1
2. Step 2
3. Step 3

---

## Tips and Best Practices

- [Tip 1]
- [Tip 2]
- [Tip 3]

---

## Troubleshooting

### Common Issues

**Issue:** [Description]
**Solution:** [How to fix it]

---

## Related Resources

- [Link to relevant documentation]
- [Link to API reference]
"""
    guide_path.write_text(content)
    return guide_path


def create_middleware_readme(server_path: Path) -> None:
    """Create middleware/README.md explaining to use mcp_middleware package."""
    content = """# Middleware

This server uses the `mcp_middleware` package for logging and other middleware.

See `packages/mcp_middleware/README.md` for documentation.

## Usage

The LoggingMiddleware is already configured in `main.py`:

```python
from mcp_middleware import LoggingMiddleware

mcp.add_middleware(LoggingMiddleware(log_level="INFO"))
```

## Custom Middleware

If you need custom middleware, create files here and import them in `main.py`.
"""
    readme_path = server_path / "middleware" / "README.md"
    readme_path.parent.mkdir(parents=True, exist_ok=True)
    readme_path.write_text(content)


def create_mcp_server(
    name: str,
    project_path: Path,
    package_path: Path | None = None,
    with_models: bool = False,
    with_database: bool = False,
    with_config: bool = False,
    with_auth: bool = False,
    with_repository: bool = False,
    with_foco: bool = True,
    force: bool = False,
) -> None:
    """Create a new MCP server by copying and transforming the template.

    Args:
        name: Name of the server (will be converted to snake_case)
        project_path: Target project directory (where mcp_servers/ lives)
        package_path: Package directory containing templates (defaults to mcp_scripts package)
        with_models: Include Pydantic models for spec-driven development
        with_database: Include database support
        with_config: Include environment configuration
        with_auth: Include authentication
        with_repository: Include repository pattern
        with_foco: Include FoCo config files
    """
    # If package_path not specified, use the installed mcp_scripts package location
    if package_path is None:
        package_path = Path(__file__).parent

    snake_name = to_snake_case(name)
    validate_server_name(snake_name)
    pascal_name = to_pascal_case(name)
    title_name = to_title_case(name)
    replacements = get_name_variants(name)

    # Templates come from the package
    template_dir = package_path / "templates" / "mcp_server_full"

    # Output goes to the project
    mcp_servers_dir = project_path / "mcp_servers"
    server_path = mcp_servers_dir / snake_name
    tests_dir = project_path / "tests"
    test_path = tests_dir / f"test_{snake_name}.py"

    # Check prerequisites
    if not template_dir.exists():
        logger.error("Template directory not found: %s", template_dir)
        sys.exit(1)

    # Check for existing servers and tests
    existing_servers = []
    if mcp_servers_dir.exists():
        existing_servers = [item.name for item in mcp_servers_dir.iterdir() if item.is_dir()]

    existing_tests = []
    if tests_dir.exists():
        existing_tests = [
            item.name
            for item in tests_dir.iterdir()
            if item.is_file()
            and item.name.startswith("test_")
            and item.suffix == ".py"
            and item.name != "test_create_mcp_server.py"
        ]

    # Warn about existing content and require --force to delete
    if existing_servers or existing_tests:
        if not force:
            existing_content = []
            if existing_servers:
                existing_content.append(f"servers: {existing_servers}")
            if existing_tests:
                existing_content.append(f"tests: {existing_tests}")
            logger.error(
                "Existing content found (%s). Use --force to delete.",
                ", ".join(existing_content),
            )
            sys.exit(1)

        # Clean up existing servers
        if mcp_servers_dir.exists():
            for item in mcp_servers_dir.iterdir():
                if item.is_dir():
                    shutil.rmtree(item)
                    logger.info("Removed existing server: %s", item.name)
                elif item.is_file():
                    item.unlink()

        # Clean up existing tests
        if tests_dir.exists():
            for item in tests_dir.iterdir():
                if item.is_file() and item.name.startswith("test_") and item.suffix == ".py":
                    if item.name != "test_create_mcp_server.py":
                        item.unlink()
                        logger.info("Removed existing test: %s", item.name)

    # Determine what to exclude
    exclude_patterns = get_exclude_patterns(
        with_models=with_models,
        with_database=with_database,
        with_config=with_config,
        with_auth=with_auth,
        with_repository=with_repository,
    )

    # Build feature list for logging
    features = []
    if with_repository:
        features.append("repository pattern")
    if with_models:
        features.append("Pydantic models")
    if with_database:
        features.append("database")
    if with_config:
        features.append("config")
    if with_auth:
        features.append("auth")
    mode = f"with {', '.join(features)}" if features else "standard"

    logger.info("Creating MCP server: %s (%s)", pascal_name, mode)
    logger.info("Directory: %s", server_path)

    # Copy and transform template
    created_files = copy_and_transform_template(
        template_dir=template_dir,
        target_dir=server_path,
        replacements=replacements,
        exclude_patterns=exclude_patterns,
    )

    # Move test file to tests/ directory
    template_test = server_path / f"test_{snake_name}.py"
    if template_test.exists():
        test_path.parent.mkdir(exist_ok=True)
        shutil.move(template_test, test_path)
        logger.info("Created tests/test_%s.py", snake_name)

    # Simplify main.py, tool, and test file if not using all features
    full_features = with_auth and with_repository
    if not full_features:
        create_simplified_main(
            server_path, snake_name, pascal_name, with_auth, with_repository, with_database
        )
        create_simplified_tool(
            server_path, snake_name, pascal_name, title_name, with_models, with_repository
        )
        create_simplified_test(test_path, snake_name, pascal_name, with_models, with_repository)

    # Create middleware README explaining mcp_middleware usage
    create_middleware_readme(server_path)

    # Create wiki SME Guide placeholder (if it doesn't exist)
    wiki_dir = project_path / "wiki"
    guide_path = create_wiki_sme_guide(wiki_dir, snake_name, pascal_name, title_name)
    if guide_path:
        logger.info("Created wiki SME Guide: %s", guide_path.name)
    else:
        logger.info("Wiki SME Guide already exists: %s-SME-Guide.md", snake_name.upper())

    # Generate FoCo files (mise.toml, arco.toml, CI workflows, README)
    if with_foco:
        foco_templates_dir = package_path / "templates" / "foco"
        ci_templates_dir = package_path / "templates" / "ci"

        # Placeholders for FoCo files
        foco_replacements = {
            "{{SERVICE_NAME}}": snake_name,
            "{{SERVER_NAME}}": snake_name,
        }

        if foco_templates_dir.exists():
            # mise.toml
            mise_template = foco_templates_dir / "mise.toml"
            if mise_template.exists():
                mise_content = mise_template.read_text()
                for key, value in foco_replacements.items():
                    mise_content = mise_content.replace(key, value)
                (server_path / "mise.toml").write_text(mise_content)
                logger.info("Created mise.toml")

            # arco.toml
            arco_template = foco_templates_dir / "arco.toml"
            if arco_template.exists():
                arco_content = arco_template.read_text()
                for key, value in foco_replacements.items():
                    arco_content = arco_content.replace(key, value)
                (server_path / "arco.toml").write_text(arco_content)
                logger.info("Created arco.toml")

            # CI workflows
            ci_dir = server_path / ".github" / "workflows"
            ci_dir.mkdir(parents=True, exist_ok=True)

            arco_validate = ci_templates_dir / "arco-validate.yml"
            if arco_validate.exists():
                (ci_dir / "arco-validate.yml").write_text(arco_validate.read_text())
                logger.info("Created .github/workflows/arco-validate.yml")

            foundry_sync = ci_templates_dir / "foundry-service-sync.yml"
            if foundry_sync.exists():
                (ci_dir / "foundry-service-sync.yml").write_text(foundry_sync.read_text())
                logger.info("Created .github/workflows/foundry-service-sync.yml")

            # README.md
            readme_template = package_path / "templates" / "README.md"
            if readme_template.exists():
                readme_content = readme_template.read_text()
                for key, value in foco_replacements.items():
                    readme_content = readme_content.replace(key, value)
                (server_path / "README.md").write_text(readme_content)
                logger.info("Created README.md")
        else:
            logger.warning("FoCo templates directory not found: %s", foco_templates_dir)

    # Ensure __init__.py files exist for all package directories
    for init_dir in ["", "middleware", "tools", "utils"]:
        if init_dir:
            init_path = server_path / init_dir / "__init__.py"
        else:
            init_path = server_path / "__init__.py"
        init_path.parent.mkdir(parents=True, exist_ok=True)
        if not init_path.exists():
            init_path.write_text("")

    if with_database:
        for init_dir in ["db", "repositories"]:
            init_path = server_path / init_dir / "__init__.py"
            init_path.parent.mkdir(parents=True, exist_ok=True)
            if not init_path.exists():
                init_path.write_text("")

    if with_repository:
        for init_dir in ["repositories", "schemas", "data", "data/synthetic"]:
            init_path = server_path / init_dir / "__init__.py"
            init_path.parent.mkdir(parents=True, exist_ok=True)
            if not init_path.exists():
                init_path.write_text("")

    # Clean up empty directories
    for dir_path in server_path.rglob("*"):
        if dir_path.is_dir() and not any(dir_path.iterdir()):
            dir_path.rmdir()

    logger.info("Successfully created MCP server: %s", pascal_name)
    logger.info("Created %d files", len(created_files))

    # Print next steps
    if with_repository:
        logger.info("REPOSITORY PATTERN SETUP:")
        logger.info("1. Define schemas in mcp_servers/%s/schemas/", snake_name)
        logger.info("2. Add synthetic data in mcp_servers/%s/data/synthetic/", snake_name)
        logger.info("3. Implement tools in mcp_servers/%s/tools/%s.py", snake_name, snake_name)
        logger.info("4. Run: cd mcp_servers/%s && python main.py", snake_name)
    elif with_models:
        logger.info("TDD WORKFLOW:")
        logger.info("1. Define API spec: Edit mcp_servers/%s/models.py", snake_name)
        logger.info("2. Run tests (RED): uv run pytest tests/test_%s.py -v", snake_name)
        logger.info(
            "3. Implement tool (GREEN): Edit mcp_servers/%s/tools/%s.py", snake_name, snake_name
        )
    else:
        logger.info("Next steps:")
        logger.info(
            "1. Edit mcp_servers/%s/tools/%s.py to implement your tool", snake_name, snake_name
        )
        logger.info("2. Edit tests/test_%s.py to implement your tests", snake_name)

    if with_auth:
        logger.info("AUTHENTICATION:")
        logger.info("1. Install: cd packages/mcp_auth && pip install -e .")
        logger.info("2. Review users in mcp_servers/%s/users.json", snake_name)


def main():
    """Main entry point for the script."""
    parser = argparse.ArgumentParser(
        description="Generate a new MCP server from template",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Basic server
  python scripts/create_mcp_server.py weather_api

  # With repository pattern (recommended for API integrations)
  python scripts/create_mcp_server.py taxjar --with-repository

  # With Pydantic models for TDD
  python scripts/create_mcp_server.py "My Cool Server" --with-models

  # Full stack with database and config
  python scripts/create_mcp_server.py database-connector --with-database --with-config
        """,
    )
    parser.add_argument(
        "name",
        help="Name of the MCP server (will be converted to snake_case)",
    )
    parser.add_argument(
        "--with-models",
        action="store_true",
        help="Include Pydantic models for spec-driven development",
    )
    parser.add_argument(
        "--with-database",
        action="store_true",
        help="Include database support (SQLAlchemy)",
    )
    parser.add_argument(
        "--with-config",
        action="store_true",
        help="Include environment configuration (pydantic-settings)",
    )
    parser.add_argument(
        "--with-auth",
        action="store_true",
        help="Include authentication using mcp-auth package",
    )
    parser.add_argument(
        "--with-repository",
        action="store_true",
        help="Include repository pattern for online/offline data access",
    )
    parser.add_argument(
        "--no-foco",
        action="store_true",
        help="Skip generating FoCo config (mise.toml, arco.toml, CI workflows for RL Studio)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force deletion of existing servers and tests without prompting",
    )

    args = parser.parse_args()

    # Templates are in mcp_scripts/templates/ (relative to this script)
    package_path = Path(__file__).resolve().parent

    # Output goes to the current working directory (the project being worked on)
    project_path = Path.cwd()

    create_mcp_server(
        args.name,
        project_path=project_path,
        package_path=package_path,
        with_models=args.with_models,
        with_database=args.with_database,
        with_config=args.with_config,
        with_auth=args.with_auth,
        with_repository=args.with_repository,
        with_foco=not args.no_foco,
        force=args.force,
    )


if __name__ == "__main__":
    main()

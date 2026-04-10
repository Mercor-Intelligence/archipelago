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


def generate_main_py(server_name: str, snake_case_name: str) -> str:
    """Generate the main.py file content."""
    return f"""from fastmcp import FastMCP
from fastmcp.server.middleware.error_handling import (
    ErrorHandlingMiddleware,
    RetryMiddleware,
)
from middleware.logging import LoggingMiddleware
from tools.{snake_case_name} import {snake_case_name}

mcp = FastMCP("{server_name}")
mcp.add_middleware(ErrorHandlingMiddleware(include_traceback=True))
mcp.add_middleware(RetryMiddleware())
mcp.add_middleware(LoggingMiddleware())

mcp.tool({snake_case_name})

if __name__ == "__main__":
    mcp.run()
"""


def generate_tool_py(snake_case_name: str, title_case_name: str) -> str:
    """Generate the tool file content."""
    return f"""from loguru import logger
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


def make_async_background[**P, R](fn: Callable[P, R]) -> Callable[P, Awaitable[R]]:
    \"\"\"
    Make a function run in the background (thread) and return an awaitable.
    \"\"\"

    @functools.wraps(fn)
    async def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
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
    It will limit concurrent calls to the function to the specified number within the same event loop.
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


def generate_test_file(snake_case_name: str, title_case_name: str) -> str:
    """Generate the test file content."""
    return f"""import asyncio
import sys
from pathlib import Path

import pytest

# Add the parent directory to the path so we can import the module
sys.path.insert(0, str(Path(__file__).parent.parent / "mcp_servers" / "{snake_case_name}"))

from tools.{snake_case_name} import {snake_case_name}


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


def create_mcp_server(name: str, base_path: Path) -> None:
    """Create a new MCP server with the given name."""
    snake_case_name = to_snake_case(name)
    pascal_case_name = to_pascal_case(name)
    title_case_name = to_title_case(name)

    # Create the server directory
    server_path = base_path / "mcp_servers" / snake_case_name
    if server_path.exists():
        print(f"Error: Server '{snake_case_name}' already exists at {server_path}")
        sys.exit(1)

    print(f"Creating MCP server: {pascal_case_name}")
    print(f"Directory: {server_path}")

    # Create directory structure
    server_path.mkdir(parents=True, exist_ok=True)
    (server_path / "middleware").mkdir(exist_ok=True)
    (server_path / "utils").mkdir(exist_ok=True)
    (server_path / "tools").mkdir(exist_ok=True)

    # Create main.py
    main_content = generate_main_py(pascal_case_name, snake_case_name)
    (server_path / "main.py").write_text(main_content)
    print("✓ Created main.py")

    # Create tool file
    tool_content = generate_tool_py(snake_case_name, title_case_name)
    (server_path / "tools" / f"{snake_case_name}.py").write_text(tool_content)
    print(f"✓ Created tools/{snake_case_name}.py")

    # Create middleware
    logging_content = generate_logging_middleware()
    (server_path / "middleware" / "logging.py").write_text(logging_content)
    print("✓ Created middleware/logging.py")

    # Create utils
    decorators_content = generate_decorators()
    (server_path / "utils" / "decorators.py").write_text(decorators_content)
    print("✓ Created utils/decorators.py")

    # Create pyrightconfig.json
    pyright_content = generate_pyrightconfig()
    (server_path / "pyrightconfig.json").write_text(pyright_content)
    print("✓ Created pyrightconfig.json")

    # Create test file
    test_dir = base_path / "test"
    test_dir.mkdir(exist_ok=True)
    test_content = generate_test_file(snake_case_name, title_case_name)
    (test_dir / f"{snake_case_name}.py").write_text(test_content)
    print(f"✓ Created test/{snake_case_name}.py")

    print(f"\nSuccessfully created MCP server: {pascal_case_name}")
    print("\nNext steps:")
    print(
        f"1. Edit {server_path / 'tools' / f'{snake_case_name}.py'} to implement your tool logic. This might mean making more files for each tool to be called."
    )
    print(f"2. Edit test/{snake_case_name}.py to implement your tests")


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

    args = parser.parse_args()

    # Get the base path (project root)
    script_path = Path(__file__).resolve()
    base_path = script_path.parent.parent

    create_mcp_server(args.name, base_path)


if __name__ == "__main__":
    main()

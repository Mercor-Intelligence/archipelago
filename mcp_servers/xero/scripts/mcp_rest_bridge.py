#!/usr/bin/env python3
"""
MCP REST Bridge - HTTP REST server that bridges to MCP stdio servers.

This bridge allows MCP servers (using stdio transport) to be accessed via HTTP REST API.
It's designed to work with custom UIs that need to call MCP tools via HTTP.

Features:
- Exposes MCP tools via REST API
- Auto-discovers database management tools
- Handles Pydantic model serialization

Usage:
    python scripts/mcp_rest_bridge.py --mcp-server mcp_servers.tableau.main --port 8000
"""

import argparse
import asyncio
import importlib
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, get_args, get_origin

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from pydantic import BaseModel

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


class MCPStdioClient:
    """Client for communicating with MCP stdio servers."""

    def __init__(self, module_path: str):
        """
        Initialize MCP stdio client.

        Args:
            module_path: Python module path (e.g., 'mcp_servers.tableau.main')
        """
        self.module_path = module_path
        self.process: subprocess.Popen | None = None
        self._lock = asyncio.Lock()
        self._request_id = 0

    async def start(self):
        """Start the MCP server process."""
        try:
            # Extract the server directory from module path
            # e.g., "mcp_servers.tableau.main" -> "mcp_servers/tableau"
            import os

            repo_root = os.getcwd()
            module_parts = self.module_path.split(".")

            # For "mcp_servers.tableau.main", we want to cd to "mcp_servers/tableau"
            if len(module_parts) >= 3:
                server_dir = os.path.join(repo_root, module_parts[0], module_parts[1])
                # Run main module from the server directory
                # Use runpy to run 'main' module from within that directory
                cmd = [
                    "sh",
                    "-c",
                    f"cd {server_dir} && uv run python -c \"import runpy; \
                        runpy.run_module('main', run_name='__main__')\"",
                ]
            else:
                # Fallback to simpler approach
                cmd = ["uv", "run", "python", "-m", self.module_path]
                server_dir = repo_root

            logger.info(f"Starting MCP server: {' '.join(cmd)}")
            logger.info(f"Server directory: {server_dir}")

            # Set up environment
            env = os.environ.copy()

            # Ensure uv is in PATH (it's installed in /root/.local/bin)
            uv_path = "/root/.local/bin"
            if "PATH" in env:
                if uv_path not in env["PATH"]:
                    env["PATH"] = f"{uv_path}:{env['PATH']}"
            else:
                env["PATH"] = f"{uv_path}:/usr/local/bin:/usr/bin:/bin"

            logger.info(f"Setting PATH to include: {uv_path}")
            logger.info(f"Working directory: {repo_root}")

            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                bufsize=0,
                env=env,
                cwd=repo_root,
            )

            # Give the process a moment to start
            await asyncio.sleep(0.5)

            # Check if process is still running
            if self.process.poll() is not None:
                # Process has already exited
                stderr_output = self.process.stderr.read()
                stderr_text = stderr_output.decode() if stderr_output else "No stderr output"
                logger.error(
                    f"MCP server process exited immediately with code {self.process.returncode}"
                )
                logger.error(f"MCP server stderr: {stderr_text}")
                raise Exception(f"MCP server failed to start: {stderr_text}")

            # Initialize the MCP server
            init_request = {
                "jsonrpc": "2.0",
                "id": self._get_next_id(),
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "mcp-rest-bridge", "version": "1.0.0"},
                },
            }

            response = await self._send_request(init_request)
            if response and "result" in response:
                logger.info(f"MCP server initialized: {response['result'].get('serverInfo', {})}")
            else:
                logger.error(f"Failed to initialize MCP server: {response}")

                # Try to read stderr for more details
                if self.process.poll() is not None:
                    stderr_output = self.process.stderr.read()
                    stderr_text = stderr_output.decode() if stderr_output else "No stderr output"
                    logger.error(f"MCP server stderr: {stderr_text}")

                raise Exception("Failed to initialize MCP server")

        except Exception as e:
            logger.error(f"Error starting MCP server: {e}")
            raise

    def _get_next_id(self) -> int:
        """Get next request ID."""
        self._request_id += 1
        return self._request_id

    async def _send_request(self, request: dict[str, Any]) -> dict[str, Any] | None:
        """
        Send a JSON-RPC request to the MCP server.

        Args:
            request: JSON-RPC request

        Returns:
            JSON-RPC response
        """
        async with self._lock:
            try:
                if not self.process or not self.process.stdin or not self.process.stdout:
                    raise Exception("MCP process not running")

                # Check if process is still alive
                if self.process.poll() is not None:
                    logger.error(f"MCP process has exited with code {self.process.returncode}")
                    return None

                # Write request
                request_json = json.dumps(request) + "\n"
                self.process.stdin.write(request_json.encode())
                self.process.stdin.flush()

                # Read response with timeout
                import select
                import time

                timeout = 5.0  # 5 second timeout
                start_time = time.time()

                while time.time() - start_time < timeout:
                    # Check if data is available
                    if self.process.stdout in select.select([self.process.stdout], [], [], 0.1)[0]:
                        response_line = self.process.stdout.readline()
                        if response_line:
                            response = json.loads(response_line.decode())
                            return response

                    # Check if process died
                    if self.process.poll() is not None:
                        logger.error(
                            f"MCP process died during request \
                                (exit code: {self.process.returncode})"
                        )
                        stderr_output = self.process.stderr.read()
                        if stderr_output:
                            logger.error(f"MCP stderr: {stderr_output.decode()}")
                        return None

                logger.error(f"Timeout waiting for MCP server response after {timeout}s")
                return None

            except Exception as e:
                logger.error(f"Error sending request to MCP server: {e}", exc_info=True)
                return None

    async def list_tools(self) -> list[dict[str, Any]]:
        """
        List all available tools from the MCP server.

        Returns:
            List of tool definitions
        """
        request = {
            "jsonrpc": "2.0",
            "id": self._get_next_id(),
            "method": "tools/list",
            "params": {},
        }

        response = await self._send_request(request)
        if response and "result" in response:
            return response["result"].get("tools", [])

        logger.error(f"Failed to list tools: {response}")
        return []

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """
        Call a tool on the MCP server.

        Args:
            tool_name: Name of the tool to call
            arguments: Tool arguments

        Returns:
            Tool response
        """
        request = {
            "jsonrpc": "2.0",
            "id": self._get_next_id(),
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        }

        response = await self._send_request(request)

        if response and "result" in response:
            return response["result"]
        elif response and "error" in response:
            raise Exception(f"Tool call error: {response['error']}")

        raise Exception("No response from MCP server")

    async def close(self):
        """Close the MCP client connection."""
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
            logger.info("MCP server process terminated")


# Global MCP client and tools registry
mcp_client: MCPStdioClient | None = None
db_tools_registry: list[dict[str, Any]] = []  # Store database tools metadata
direct_tools_registry: list[dict[str, Any]] = []  # Store directly imported tools metadata


def setup_database(module_path: str):
    """
    Auto-detect and setup database for the MCP server.

    Args:
        module_path: MCP server module path

    Returns:
        Database engine if found, None otherwise
    """
    try:
        parts = module_path.split(".")
        server_module_path = ".".join(parts[:-1])  # e.g., mcp_servers.tableau

        # Add server directory to sys.path for relative imports
        project_root = Path(__file__).parent.parent
        server_dir = project_root / server_module_path.replace(".", "/")
        if str(server_dir) not in sys.path:
            sys.path.insert(0, str(server_dir))
            logger.info(f"Added to sys.path: {server_dir}")

        # Set server-specific XERO_DATABASE_URL if not set
        if "XERO_DATABASE_URL" not in os.environ:
            server_name = parts[1] if len(parts) >= 2 else "default"
            # Use absolute path to ensure migrations and tools use the same database
            db_file = Path.cwd() / f"{server_name}_data.db"
            db_path = f"sqlite+aiosqlite:///{db_file}"
            os.environ["XERO_DATABASE_URL"] = db_path
            logger.info(f"Auto-configured database: {db_path}")
            logger.info(f"Database file location: {db_file}")

        # Try to import db.session module
        db_module_path = f"{server_module_path}.db.session"
        try:
            db_module = importlib.import_module(db_module_path)
            logger.info(f"Database module imported: {db_module_path}")
        except ModuleNotFoundError as e:
            logger.info(f"No database detected: {db_module_path} - {e}")
            return None

        # Run Alembic migrations if alembic.ini exists
        project_root = Path(__file__).parent.parent
        server_dir = project_root / server_module_path.replace(".", "/")
        alembic_ini = server_dir / "alembic.ini"

        logger.info(f"Checking for alembic.ini at: {alembic_ini}")
        logger.info(f"Alembic.ini exists: {alembic_ini.exists()}")

        if alembic_ini.exists():
            logger.info(f"Running database migrations from {server_dir}...")
            try:
                import subprocess

                result = subprocess.run(
                    ["uv", "run", "alembic", "upgrade", "head"],
                    cwd=str(server_dir),
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                logger.info(f"Migration returncode: {result.returncode}")
                logger.info(f"Migration stdout: {result.stdout}")
                if result.stderr:
                    logger.info(f"Migration stderr: {result.stderr}")

                if result.returncode == 0:
                    logger.info("✅ Database migrations completed successfully")
                else:
                    logger.error(f"❌ Migration failed with code {result.returncode}")
            except Exception as e:
                logger.error(f"Could not run migrations: {e}", exc_info=True)
        else:
            logger.warning(f"No alembic.ini found at {alembic_ini}")

        # Get engine
        engine = None
        if hasattr(db_module, "engine"):
            engine = db_module.engine
        elif hasattr(db_module, "get_engine"):
            result = db_module.get_engine()
            engine = asyncio.run(result) if asyncio.iscoroutine(result) else result
        elif hasattr(db_module, "_engine"):
            engine = db_module._engine

        if engine:
            logger.info("Database engine found")
            return engine

    except Exception as e:
        logger.warning(f"Could not setup database: {e}")

    return None


def initialize_mcp_server(module_path: str) -> dict:
    """
    Initialize the MCP server module if needed (pattern-agnostic).

    This handles multiple initialization patterns:
    - Module-level initialization (runs on import)
    - Explicit initialization functions
    - Global provider patterns
    - Standalone tools (no initialization needed)

    Args:
        module_path: MCP server module path (e.g., "mcp_servers.xero.main")

    Returns:
        dict with initialization status and metadata
    """
    result = {"success": False, "pattern": None, "provider": None, "message": None}

    try:
        logger.info(f"Attempting to initialize MCP server: {module_path}")

        # Step 1: Import the main module
        # This alone may trigger initialization for some patterns
        try:
            main_module = importlib.import_module(module_path)
            logger.info(f"✓ Imported {module_path}")
        except Exception as e:
            result["message"] = f"Failed to import module: {e}"
            logger.warning(result["message"])
            return result

        # Step 2: Check for explicit initialization function
        # Pattern: def initialize_provider() or def initialize()
        for init_func_name in ["initialize_provider", "initialize", "setup", "init"]:
            if hasattr(main_module, init_func_name):
                logger.info(f"✓ Found initialization function: {init_func_name}()")
                try:
                    init_func = getattr(main_module, init_func_name)
                    provider = init_func()

                    # Handle async initialization
                    if asyncio.iscoroutine(provider):
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        provider = loop.run_until_complete(provider)
                        loop.close()

                    result.update(
                        {
                            "success": True,
                            "pattern": f"explicit_init:{init_func_name}",
                            "provider": provider.__class__.__name__ if provider else None,
                            "message": f"Initialized via {init_func_name}()",
                        }
                    )
                    logger.info(f"✅ {result['message']}")
                    return result

                except Exception as e:
                    logger.warning(f"Initialization function {init_func_name}() failed: {e}")

        # Step 3: Check for module-level provider
        # Pattern: provider = OfflineProvider() at module level
        if hasattr(main_module, "provider"):
            provider = main_module.provider
            if provider is not None:
                result.update(
                    {
                        "success": True,
                        "pattern": "module_level_provider",
                        "provider": provider.__class__.__name__,
                        "message": "Provider initialized at module level",
                    }
                )
                logger.info(f"✅ {result['message']}")
                return result

        # Step 4: Check for tools with global state
        # Pattern: xero_tools.set_provider() / get_provider()
        parts = module_path.split(".")
        if len(parts) >= 3:
            server_name = parts[1]  # e.g., "xero" from "mcp_servers.xero.main"
            tools_module_path = f"{'.'.join(parts[:-1])}.tools.{server_name}_tools"

            try:
                tools_module = importlib.import_module(tools_module_path)

                # Check if provider is accessible via get_provider()
                if hasattr(tools_module, "get_provider"):
                    try:
                        provider = tools_module.get_provider()
                        result.update(
                            {
                                "success": True,
                                "pattern": "tools_global_provider",
                                "provider": provider.__class__.__name__,
                                "message": "Provider found in tools module",
                            }
                        )
                        logger.info(f"✅ {result['message']}")
                        return result
                    except RuntimeError:
                        # Provider not set - this is OK, tools may be standalone
                        pass

            except ImportError:
                pass

        # Step 5: Assume standalone tools (no initialization needed)
        # This is OK! Many MCP servers don't need initialization
        result.update(
            {
                "success": True,
                "pattern": "standalone",
                "provider": None,
                "message": "No initialization required (standalone tools pattern)",
            }
        )
        logger.info(f"✅ {result['message']}")
        return result

    except Exception as e:
        result["message"] = f"Unexpected error during initialization: {e}"
        logger.error(result["message"], exc_info=True)
        return result


def _get_basemodel_from_annotation(annotation: Any) -> type[BaseModel] | None:
    """Return the BaseModel subclass contained in the annotation, if any."""
    origin = get_origin(annotation)
    if origin is None:
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            return annotation
        return None

    for arg in get_args(annotation):
        model = _get_basemodel_from_annotation(arg)
        if model:
            return model

    return None


def discover_direct_tools(app: FastAPI, module_path: str) -> int:
    """
    Discover tools by scanning the tools directory directly.
    This finds all Pydantic-based tool functions, even if not registered with FastMCP.

    Args:
        app: FastAPI application
        module_path: MCP server module path

    Returns:
        Number of tools discovered
    """
    global direct_tools_registry

    try:
        parts = module_path.split(".")
        server_module_path = ".".join(parts[:-1])  # e.g., mcp_servers.tableau

        # Add server directory to path for relative imports
        server_dir = Path.cwd() / server_module_path.replace(".", "/")
        if str(server_dir) not in sys.path:
            sys.path.insert(0, str(server_dir))

        # Scan tools directory
        tools_dir = server_dir / "tools"
        if not tools_dir.exists():
            logger.warning(f"Tools directory not found: {tools_dir}")
            return 0

        logger.info(f"Scanning tools directory: {tools_dir}")
        count = 0

        for tool_file in tools_dir.glob("*.py"):
            if tool_file.name.startswith("_") or tool_file.name == "__init__.py":
                continue

            # Import the tool module
            tool_module_name = f"{server_module_path}.tools.{tool_file.stem}"
            try:
                tool_module = importlib.import_module(tool_module_name)
            except Exception as e:
                logger.warning(f"Could not import {tool_module_name}: {e}")
                continue

            # Find tool functions (functions with Pydantic input/output)
            for name, obj in inspect.getmembers(tool_module):
                if not (inspect.isfunction(obj) or inspect.iscoroutinefunction(obj)):
                    continue
                if name.startswith("_"):
                    continue

                try:
                    sig = inspect.signature(obj)
                    params = list(sig.parameters.values())

                    if not params:
                        continue

                    first_param = params[0]
                    input_model = _get_basemodel_from_annotation(first_param.annotation)
                    if input_model is None:
                        continue

                    invalid_param = False
                    for extra_param in params[1:]:
                        if extra_param.default is inspect._empty and extra_param.kind not in (
                            inspect.Parameter.VAR_POSITIONAL,
                            inspect.Parameter.VAR_KEYWORD,
                        ):
                            invalid_param = True
                            break
                    if invalid_param:
                        continue

                    output_model = sig.return_annotation

                    # Store metadata
                    tool_metadata = {
                        "name": name,
                        "description": inspect.getdoc(obj) or f"{name} tool",
                        "method": "POST",
                        "parameters": [],
                    }

                    # Extract parameters
                    try:
                        schema = input_model.model_json_schema()
                        properties = schema.get("properties", {})
                        required_fields = schema.get("required", [])

                        for param_name, param_info in properties.items():
                            tool_metadata["parameters"].append(
                                {
                                    "name": param_name,
                                    "type": param_info.get("type", "string"),
                                    "required": param_name in required_fields,
                                    "description": param_info.get("description", ""),
                                }
                            )
                    except Exception as e:
                        logger.warning(f"Could not extract parameters for {name}: {e}")

                    direct_tools_registry.append(tool_metadata)

                    # Create endpoint with proper closure capture
                    def make_handler(func, in_model, out_model):
                        async def handler(request_body: dict[str, Any]):
                            try:
                                # Parse raw JSON dict into Pydantic model
                                # This handles the case where UI sends {"site_id": "..."} directly
                                validated_request = in_model(**request_body)

                                # Call the function with the validated Pydantic model
                                if inspect.iscoroutinefunction(func):
                                    result = await func(validated_request)
                                else:
                                    result = func(validated_request)
                                    # Handle wrapped async functions (from decorators)
                                    if inspect.iscoroutine(result):
                                        result = await result
                                return result
                            except Exception as e:
                                logger.error(f"Tool {func.__name__} execution error: {e}")
                                raise HTTPException(status_code=500, detail=str(e)) from e

                        return handler

                    # Register the endpoint with proper types
                    endpoint_handler = make_handler(obj, input_model, output_model)
                    app.post(f"/tools/{name}", response_model=output_model)(endpoint_handler)
                    count += 1
                    logger.info(f"Discovered direct tool: {name}")

                except Exception as e:
                    logger.debug(f"Skipped {name}: {e}")

        return count

    except Exception as e:
        logger.warning(f"Could not discover direct tools: {e}")
        return 0


def add_database_tools(app: FastAPI, engine) -> int:
    """
    Add database management tools to the FastAPI app.

    Args:
        app: FastAPI application
        engine: SQLAlchemy engine

    Returns:
        Number of tools added
    """
    global db_tools_registry

    if not engine:
        return 0

    try:
        # Try to import db_tools module
        from db_tools import get_db_management_tools

        db_tools = get_db_management_tools(engine)
        count = 0

        for tool_name, tool_info in db_tools.items():
            input_model = tool_info["input_model"]
            output_model = tool_info["output_model"]
            func = tool_info["function"]

            # Store metadata for /api/discover endpoint
            tool_metadata = {
                "name": tool_name,
                "description": inspect.getdoc(func) or f"Database management tool: {tool_name}",
                "method": "GET" if (input_model is None or input_model is type(None)) else "POST",
                "parameters": [],
            }

            # Extract parameters from Pydantic model if present
            if input_model and input_model is not None and input_model is not type(None):
                try:
                    if hasattr(input_model, "model_json_schema"):
                        schema = input_model.model_json_schema()
                        properties = schema.get("properties", {})
                        required_fields = schema.get("required", [])

                        for param_name, param_info in properties.items():
                            tool_metadata["parameters"].append(
                                {
                                    "name": param_name,
                                    "type": param_info.get("type", "string"),
                                    "required": param_name in required_fields,
                                    "description": param_info.get("description", ""),
                                }
                            )
                except Exception as e:
                    logger.warning(f"Could not extract parameters for {tool_name}: {e}")

            db_tools_registry.append(tool_metadata)

            # Create endpoint for each DB tool
            if input_model is None or input_model is type(None):
                # No-parameter tool (like list_tables)
                async def handler(func=func):
                    try:
                        result = func() if not inspect.iscoroutinefunction(func) else await func()
                        if inspect.iscoroutine(result):
                            result = await result
                        return result
                    except Exception as e:
                        raise HTTPException(status_code=500, detail=str(e)) from e

                app.get(f"/tools/{tool_name}", response_model=output_model)(handler)
            else:
                # Parameterized tool
                async def handler(request: input_model, func=func):
                    try:
                        result = (
                            func(request)
                            if not inspect.iscoroutinefunction(func)
                            else await func(request)
                        )
                        if inspect.iscoroutine(result):
                            result = await result
                        return result
                    except Exception as e:
                        raise HTTPException(status_code=500, detail=str(e)) from e

                app.post(f"/tools/{tool_name}", response_model=output_model)(handler)

            count += 1
            logger.info(f"Added DB tool: {tool_name}")

        return count

    except ImportError:
        logger.info("No db_tools module found")
        return 0
    except Exception as e:
        logger.warning(f"Could not add database tools: {e}")
        return 0


def create_app(module_path: str) -> FastAPI:
    """
    Create FastAPI app for the REST bridge.

    Args:
        module_path: Python module path for MCP server

    Returns:
        FastAPI application
    """
    app = FastAPI(
        title="MCP REST Bridge",
        description="REST API bridge for MCP stdio servers",
        version="1.0.0",
    )

    # Add CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.on_event("startup")
    async def startup_event():
        """Initialize MCP client on startup."""
        global mcp_client
        mcp_client = MCPStdioClient(module_path)
        await mcp_client.start()
        logger.info("REST bridge started")

    @app.on_event("shutdown")
    async def shutdown_event():
        """Clean up on shutdown."""
        global mcp_client
        if mcp_client:
            await mcp_client.close()
        logger.info("REST bridge stopped")

    @app.get("/")
    async def root():
        """Root endpoint."""
        return {"name": "MCP REST Bridge", "version": "1.0.0", "status": "running"}

    @app.get("/api/discover")
    async def discover_tools():
        """
        Discover all available tools (Direct + MCP + Database).

        Returns:
            List of tools with their metadata
        """
        global db_tools_registry, direct_tools_registry

        all_tools = []

        # Priority 1: Direct tools (scanned from tools/ directory)
        all_tools.extend(direct_tools_registry)
        logger.info(f"Direct tools: {len(direct_tools_registry)}")

        # Priority 2: Database tools
        all_tools.extend(db_tools_registry)
        logger.info(f"Database tools: {len(db_tools_registry)}")

        # Priority 3: MCP stdio tools (fallback if direct discovery failed)
        # Skip if we already have tools from direct discovery to avoid duplicates
        if not direct_tools_registry and mcp_client:
            try:
                mcp_tools = await mcp_client.list_tools()

                # Convert MCP tool format to REST bridge format
                for tool in mcp_tools:
                    rest_tool = {
                        "name": tool.get("name", ""),
                        "description": tool.get("description", ""),
                        "method": "POST",
                        "parameters": [],
                    }

                    # Extract parameters from input schema
                    input_schema = tool.get("inputSchema", {})
                    properties = input_schema.get("properties", {})
                    required = input_schema.get("required", [])

                    for param_name, param_info in properties.items():
                        rest_tool["parameters"].append(
                            {
                                "name": param_name,
                                "type": param_info.get("type", "string"),
                                "required": param_name in required,
                                "description": param_info.get("description", ""),
                            }
                        )

                    all_tools.append(rest_tool)

                logger.info(f"MCP stdio tools: {len(mcp_tools)}")
            except Exception as e:
                logger.error(f"Error listing MCP tools: {e}")

        logger.info(f"Total tools: {len(all_tools)}")

        return {"tools": all_tools}

    # Note: We don't create a catch-all /tools/{tool_name} endpoint here
    # because direct tool discovery creates specific typed endpoints for each tool.
    # This ensures proper Pydantic validation for each tool individually.

    return app


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="MCP REST Bridge")
    parser.add_argument(
        "--mcp-server",
        required=True,
        help="Python module path for MCP server (e.g., mcp_servers.tableau.main)",
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="Port to run the REST server on (default: 8000)"
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)")

    args = parser.parse_args()

    # Setup database first (before creating app)
    engine = setup_database(args.mcp_server)

    # Initialize the MCP server module (handles multiple patterns gracefully)
    init_result = initialize_mcp_server(args.mcp_server)
    logger.info(f"Initialization result: {init_result['pattern']} - {init_result['message']}")

    # Log warning if initialization failed (but don't abort - tools may still work)
    if not init_result["success"]:
        logger.warning("⚠️  MCP server initialization incomplete - some tools may not work")
        logger.warning(f"   Reason: {init_result['message']}")

    # Create the app with MCP stdio tools
    app = create_app(args.mcp_server)

    # Discover tools directly from tools/ directory (more complete)
    direct_tools_count = discover_direct_tools(app, args.mcp_server)
    logger.info(f"Discovered {direct_tools_count} direct tools")

    db_tools_count = 0

    # Add database management tools if database exists
    if engine:
        db_tools_count = add_database_tools(app, engine)
        logger.info(f"Added {db_tools_count} database management tools")

    total_tools = direct_tools_count + db_tools_count
    logger.info(f"Total tools available: {total_tools}")
    logger.info(f"Starting REST bridge for {args.mcp_server} on {args.host}:{args.port}")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

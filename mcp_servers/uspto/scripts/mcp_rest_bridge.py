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
from typing import Any

import uvicorn
from fastapi import FastAPI, HTTPException, Request
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

            # For "mcp_servers.tableau.ui", we want to cd to "mcp_servers/tableau"
            if len(module_parts) >= 3:
                server_dir = os.path.join(repo_root, module_parts[0], module_parts[1])
                # Run the specified module (ui or main) from the server directory
                module_name = module_parts[-1]  # 'ui' or 'main'
                cmd = [
                    "sh",
                    "-c",
                    f"cd {server_dir} && uv run python -c \"import runpy; \
                        runpy.run_module('{module_name}', run_name='__main__')\"",
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

            # Add repo_root to PYTHONPATH for absolute imports (e.g., from mcp_servers.adp.config)
            if "PYTHONPATH" in env:
                env["PYTHONPATH"] = f"{repo_root}:{env['PYTHONPATH']}"
            else:
                env["PYTHONPATH"] = repo_root

            logger.info(f"Setting PATH to include: {uv_path}")
            logger.info(f"Setting PYTHONPATH to include: {repo_root}")
            logger.info(f"Working directory: {repo_root}")

            # Always use stderr=None to avoid buffer deadlock. When stderr=PIPE
            # but never consumed, the 64KB buffer fills and blocks the subprocess.
            self.process = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,
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
                logger.error(
                    f"MCP server process exited immediately with code {self.process.returncode}"
                )
                if self.process.stderr:
                    stderr_output = self.process.stderr.read()
                    stderr_text = stderr_output.decode() if stderr_output else "No stderr output"
                    logger.error(f"MCP server stderr: {stderr_text}")
                    raise Exception(f"MCP server failed to start: {stderr_text}")
                raise Exception("MCP server failed to start")

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
            elif response and "error" in response:
                # Server doesn't support initialize method - this is OK if
                # direct tools were discovered
                error_msg = response["error"].get("message", "Unknown error")
                logger.warning(f"MCP server doesn't support initialize: {error_msg}")
                logger.warning("Continuing with direct tools only (MCP stdio fallback disabled)")
            else:
                logger.error(f"Failed to initialize MCP server: {response}")

                # Try to read stderr for more details
                if self.process.poll() is not None and self.process.stderr:
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

                # Read response with timeout (cross-platform, works on Windows)
                # Increased timeout to 120s to allow for `uv run` package sync
                timeout = 120.0

                try:
                    # Use asyncio to read with timeout (works on all platforms)
                    loop = asyncio.get_running_loop()
                    response_line = await asyncio.wait_for(
                        loop.run_in_executor(None, self.process.stdout.readline),
                        timeout=timeout,
                    )

                    if response_line:
                        response = json.loads(response_line.decode())
                        return response

                    # Empty response - check if process died
                    if self.process.poll() is not None:
                        logger.error(
                            f"MCP process died during request "
                            f"(exit code: {self.process.returncode})"
                        )
                        if self.process.stderr:
                            stderr_output = self.process.stderr.read()
                            if stderr_output:
                                logger.error(f"MCP stderr: {stderr_output.decode()}")
                        return None

                except TimeoutError:
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

    async def call_tool(
        self, tool_name: str, arguments: dict[str, Any], headers: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """
        Call a tool on the MCP server.

        Args:
            tool_name: Name of the tool to call
            arguments: Tool arguments
            headers: Optional HTTP headers (passed as _headers param, not in arguments)

        Returns:
            Tool response
        """
        params = {"name": tool_name, "arguments": arguments}

        # Add _headers in params._meta field to pass headers to middleware
        # RestBridgeMiddleware will extract this; servers without it will safely ignore it
        if headers:
            params["_meta"] = {"_headers": headers}

        request = {
            "jsonrpc": "2.0",
            "id": self._get_next_id(),
            "method": "tools/call",
            "params": params,
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
# Cache of discovered tools, keyed by tool name (populated by /api/discover)
discovered_tools_cache: dict[str, dict[str, Any]] = {}
# Flag to track if discovery has been run
_discovery_complete: bool = False


async def ensure_tools_discovered():
    """
    Ensure tools are discovered and cached.

    This is called automatically before tool calls to ensure the wrapper
    parameter cache is populated, even if /api/discover wasn't called first.
    """
    global _discovery_complete

    if _discovery_complete:
        return

    if not mcp_client:
        return

    try:
        mcp_tools = await mcp_client.list_tools()

        for tool in mcp_tools:
            tool_name = tool.get("name", "")
            if tool_name in discovered_tools_cache:
                continue

            rest_tool = {
                "name": tool_name,
                "description": tool.get("description", ""),
                "method": "POST",
                "parameters": [],
            }

            # Extract parameters from input schema
            input_schema = tool.get("inputSchema", {})
            properties = input_schema.get("properties", {})
            defs = input_schema.get("$defs", input_schema.get("definitions", {}))

            # Helper to resolve $ref and allOf references in JSON Schema
            # Uses visited set to prevent infinite recursion with circular refs
            def resolve_ref(prop_info: dict, visited: set | None = None) -> dict:
                if visited is None:
                    visited = set()

                if "$ref" in prop_info:
                    ref = prop_info["$ref"]
                    # Check for circular reference
                    if ref in visited:
                        return prop_info  # Return as-is to avoid infinite loop
                    if ref.startswith("#/$defs/") or ref.startswith("#/definitions/"):
                        def_name = ref.split("/")[-1]
                        resolved = defs.get(def_name, {})
                        # Track this ref as visited before recursing
                        visited.add(ref)
                        # Recursively resolve if the definition also has $ref or allOf
                        return resolve_ref(resolved, visited)
                    return prop_info
                if "allOf" in prop_info:
                    # Merge all items in allOf array
                    merged = {}
                    merged_props = {}
                    merged_required = []
                    for item in prop_info["allOf"]:
                        resolved_item = resolve_ref(item, visited)
                        # Merge properties
                        if "properties" in resolved_item:
                            merged_props.update(resolved_item["properties"])
                        if "required" in resolved_item:
                            merged_required.extend(resolved_item["required"])
                        # Merge other keys (type, title, etc.)
                        for key, val in resolved_item.items():
                            if key not in ("properties", "required"):
                                merged[key] = val
                    if merged_props:
                        merged["properties"] = merged_props
                    if merged_required:
                        merged["required"] = list(set(merged_required))
                    return merged
                return prop_info

            # Check for single Pydantic wrapper pattern (e.g., params: ListUsersInput)
            if len(properties) == 1:
                wrapper_name = list(properties.keys())[0]
                wrapper_info = properties[wrapper_name]
                resolved_info = resolve_ref(wrapper_info)

                if resolved_info.get("type") == "object" or "properties" in resolved_info:
                    rest_tool["_wrapper_param"] = wrapper_name

            # Cache the tool for lookup during call_mcp_tool
            discovered_tools_cache[tool_name] = rest_tool

        _discovery_complete = True
        logger.debug(f"Tools discovered and cached: {len(discovered_tools_cache)} tools")
    except Exception as e:
        logger.warning(f"Failed to discover tools: {e}")


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

        # Add project root (cwd) to sys.path for absolute imports
        project_root = Path.cwd()
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))
            logger.info(f"Added to sys.path: {project_root}")

        # Add server directory to sys.path for relative imports
        server_dir = project_root / server_module_path.replace(".", "/")
        if str(server_dir) not in sys.path:
            sys.path.insert(0, str(server_dir))
            logger.info(f"Added to sys.path: {server_dir}")

        # Set server-specific DATABASE_URL if not set
        if "DATABASE_URL" not in os.environ:
            server_name = parts[1] if len(parts) >= 2 else "default"
            # Use absolute path to ensure migrations and tools use the same database
            db_file = Path.cwd() / f"{server_name}_data.db"
            db_path = f"sqlite+aiosqlite:///{db_file}"
            os.environ["DATABASE_URL"] = db_path
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
        server_dir = Path.cwd() / server_module_path.replace(".", "/")
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
    try:
        parts = module_path.split(".")
        server_module_path = ".".join(parts[:-1])  # e.g., mcp_servers.tableau

        # Add project root to path for absolute imports
        # (e.g., "from mcp_servers.sap.models import ...")
        project_root = Path.cwd()
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))

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

        # Initialize the server if it has a create_server function
        # This ensures providers (OfflineProvider, etc.) are initialized before tools are imported
        try:
            main_module = importlib.import_module(module_path)
            if hasattr(main_module, "create_server"):
                logger.info("Calling create_server() to initialize providers")
                main_module.create_server()
            if hasattr(main_module, "mcp"):
                # FastMCP server object exists, initialization may have happened on import
                _ = main_module.mcp  # Access to trigger any lazy initialization
                logger.info("Found mcp server object, providers should be initialized")
        except Exception as e:
            logger.warning(f"Could not initialize server module: {e}")

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

                    # Check for Pydantic model signature (1 param with BaseModel)
                    is_pydantic_tool = False
                    input_model = None

                    if len(params) == 1:
                        first_param = params[0]
                        if (
                            hasattr(first_param.annotation, "__mro__")
                            and BaseModel in first_param.annotation.__mro__
                        ):
                            is_pydantic_tool = True
                            input_model = first_param.annotation

                    # For non-Pydantic tools, skip them here - they're handled via MCP stdio
                    if not is_pydantic_tool:
                        continue

                    # Store metadata
                    tool_metadata = {
                        "name": name,
                        "description": inspect.getdoc(obj) or f"{name} tool",
                        "method": "POST",
                        "parameters": [],
                    }

                    # Extract parameters from Pydantic model
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

                    # Note: We no longer create direct HTTP endpoints for individual tools
                    # because auth tokens are managed by the MCP stdio process.
                    # All tool calls go through the MCP fallback endpoint which routes
                    # to the MCP stdio process where auth is properly handled.
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
        # Import db_tools from scripts directory (project root is already in sys.path at line 35)
        from scripts.db_tools import get_db_management_tools

        logger.info("Successfully imported db_tools module")

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
                        raise HTTPException(status_code=500, detail=str(e))

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
                        raise HTTPException(status_code=500, detail=str(e))

                app.post(f"/tools/{tool_name}", response_model=output_model)(handler)

            count += 1
            logger.info(f"Added DB tool: {tool_name}")

        return count

    except ImportError as e:
        logger.warning(f"Could not import db_tools module: {e}")
        return 0
    except Exception as e:
        logger.warning(f"Could not add database tools: {e}", exc_info=True)
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
        global db_tools_registry

        all_tools = []

        # Note: Direct tools from tools/ directory are NOT included here.
        # All tool calls go through the MCP fallback endpoint which routes
        # to the MCP stdio process where auth is properly handled.
        # Direct tools would bypass auth and cause naming conflicts.

        # Priority 1: Database tools (these are REST-only, not in MCP)
        all_tools.extend(db_tools_registry)
        logger.info(f"Database tools: {len(db_tools_registry)}")

        # Priority 2: MCP stdio tools (includes all tools registered via FastMCP)
        if mcp_client:
            try:
                mcp_tools = await mcp_client.list_tools()

                # Get names of already discovered tools
                existing_tool_names = {t["name"] for t in all_tools}

                # Convert MCP tool format to REST bridge format
                mcp_tools_added = 0
                for tool in mcp_tools:
                    tool_name = tool.get("name", "")

                    # Skip if already discovered directly
                    if tool_name in existing_tool_names:
                        continue

                    rest_tool = {
                        "name": tool_name,
                        "description": tool.get("description", ""),
                        "method": "POST",
                        "parameters": [],
                    }

                    # Extract parameters from input schema
                    input_schema = tool.get("inputSchema", {})
                    properties = input_schema.get("properties", {})
                    required = input_schema.get("required", [])
                    defs = input_schema.get("$defs", input_schema.get("definitions", {}))

                    # Helper to resolve $ref references in JSON Schema
                    # Uses visited set to prevent infinite recursion with circular refs
                    def resolve_ref(prop_info: dict, visited: set | None = None) -> dict:
                        if visited is None:
                            visited = set()

                        if "$ref" in prop_info:
                            ref = prop_info["$ref"]
                            # Check for circular reference
                            if ref in visited:
                                return prop_info  # Return as-is to avoid infinite loop
                            if ref.startswith("#/$defs/") or ref.startswith("#/definitions/"):
                                def_name = ref.split("/")[-1]
                                resolved = defs.get(def_name, {})
                                # Track this ref as visited before recursing
                                visited.add(ref)
                                # Recursively resolve if the definition also has allOf
                                return resolve_ref(resolved, visited)
                            return prop_info
                        if "allOf" in prop_info:
                            # Merge all items in allOf array
                            merged = {}
                            merged_props = {}
                            merged_required = []
                            for item in prop_info["allOf"]:
                                resolved_item = resolve_ref(item, visited)
                                # Merge properties
                                if "properties" in resolved_item:
                                    merged_props.update(resolved_item["properties"])
                                if "required" in resolved_item:
                                    merged_required.extend(resolved_item["required"])
                                # Merge other keys (type, title, etc.)
                                for key, val in resolved_item.items():
                                    if key not in ("properties", "required"):
                                        merged[key] = val
                            if merged_props:
                                merged["properties"] = merged_props
                            if merged_required:
                                merged["required"] = list(set(merged_required))
                            return merged
                        return prop_info

                    # Check for single Pydantic wrapper pattern (e.g., params: ListUsersInput)
                    wrapper_param = None
                    if len(properties) == 1:
                        wrapper_name = list(properties.keys())[0]
                        wrapper_info = properties[wrapper_name]
                        resolved_info = resolve_ref(wrapper_info)
                        if resolved_info.get("type") == "object" or "properties" in resolved_info:
                            # Flatten nested properties
                            wrapper_param = wrapper_name
                            nested_props = resolved_info.get("properties", {})
                            nested_required = resolved_info.get("required", [])
                            for nested_name, nested_info in nested_props.items():
                                # Resolve nested $refs too
                                resolved_nested = resolve_ref(nested_info)
                                rest_tool["parameters"].append(
                                    {
                                        "name": nested_name,
                                        "type": resolved_nested.get("type", "string"),
                                        "required": nested_name in nested_required,
                                        "description": resolved_nested.get("description", ""),
                                    }
                                )

                    # If no wrapper pattern detected, use standard parameter extraction
                    if wrapper_param is None:
                        for param_name, param_info in properties.items():
                            # Resolve $ref for consistent behavior with wrapper path
                            resolved_info = resolve_ref(param_info)
                            rest_tool["parameters"].append(
                                {
                                    "name": param_name,
                                    "type": resolved_info.get("type", "string"),
                                    "required": param_name in required,
                                    "description": resolved_info.get("description", ""),
                                }
                            )

                    # Store wrapper info in the tool definition for reconstruction during tool call
                    if wrapper_param:
                        rest_tool["_wrapper_param"] = wrapper_param

                    # Cache the tool for lookup during call_mcp_tool
                    discovered_tools_cache[tool_name] = rest_tool

                    all_tools.append(rest_tool)
                    mcp_tools_added += 1

                logger.info(f"MCP stdio tools: {mcp_tools_added} (of {len(mcp_tools)} total)")
            except Exception as e:
                logger.error(f"Error listing MCP tools: {e}")

        logger.info(f"Total tools: {len(all_tools)}")

        return {"tools": all_tools}

    return app


def add_mcp_fallback_endpoint(app: FastAPI):
    """
    Add catch-all endpoint for MCP tools not discovered directly.

    This must be called AFTER discover_direct_tools() to ensure specific
    tool endpoints take priority over this fallback.
    """

    @app.post("/tools/{tool_name}")
    async def call_mcp_tool(tool_name: str, request: Request, request_body: dict[str, Any] = None):
        """
        Call an MCP tool by name.

        This endpoint handles tools that weren't discovered directly from the tools/
        directory (e.g., login_tool registered via setup_auth).
        """
        if request_body is None:
            request_body = {}

        if not mcp_client:
            raise HTTPException(status_code=503, detail="MCP client not initialized")

        # Ensure tools are discovered so we know about wrapper parameters
        await ensure_tools_discovered()

        # Check if tool uses Pydantic wrapper pattern and wrap parameters if needed
        tool_info = discovered_tools_cache.get(tool_name)
        if tool_info and "_wrapper_param" in tool_info:
            wrapper_param = tool_info["_wrapper_param"]
            # Wrap the flat parameters under the wrapper param name
            request_body = {wrapper_param: request_body}

        # Extract HTTP headers to pass separately (not in arguments)
        # This avoids Pydantic validation errors in servers without RestBridgeMiddleware
        headers_dict = dict(request.headers) if request.headers else None
        if headers_dict:
            header_keys = list(headers_dict.keys())
            logger.debug(f"[REST-BRIDGE] Passing headers for {tool_name}: keys={header_keys}")

        try:
            result = await mcp_client.call_tool(tool_name, request_body, headers=headers_dict)
            logger.debug(f"MCP result for {tool_name}: {result}")

            # Check for MCP error response
            if isinstance(result, dict) and result.get("isError"):
                content = result.get("content", [])
                error_msg = "Unknown error"
                if isinstance(content, list) and len(content) > 0:
                    first_item = content[0]
                    if isinstance(first_item, dict) and "text" in first_item:
                        error_msg = first_item["text"]

                # Determine HTTP status code based on error message
                error_text = error_msg.lower()
                if "authentication required" in error_text or "api key required" in error_text:
                    raise HTTPException(status_code=401, detail=error_msg)
                elif "access denied" in error_text:
                    raise HTTPException(status_code=403, detail=error_msg)
                elif "not found" in error_text:
                    raise HTTPException(status_code=404, detail=error_msg)
                elif "rate limit" in error_text:
                    raise HTTPException(status_code=429, detail=error_msg)
                elif "validation" in error_text:
                    raise HTTPException(status_code=422, detail=error_msg)
                else:
                    raise HTTPException(status_code=400, detail=error_msg)

            # Extract text content from MCP result
            if isinstance(result, dict) and "content" in result:
                content = result["content"]
                if isinstance(content, list) and len(content) > 0:
                    first_item = content[0]
                    if isinstance(first_item, dict) and "text" in first_item:
                        text = first_item["text"]
                        logger.debug(
                            f"Parsing JSON text: {text[:200] if len(text) > 200 else text}"
                        )
                        return json.loads(text)
                    return first_item
                return content
            return result
        except HTTPException:
            raise
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error for {tool_name}: {e}")
            logger.error(f"Raw result: {result}")
            raise HTTPException(status_code=500, detail=str(e))
        except Exception as e:
            logger.error(f"Error calling MCP tool {tool_name}: {e}")
            raise HTTPException(status_code=500, detail=str(e))


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

    # Add MCP fallback endpoint AFTER specific tool endpoints are registered
    # This ensures direct tool endpoints take priority over the catch-all
    add_mcp_fallback_endpoint(app)
    logger.info("Added MCP fallback endpoint for non-direct tools")

    total_tools = direct_tools_count + db_tools_count
    logger.info(f"Total tools available: {total_tools}")
    logger.info(f"Starting REST bridge for {args.mcp_server} on {args.host}:{args.port}")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

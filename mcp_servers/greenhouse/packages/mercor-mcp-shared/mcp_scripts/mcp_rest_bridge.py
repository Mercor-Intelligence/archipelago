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
    python scripts/mcp_rest_bridge.py --mcp-server mcp_servers.example.ui --port 8000
"""

import argparse
import asyncio
import csv
import importlib
import inspect
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import uvicorn
from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from loguru import logger
from pydantic import BaseModel, Field
from sqlalchemy import inspect as sqlalchemy_inspect

# =============================================================================
# Helper Functions
# =============================================================================


def get_sync_url(engine) -> str:
    """
    Convert an async SQLAlchemy engine URL to a sync URL for pandas compatibility.

    Args:
        engine: SQLAlchemy engine (sync or async)

    Returns:
        Synchronous database URL string
    """
    return (
        str(engine.url).replace("+aiosqlite", "").replace("+asyncpg", "").replace("+aiomysql", "")
    )


def get_file_db_path(db_url: str) -> str:
    """
    Extract the file path from a file-based database URL.

    Works with any file-based database URL (SQLite, DuckDB, etc.) that uses
    the standard "scheme:///path" format.

    Args:
        db_url: File-based database URL (e.g., "sqlite:///path/to/db.sqlite",
                "duckdb:///path/to/db.duckdb", "sqlite+aiosqlite:///path/to/db")

    Returns:
        Path to the database file
    """
    # File-based URLs use scheme:///path or scheme+driver:///path
    # Find the triple slash and extract everything after it
    if ":///" in db_url:
        return db_url.split(":///", 1)[1]
    # Fallback: return as-is if format doesn't match
    return db_url


def is_file_based_db(db_url: str) -> bool:
    """
    Check if a database URL is for a file-based database (vs host/port).

    File-based databases (SQLite, DuckDB) use local file paths.
    Host-based databases (PostgreSQL, MySQL) use network connections.

    Args:
        db_url: Database URL string (or engine URL converted to string)

    Returns:
        True if the URL is for a file-based database
    """
    # File-based URLs use ":///" (empty host) or have no host component
    # Examples: sqlite:///path, duckdb:///path, sqlite+aiosqlite:///path
    # Host-based: postgresql://host/db, mysql://host:port/db
    if "://" not in db_url:
        return False
    # Check for empty host (triple slash after scheme)
    scheme_end = db_url.index("://") + 3
    return db_url[scheme_end : scheme_end + 1] == "/" or db_url[scheme_end : scheme_end + 1] == ""


def is_sqlite_db(db_url: str) -> bool:
    """
    Check if a database URL is for SQLite specifically.

    SQLite requires special handling for PRAGMA statements.

    Args:
        db_url: Database URL string

    Returns:
        True if the URL is for a SQLite database
    """
    return db_url.startswith("sqlite")


# Safe directory for local file imports/exports (configurable via environment variable)
# Defaults to /tmp/mcp-local-files to prevent arbitrary file system access
LOCAL_FILES_SAFE_DIR = Path(os.environ.get("MCP_LOCAL_FILES_DIR", "/tmp/mcp-local-files"))


def validate_local_path(local_path: str, operation: str = "access") -> Path:
    """
    Validate that a local file path is within the safe directory.

    This prevents path traversal attacks where an attacker could specify
    paths like "../../../etc/passwd" or absolute paths outside the safe directory.

    Args:
        local_path: User-provided file path
        operation: Description of the operation (for error messages)

    Returns:
        Resolved absolute path within the safe directory

    Raises:
        HTTPException: If the path is outside the safe directory or invalid
    """
    # Ensure safe directory exists
    LOCAL_FILES_SAFE_DIR.mkdir(parents=True, exist_ok=True)

    # Convert to Path and resolve to absolute path
    # If local_path is relative, resolve it relative to the safe directory
    # If local_path is absolute, we'll check it's still within safe directory
    user_path = Path(local_path)

    if user_path.is_absolute():
        resolved_path = user_path.resolve()
    else:
        # Relative paths are resolved relative to the safe directory
        resolved_path = (LOCAL_FILES_SAFE_DIR / user_path).resolve()

    # Security check: ensure resolved path is within the safe directory
    safe_dir_resolved = LOCAL_FILES_SAFE_DIR.resolve()
    try:
        resolved_path.relative_to(safe_dir_resolved)
    except ValueError:
        # Path is outside safe directory - this is a security violation
        logger.warning(
            f"Path traversal attempt blocked: '{local_path}' resolves to '{resolved_path}' "
            f"which is outside safe directory '{safe_dir_resolved}'"
        )
        raise HTTPException(
            status_code=403,
            detail=f"Access denied: local file {operation} is restricted to {safe_dir_resolved}. "
            f"Use relative paths or paths within this directory.",
        )

    return resolved_path


def extract_table_name(csv_filename: str) -> str:
    """
    Extract table name from a CSV filename.

    Simply removes path components and .csv extension, preserving the original
    table name exactly. Used for import to match export's table names.

    Args:
        csv_filename: CSV filename (e.g., "user-data.csv" or "path/to/users.csv")

    Returns:
        Table name extracted from filename (e.g., "user-data" or "users")

    Raises:
        ValueError: If the resulting name would be empty
    """
    # Remove any path components
    name = os.path.basename(csv_filename)

    # Remove .csv extension if present
    if name.lower().endswith(".csv"):
        name = name[:-4]

    if not name:
        raise ValueError(f"Invalid CSV filename: '{csv_filename}' results in empty table name")

    return name


def sanitize_table_name(name: str) -> str:
    """
    Sanitize a user-provided string for use as a SQL table name.

    This prevents SQL injection by only allowing alphanumeric characters and underscores.
    Leading digits are prefixed with underscore to ensure valid SQL identifiers.

    Note: For import/export round-trips, use extract_table_name() instead to preserve
    the original table name. This function is for sanitizing untrusted external input.

    Args:
        name: Raw table name from user input (e.g., filename)

    Returns:
        Sanitized table name safe for SQL queries

    Raises:
        ValueError: If the sanitized name would be empty
    """
    # Remove any path components (in case of nested zip files)
    name = os.path.basename(name)

    # Remove file extension if present
    if "." in name:
        name = name.rsplit(".", 1)[0]

    # Replace any non-alphanumeric characters (except underscore) with underscore
    sanitized = re.sub(r"[^a-zA-Z0-9_]", "_", name)

    # Remove consecutive underscores
    sanitized = re.sub(r"_+", "_", sanitized)

    # Remove leading/trailing underscores
    sanitized = sanitized.strip("_")

    # Ensure it doesn't start with a digit (invalid SQL identifier)
    if sanitized and sanitized[0].isdigit():
        sanitized = f"t_{sanitized}"

    # Ensure we have a valid name
    if not sanitized:
        raise ValueError(f"Invalid table name derived from: {name}")

    # Limit length to reasonable SQL identifier length (most DBs support at least 128)
    sanitized = sanitized[:128]

    return sanitized


def parse_s3_path(s3_path: str) -> tuple[str, str]:
    """
    Parse an S3 path into bucket and key components.

    Args:
        s3_path: S3 path (e.g., "s3://bucket/path/to/file")

    Returns:
        Tuple of (bucket, key)

    Raises:
        ValueError: If the S3 path format is invalid
    """
    if not s3_path.startswith("s3://"):
        raise ValueError("Invalid S3 path format. Expected: s3://bucket/key")

    parts = s3_path[5:].split("/", 1)
    if len(parts) != 2:
        raise ValueError("Invalid S3 path format. Expected: s3://bucket/key")

    return parts[0], parts[1]


def run_alembic_migrations(module_path: str) -> tuple[bool, str]:
    """
    Run Alembic migrations for an MCP server module.

    Args:
        module_path: MCP server module path (e.g., 'mcp_servers.tableau.main')

    Returns:
        Tuple of (success: bool, message: str)
    """
    try:
        parts = module_path.split(".")
        server_module_path = ".".join(parts[:-1])  # e.g., mcp_servers.tableau

        project_root = Path.cwd()
        server_dir = project_root / server_module_path.replace(".", "/")
        alembic_ini = server_dir / "alembic.ini"

        if not alembic_ini.exists():
            return False, f"No alembic.ini found at {alembic_ini}"

        logger.info(f"Running database migrations from {server_dir}...")

        result = subprocess.run(
            ["uv", "run", "alembic", "upgrade", "head"],
            cwd=str(server_dir),
            capture_output=True,
            text=True,
            timeout=60,
        )

        if result.returncode == 0:
            logger.info("Database migrations completed successfully")
            logger.info(f"Migration stdout: {result.stdout}")
            return True, "Migrations completed successfully"
        else:
            error_msg = f"Migration failed with code {result.returncode}: {result.stderr}"
            logger.error(error_msg)
            return False, error_msg

    except subprocess.TimeoutExpired:
        error_msg = "Migration timed out after 60 seconds"
        logger.error(error_msg)
        return False, error_msg
    except Exception as e:
        error_msg = f"Could not run migrations: {e}"
        logger.error(error_msg, exc_info=True)
        return False, error_msg


# =============================================================================
# Trajectory Recording Models
# =============================================================================


class ToolCallRecord(BaseModel):
    """Single tool call record for trajectory tracking.

    Records everything needed to replay or analyze a tool call:
    - What tool was called with what arguments
    - What the response was (success or error)
    - Timing information for performance analysis
    """

    request_id: str = Field(..., description="Unique ID for this call")
    tool_name: str = Field(..., description="Name of the tool called")
    arguments: dict[str, Any] = Field(..., description="Input arguments")
    response: Any = Field(..., description="Tool response (success or error)")
    success: bool = Field(..., description="Whether the call succeeded")
    error_message: str | None = Field(None, description="Error message if failed")
    timestamp: datetime = Field(..., description="When the call was made")
    duration_ms: float = Field(..., description="Execution time in milliseconds")


class TrajectorySession(BaseModel):
    """Recording session for capturing tool call sequences.

    A trajectory session captures a sequence of tool calls that can be used for:
    - Training RL agents (golden trajectories)
    - Evaluating agent performance
    - Debugging and analysis
    - Generating training data
    """

    session_id: str = Field(..., description="Unique session identifier")
    started_at: datetime = Field(..., description="When recording started")
    stopped_at: datetime | None = Field(None, description="When recording stopped (None if active)")
    is_active: bool = Field(True, description="Whether recording is ongoing")
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)


class TrajectorySessionSummary(BaseModel):
    """Session summary without full tool call history (for listing)."""

    session_id: str
    started_at: datetime
    stopped_at: datetime | None
    is_active: bool
    total_calls: int


class TrajectoryManager:
    """Manages trajectory recording sessions.

    Thread-safe manager for creating, recording to, and exporting trajectory sessions.
    Sessions are stored in-memory and are lost on restart.

    Usage:
        # Start a recording session
        session = await trajectory_manager.start_session()

        # Record tool calls (done automatically by tool handlers)
        await trajectory_manager.record_call(session_id, record)

        # Export the session
        session = await trajectory_manager.get_session(session_id)

        # Stop recording
        await trajectory_manager.stop_session(session_id)
    """

    def __init__(self):
        self._sessions: dict[str, TrajectorySession] = {}
        self._lock = asyncio.Lock()

    async def start_session(self, session_id: str | None = None) -> TrajectorySession:
        """Start a new recording session.

        Args:
            session_id: Optional custom session ID. Auto-generated if not provided.

        Returns:
            The new TrajectorySession

        Raises:
            ValueError: If session_id already exists
        """
        async with self._lock:
            if session_id is None:
                session_id = f"traj_{uuid.uuid4().hex[:12]}"

            if session_id in self._sessions:
                raise ValueError(f"Session {session_id} already exists")

            session = TrajectorySession(
                session_id=session_id,
                started_at=datetime.now(),
                is_active=True,
            )
            self._sessions[session_id] = session
            logger.info(f"Started trajectory recording session: {session_id}")
            return session

    async def stop_session(self, session_id: str) -> TrajectorySession:
        """Stop a recording session.

        Args:
            session_id: Session to stop

        Returns:
            The stopped TrajectorySession

        Raises:
            ValueError: If session not found or already stopped
        """
        async with self._lock:
            if session_id not in self._sessions:
                raise ValueError(f"Session {session_id} not found")

            session = self._sessions[session_id]
            if not session.is_active:
                raise ValueError(f"Session {session_id} is already stopped")

            session.stopped_at = datetime.now()
            session.is_active = False
            logger.info(
                f"Stopped trajectory recording session: {session_id} "
                f"(recorded {len(session.tool_calls)} calls)"
            )
            return session

    async def record_call(self, session_id: str, record: ToolCallRecord) -> None:
        """Record a tool call to a session.

        Silently skips if session doesn't exist or is inactive (opt-in recording).

        Args:
            session_id: Session to record to
            record: The tool call record
        """
        async with self._lock:
            if session_id not in self._sessions:
                return  # Silently skip if session doesn't exist

            session = self._sessions[session_id]
            if session.is_active:
                session.tool_calls.append(record)

    async def get_session(self, session_id: str) -> TrajectorySession | None:
        """Get a session by ID.

        Returns a deep copy to prevent race conditions during serialization.

        Args:
            session_id: Session to get

        Returns:
            Deep copy of the session, or None if not found
        """
        async with self._lock:
            session = self._sessions.get(session_id)
            if session is None:
                return None
            # Return deep copy to ensure data consistency during export
            return session.model_copy(deep=True)

    async def delete_session(self, session_id: str) -> bool:
        """Delete a session.

        Args:
            session_id: Session to delete

        Returns:
            True if deleted, False if not found
        """
        async with self._lock:
            if session_id in self._sessions:
                del self._sessions[session_id]
                logger.info(f"Deleted trajectory session: {session_id}")
                return True
            return False

    async def update_session(
        self, session_id: str, tool_call_ids_to_keep: list[str]
    ) -> TrajectorySession:
        """Update a session by keeping only specified tool calls.

        Useful for pruning bad calls from a recording before export.

        Args:
            session_id: Session to update
            tool_call_ids_to_keep: List of request_ids to keep (others are deleted)

        Returns:
            Updated TrajectorySession (deep copy)

        Raises:
            ValueError: If session not found
        """
        async with self._lock:
            if session_id not in self._sessions:
                raise ValueError(f"Session {session_id} not found")

            session = self._sessions[session_id]
            original_count = len(session.tool_calls)
            session.tool_calls = [
                tc for tc in session.tool_calls if tc.request_id in tool_call_ids_to_keep
            ]
            new_count = len(session.tool_calls)
            logger.info(
                f"Updated trajectory session {session_id}: "
                f"{original_count} -> {new_count} tool calls"
            )
            return session.model_copy(deep=True)

    async def list_sessions(self) -> list[TrajectorySessionSummary]:
        """List all sessions with summary info."""
        async with self._lock:
            return [
                TrajectorySessionSummary(
                    session_id=s.session_id,
                    started_at=s.started_at,
                    stopped_at=s.stopped_at,
                    is_active=s.is_active,
                    total_calls=len(s.tool_calls),
                )
                for s in self._sessions.values()
            ]

    async def get_active_session_ids(self) -> list[str]:
        """Get list of active session IDs."""
        async with self._lock:
            return [sid for sid, s in self._sessions.items() if s.is_active]


# Global trajectory manager instance
trajectory_manager = TrajectoryManager()


# =============================================================================
# MCP Stdio Client
# =============================================================================


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
            import os

            repo_root = os.getcwd()

            # Use python -m to run the module with proper package context
            # This ensures relative imports work correctly
            cmd = ["uv", "run", "python", "-m", self.module_path]

            logger.info(f"Starting MCP server: {' '.join(cmd)}")
            logger.info(f"Working directory: {repo_root}")

            # Set up environment
            env = os.environ.copy()

            # Force stdio transport for the MCP subprocess (bridge communicates via stdin/stdout)
            env["MCP_TRANSPORT"] = "stdio"

            # Ensure uv is in PATH (it's installed in /root/.local/bin)
            uv_path = "/root/.local/bin"
            if "PATH" in env:
                if uv_path not in env["PATH"]:
                    env["PATH"] = f"{uv_path}:{env['PATH']}"
            else:
                env["PATH"] = f"{uv_path}:/usr/local/bin:/usr/bin:/bin"

            # Add repo_root to PYTHONPATH for absolute imports (e.g., from mcp_servers.adp.config)
            # Also add server directory for relative-style imports (e.g., from db.session)
            parts = self.module_path.split(".")
            server_module_path = ".".join(parts[:-1])  # e.g., mcp_servers.tableau
            server_dir = os.path.join(repo_root, server_module_path.replace(".", "/"))

            pythonpath_parts = [repo_root, server_dir]
            if "PYTHONPATH" in env:
                pythonpath_parts.append(env["PYTHONPATH"])
            env["PYTHONPATH"] = ":".join(pythonpath_parts)

            logger.info(f"Setting PATH to include: {uv_path}")
            logger.info(f"Setting PYTHONPATH to include: {repo_root}, {server_dir}")
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
            # Clean up subprocess if it was started
            if self.process and self.process.poll() is None:
                logger.info("Terminating MCP server subprocess due to initialization failure")
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    logger.warning("Subprocess did not terminate gracefully, killing it")
                    self.process.kill()
                    self.process.wait()
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


def _extract_tool_metadata(tool: dict[str, Any]) -> dict[str, Any]:
    """
    Extract REST tool metadata from MCP tool schema.

    Handles both single wrapper patterns (e.g., params: ListUsersInput) and
    mixed patterns (e.g., session_id: str, request: CreatePatientRequest).

    This is shared by /api/discover and ensure_tools_discovered() to ensure
    consistent parameter detection logic.

    Args:
        tool: MCP tool definition with inputSchema

    Returns:
        REST tool dict with name, description, method, parameters, and
        optional _wrapper_param, _nested_params, _simple_params metadata
    """
    tool_name = tool.get("name", "")
    rest_tool: dict[str, Any] = {
        "name": tool_name,
        "description": tool.get("description", ""),
        "method": "POST",
        "parameters": [],
    }

    input_schema = tool.get("inputSchema", {})
    properties = input_schema.get("properties", {})
    required = input_schema.get("required", [])
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

    # Detect Pydantic model parameters that need wrapping
    # Case 1: Single wrapper (e.g., params: ListUsersInput)
    # Case 2: Mixed params (e.g., session_id: str, request: CreatePatientRequest)
    nested_params = {}  # Maps param name -> list of its inner property names
    simple_params = []  # List of simple (non-nested) param names

    for prop_name, prop_info in properties.items():
        # Check for $ref or allOf (Pydantic v2 wraps $ref in allOf when adding metadata)
        if "$ref" in prop_info or "allOf" in prop_info:
            # This is a reference to a Pydantic model or simple type alias
            resolved_info = resolve_ref(prop_info)
            if "properties" in resolved_info:
                # Store the inner property names for this nested param
                nested_params[prop_name] = list(resolved_info.get("properties", {}).keys())
            else:
                # $ref to a simple type (string alias, enum, etc.) - treat as simple param
                simple_params.append(prop_name)
        else:
            simple_params.append(prop_name)

    if len(properties) == 1 and len(nested_params) == 1:
        # Single wrapper pattern - all flat params go under this key
        wrapper_name = list(nested_params.keys())[0]
        rest_tool["_wrapper_param"] = wrapper_name

        # Flatten nested properties for display in REST tool parameters
        wrapper_info = properties[wrapper_name]
        resolved_info = resolve_ref(wrapper_info)
        nested_props = resolved_info.get("properties", {})
        nested_required = resolved_info.get("required", [])
        for nested_name, nested_info in nested_props.items():
            resolved_nested = resolve_ref(nested_info)
            rest_tool["parameters"].append(
                {
                    "name": nested_name,
                    "type": resolved_nested.get("type", "string"),
                    "required": nested_name in nested_required,
                    "description": resolved_nested.get("description", ""),
                }
            )
    elif nested_params:
        # Mixed pattern - need to know which params are nested
        rest_tool["_nested_params"] = nested_params
        rest_tool["_simple_params"] = simple_params

        # Add all parameters (both simple and nested) for display
        for param_name, param_info in properties.items():
            resolved_info = resolve_ref(param_info)
            rest_tool["parameters"].append(
                {
                    "name": param_name,
                    "type": resolved_info.get("type", "string"),
                    "required": param_name in required,
                    "description": resolved_info.get("description", ""),
                }
            )
    else:
        # No wrapper pattern - standard parameter extraction
        for param_name, param_info in properties.items():
            resolved_info = resolve_ref(param_info)
            rest_tool["parameters"].append(
                {
                    "name": param_name,
                    "type": resolved_info.get("type", "string"),
                    "required": param_name in required,
                    "description": resolved_info.get("description", ""),
                }
            )

    return rest_tool


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

            # Use shared helper for consistent parameter detection
            rest_tool = _extract_tool_metadata(tool)

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

        # Try to import database module from various patterns
        db_module = None
        db_module_paths = [
            f"{server_module_path}.db.session",  # Standard pattern (Tableau, etc.)
            f"{server_module_path}.repositories.database",  # OpenEMR pattern
        ]

        for db_module_path in db_module_paths:
            try:
                db_module = importlib.import_module(db_module_path)
                logger.info(f"Database module imported: {db_module_path}")

                # For OpenEMR-style databases, ensure initialization
                if hasattr(db_module, "ensure_database_initialized"):
                    db_module.ensure_database_initialized()
                    logger.info("Called ensure_database_initialized()")

                break
            except ModuleNotFoundError:
                logger.debug(f"Database module not found: {db_module_path}")
                continue

        if db_module is None:
            logger.info(f"No database detected in any of: {db_module_paths}")
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
        # Import db_tools from mcp_middleware (shared library)
        from mcp_middleware.db_tools import get_db_management_tools

        logger.info("Successfully imported db_tools module from mcp_middleware")

        # Pass a callable that retrieves the current engine from app.state.
        # This ensures tools use the current engine even after /clear replaces it.
        db_tools = get_db_management_tools(lambda: app.state.engine)
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
                async def _no_param_handler(func=func):
                    try:
                        result = func() if not inspect.iscoroutinefunction(func) else await func()
                        if inspect.iscoroutine(result):
                            result = await result
                        return result
                    except Exception as e:
                        raise HTTPException(status_code=500, detail=str(e))

                app.get(f"/tools/{tool_name}", response_model=output_model)(_no_param_handler)
            else:
                # Parameterized tool
                async def _param_handler(request: input_model, func=func):  # pyright: ignore[reportInvalidTypeForm]
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

                app.post(f"/tools/{tool_name}", response_model=output_model)(_param_handler)

            count += 1
            logger.info(f"Added DB tool: {tool_name}")

        return count

    except ImportError as e:
        logger.warning(f"Could not import db_tools module: {e}")
        return 0
    except Exception as e:
        logger.warning(f"Could not add database tools: {e}", exc_info=True)
        return 0


# =============================================================================
# Export/Import Helper Functions (module-level for testability)
# =============================================================================


async def _create_export_zip_from_engine(db_engine) -> bytes:
    """Create a ZIP archive containing all tables as CSV files from database engine.

    Uses run_sync to execute on the same connection, which is required for
    in-memory SQLite databases (they are connection-scoped).

    Raises:
        RuntimeError: If any tables fail to export (to prevent incomplete backups)
    """

    async with db_engine.connect() as conn:

        def sync_export(sync_conn):
            """Synchronous export logic running on the same connection."""
            inspector = sqlalchemy_inspect(sync_conn)
            table_names = inspector.get_table_names()

            logger.info(f"Found {len(table_names)} tables to export: {table_names}")

            zip_buffer = io.BytesIO()
            failed_tables: list[tuple[str, str]] = []  # (table_name, error_message)

            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
                for table_name in table_names:
                    try:
                        df = pd.read_sql_table(table_name, sync_conn)
                        csv_buffer = io.StringIO()
                        # Use \N for NULL (MySQL convention) to distinguish from empty strings
                        df.to_csv(csv_buffer, index=False, na_rep="\\N")
                        zip_file.writestr(f"{table_name}.csv", csv_buffer.getvalue())
                        logger.info(f"Exported table {table_name} with {len(df)} rows")
                    except Exception as e:
                        logger.error(f"Failed to export table {table_name}: {e}")
                        failed_tables.append((table_name, str(e)))

            if failed_tables:
                failed_info = ", ".join(f"{name}: {err}" for name, err in failed_tables)
                raise RuntimeError(f"Export incomplete - failed tables: {failed_info}")

            return zip_buffer.getvalue()

        return await conn.run_sync(sync_export)


def _create_export_zip(db_path: str) -> bytes:
    r"""Create a ZIP archive from SQLite database (fallback).

    Uses \\N to represent NULL values for consistency with the pandas-based export.
    """
    zip_buffer = io.BytesIO()

    def convert_row(row):
        r"""Convert None values to \\N marker for NULL consistency."""
        return ["\\N" if val is None else val for val in row]

    with (
        zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file,
        sqlite3.connect(db_path) as conn,
    ):
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        tables = cursor.fetchall()

        for (table_name,) in tables:
            cursor.execute(f'SELECT * FROM "{table_name}"')
            rows = cursor.fetchall()
            headers = [desc[0] for desc in cursor.description]

            csv_buffer_inner = io.StringIO()
            csv_writer = csv.writer(csv_buffer_inner)
            csv_writer.writerow(headers)
            csv_writer.writerows(convert_row(row) for row in rows)
            zip_file.writestr(f"{table_name}.csv", csv_buffer_inner.getvalue())

    return zip_buffer.getvalue()


async def _import_from_csv_zip(file_data: bytes, db_engine) -> None:
    """Import data from a ZIP file containing CSV files.

    All tables are imported atomically in a single transaction. If any table
    fails to import, the entire operation is rolled back to maintain consistency.

    Security: Table names are validated against existing database tables to prevent
    SQL injection. Only CSVs targeting tables that already exist can be imported.

    Args:
        file_data: Raw bytes of a ZIP file containing CSV files
        db_engine: SQLAlchemy async engine

    Raises:
        ValueError: If any CSV file fails to import or targets a non-existent table
    """
    from sqlalchemy import text

    zip_buffer = io.BytesIO(file_data)
    db_url = str(db_engine.url)
    is_sqlite = is_sqlite_db(db_url)

    # Get list of existing tables for validation (prevents SQL injection)
    async with db_engine.connect() as conn:
        existing_tables = await conn.run_sync(
            lambda sync_conn: set(sqlalchemy_inspect(sync_conn).get_table_names())
        )
    logger.debug(f"Existing tables in database: {existing_tables}")

    with zipfile.ZipFile(zip_buffer, "r") as zip_file:
        csv_files = [f for f in zip_file.namelist() if f.endswith(".csv")]
        logger.info(f"Found {len(csv_files)} CSV files to import: {csv_files}")

        # Check for duplicate table names (use extract_table_name to preserve original names)
        table_names_map: dict[str, list[str]] = {}
        for csv_filename in csv_files:
            table_name = extract_table_name(csv_filename)
            table_names_map.setdefault(table_name, []).append(csv_filename)

        duplicates = {k: v for k, v in table_names_map.items() if len(v) > 1}
        if duplicates:
            dup_info = ", ".join(f"'{table}' from {files}" for table, files in duplicates.items())
            raise ValueError(
                f"Multiple CSV files map to the same table name: {dup_info}. "
                "Rename CSV files to avoid data loss."
            )

        # Validate all table names exist in database (prevents SQL injection)
        unknown_tables = set(table_names_map.keys()) - existing_tables
        if unknown_tables:
            raise ValueError(
                f"CSV files target non-existent tables: {sorted(unknown_tables)}. "
                f"Only existing tables can be imported: {sorted(existing_tables)}"
            )

        # Phase 1: Parse all CSV files and prepare DataFrames before touching the database.
        # This ensures we fail fast on malformed CSVs without modifying any data.
        tables_to_import: list[tuple[str, str, pd.DataFrame]] = []
        for csv_filename in csv_files:
            try:
                table_name = extract_table_name(csv_filename)
                csv_data = zip_file.read(csv_filename).decode("utf-8")
                # Use \N as NULL marker, keep_default_na=False preserves empty strings
                df = pd.read_csv(io.StringIO(csv_data), na_values=["\\N"], keep_default_na=False)
                # Convert NaN to None for database insertion
                df = df.where(pd.notnull(df), None)
                tables_to_import.append((csv_filename, table_name, df))
            except Exception as e:
                logger.error(f"Could not parse CSV file {csv_filename}: {e}")
                raise ValueError(f"Failed to parse '{csv_filename}': {str(e)}")

        # Phase 2: Import all tables in a single transaction for atomicity.
        # Either all tables are updated or none are (on error, rollback).
        async with db_engine.connect() as conn:
            if is_sqlite:
                await conn.run_sync(lambda c: c.exec_driver_sql("PRAGMA foreign_keys = OFF"))
            try:
                for csv_filename, table_name, df in tables_to_import:
                    try:
                        await conn.execute(text(f'DELETE FROM "{table_name}"'))
                        # run_sync executes pandas to_sql within the same transaction
                        # Capture table_name and df in closure to avoid late binding
                        await conn.run_sync(
                            lambda sync_conn, tn=table_name, data=df: data.to_sql(
                                tn, sync_conn, if_exists="append", index=False
                            )
                        )
                        logger.info(f"Prepared table {table_name} with {len(df)} rows")
                    except Exception as e:
                        logger.error(f"Could not import CSV file {csv_filename}: {e}")
                        raise ValueError(f"Failed to import from '{csv_filename}': {str(e)}")

                # Commit only after all tables succeed
                await conn.commit()
                logger.info(f"Successfully imported {len(tables_to_import)} tables")

            finally:
                if is_sqlite:
                    await conn.run_sync(lambda c: c.exec_driver_sql("PRAGMA foreign_keys = ON"))


def create_app(module_path: str, engine=None) -> FastAPI:
    """
    Create FastAPI app for the REST bridge.

    Args:
        module_path: Python module path for MCP server
        engine: SQLAlchemy database engine (optional)

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

    # Store engine for later use
    app.state.engine = engine

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
        # Dispose database engine to release connection pool resources
        if hasattr(app.state, "engine") and app.state.engine is not None:
            await app.state.engine.dispose()
            logger.info("Database engine disposed")
        logger.info("REST bridge stopped")

    @app.get("/")
    async def root():
        """Root endpoint providing API information."""
        return {
            "name": "MCP REST Bridge",
            "version": "1.0.0",
            "status": "running",
            "endpoints": {
                "health": "/health",
                "discover": "/api/discover",
                "tools": "/tools/{tool_name}",
                "export": "/export",
                "import": "/import",
            },
        }

    @app.get("/health")
    async def health():
        """Health check endpoint."""
        return {"status": "ok"}

    @app.get("/files/{root}/{path:path}")
    async def serve_file(root: str, path: str):
        """
        Serve a file from a filesystem root as base64-encoded JSON.

        Returns JSON with base64 content to work through proxies that expect JSON responses.
        This bypasses the MCP stdio transport which has buffer size limits for large files.
        """
        import base64
        import mimetypes

        try:
            from mcp_middleware.fs_tools import _resolve_path

            resolved_path, fs_root = _resolve_path(root, path)

            if not resolved_path.exists():
                raise HTTPException(status_code=404, detail=f"File not found: {path}")

            if not resolved_path.is_file():
                raise HTTPException(status_code=400, detail=f"Not a file: {path}")

            # Check file size (100 MB limit, same as download_file_impl)
            max_file_size = 100 * 1024 * 1024
            try:
                file_size = resolved_path.stat().st_size
                if file_size > max_file_size:
                    size_mb = file_size / (1024 * 1024)
                    limit_mb = max_file_size / (1024 * 1024)
                    raise HTTPException(
                        status_code=400,
                        detail=f"File too large: {size_mb:.1f} MB exceeds {limit_mb:.0f} MB limit",
                    )
            except OSError as e:
                raise HTTPException(status_code=500, detail=f"File size check error: {e}")

            # Read file and encode as base64
            try:
                with open(resolved_path, "rb") as f:
                    content = f.read()
            except PermissionError:
                raise HTTPException(status_code=403, detail=f"Permission denied: {path}")
            except OSError as e:
                raise HTTPException(status_code=500, detail=f"Read error: {e}")

            content_base64 = base64.b64encode(content).decode("utf-8")
            mime_type, _ = mimetypes.guess_type(str(resolved_path))

            return {
                "content_base64": content_base64,
                "mime_type": mime_type,
                "file_name": resolved_path.name,
            }
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.get("/api/discover")
    async def discover_tools():
        """
        Discover all available tools (Direct + MCP + Database).

        Returns:
            List of tools with their metadata
        """
        # Ensure MCP tools are discovered and cached with consistent parameter detection
        await ensure_tools_discovered()

        all_tools = []

        # Note: Direct tools from tools/ directory are NOT included here.
        # All tool calls go through the MCP fallback endpoint which routes
        # to the MCP stdio process where auth is properly handled.
        # Direct tools would bypass auth and cause naming conflicts.

        # Priority 1: Database tools (these are REST-only, not in MCP)
        all_tools.extend(db_tools_registry)
        logger.info(f"Database tools: {len(db_tools_registry)}")

        # Priority 2: MCP stdio tools from cache (populated by ensure_tools_discovered)
        existing_tool_names = {t["name"] for t in all_tools}
        mcp_tools_added = 0
        for tool_name, rest_tool in discovered_tools_cache.items():
            if tool_name not in existing_tool_names:
                all_tools.append(rest_tool)
                mcp_tools_added += 1

        logger.info(f"MCP stdio tools: {mcp_tools_added}")
        logger.info(f"Total tools: {len(all_tools)}")

        return {"tools": all_tools}

    # ==========================================================================
    # Import/Export Endpoints
    # ==========================================================================
    # Note: _create_export_zip_from_engine, _create_export_zip, and
    # _import_from_csv_zip are defined at module level for testability

    async def _do_export():
        """Internal export logic."""
        current_engine = app.state.engine
        if not current_engine:
            db_url = os.environ.get("DATABASE_URL", "")
            if is_sqlite_db(db_url):
                db_path = get_file_db_path(db_url)
                if os.path.exists(db_path):
                    try:
                        # Run sync export in thread pool to avoid blocking event loop
                        zip_data = await asyncio.to_thread(_create_export_zip, db_path)
                        logger.info(f"Exported SQLite database as ZIP: {len(zip_data)} bytes")
                        return Response(
                            content=zip_data,
                            media_type="application/zip",
                            headers={"Content-Disposition": "attachment; filename=export.zip"},
                        )
                    except Exception as e:
                        logger.error(f"SQLite export failed: {e}")
                        raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")
                else:
                    raise HTTPException(status_code=404, detail="Database file not found")
            else:
                raise HTTPException(status_code=500, detail="Could not access database engine")

        try:
            zip_data = await _create_export_zip_from_engine(current_engine)
            logger.info(f"Exported database as ZIP: {len(zip_data)} bytes")
            return Response(
                content=zip_data,
                media_type="application/zip",
                headers={"Content-Disposition": "attachment; filename=export.zip"},
            )
        except Exception as e:
            logger.error(f"Export failed: {e}")
            raise HTTPException(status_code=500, detail=f"Export failed: {str(e)}")

    @app.get("/export")
    async def export_data_get():
        """GET endpoint for export - returns ZIP of CSVs.

        When a database engine is available, works with any database type.
        Fallback mode (no engine) only supports SQLite databases.
        """
        return await _do_export()

    @app.post("/export")
    async def export_data_post():
        """POST endpoint for export - returns ZIP of CSVs.

        When a database engine is available, works with any database type.
        Fallback mode (no engine) only supports SQLite databases.
        """
        return await _do_export()

    @app.post("/import")
    async def import_data(file: UploadFile | None = None):
        """Import state from uploaded file.

        Accepts ZIP files containing CSVs. When a database engine is available,
        works with any database type. Fallback mode (no engine) only supports
        SQLite databases. Raw .db file import is SQLite-only.
        """
        if file is None:
            raise HTTPException(status_code=400, detail="File upload required")

        try:
            file_data = await file.read()
            current_engine = app.state.engine

            if current_engine:
                if (
                    file.filename and file.filename.endswith(".zip")
                ) or file.content_type == "application/zip":
                    try:
                        zip_buffer = io.BytesIO(file_data)
                        with zipfile.ZipFile(zip_buffer, "r") as zf:
                            csv_files = [f for f in zf.namelist() if f.endswith(".csv")]
                            if not csv_files:
                                raise HTTPException(
                                    status_code=400,
                                    detail="Invalid ZIP file: No CSV files found",
                                )
                    except zipfile.BadZipFile:
                        raise HTTPException(
                            status_code=400,
                            detail="Invalid ZIP file: File is not a valid ZIP archive",
                        )

                    await _import_from_csv_zip(file_data, current_engine)
                    return {"success": True, "message": "Data imported from CSV ZIP file"}
                else:
                    raise HTTPException(
                        status_code=400,
                        detail="Unsupported file format. Expected ZIP with CSV files.",
                    )
            else:
                db_url = os.environ.get("DATABASE_URL", "")
                if is_sqlite_db(db_url):
                    # Check if it's a ZIP file with CSVs
                    if (
                        file.filename and file.filename.endswith(".zip")
                    ) or file.content_type == "application/zip":
                        try:
                            zip_buffer = io.BytesIO(file_data)
                            with zipfile.ZipFile(zip_buffer, "r") as zf:
                                csv_files = [f for f in zf.namelist() if f.endswith(".csv")]
                                if not csv_files:
                                    raise HTTPException(
                                        status_code=400,
                                        detail="Invalid ZIP file: No CSV files found",
                                    )
                        except zipfile.BadZipFile:
                            raise HTTPException(
                                status_code=400,
                                detail="Invalid ZIP file: File is not a valid ZIP archive",
                            )
                        # Create async engine from SQLite URL and import CSVs
                        from sqlalchemy.ext.asyncio import create_async_engine

                        # Convert to async URL if needed (avoid double conversion)
                        if db_url.startswith("sqlite+aiosqlite://"):
                            async_db_url = db_url
                        else:
                            async_db_url = db_url.replace("sqlite://", "sqlite+aiosqlite://")
                        temp_engine = create_async_engine(async_db_url)
                        try:
                            await _import_from_csv_zip(file_data, temp_engine)
                        finally:
                            await temp_engine.dispose()
                        return {"success": True, "message": "Data imported from CSV ZIP file"}
                    elif file.filename and file.filename.endswith(".db"):
                        # Raw .db file replacement - validate it's actually SQLite
                        # SQLite files start with "SQLite format 3\x00"
                        sqlite_header = b"SQLite format 3\x00"
                        if not file_data.startswith(sqlite_header):
                            raise HTTPException(
                                status_code=400,
                                detail="Invalid SQLite file: missing SQLite header",
                            )
                        db_path = get_file_db_path(db_url)
                        with open(db_path, "wb") as f:
                            f.write(file_data)
                        return {"success": True, "message": "Database file replaced"}
                    else:
                        raise HTTPException(
                            status_code=400,
                            detail="Unsupported format. Expected ZIP with CSVs or .db file.",
                        )
                else:
                    raise HTTPException(
                        status_code=400,
                        detail="No database engine available. Cannot import file.",
                    )

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"File upload import failed: {e}")
            raise HTTPException(status_code=400, detail=f"Import failed: {str(e)}")

    @app.post("/clear")
    async def clear_data():
        """Clear all data in the database (reset to empty state).

        Only supported for file-based databases (SQLite, DuckDB). Deletes the
        database file and runs Alembic migrations to recreate the schema.
        """
        db_url = os.environ.get("DATABASE_URL", "")
        if not is_file_based_db(db_url):
            raise HTTPException(
                status_code=400, detail="Clear only supported for file-based databases"
            )

        db_path = get_file_db_path(db_url)

        # Pre-check: Verify migrations can run BEFORE deleting the database
        # This prevents leaving the system in a broken state with no data and no schema
        parts = module_path.split(".")
        server_module_path = ".".join(parts[:-1])
        project_root = Path.cwd()
        server_dir = project_root / server_module_path.replace(".", "/")
        alembic_ini = server_dir / "alembic.ini"

        if not alembic_ini.exists():
            raise HTTPException(
                status_code=400,
                detail=f"Cannot clear database: No alembic.ini found at {alembic_ini}. "
                "Schema recreation requires Alembic migrations.",
            )

        try:
            current_engine = app.state.engine
            if current_engine:
                await current_engine.dispose()
                # Clear reference to disposed engine to prevent subsequent requests
                # from using a disposed connection pool
                app.state.engine = None
                logger.info("Disposed SQLAlchemy connection pool before database clear")

            if os.path.exists(db_path):
                os.remove(db_path)
                logger.info(f"Removed database file: {db_path}")

            success, message = run_alembic_migrations(module_path)

            if success:
                # Restore the database engine - this is critical for subsequent requests
                fresh_engine = None

                # Define db_module_paths before try block so it's available for fallback
                parts = module_path.split(".")
                server_module_path = ".".join(parts[:-1])
                # Reuse parts/server_module_path computed earlier (lines 1779-1780)
                db_module_paths = [
                    f"{server_module_path}.db.session",  # Standard pattern
                    f"{server_module_path}.repositories.database",  # OpenEMR pattern
                ]

                try:
                    db_module = None
                    for db_module_path in db_module_paths:
                        try:
                            db_module = importlib.import_module(db_module_path)
                            logger.info(f"Found database module at {db_module_path}")
                            break
                        except ImportError:
                            continue

                    if db_module:
                        if hasattr(db_module, "reset_engine"):
                            db_module.reset_engine()
                            logger.info("Reset database engine cache")

                        if hasattr(db_module, "engine"):
                            fresh_engine = db_module.engine
                        elif hasattr(db_module, "get_engine"):
                            result = db_module.get_engine()
                            fresh_engine = await result if asyncio.iscoroutine(result) else result
                        elif hasattr(db_module, "_engine"):
                            fresh_engine = db_module._engine

                except Exception as e:
                    logger.warning(f"Could not get engine from db module: {e}")

                # Fallback: try reimporting db module if we still don't have an engine
                # Note: can't use setup_database() here as it uses asyncio.run()
                # which fails inside an already-running event loop
                if not fresh_engine:
                    logger.info("Attempting engine restoration via fresh db module import")
                    try:
                        # Force reimport of the db module to get fresh engine
                        for module_path_str in db_module_paths:
                            if module_path_str in sys.modules:
                                del sys.modules[module_path_str]

                        for module_path_str in db_module_paths:
                            try:
                                fresh_db_module = importlib.import_module(module_path_str)
                                if hasattr(fresh_db_module, "engine"):
                                    fresh_engine = fresh_db_module.engine
                                elif hasattr(fresh_db_module, "get_engine"):
                                    result = fresh_db_module.get_engine()
                                    fresh_engine = (
                                        await result if asyncio.iscoroutine(result) else result
                                    )
                                elif hasattr(fresh_db_module, "_engine"):
                                    fresh_engine = fresh_db_module._engine
                                if fresh_engine:
                                    break
                            except ImportError:
                                continue
                    except Exception as e2:
                        logger.warning(f"Fallback engine restoration failed: {e2}")

                if fresh_engine:
                    app.state.engine = fresh_engine
                    logger.info("Updated app.state.engine with fresh engine")
                else:
                    # Critical: we cleared the DB but can't restore the engine
                    raise HTTPException(
                        status_code=500,
                        detail="Database cleared but failed to restore database engine. "
                        "Server restart may be required.",
                    )

                return {
                    "success": True,
                    "message": "Database cleared and schema recreated via migrations.",
                }
            else:
                raise HTTPException(
                    status_code=500,
                    detail=f"Database file removed but migrations failed: {message}",
                )

        except HTTPException:
            raise
        except Exception as e:
            logger.error(f"Clear failed: {e}")
            raise HTTPException(status_code=500, detail=f"Clear failed: {str(e)}")

    return app


def add_mcp_tool_endpoint(app: FastAPI):
    """
    Add endpoint for calling MCP tools via stdio.

    All tool calls are routed through the MCP stdio subprocess to ensure
    proper session handling and authentication.
    """

    async def _call_mcp_tool_impl(tool_name: str, request: Request):
        """Call an MCP tool by name, routing through the MCP stdio subprocess.

        Supports trajectory recording when `trajectory_session` query param is provided.
        """
        # Trajectory recording setup
        trajectory_session_id = request.query_params.get("trajectory_session")
        request_id = str(uuid.uuid4())
        start_time = time.time()

        # Get request body for POST, query params for GET
        if request.method == "POST":
            try:
                request_body = await request.json()
            except Exception:
                request_body = {}
        else:
            # Filter out trajectory_session from args (it's not a tool parameter)
            request_body = {
                k: v for k, v in request.query_params.items() if k != "trajectory_session"
            }

        if not mcp_client:
            raise HTTPException(status_code=503, detail="MCP client not initialized")

        # Ensure tools are discovered so we know about wrapper parameters
        await ensure_tools_discovered()

        # Check if tool uses Pydantic wrapper pattern and wrap parameters if needed
        tool_info = discovered_tools_cache.get(tool_name)
        if tool_info:
            if "_wrapper_param" in tool_info:
                # Single wrapper pattern - all flat params go under this key
                wrapper_param = tool_info["_wrapper_param"]
                request_body = {wrapper_param: request_body}
            elif "_nested_params" in tool_info:
                # Mixed pattern - some params are simple, some are nested Pydantic models
                nested_params = tool_info["_nested_params"]
                simple_params = tool_info.get("_simple_params", [])

                # Handle each param individually to support partially-structured requests
                restructured = {}

                # Copy simple params directly
                for param_name in simple_params:
                    if param_name in request_body:
                        restructured[param_name] = request_body[param_name]

                # For each nested param, either use existing dict or collect from flat params
                for nested_name, inner_props in nested_params.items():
                    if nested_name in request_body and isinstance(request_body[nested_name], dict):
                        # Already structured - use as-is
                        restructured[nested_name] = request_body[nested_name]
                    else:
                        # Collect inner properties from flat request
                        nested_obj = {}
                        for prop_name in inner_props:
                            if prop_name in request_body:
                                nested_obj[prop_name] = request_body[prop_name]
                        if nested_obj:
                            restructured[nested_name] = nested_obj

                request_body = restructured

        # Extract HTTP headers to pass separately (not in arguments)
        # This avoids Pydantic validation errors in servers without RestBridgeMiddleware
        headers_dict = dict(request.headers) if request.headers else None
        if headers_dict:
            header_keys = list(headers_dict.keys())
            logger.debug(f"[REST-BRIDGE] Passing headers for {tool_name}: keys={header_keys}")

        # Helper to record trajectory call (fault-tolerant to avoid masking tool errors)
        async def record_trajectory(
            response_data: Any, success: bool, error_message: str | None = None
        ):
            if not trajectory_session_id:
                return
            try:
                duration_ms = (time.time() - start_time) * 1000
                # Serialize response for storage
                try:
                    if hasattr(response_data, "model_dump"):
                        serialized = response_data.model_dump()
                    elif isinstance(response_data, dict):
                        serialized = response_data
                    else:
                        serialized = str(response_data) if response_data else None
                except Exception:
                    serialized = str(response_data) if response_data else None

                record = ToolCallRecord(
                    request_id=request_id,
                    tool_name=tool_name,
                    arguments=request_body,
                    response=serialized,
                    success=success,
                    error_message=error_message,
                    timestamp=datetime.now(),
                    duration_ms=duration_ms,
                )
                await trajectory_manager.record_call(trajectory_session_id, record)
            except Exception as e:
                # Log but don't propagate - trajectory recording shouldn't mask tool errors
                logger.warning(f"Failed to record trajectory for {tool_name}: {e}")

        result = None  # Initialize for error handlers
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

                # Record failed call before raising
                await record_trajectory(None, success=False, error_message=error_msg)

                # Determine HTTP status code based on error message
                if "Authentication required" in error_msg:
                    raise HTTPException(status_code=401, detail=error_msg)
                elif "Access denied" in error_msg:
                    raise HTTPException(status_code=403, detail=error_msg)
                else:
                    raise HTTPException(status_code=400, detail=error_msg)

            # Extract content from MCP result
            response_data = result
            if isinstance(result, dict):
                # Prefer structuredContent if available (already parsed)
                if "structuredContent" in result:
                    response_data = result["structuredContent"]
                # Fall back to parsing text content
                elif "content" in result:
                    content = result["content"]
                    if isinstance(content, list) and len(content) > 0:
                        first_item = content[0]
                        if isinstance(first_item, dict) and "text" in first_item:
                            text = first_item["text"]
                            # Try to parse as JSON, fall back to raw text
                            try:
                                response_data = json.loads(text)
                            except json.JSONDecodeError:
                                # Not JSON, return as plain text result
                                response_data = {"result": text}
                        else:
                            response_data = first_item
                    else:
                        response_data = content

            # Record successful call
            await record_trajectory(response_data, success=True)
            return response_data

        except HTTPException:
            # HTTPException from MCP error is already recorded above, so just re-raise
            raise
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error for {tool_name}: {e}")
            logger.error(f"Raw result: {result}")
            await record_trajectory(None, success=False, error_message=str(e))
            raise HTTPException(status_code=500, detail=str(e))
        except Exception as e:
            logger.error(f"Error calling MCP tool {tool_name}: {e}")
            await record_trajectory(None, success=False, error_message=str(e))
            raise HTTPException(status_code=500, detail=str(e))

    # Register both GET and POST for tool calls
    @app.get("/tools/{tool_name}")
    async def call_mcp_tool_get(tool_name: str, request: Request):
        return await _call_mcp_tool_impl(tool_name, request)

    @app.post("/tools/{tool_name}")
    async def call_mcp_tool_post(tool_name: str, request: Request):
        return await _call_mcp_tool_impl(tool_name, request)


def add_session_endpoints(app: FastAPI, module_path: str):
    """
    Add generic session management endpoints if the server supports sessions.

    Dynamically imports the server's session model and database helpers.
    Sessions provide user isolation for multi-tenant applications.

    ## Enabling Session Support

    To enable session endpoints for your MCP server, you need to provide:

    1. **SessionModel** - A SQLAlchemy ORM model in `{server}/models/__init__.py`:

        ```python
        # mcp_servers/myserver/models/__init__.py
        from .session import SessionModel

        # mcp_servers/myserver/models/session.py
        from datetime import datetime
        from sqlalchemy import JSON, DateTime, String
        from sqlalchemy.orm import Mapped, mapped_column
        from .base import Base

        class SessionModel(Base):
            __tablename__ = "sessions"

            session_id: Mapped[str] = mapped_column(String(36), primary_key=True)
            created_at: Mapped[datetime] = mapped_column(DateTime, index=True)
            last_accessed_at: Mapped[datetime] = mapped_column(DateTime)
            extra_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)
        ```

    2. **Database helpers** in `{server}/repositories/database.py`:

        ```python
        from contextlib import contextmanager
        from sqlalchemy.orm import Session

        @contextmanager
        def get_session() -> Generator[Session, None, None]:
            '''Get a database session (context manager).'''
            session = SessionFactory()
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise
            finally:
                session.close()

        def ensure_database_initialized() -> None:
            '''Optional: Initialize database if not already done.'''
            ...

        def clear_session_data(session_id: str) -> int:
            '''Optional: Clear all data for a session. Returns records deleted.'''
            ...
        ```

    ## Endpoints Added

    When session support is detected, these endpoints are registered:

    - `POST /sessions` - Create a new session, returns session_id
    - `GET /sessions/{session_id}` - Get session info, updates last_accessed_at
    - `DELETE /sessions/{session_id}` - Delete session and associated data

    Args:
        app: FastAPI application
        module_path: MCP server module path (e.g., 'mcp_servers.openemr.main')
    """
    from datetime import UTC, datetime
    from uuid import uuid4

    # Derive the server module path (e.g., mcp_servers.openemr)
    parts = module_path.split(".")
    server_module_path = ".".join(parts[:-1])

    # Try to import session support from the server
    try:
        models_module = importlib.import_module(f"{server_module_path}.models")
        db_module = importlib.import_module(f"{server_module_path}.repositories.database")

        if not hasattr(models_module, "SessionModel"):
            logger.info("Server does not have SessionModel, skipping session endpoints")
            return

        if not hasattr(db_module, "get_session"):
            logger.info("Server does not have get_session(), skipping session endpoints")
            return

        session_model_class = models_module.SessionModel
        get_db_session = db_module.get_session

        # Check for optional ensure_database_initialized
        ensure_db = None
        if hasattr(db_module, "ensure_database_initialized"):
            ensure_db = db_module.ensure_database_initialized

        # Check for optional clear_session_data (server-specific cleanup)
        clear_session_data = None
        if hasattr(db_module, "clear_session_data"):
            clear_session_data = db_module.clear_session_data

        logger.info("Session support detected, adding session endpoints")

        @app.post("/sessions")
        async def create_session():
            """Create a new session for user isolation."""
            if ensure_db:
                ensure_db()

            session_id = str(uuid4())
            now = datetime.now(UTC)

            with get_db_session() as db_session:
                new_session = session_model_class(
                    session_id=session_id,
                    created_at=now,
                    last_accessed_at=now,
                )
                db_session.add(new_session)
                db_session.commit()

            logger.info(f"Created session: {session_id}")
            return {
                "session_id": session_id,
                "created_at": now.isoformat(),
                "message": "Session created successfully",
            }

        @app.get("/sessions/{session_id}")
        async def get_session_info(session_id: str):
            """Get session information."""
            if ensure_db:
                ensure_db()

            with get_db_session() as db_session:
                session_record = (
                    db_session.query(session_model_class).filter_by(session_id=session_id).first()
                )

                if not session_record:
                    raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

                # Update last_accessed_at
                session_record.last_accessed_at = datetime.now(UTC)
                db_session.commit()

                return {
                    "session_id": session_record.session_id,
                    "created_at": session_record.created_at.isoformat(),
                    "last_accessed_at": session_record.last_accessed_at.isoformat(),
                    "extra_data": session_record.extra_data,
                }

        @app.delete("/sessions/{session_id}")
        async def delete_session(session_id: str):
            """Delete a session and all associated data."""
            if ensure_db:
                ensure_db()

            # First check if the session exists
            with get_db_session() as db_session:
                session_exists = (
                    db_session.query(session_model_class).filter_by(session_id=session_id).first()
                    is not None
                )

            if not session_exists:
                raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

            # Call server-specific cleanup if available
            records_deleted = 0
            if clear_session_data:
                try:
                    records_deleted = clear_session_data(session_id)
                except Exception as e:
                    logger.error(f"Error clearing session data: {e}")
                    raise HTTPException(
                        status_code=500,
                        detail=f"Failed to delete session: {e}",
                    )

            # Always delete the session record (after clearing associated data)
            with get_db_session() as db_session:
                deleted = (
                    db_session.query(session_model_class).filter_by(session_id=session_id).delete()
                )
                db_session.commit()
                records_deleted += deleted

            logger.info(f"Deleted session {session_id}: {records_deleted} records")
            return {
                "session_id": session_id,
                "records_deleted": records_deleted,
                "message": "Session deleted successfully",
            }

        logger.info("Added session endpoints: POST /sessions, GET/DELETE /sessions/{session_id}")

    except ImportError as e:
        logger.info(f"Session support not available: {e}")
    except Exception as e:
        logger.warning(f"Could not add session endpoints: {e}")


def add_trajectory_endpoints(app: FastAPI):
    """
    Add trajectory recording endpoints for capturing tool call sequences.

    Trajectory recording is used to capture "golden trajectories" - sequences of
    tool calls that can be used for training/evaluating RL agents or for debugging.

    ## Endpoints

    - `POST /trajectory/start` - Start a new recording session
    - `POST /trajectory/stop/{session_id}` - Stop a recording session
    - `GET /trajectory/sessions` - List all recording sessions
    - `GET /trajectory/session/{session_id}` - Get a session with all tool calls
    - `DELETE /trajectory/session/{session_id}` - Delete a session
    - `PUT /trajectory/session/{session_id}` - Update session (prune tool calls)

    ## Recording Tool Calls

    To record tool calls to a trajectory session, add the `trajectory_session` query
    parameter to your tool call requests:

        POST /tools/my_tool?trajectory_session=traj_abc123
        {"arg1": "value1"}

    Only requests with this parameter are recorded. This allows opt-in recording
    without affecting normal tool usage.

    Args:
        app: FastAPI application
    """

    @app.post("/trajectory/start")
    async def start_trajectory_session(session_id: str | None = None):
        """Start a new trajectory recording session.

        Args:
            session_id: Optional custom session ID. Auto-generated if not provided.

        Returns:
            Session info including the session_id to use for recording.
        """
        try:
            session = await trajectory_manager.start_session(session_id)
            return {
                "session_id": session.session_id,
                "started_at": session.started_at.isoformat(),
                "is_active": session.is_active,
                "message": "Trajectory recording started. Add ?trajectory_session="
                f"{session.session_id} to tool calls to record them.",
            }
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/trajectory/stop/{session_id}")
    async def stop_trajectory_session(session_id: str):
        """Stop a trajectory recording session.

        Args:
            session_id: Session to stop

        Returns:
            Session summary with total calls recorded.
        """
        try:
            session = await trajectory_manager.stop_session(session_id)
            return {
                "session_id": session.session_id,
                "started_at": session.started_at.isoformat(),
                "stopped_at": session.stopped_at.isoformat() if session.stopped_at else None,
                "is_active": session.is_active,
                "total_calls": len(session.tool_calls),
                "message": "Trajectory recording stopped.",
            }
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.get("/trajectory/sessions")
    async def list_trajectory_sessions():
        """List all trajectory recording sessions.

        Returns:
            List of session summaries (without full tool call history).
        """
        sessions = await trajectory_manager.list_sessions()
        return {
            "sessions": [s.model_dump() for s in sessions],
            "total": len(sessions),
        }

    @app.get("/trajectory/session/{session_id}")
    async def get_trajectory_session(session_id: str):
        """Get a trajectory session with all recorded tool calls.

        Args:
            session_id: Session to retrieve

        Returns:
            Full session data including all tool calls.
        """
        session = await trajectory_manager.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

        return session.model_dump()

    @app.delete("/trajectory/session/{session_id}")
    async def delete_trajectory_session(session_id: str):
        """Delete a trajectory recording session.

        Args:
            session_id: Session to delete

        Returns:
            Confirmation message.
        """
        deleted = await trajectory_manager.delete_session(session_id)
        if not deleted:
            raise HTTPException(status_code=404, detail=f"Session {session_id} not found")

        return {"session_id": session_id, "message": "Session deleted"}

    @app.put("/trajectory/session/{session_id}")
    async def update_trajectory_session(session_id: str, tool_call_ids: list[str]):
        """Update a trajectory session by keeping only specified tool calls.

        Useful for pruning bad or irrelevant calls from a recording before export.

        Args:
            session_id: Session to update
            tool_call_ids: List of request_ids to keep (others are deleted)

        Returns:
            Updated session summary.
        """
        try:
            session = await trajectory_manager.update_session(session_id, tool_call_ids)
            return {
                "session_id": session.session_id,
                "total_calls": len(session.tool_calls),
                "message": f"Session updated. Kept {len(session.tool_calls)} tool calls.",
            }
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    logger.info(
        "Added trajectory endpoints: POST /trajectory/start, POST /trajectory/stop/{id}, "
        "GET /trajectory/sessions, GET/DELETE/PUT /trajectory/session/{id}"
    )


def load_server_hooks(app: FastAPI, module_path: str, engine=None) -> dict:
    """
    Load server-specific REST bridge hooks if available.

    This function looks for a `rest_bridge_hooks` module in the server's package
    and calls its `register_endpoints()` function to add custom REST endpoints.

    To add custom endpoints and static file mounts to your MCP server's REST bridge:

    1. Create a file at `mcp_servers/{your_server}/rest_bridge_hooks.py`

    2. Define the `register_endpoints` function and/or `STATIC_FILE_MOUNTS`:

        ```python
        from fastapi import FastAPI

        # Optional: Configure static file mounts
        # If not defined, falls back to convention: ui/{server_name}/out -> /ui/{server_name}
        STATIC_FILE_MOUNTS = [
            {"url_path": "/ui/myserver", "directory": "ui/myserver/out", "html": True},
            {"url_path": "/static", "directory": "static"},
        ]

        def register_endpoints(app: FastAPI, module_path: str, engine=None):
            '''Register custom REST endpoints for this server.

            Args:
                app: The FastAPI application instance
                module_path: The MCP server module path (e.g., 'mcp_servers.myserver.main')
                engine: Optional SQLAlchemy database engine (None if no database)
            '''
            @app.get("/api/my-custom-endpoint")
            async def my_custom_endpoint():
                return {"message": "Hello from custom endpoint"}

            @app.post("/api/export-data")
            async def export_data():
                # Your custom logic here
                ...
        ```

    3. The hooks module is automatically discovered and loaded when the REST bridge starts.

    Args:
        app: FastAPI application
        module_path: MCP server module path (e.g., 'mcp_servers.openemr.main')
        engine: Optional database engine

    Returns:
        Dictionary with extracted configuration (e.g., {"static_mounts": [...]})
    """
    config = {}

    # Derive the server module path (e.g., mcp_servers.openemr)
    parts = module_path.split(".")
    server_module_path = ".".join(parts[:-1])

    # Look for rest_bridge_hooks module
    hooks_module_path = f"{server_module_path}.rest_bridge_hooks"
    try:
        hooks_module = importlib.import_module(hooks_module_path)
        logger.info(f"Found server hooks module: {hooks_module_path}")

        # Extract static file mount configuration
        if hasattr(hooks_module, "STATIC_FILE_MOUNTS"):
            config["static_mounts"] = hooks_module.STATIC_FILE_MOUNTS
            logger.info(f"Found {len(config['static_mounts'])} static file mount(s) in hooks")

        # Call register_endpoints if defined
        if hasattr(hooks_module, "register_endpoints"):
            hooks_module.register_endpoints(app, module_path, engine)
            logger.info("Server hooks loaded successfully")

    except ImportError:
        # No hooks module found - this is fine, not all servers need custom endpoints
        logger.debug(f"No server hooks found at {hooks_module_path}")
    except Exception as e:
        logger.warning(f"Error loading server hooks from {hooks_module_path}: {e}")

    # Auto-register CSV import/validation endpoints if database has Base
    # No per-server ui_csv_endpoints.py wrapper needed — the bridge discovers
    # Base from the already-imported db module and calls the shared registration.
    if engine:
        try:
            from mcp_scripts.csv_endpoints import register_csv_endpoints

            # Find Base from the db module that setup_database already imported
            base = None
            for db_path in [
                f"{server_module_path}.db.session",
                f"{server_module_path}.db.models",
            ]:
                db_mod = sys.modules.get(db_path)
                if db_mod and hasattr(db_mod, "Base"):
                    base = db_mod.Base
                    break

            if base is not None:
                register_csv_endpoints(app, base, engine)
                logger.info("CSV import/validation endpoints auto-registered")
            else:
                logger.debug("No Base found in db module — skipping CSV endpoints")
        except ImportError:
            logger.debug("mcp_scripts.csv_endpoints not available — skipping CSV endpoints")
        except Exception as e:
            logger.warning(f"Error auto-registering CSV endpoints: {e}")

    return config


def add_ui_static_files(app: FastAPI, module_path: str, static_mounts: list[dict] | None = None):
    """
    Mount UI static files based on server configuration.

    This function handles static file serving for MCP server UIs. It supports two modes:

    1. **Explicit Configuration** (recommended): Define STATIC_FILE_MOUNTS in your
       server's rest_bridge_hooks.py module to specify exactly which directories
       to mount and where.

    2. **Convention-Based Discovery**: If no explicit configuration is provided,
       the bridge looks for ui/{server_name}/out and mounts it at /ui/{server_name}.

    ## Configuring Static File Mounts

    Create a `rest_bridge_hooks.py` file in your MCP server directory:

        # mcp_servers/myserver/rest_bridge_hooks.py

        STATIC_FILE_MOUNTS = [
            {
                "url_path": "/ui/myserver",  # URL path where files are served
                "directory": "ui/myserver/out",  # Directory path (relative to repo root)
                "html": True,  # Enable HTML mode (serves index.html for directories)
                "name": "myserver_ui",  # Optional: name for the mount (auto-generated if omitted)
                "no_cache": True,  # Optional: disable caching for HTML files (default: True)
            },
            # You can mount multiple directories:
            {
                "url_path": "/static/images",
                "directory": "assets/images",
                "html": False,
            },
        ]

    ## Mount Configuration Options

    - **url_path** (required): The URL path prefix where files will be served.
      Example: "/ui/myserver" serves files at http://localhost:8000/ui/myserver/

    - **directory** (required): Path to the directory containing static files.
      Relative to the repository root (current working directory).

    - **html** (optional, default: False): Enable HTML mode for single-page apps.
      When True, requests to directories serve index.html, and 404s fall back to index.html.

    - **name** (optional): Internal name for the mount. Auto-generated from url_path if omitted.

    - **no_cache** (optional, default: True for html=True mounts): Disable browser caching
      for HTML files. Recommended for development to avoid stale Next.js build references.

    ## Cache Control

    By default, HTML files are served with cache-control headers that prevent browser caching.
    This is important for Next.js apps where build IDs change between deploys:

        Cache-Control: no-cache, no-store, must-revalidate
        Pragma: no-cache
        Expires: 0

    Set `no_cache: False` in your mount config to allow browser caching.

    Args:
        app: FastAPI application
        module_path: MCP server module path (e.g., 'mcp_servers.openemr.main')
        static_mounts: Optional list of static mount configurations from hooks module
    """
    from fastapi.responses import Response
    from starlette.staticfiles import StaticFiles

    class NoCacheStaticFiles(StaticFiles):
        """StaticFiles subclass that disables caching for HTML files.

        This prevents browsers from caching stale HTML that references
        old Next.js build IDs after a redeploy.
        """

        async def get_response(self, path: str, scope):
            response = await super().get_response(path, scope)
            # Prevent caching of HTML files to avoid stale build ID references.
            # Check Content-Type header instead of path because html=True mode
            # serves index.html for directory paths where path is "" or "dir/".
            if isinstance(response, Response):
                content_type = response.headers.get("content-type", "")
                if "text/html" in content_type:
                    response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
                    response.headers["Pragma"] = "no-cache"
                    response.headers["Expires"] = "0"
            return response

    if static_mounts:
        # Use explicit configuration from hooks module
        for mount_config in static_mounts:
            url_path = mount_config.get("url_path")
            directory = mount_config.get("directory")
            html_mode = mount_config.get("html", False)
            # Generate a name from the url_path if not provided
            default_name = url_path.replace("/", "_").strip("_") if url_path else "static"
            name = mount_config.get("name", default_name)
            # Default to no_cache=True for html mounts to prevent stale build issues
            no_cache = mount_config.get("no_cache", html_mode)

            if not url_path or not directory:
                logger.warning(
                    f"Invalid static mount config (missing url_path or directory): {mount_config}"
                )
                continue

            # Resolve directory relative to cwd
            dir_path = Path.cwd() / directory

            if not dir_path.exists():
                logger.warning(f"Static directory not found: {dir_path}")
                continue

            try:
                # Use NoCacheStaticFiles for html mounts with no_cache enabled
                if html_mode and no_cache:
                    static_app = NoCacheStaticFiles(directory=str(dir_path), html=True)
                else:
                    static_app = StaticFiles(directory=str(dir_path), html=html_mode)

                app.mount(url_path, static_app, name=name)
                cache_info = " (no-cache)" if (html_mode and no_cache) else ""
                logger.info(f"Mounted static files: {url_path} -> {dir_path}{cache_info}")
            except Exception as e:
                logger.warning(f"Could not mount static files at {url_path}: {e}")
        return

    # Fallback: convention-based discovery
    parts = module_path.split(".")
    if len(parts) >= 2:
        server_name = parts[1]
    else:
        server_name = parts[0]

    ui_dir = Path.cwd() / "ui" / server_name / "out"

    if not ui_dir.exists():
        logger.info(f"No UI directory found at {ui_dir}, skipping static file mounting")
        return

    try:
        # Use NoCacheStaticFiles for convention-based mounts (always html mode)
        app.mount(
            f"/ui/{server_name}",
            NoCacheStaticFiles(directory=str(ui_dir), html=True),
            name=f"ui_{server_name}",
        )
        logger.info(f"Mounted UI static files: /ui/{server_name} -> {ui_dir} (no-cache)")
    except Exception as e:
        logger.warning(f"Could not mount UI static files: {e}")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(description="MCP REST Bridge")
    parser.add_argument(
        "--mcp-server",
        required=True,
        help=(
            "Python module path for MCP server "
            "(e.g., mcp_servers.tableau.main or mcp_servers.example.ui)"
        ),
    )
    parser.add_argument(
        "--port", type=int, default=8000, help="Port to run the REST server on (default: 8000)"
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind to (default: 127.0.0.1)")

    args = parser.parse_args()

    # Setup database first (before creating app)
    engine = setup_database(args.mcp_server)

    # Create the app with MCP stdio tools
    app = create_app(args.mcp_server, engine=engine)

    db_tools_count = 0

    # Add database management tools if database exists
    if engine:
        db_tools_count = add_database_tools(app, engine)
        logger.info(f"Added {db_tools_count} database management tools")

    # Add MCP tool endpoint - routes all tool calls through MCP stdio
    add_mcp_tool_endpoint(app)
    logger.info("Added MCP tool endpoint")

    # Add session endpoints if the server supports sessions
    add_session_endpoints(app, args.mcp_server)

    # Add trajectory recording endpoints (for golden trajectory capture)
    add_trajectory_endpoints(app)

    # Load server-specific hooks (custom endpoints like /api/export-excel)
    # See load_server_hooks() docstring for how to add custom endpoints
    hooks_config = load_server_hooks(app, args.mcp_server, engine)

    # Mount UI static files (uses hooks config if available, else convention-based)
    add_ui_static_files(app, args.mcp_server, hooks_config.get("static_mounts"))

    logger.info(f"Database tools available: {db_tools_count}")
    logger.info(f"Starting REST bridge for {args.mcp_server} on {args.host}:{args.port}")

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()

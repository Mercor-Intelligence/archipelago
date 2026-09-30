"""
Server Detector - Auto-detect MCP server configuration from directory structure.
"""

import sys
from pathlib import Path

from pydantic import ValidationError

# Add parent directory to path for imports (same pattern as CLI)
sys.path.insert(0, str(Path(__file__).parent.parent))

from parser.build_spec_parser import BuildSpec, CategoryConfig, ServerConfig


class ServerDetector:
    """Auto-detect server configuration from directory structure."""

    def detect_from_directory(self, server_path: str | Path) -> BuildSpec:
        """
        Auto-detect server configuration from directory.

        Args:
            server_path: Path to MCP server directory

        Returns:
            BuildSpec with smart defaults
        """
        server_path = Path(server_path).resolve()

        if not server_path.exists():
            raise FileNotFoundError(f"Server directory not found: {server_path}")

        # Infer server name from directory
        server_name = server_path.name

        # Create display name (Title Case from snake_case)
        display_name = self._to_title_case(server_name)

        # Infer tools module path
        tools_module = self._infer_tools_module(server_path)

        # Create default server config
        server_config = ServerConfig(
            name=server_name,
            display_name=display_name,
            description=f"{display_name} API tools",
            tools_module=tools_module,
            base_url="http://localhost:8000",
            requires_auth=False,
            auth_type="none",
            icon="",
        )

        # Create default category
        category = CategoryConfig(
            name=display_name, description=f"{display_name} tools", servers=[server_name]
        )

        # Create build spec
        build_spec = BuildSpec(
            version="1.0", servers=[server_config], categories=[category], tool_overrides=[]
        )

        return build_spec

    def detect_with_yaml_override(self, server_path: str | Path) -> BuildSpec:
        """
        Auto-detect server config and merge with YAML if present.

        Args:
            server_path: Path to MCP server directory

        Returns:
            BuildSpec with YAML overrides applied to defaults
        """
        from parser.build_spec_parser import BuildSpecParser

        server_path = Path(server_path)

        # Start with auto-detected defaults
        build_spec = self.detect_from_directory(server_path)

        # Check for optional YAML file
        yaml_path = server_path / "mcp-build-spec.yaml"

        if yaml_path.exists():
            print("  Found mcp-build-spec.yaml, checking for overrides...")
            parser = BuildSpecParser()

            try:
                yaml_spec = parser.parse(yaml_path)

                # Merge: YAML values override auto-detected defaults
                build_spec = self._merge_specs(build_spec, yaml_spec)
                print("  Merged YAML overrides with defaults")
            except (TypeError, ValueError, ValidationError):
                # YAML is empty or invalid, use defaults
                print("  YAML file is empty or commented out, using smart defaults")
        else:
            print("  No mcp-build-spec.yaml found, using smart defaults")

        return build_spec

    def _infer_tools_module(self, server_path: Path) -> str:
        """
        Infer Python module path for tools.

        Looks for:
        1. mcp_servers/*/tools/*.py files (nested structure)
        2. tools/*.py files (preferred)
        3. *.py files in root
        """
        # First check for nested mcp_servers structure (e.g., mcp_servers/sap/tools/)
        mcp_servers_dir = server_path / "mcp_servers"
        if mcp_servers_dir.exists() and mcp_servers_dir.is_dir():
            # Find subdirectories with tools
            server_name_lower = server_path.name.lower().replace("-", "").replace("_", "")
            candidates = []

            for subdir in mcp_servers_dir.iterdir():
                if subdir.is_dir() and not subdir.name.startswith("_"):
                    tools_dir = subdir / "tools"
                    if tools_dir.exists() and tools_dir.is_dir():
                        tool_files = list(tools_dir.glob("*.py"))
                        tool_files = [f for f in tool_files if f.stem != "__init__"]
                        if tool_files:
                            candidates.append(subdir.name)

            if candidates:
                # Prefer subdirectory matching server name with longest match
                # (e.g., "sapphire" for "mercor-SAPPHIRE")
                best_match = None
                best_match_len = 0

                for candidate in candidates:
                    candidate_lower = candidate.lower()
                    match_len = 0

                    # Check if candidate is substring of server name
                    if candidate_lower in server_name_lower:
                        match_len = len(candidate_lower)
                    # Check if server name is substring of candidate
                    elif server_name_lower in candidate_lower:
                        match_len = len(server_name_lower)

                    # Update if this is a better match (longer, or same length but
                    # lexicographically smaller for determinism)
                    if match_len > 0 and (
                        match_len > best_match_len
                        or (
                            match_len == best_match_len
                            and (best_match is None or candidate < best_match)
                        )
                    ):
                        best_match = candidate
                        best_match_len = match_len

                # If no match found, use first candidate (sorted for determinism)
                if not best_match:
                    best_match = sorted(candidates)[0]

                # Add server_path to sys.path for imports
                if str(server_path) not in sys.path:
                    sys.path.insert(0, str(server_path))
                # Sanitize subdirectory name for use as Python module
                return f"mcp_servers.{self._sanitize_module_name(best_match)}.tools"

        # Get relative path from current directory
        try:
            rel_path = server_path.relative_to(Path.cwd())
            # Convert path to module notation and sanitize for Python identifiers
            module_base = self._sanitize_module_name(rel_path.as_posix().replace("/", "."))
        except ValueError:
            # If not relative to cwd, add to sys.path and use simple name
            if str(server_path) not in sys.path:
                sys.path.insert(0, str(server_path))
            # Sanitize directory name for use as Python module
            module_base = self._sanitize_module_name(server_path.name)

        # Check if tools directory exists
        tools_dir = server_path / "tools"
        if tools_dir.exists() and tools_dir.is_dir():
            # Find .py files in tools directory
            tool_files = list(tools_dir.glob("*.py"))
            tool_files = [f for f in tool_files if f.stem != "__init__"]

            if tool_files:
                # Return tools directory module path (not individual file)
                # Scanner will discover all tools in this directory
                return f"{module_base}.tools"

        # Fallback: look for .py files in server root
        py_files = list(server_path.glob("*.py"))
        py_files = [f for f in py_files if f.stem not in ["__init__", "main"]]

        if py_files:
            return f"{module_base}.{self._sanitize_module_name(py_files[0].stem)}"

        # Last resort: assume tools directory pattern
        return f"{module_base}.tools.{self._sanitize_module_name(server_path.name)}"

    def _sanitize_module_name(self, name: str) -> str:
        """
        Sanitize a directory/file name for use as a Python module name.

        Replaces hyphens with underscores to create valid Python identifiers.

        Args:
            name: Directory or file name (may contain hyphens)

        Returns:
            Sanitized name suitable for Python module paths
        """
        return name.replace("-", "_")

    def _to_title_case(self, snake_case: str) -> str:
        """Convert snake_case to Title Case."""
        words = snake_case.split("_")
        return " ".join(word.capitalize() for word in words)

    def _merge_specs(self, default_spec: BuildSpec, yaml_spec: BuildSpec) -> BuildSpec:
        """
        Merge YAML spec with defaults, preferring YAML values.

        Args:
            default_spec: Auto-detected defaults
            yaml_spec: Values from YAML file

        Returns:
            Merged BuildSpec
        """
        # Use YAML values, falling back to defaults
        return BuildSpec(
            version=yaml_spec.version,
            servers=yaml_spec.servers if yaml_spec.servers else default_spec.servers,
            categories=(yaml_spec.categories if yaml_spec.categories else default_spec.categories),
            tool_overrides=(
                yaml_spec.tool_overrides
                if yaml_spec.tool_overrides
                else default_spec.tool_overrides
            ),
        )

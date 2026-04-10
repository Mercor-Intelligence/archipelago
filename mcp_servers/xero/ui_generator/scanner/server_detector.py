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
        server_path = Path(server_path)

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
        1. tools/*.py files (preferred)
        2. *.py files in root
        """
        # Get relative path from current directory
        try:
            rel_path = server_path.relative_to(Path.cwd())
            module_base = rel_path.as_posix().replace("/", ".")
        except ValueError:
            # If not relative to cwd, use absolute path logic
            module_base = ".".join(server_path.parts[-3:])  # Last 3 parts

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
            return f"{module_base}.{py_files[0].stem}"

        # Last resort: assume tools directory pattern
        return f"{module_base}.tools.{server_path.name}"

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

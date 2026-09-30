"""
CLI - Command-line interface for MCP UI Generator.
"""

import asyncio
import sys
import traceback
from pathlib import Path

import click

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
# Add current working directory to path for module imports
sys.path.insert(0, str(Path.cwd()))

from codegen.generator import CodeGenerator
from converter.schema_converter import SchemaConverter
from parser.build_spec_parser import BuildSpecParser
from scanner.mcp_runtime_scanner import MCPRuntimeScanner
from scanner.server_detector import ServerDetector


def get_module_prefix_from_path(server_dir: Path) -> str:
    """Derive module prefix from server directory path.

    Handles both standard and src-prefixed paths:
    - mcp_servers/example -> mcp_servers.example
    - src/mcp_servers/example -> src.mcp_servers.example

    Args:
        server_dir: Path to the server directory

    Returns:
        Module prefix (e.g., "mcp_servers.example_server" or "src.mcp_servers.example_server")
    """
    parts = server_dir.parts

    # Check for src/mcp_servers/<name>/ pattern
    if len(parts) >= 3 and parts[-3] == "src" and parts[-2] == "mcp_servers":
        return f"src.mcp_servers.{parts[-1]}"

    # Standard mcp_servers/<name>/ pattern
    if len(parts) >= 2:
        return f"{parts[-2]}.{parts[-1]}"

    # Fallback to just the directory name
    return parts[-1] if parts else "unknown"


def get_entrypoint_module(server_dir: Path, module_prefix: str) -> str:
    """Get the entrypoint module for a server (ui.py or main.py).

    Prefers ui.py if both exist.

    Args:
        server_dir: Path to the server directory
        module_prefix: The module prefix (e.g., "mcp_servers.example_server")

    Returns:
        Full module path (e.g., "mcp_servers.example_server.ui")
    """
    # Prefer ui.py over main.py
    for entrypoint in ["ui", "main"]:
        if (server_dir / f"{entrypoint}.py").exists():
            return f"{module_prefix}.{entrypoint}"
    # Fallback to main if neither exists (will error later)
    return f"{module_prefix}.main"


@click.group()
def cli():
    """MCP UI Generator - Auto-generate web UIs for MCP servers."""
    pass


@cli.command()
@click.option(
    "--build-spec",
    type=click.Path(exists=True),
    help="Path to mcp-build-spec.yaml (optional if --server provided)",
)
@click.option(
    "--server",
    type=click.Path(exists=True),
    help="Path to MCP server directory (auto-detects configuration)",
)
@click.option(
    "--output",
    required=False,
    type=click.Path(),
    help="Output directory for generated UI (default: ui/{server_name}/ for --server mode)",
)
@click.option(
    "--servers",
    help="Comma-separated list of servers to include (default: all)",
)
@click.option(
    "--reference-ui",
    type=click.Path(exists=True),
    help="Path to reference UI directory (default: auto-detect)",
)
@click.option(
    "--project-name",
    default="mcp-ui",
    help="Project name for generated UI",
)
@click.option(
    "--base-url",
    default="http://localhost:8000",
    help="Base URL for the MCP server API (default: http://localhost:8000)",
)
def generate(
    build_spec: str | None,
    server: str | None,
    output: str | None,
    servers: str | None,
    reference_ui: str | None,
    project_name: str,
    base_url: str,
):
    """Generate a complete UI from MCP servers (zero-config or explicit YAML)."""
    asyncio.run(
        _generate_async(
            build_spec=build_spec,
            server=server,
            output=output,
            servers=servers,
            reference_ui=reference_ui,
            project_name=project_name,
            base_url=base_url,
        )
    )


async def _generate_async(
    build_spec: str | None,
    server: str | None,
    output: str | None,
    servers: str | None,
    reference_ui: str | None,
    project_name: str,
    base_url: str,
):
    """Async implementation of generate command."""
    import os

    # Set MCP_UI_GEN to disable Gemini compatibility during schema scanning
    # This preserves full schema structure with individual fields for UI forms
    os.environ["MCP_UI_GEN"] = "true"

    # Validate input
    if not build_spec and not server:
        click.echo("Error: Either --build-spec or --server must be provided", err=True)
        click.echo("\nExamples:")
        click.echo("  # Zero-config mode (auto-detect from server directory)")
        click.echo("  mcp-ui-gen generate --server mcp_servers/your_server")
        click.echo("  # With custom output:")
        click.echo("  mcp-ui-gen generate --server mcp_servers/your_server --output custom-ui/")
        click.echo("")
        click.echo("  # Explicit YAML mode")
        click.echo("  mcp-ui-gen generate --build-spec path/to/mcp-build-spec.yaml --output ui/")
        sys.exit(1)

    # Keep track of original server path for later use
    original_server_path = server

    if build_spec and server:
        click.echo(
            "Warning: Both --build-spec and --server provided. Using --build-spec.", err=True
        )
        server = None

    # Smart default for output directory
    if output is None:
        if server:
            # Default to ui/{server_name}/ for zero-config mode
            server_path = Path(server)
            server_name = server_path.name
            repo_root = Path.cwd()
            output = str(repo_root / "ui" / server_name)
            click.echo(f"No --output specified, using default: {output}")
        else:
            # For build-spec mode, require explicit output
            click.echo("Error: --output is required when using --build-spec", err=True)
            sys.exit(1)

    if server:
        click.echo(f"Generating UI from server: {server}")
        click.echo("(Zero-config mode with smart defaults)\n")
    else:
        click.echo(f"Generating UI from build spec: {build_spec}\n")

    try:
        # 1. Parse or detect build spec
        if server:
            click.echo("Auto-detecting server configuration...")
            detector = ServerDetector()
            spec = detector.detect_with_yaml_override(server)

            # Override base_url if provided via CLI
            if base_url != "http://localhost:8000":
                for srv in spec.servers:
                    srv.base_url = base_url
        else:
            click.echo("Parsing build spec...")
            spec_parser = BuildSpecParser()
            spec = spec_parser.parse(build_spec)

        # Filter servers if specified
        if servers:
            server_names = [s.strip() for s in servers.split(",")]
            spec.servers = [s for s in spec.servers if s.name in server_names]
            click.echo(f"  Filtered to servers: {', '.join(server_names)}")

        click.echo(f"  Found {len(spec.servers)} server(s)")

        # Use project_name from spec if available (CLI arg is the default)
        if spec.project_name and project_name == "mcp-ui":
            project_name = spec.project_name
            click.echo(f"  Using project name from spec: {project_name}")
        # If still default, derive from first server name or output directory
        elif project_name == "mcp-ui":
            if spec.servers:
                project_name = spec.servers[0].name
                click.echo(f"  Using project name from server: {project_name}")

        # 2. Scan for tools using runtime discovery
        click.echo(f"\nScanning {len(spec.servers)} server(s) for tools...")

        all_tools = []
        runtime_models = []  # Models discovered from tool schemas ($defs)
        runtime_scanner = MCPRuntimeScanner()

        for srv in spec.servers:
            click.echo(f"\n  Scanning {srv.display_name} ({srv.name})...")

            # Warn if tools_module is specified (deprecated, now auto-detected)
            if srv.tools_module and srv.tools_module != "tools":
                click.echo(
                    "    Warning: tools_module is deprecated and ignored. "
                    "Entrypoint is auto-detected from ui.py or main.py."
                )

            try:
                # Determine server directory from --server path or derive from server name
                if original_server_path:
                    server_dir = Path(original_server_path)
                else:
                    # Fall back to conventional location
                    server_dir = Path("mcp_servers") / srv.name

                # Derive module prefix using consistent logic that handles src/ paths
                module_prefix = get_module_prefix_from_path(server_dir)

                # Auto-detect entrypoint (prefers ui.py over main.py)
                main_module = get_entrypoint_module(server_dir, module_prefix)

                # Import server and discover tools and referenced models from FastMCP
                tools, models = await runtime_scanner.scan_server(main_module, server_dir)
                click.echo(f"    Found {len(tools)} tool(s)")
                if models:
                    click.echo(f"    Found {len(models)} referenced model(s)")
                    runtime_models.extend(models)

                # Add server name to each tool
                for tool in tools:
                    tool["server"] = srv.name
                    all_tools.append(tool)

            except Exception as e:
                click.echo(f"    ERROR: Tool discovery failed for {srv.name}", err=True)
                click.echo(f"    {type(e).__name__}: {e}", err=True)
                # Show the full traceback for debugging
                click.echo("    Traceback:", err=True)
                for line in traceback.format_exc().strip().split("\n"):
                    click.echo(f"      {line}", err=True)
                # Provide hints for common errors
                error_str = str(e).lower()
                if "edgar_user_agent" in error_str or "environment variable" in error_str:
                    click.echo(
                        "    HINT: Ensure .env file exists and is loaded by config.py", err=True
                    )
                    click.echo(
                        "          Add 'from dotenv import load_dotenv; load_dotenv()' "
                        "to config.py",
                        err=True,
                    )
                elif "no module named" in error_str:
                    click.echo(
                        "    HINT: Check that all dependencies are installed (uv sync)", err=True
                    )
                elif "permission" in error_str:
                    click.echo("    HINT: Check file permissions on the server directory", err=True)

        if all_tools:
            click.echo(f"\n  Total: {len(all_tools)} tool(s) found")
        else:
            click.echo("\n" + "=" * 60, err=True)
            click.echo("  ERROR: No tools discovered!", err=True)
            click.echo("=" * 60, err=True)
            click.echo("  The generated UI will have an empty dataTypes array.", err=True)
            click.echo("  Common causes:", err=True)
            click.echo("    - Server import failed (check errors above)", err=True)
            click.echo("    - Missing .env file or environment variables", err=True)
            click.echo("    - No FastMCP instance found in main.py/ui.py", err=True)
            click.echo("    - No tools registered with @mcp.tool decorator", err=True)
            click.echo("=" * 60, err=True)

        # 3. Convert to TypeScript config
        click.echo("\nConverting to TypeScript format...")
        converter = SchemaConverter()
        ts_configs = []

        build_spec_dict = spec.model_dump(exclude_none=True)

        for tool in all_tools:
            try:
                parsed_schema = tool.get("parsed_schema", {})
                ts_config = converter.convert_to_typescript(
                    tool,
                    parsed_schema,
                    build_spec_dict,
                    tool["server"],
                )
                if ts_config is not None:
                    ts_configs.append(ts_config)
            except Exception as e:
                click.echo(f"  Warning: Failed to convert {tool['name']}: {e}")

        click.echo(f"  Converted {len(ts_configs)} tool(s)")

        # Sort tools by ID for deterministic output
        ts_configs.sort(key=lambda t: t.get("id", ""))

        # 4. Use models discovered from tool schemas ($defs)
        # Models are extracted from JSON Schema during runtime scanning
        all_models = runtime_models
        if all_models:
            click.echo(f"\nFound {len(all_models)} model(s) from tool schemas")
        else:
            click.echo("\nNo models found in tool schemas")

        # Sort models by name for deterministic output
        all_models.sort(key=lambda m: m.get("name", ""))

        # 4b. Scan for users.json to get personas for login UI
        click.echo("\nScanning for personas (users.json)...")
        import json

        all_personas = []

        # Build list of (server_dir, label) to scan
        persona_scan_dirs: list[tuple[Path, str]] = []
        if original_server_path:
            server_path = Path(original_server_path)
            persona_scan_dirs.append((server_path, server_path.name))
        else:
            for srv in spec.servers:
                module_parts = srv.tools_module.split(".")
                if len(module_parts) >= 2:
                    server_dir = Path(module_parts[0]) / module_parts[1]
                    persona_scan_dirs.append((server_dir, srv.name))

        # Scan each server directory for users.json
        for server_dir, label in persona_scan_dirs:
            users_file = server_dir / "users.json"
            if users_file.exists():
                try:
                    with open(users_file) as f:
                        users_data = json.load(f)
                    count = 0
                    for username, user_info in users_data.items():
                        all_personas.append(
                            {
                                "username": username,
                                "password": user_info.get("password", ""),
                                "description": user_info.get("description", ""),
                                "roles": user_info.get("roles", []),
                            }
                        )
                        count += 1
                    click.echo(f"  Found {count} persona(s) in {label}/users.json")
                except Exception as e:
                    click.echo(f"  Warning: Failed to parse {label}/users.json: {e}")

        if not all_personas and persona_scan_dirs:
            click.echo("  No users.json found")

        # 5. Find reference UI
        if reference_ui is None:
            # Try to auto-detect
            current_dir = Path.cwd()
            possible_paths = [
                current_dir / "user-api-tool-bench-reference",
                current_dir.parent / "user-api-tool-bench-reference",
                Path(__file__).parent.parent.parent / "user-api-tool-bench-reference",
            ]

            for path in possible_paths:
                if path.exists():
                    reference_ui = str(path)
                    break

            if reference_ui is None:
                click.echo("\nWarning: Could not auto-detect reference UI directory")
                click.echo("  Static files will not be copied. Use --reference-ui to specify.")

        # 6. Generate code
        click.echo("\nGenerating code...")
        output_path = Path(output)
        reference_ui_path = Path(reference_ui) if reference_ui else None

        generator = CodeGenerator()
        generator.generate_complete_ui(
            output_dir=output_path,
            reference_ui_dir=reference_ui_path,
            tools=ts_configs,
            build_spec=spec,
            project_name=project_name,
            models=all_models if all_models else None,
            personas=all_personas if all_personas else None,
            server_dir=Path(original_server_path) if original_server_path else None,
        )

        # Success message
        click.echo(f"\n{'=' * 60}")
        click.echo("UI generated successfully!")
        click.echo(f"{'=' * 60}")

        if original_server_path:
            click.echo("\nZero-config mode used. To customize:")
            click.echo(f"  1. Create {original_server_path}/mcp-build-spec.yaml")
            click.echo("  2. Add display names, categories, or auth settings")
            click.echo("  3. Re-run the generate command")

        click.echo("\nNext steps:")
        click.echo(f"  cd {output}")
        click.echo("  npm install")
        click.echo("  cp .env.local.example .env.local")

        click.echo("  # Edit .env.local and set ACCESS_CODE=your-password")
        click.echo("  npm run dev")

        if original_server_path:
            click.echo("\nTo use the UI, start the REST bridge in another terminal:")
            # Use same module prefix derivation as scanning for consistency
            server_dir = Path(original_server_path)
            server_module = get_module_prefix_from_path(server_dir)
            # Determine entrypoint (ui.py or main.py)
            entrypoint = get_entrypoint_module(server_dir, server_module)
            click.echo(f"  python scripts/mcp_rest_bridge.py --mcp-server {entrypoint} --port 8000")

        click.echo("\nNote: If you encounter file permission errors when running npm install:")
        click.echo(f"  sudo chown -R $(whoami):$(whoami) {output}")
        click.echo("\n")

    except Exception as e:
        click.echo(f"\nError: Error: {e}", err=True)
        traceback.print_exc()
        sys.exit(1)


@cli.command()
@click.option(
    "--build-spec",
    required=True,
    type=click.Path(exists=True),
    help="Path to mcp-build-spec.yaml",
)
def validate(build_spec: str):
    """Validate build spec and check for errors."""
    click.echo(f"Validating {build_spec}...")

    try:
        spec_parser = BuildSpecParser()
        spec = spec_parser.parse(build_spec)

        click.echo("\nBuild spec is valid!")
        click.echo(f"\n{'=' * 60}")
        click.echo("Summary:")
        click.echo(f"{'=' * 60}")
        click.echo(f"\nVersion: {spec.version}")
        click.echo(f"\nServers: {len(spec.servers)}")
        for server in spec.servers:
            click.echo(f"  • {server.display_name} ({server.name})")
            click.echo(f"    Module: {server.tools_module}")
            click.echo(f"    URL: {server.base_url}")

        click.echo(f"\nCategories: {len(spec.categories)}")
        for category in spec.categories:
            click.echo(f"  • {category.name}: {category.description}")

        if spec.tool_overrides:
            click.echo(f"\nTool Overrides: {len(spec.tool_overrides)}")
            for override in spec.tool_overrides:
                click.echo(f"  • {override.tool}")

    except Exception as e:
        click.echo(f"Error: Validation failed: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.option(
    "--build-spec",
    required=True,
    type=click.Path(exists=True),
    help="Path to mcp-build-spec.yaml",
)
@click.option(
    "--server",
    help="Show tools for specific server only",
)
def inspect(build_spec: str, server: str | None):
    """Show detailed information about tools."""
    asyncio.run(_inspect_async(build_spec, server))


async def _inspect_async(build_spec: str, server: str | None):
    """Async implementation of inspect command."""
    try:
        spec_parser = BuildSpecParser()
        spec = spec_parser.parse(build_spec)

        scanner = MCPRuntimeScanner()

        servers_to_inspect = spec.servers
        if server:
            servers_to_inspect = [s for s in spec.servers if s.name == server]
            if not servers_to_inspect:
                click.echo(f"Error: Server '{server}' not found in build spec", err=True)
                return

        for srv in servers_to_inspect:
            click.echo(f"\n{'=' * 60}")
            click.echo(f"{srv.display_name} ({srv.name})")
            click.echo(f"{'=' * 60}")

            try:
                # Use conventional location for server directory
                server_dir = Path("mcp_servers") / srv.name
                module_prefix = f"mcp_servers.{srv.name}"

                # Auto-detect entrypoint (prefers ui.py over main.py)
                main_module = get_entrypoint_module(server_dir, module_prefix)

                tools, _ = await scanner.scan_server(main_module, server_dir)

                click.echo(f"\nFound {len(tools)} tool(s):\n")

                for tool in tools:
                    click.echo(f"  Tool: {tool['name']}")
                    desc = tool.get("description", "")
                    if desc:
                        click.echo(f"  Description: {desc[:80]}...")

                    # Show fields from parsed schema
                    parsed_schema = tool.get("parsed_schema", {})
                    fields = parsed_schema.get("fields", [])
                    if fields:
                        click.echo(f"  Fields: {len(fields)}")
                        for field in fields:
                            req = "required" if field.get("required") else "optional"
                            click.echo(f"    - {field['name']} ({field.get('type', 'any')}, {req})")

                    click.echo()

            except Exception as e:
                click.echo(f"  Error: Failed to scan server: {e}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    cli()

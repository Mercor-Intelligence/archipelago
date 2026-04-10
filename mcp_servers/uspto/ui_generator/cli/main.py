"""
CLI - Command-line interface for MCP UI Generator.
"""

import sys
from pathlib import Path

import click

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))
# Add current working directory to path for module imports
sys.path.insert(0, str(Path.cwd()))

from codegen.generator import CodeGenerator
from converter.schema_converter import SchemaConverter
from parser.build_spec_parser import BuildSpecParser
from parser.pydantic_parser import PydanticParser
from scanner.mcp_scanner import MCPToolScanner
from scanner.server_detector import ServerDetector


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
        # If still default, derive from first server name
        elif project_name == "mcp-ui":
            if spec.servers:
                project_name = spec.servers[0].name
                click.echo(f"  Using project name from server: {project_name}")

        # 2. Scan for tools
        click.echo(f"\nScanning {len(spec.servers)} server(s) for tools...")
        scanner = MCPToolScanner()
        parser = PydanticParser()
        all_tools = []

        for server in spec.servers:
            click.echo(f"\n  Scanning {server.display_name} ({server.name})...")

            try:
                # Try to scan the module
                tools = scanner.scan_module(server.tools_module)
                click.echo(f"    Found {len(tools)} tool(s)")

                # Parse each tool's Pydantic model
                for tool in tools:
                    if "input_model" in tool and tool["input_model"]:
                        try:
                            schema = parser.parse_model(tool["input_model"])
                            tool["parsed_schema"] = schema
                            tool["server"] = server.name
                            all_tools.append(tool)
                        except Exception as e:
                            click.echo(f"    Warning: Failed to parse {tool['name']}: {e}")

            except ImportError as e:
                click.echo(f"    Warning: Failed to import module {server.tools_module}: {e}")
                click.echo("    Trying directory scan instead...")

                # Fallback to directory scanning
                # Try to find the module directory
                module_path = Path(server.tools_module.replace(".", "/"))
                if module_path.exists():
                    tools = scanner.scan_directory(module_path)
                    click.echo(f"    Found {len(tools)} tool(s)")
                    # Set server name for each tool from directory scan
                    for tool in tools:
                        tool["server"] = server.name
                    all_tools.extend(tools)
                else:
                    click.echo("    Could not find module or directory")

        if all_tools:
            click.echo(f"\n  Total: {len(all_tools)} tool(s) found")
        else:
            click.echo("\n  Warning: No tools found. Will continue with database scanning only.")

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
                # Skip hidden tools (convert_to_typescript returns None for hidden tools)
                if ts_config is not None:
                    ts_configs.append(ts_config)
            except Exception as e:
                click.echo(f"  Warning: Failed to convert {tool['name']}: {e}")

        click.echo(f"  Converted {len(ts_configs)} tool(s)")

        # Sort tools for deterministic output
        ts_configs.sort(key=lambda t: t.get("id", ""))

        # 4. Scan for database models and generate sample data
        click.echo("\nScanning for database models...")
        from codegen.sample_data_generator import SampleDataGenerator
        from scanner.db_scanner import DatabaseScanner

        db_scanner = DatabaseScanner()
        sample_generator = SampleDataGenerator(num_rows=3)
        all_sample_data = []

        # Scan each server directory for database models
        if original_server_path:
            # Zero-config mode: scan the server directory
            server_path = Path(original_server_path)
            tables = db_scanner.scan_server_directory(server_path)
            if tables:
                click.echo(f"  Found {len(tables)} table(s) in {server_path.name}")
                sample_data = sample_generator.generate_sample_data(tables, server_path.name)
                all_sample_data.extend(sample_data)
        else:
            # Build spec mode: scan each server's base directory
            for srv in spec.servers:
                # Attempt to find server directory from tools_module
                # e.g., "mcp_servers.powerbi.tools" -> "mcp_servers/powerbi"
                module_parts = srv.tools_module.split(".")
                if len(module_parts) >= 2:
                    server_dir = Path(module_parts[0]) / module_parts[1]
                    if server_dir.exists():
                        tables = db_scanner.scan_server_directory(server_dir)
                        if tables:
                            click.echo(f"  Found {len(tables)} table(s) in {srv.name}")
                            sample_data = sample_generator.generate_sample_data(tables, srv.name)
                            all_sample_data.extend(sample_data)

        if all_sample_data:
            click.echo(f"  Generated sample data for {len(all_sample_data)} table(s)")
        else:
            click.echo("  No database models found")

        # Sort sample data for deterministic output
        all_sample_data.sort(key=lambda t: t.get("table_name", ""))

        # Convert sample data to TypeScript format
        ts_sample_data = converter.convert_sample_data_to_typescript(all_sample_data)

        # 4b. Scan for models.py and generate documentation
        click.echo("\nScanning for Pydantic models documentation...")
        from scanner.model_scanner import ModelScanner

        model_scanner = ModelScanner()
        all_models = []

        # Scan each server directory for models.py
        if original_server_path:
            # Zero-config mode: scan the server directory
            server_path = Path(original_server_path)
            models_file = server_path / "models.py"
            if models_file.exists():
                try:
                    models = model_scanner.scan_models_file(models_file)
                    if models:
                        click.echo(
                            f"  Found {len(models)} model(s) in {server_path.name}/models.py"
                        )
                        all_models.extend(model_scanner.export_to_dict(models))
                except Exception as e:
                    click.echo(f"  Warning: Failed to scan models.py: {e}")
            # Also check schemas directory for Xero-like structures
            schemas_dir = server_path / "schemas"
            if schemas_dir.exists():
                click.echo(f"  Found schemas directory in {server_path.name}")
                for schema_file in schemas_dir.glob("*.py"):
                    if schema_file.name.startswith("_"):
                        continue
                    try:
                        models = model_scanner.scan_models_file(schema_file)
                        if models:
                            click.echo(
                                f"    Found {len(models)} model(s) in schemas/{schema_file.name}"
                            )
                            all_models.extend(model_scanner.export_to_dict(models))
                    except Exception as e:
                        click.echo(f"    Warning: Failed to scan {schema_file.name}: {e}")
        else:
            # Build spec mode: scan each server's directory
            for srv in spec.servers:
                module_parts = srv.tools_module.split(".")
                if len(module_parts) >= 2:
                    server_dir = Path(module_parts[0]) / module_parts[1]
                    if server_dir.exists():
                        models_file = server_dir / "models.py"
                        if models_file.exists():
                            try:
                                models = model_scanner.scan_models_file(models_file)
                                if models:
                                    click.echo(
                                        f"  Found {len(models)} model(s) in {srv.name}/models.py"
                                    )
                                    all_models.extend(model_scanner.export_to_dict(models))
                            except Exception as e:
                                click.echo(f"  Warning: Failed to scan {srv.name}/models.py: {e}")
                        # Also check schemas directory
                        schemas_dir = server_dir / "schemas"
                        if schemas_dir.exists():
                            click.echo(f"  Found schemas directory in {srv.name}")
                            for schema_file in schemas_dir.glob("*.py"):
                                if schema_file.name.startswith("_"):
                                    continue
                                try:
                                    models = model_scanner.scan_models_file(schema_file)
                                    if models:
                                        click.echo(
                                            f"    Found {len(models)} model(s) in "
                                            f"schemas/{schema_file.name}"
                                        )
                                        all_models.extend(model_scanner.export_to_dict(models))
                                except Exception as e:
                                    click.echo(
                                        f"    Warning: Failed to scan {schema_file.name}: {e}"
                                    )

        if all_models:
            click.echo(f"  Total: {len(all_models)} model(s) found for documentation")
        else:
            click.echo("  No models found")

        # Sort models for deterministic output
        all_models.sort(key=lambda m: m.get("name", ""))

        # 4c. Scan for users.json to get personas for login UI
        click.echo("\nScanning for personas (users.json)...")
        all_personas = []
        persona_scan_dirs: list[tuple[Path, str]] = []

        # Build list of directories to scan
        if original_server_path:
            # Zero-config mode: scan the server directory
            server_path = Path(original_server_path)
            persona_scan_dirs.append((server_path, server_path.name))
        else:
            # Build spec mode: scan each server's directory
            for srv in spec.servers:
                module_parts = srv.tools_module.split(".")
                if len(module_parts) >= 2:
                    server_dir = Path(module_parts[0]) / module_parts[1]
                    if server_dir.exists():
                        persona_scan_dirs.append((server_dir, srv.name))

        # Scan each directory for users.json
        for server_dir, label in persona_scan_dirs:
            users_json_path = server_dir / "users.json"
            if users_json_path.exists():
                try:
                    import json

                    with open(users_json_path) as f:
                        users_data = json.load(f)

                    # Extract users as personas
                    if isinstance(users_data, list):
                        personas = users_data
                    elif isinstance(users_data, dict):
                        if "users" in users_data:
                            # Format: {"users": [...]}
                            personas = users_data["users"]
                        else:
                            # Format: {username: {password: ..., ...}, ...}
                            # Convert to list with username extracted from key
                            personas = []
                            for username, user_info in users_data.items():
                                if isinstance(user_info, dict):
                                    persona = {"username": username, **user_info}
                                    personas.append(persona)
                    else:
                        personas = []

                    # Add to all_personas
                    for persona in personas:
                        if isinstance(persona, dict):
                            all_personas.append(
                                {
                                    "username": persona.get("username", ""),
                                    "password": persona.get("password", ""),
                                    "label": persona.get("label", persona.get("username", "")),
                                    "server": label,
                                }
                            )

                    count = len(personas)
                    if count > 0:
                        click.echo(f"  Found {count} persona(s) in {label}/users.json")
                except Exception as e:
                    click.echo(f"  Warning: Failed to parse {label}/users.json: {e}")

        if not all_personas and persona_scan_dirs:
            click.echo("  No personas found (no users.json files)")
        elif all_personas:
            click.echo(f"  Total: {len(all_personas)} persona(s) available for login UI")

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
        output_path = Path(output)
        reference_ui_path = Path(reference_ui) if reference_ui else None
        wiki_path = Path.cwd() / "wiki"

        generator = CodeGenerator()
        generator.generate_complete_ui(
            output_dir=output_path,
            reference_ui_dir=reference_ui_path,
            tools=ts_configs,
            build_spec=spec,
            project_name=project_name,
            sample_data_tables=ts_sample_data,
            models=all_models if all_models else None,
            personas=all_personas if all_personas else None,
            wiki_dir=wiki_path if wiki_path.exists() else None,
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
            # Extract server module path (remove .tools.tool_name from tools_module)
            tools_module = spec.servers[0].tools_module
            # Get base module path (e.g., mcp_servers.your_server)
            server_module = ".".join(tools_module.split(".")[:2])
            click.echo(
                f"  python scripts/mcp_rest_bridge.py --mcp-server {server_module}.main --port 8000"
            )
        click.echo("\n")

    except Exception as e:
        click.echo(f"\nError: {e}", err=True)
        import traceback

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
    try:
        spec_parser = BuildSpecParser()
        spec = spec_parser.parse(build_spec)

        scanner = MCPToolScanner()
        parser = PydanticParser()

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
                tools = scanner.scan_module(srv.tools_module)

                click.echo(f"\nFound {len(tools)} tool(s):\n")

                for tool in tools:
                    click.echo(f"  Tool: {tool['name']}")
                    click.echo(f"  Description: {tool['description'][:80]}...")

                    if "input_model" in tool and tool["input_model"]:
                        click.echo(f"  Input: {tool['input_model'].__name__}")

                        # Parse and show fields
                        try:
                            schema = parser.parse_model(tool["input_model"])
                            click.echo(f"  Fields: {len(schema['fields'])}")
                            for field in schema["fields"]:
                                req = "required" if field["required"] else "optional"
                                click.echo(f"    - {field['name']} ({field['type']}, {req})")
                        except Exception as e:
                            click.echo(f"    Warning: Could not parse schema: {e}")

                    if "output_model" in tool and tool["output_model"]:
                        click.echo(f"  Output: {tool['output_model'].__name__}")

                    click.echo()

            except ImportError as e:
                click.echo(f"  Error: Failed to import module: {e}")

    except Exception as e:
        click.echo(f"Error: {e}", err=True)
        import traceback

        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    cli()

"""
Code Generator - Generate TypeScript/JavaScript files from templates.
"""

import json
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape


class CodeGenerator:
    """Generate UI code from templates."""

    def __init__(self, template_dir: str | Path | None = None):
        """
        Initialize code generator.

        Args:
            template_dir: Path to templates directory. If None, uses default.
        """
        if template_dir is None:
            # Use default templates directory
            current_file = Path(__file__)
            template_dir = current_file.parent.parent / "templates"

        self.template_dir = Path(template_dir)

        # Set up Jinja2 environment
        self.env = Environment(
            loader=FileSystemLoader(str(self.template_dir)),
            autoescape=select_autoescape(["html", "xml"]),
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,  # Preserve trailing newlines from templates
        )

    def generate_api_config(
        self,
        tools: list[dict[str, Any]],
        servers: list[dict[str, Any]],
        sample_data_tables: list[dict[str, Any]] | None = None,
        models: list[dict[str, Any]] | None = None,
    ) -> str:
        """
        Generate api-config.ts content.

        Args:
            tools: List of tool configurations
            servers: List of server configurations
            sample_data_tables: Optional list of sample data tables for databases
            models: Optional list of Pydantic models for documentation

        Returns:
            Generated TypeScript code
        """
        template = self.env.get_template("user-api-tool-bench/lib/api-config.ts.j2")

        return template.render(
            tools=tools,
            servers=servers,
            sample_data_tables=sample_data_tables or [],
            models=models or [],
            generation_time=datetime.now().isoformat(),
        )

    def generate_api_handler(
        self,
        servers: list[dict[str, Any]],
    ) -> str:
        """
        Generate pages/api/call.ts content.

        Args:
            servers: List of server configurations

        Returns:
            Generated TypeScript code
        """
        template = self.env.get_template("user-api-tool-bench/pages/api/call.ts.j2")

        return template.render(
            servers=servers,
        )

    def generate_package_json(
        self,
        project_name: str,
    ) -> str:
        """Generate package.json content."""
        template = self.env.get_template("user-api-tool-bench/config/package.json.j2")

        return template.render(
            project_name=project_name,
        )

    def generate_env_example(
        self,
        servers: list[dict[str, Any]],
    ) -> str:
        """Generate .env.local.example content."""
        template = self.env.get_template("user-api-tool-bench/config/env-example.j2")

        return template.render(
            servers=servers,
        )

    def write_file(self, filepath: Path, content: str):
        """Write content to file, creating parent directories if needed."""
        filepath.parent.mkdir(parents=True, exist_ok=True)
        filepath.write_text(content, encoding="utf-8")

    def generate_api_tool_component(self, personas: list[dict[str, Any]] | None = None) -> str:
        """Generate the main ApiTool.tsx component.

        Args:
            personas: Optional list of persona configurations from users.json
        """
        template = self.env.get_template("user-api-tool-bench/components/ApiTool.tsx.j2")
        return template.render(personas=personas or [])

    def generate_index_page(self, project_name: str) -> str:
        """Generate the index.tsx page."""
        template = self.env.get_template("user-api-tool-bench/pages/index.tsx.j2")
        return template.render(project_name=project_name)

    def generate_tsconfig(self) -> str:
        """Generate tsconfig.json."""
        template = self.env.get_template("user-api-tool-bench/config/tsconfig.json.j2")
        return template.render()

    def generate_next_config(self, project_name: str) -> str:
        """Generate next.config.js."""
        template = self.env.get_template("user-api-tool-bench/config/next.config.js.j2")
        return template.render(project_name=project_name)

    def generate_tailwind_config(self) -> str:
        """Generate tailwind.config.js."""
        template = self.env.get_template("user-api-tool-bench/config/tailwind.config.js.j2")
        return template.render()

    def generate_postcss_config(self) -> str:
        """Generate postcss.config.js."""
        template = self.env.get_template("user-api-tool-bench/config/postcss.config.js.j2")
        return template.render()

    def generate_app_page(self) -> str:
        """Generate pages/_app.tsx."""
        template = self.env.get_template("user-api-tool-bench/pages/_app.tsx.j2")
        return template.render()

    def generate_globals_css(self) -> str:
        """Generate styles/globals.css."""
        template = self.env.get_template("user-api-tool-bench/styles/globals.css.j2")
        return template.render()

    def generate_rate_limit(self) -> str:
        """Generate lib/rate-limit.ts."""
        template = self.env.get_template("user-api-tool-bench/lib/rate-limit.ts.j2")
        return template.render()

    def generate_guide_json(self, project_name: str, wiki_dir: Path | None) -> str | None:
        """
        Generate guide.json content from wiki SME guide markdown.

        Args:
            project_name: Server name (used to find wiki file)
            wiki_dir: Path to wiki directory

        Returns:
            JSON string with {content, lastModified} or None if wiki file doesn't exist
        """
        if wiki_dir is None:
            return None

        # Convention: wiki/{SERVER_NAME}-SME-Guide.md (uppercase server name)
        guide_filename = f"{project_name.upper()}-SME-Guide.md"
        guide_path = wiki_dir / guide_filename

        if not guide_path.exists():
            return None

        # Read markdown content
        content = guide_path.read_text(encoding="utf-8")

        # Get git commit time (consistent across machines, unlike file mtime)
        try:
            result = subprocess.run(
                ["git", "log", "-1", "--format=%ct", str(guide_path)],
                capture_output=True,
                text=True,
                cwd=wiki_dir,
            )
            if result.returncode == 0 and result.stdout.strip():
                last_modified = int(result.stdout.strip())
            else:
                # Fallback to file mtime if git fails
                last_modified = int(guide_path.stat().st_mtime)
        except Exception:
            # Fallback to file mtime if git not available
            last_modified = int(guide_path.stat().st_mtime)

        return json.dumps({"content": content, "lastModified": last_modified})

    def generate_models_page(
        self, models: list[dict[str, Any]], project_name: str = "MCP Server"
    ) -> str:
        """Generate pages/models.tsx for API models documentation."""
        template = self.env.get_template("user-api-tool-bench/pages/models.tsx.j2")
        return template.render(models=models, project_name=project_name)

    def copy_static_files(
        self,
        output_dir: Path,
        reference_ui_dir: Path,
    ):
        """
        Copy static files from reference UI.

        Args:
            output_dir: Output directory for generated UI
            reference_ui_dir: Path to reference UI directory
        """
        # List of optional static files to copy (core files are generated)
        static_files = [
            "pages/api/auth/login.ts",
            "pages/api/endpoints.ts",
            "pages/api/health.ts",
            "lib/auth.ts",
        ]

        for file_path in static_files:
            src = reference_ui_dir / file_path
            dst = output_dir / file_path

            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(src, dst)
                print(f"  Copied {file_path}")
            else:
                print(f"  Warning: {file_path} not found in reference UI")

    def generate_complete_ui(
        self,
        output_dir: Path,
        reference_ui_dir: Path,
        tools: list[dict[str, Any]],
        build_spec: Any,
        project_name: str = "mcp-ui",
        sample_data_tables: list[dict[str, Any]] | None = None,
        models: list[dict[str, Any]] | None = None,
        personas: list[dict[str, Any]] | None = None,
        wiki_dir: Path | None = None,
    ):
        """
        Generate complete UI application.

        Args:
            output_dir: Output directory path
            reference_ui_dir: Reference UI directory path
            tools: List of tool configurations
            build_spec: Build specification object
            project_name: Name for the generated project
            sample_data_tables: Optional list of sample data tables for databases
            models: Optional list of Pydantic models for documentation
            personas: Optional list of personas from users.json for login UI
            wiki_dir: Optional path to wiki directory for SME guide generation
        """
        output_dir = Path(output_dir)
        reference_ui_dir = Path(reference_ui_dir) if reference_ui_dir is not None else None

        print("\nGenerating UI files...")

        # Create output directory
        output_dir.mkdir(parents=True, exist_ok=True)

        # Convert build_spec to dicts for templates
        servers = [s.model_dump() for s in build_spec.servers]

        # Generate config files
        print("  Generating api-config.ts")
        config_code = self.generate_api_config(tools, servers, sample_data_tables, models)
        self.write_file(output_dir / "lib" / "api-config.ts", config_code)

        print("  Generating pages/api/call.ts")
        api_code = self.generate_api_handler(servers)
        self.write_file(output_dir / "pages" / "api" / "call.ts", api_code)

        print("  Generating package.json")
        package_json = self.generate_package_json(project_name)
        self.write_file(output_dir / "package.json", package_json)

        print("  Generating .env.local.example")
        env_example = self.generate_env_example(servers)
        self.write_file(output_dir / ".env.local.example", env_example)

        print("  Generating components/ApiTool.tsx")
        api_tool = self.generate_api_tool_component(personas=personas)
        self.write_file(output_dir / "components" / "ApiTool.tsx", api_tool)

        print("  Copying components/downloadUtils.ts")
        download_utils_src = (
            self.template_dir / "user-api-tool-bench" / "components" / "downloadUtils.ts"
        )
        if download_utils_src.exists():
            download_utils_content = download_utils_src.read_text(encoding="utf-8")
            self.write_file(output_dir / "components" / "downloadUtils.ts", download_utils_content)
        else:
            print(f"    Warning: downloadUtils.ts not found at {download_utils_src}")

        print("  Generating pages/index.tsx")
        index_page = self.generate_index_page(project_name)
        self.write_file(output_dir / "pages" / "index.tsx", index_page)

        print("  Generating tsconfig.json")
        tsconfig = self.generate_tsconfig()
        self.write_file(output_dir / "tsconfig.json", tsconfig)

        print("  Generating next.config.js")
        next_config = self.generate_next_config(project_name)
        self.write_file(output_dir / "next.config.js", next_config)

        print("  Generating tailwind.config.js")
        tailwind_config = self.generate_tailwind_config()
        self.write_file(output_dir / "tailwind.config.js", tailwind_config)

        print("  Generating postcss.config.js")
        postcss_config = self.generate_postcss_config()
        self.write_file(output_dir / "postcss.config.js", postcss_config)

        print("  Generating pages/_app.tsx")
        app_page = self.generate_app_page()
        self.write_file(output_dir / "pages" / "_app.tsx", app_page)

        print("  Generating styles/globals.css")
        globals_css = self.generate_globals_css()
        self.write_file(output_dir / "styles" / "globals.css", globals_css)

        print("  Generating lib/rate-limit.ts")
        rate_limit = self.generate_rate_limit()
        self.write_file(output_dir / "lib" / "rate-limit.ts", rate_limit)

        # Generate guide.json from wiki if available, or delete if source was removed
        guide_json = self.generate_guide_json(project_name, wiki_dir)
        guide_json_path = output_dir / "public" / "guide.json"
        if guide_json:
            print("  Generating public/guide.json (from wiki)")
            self.write_file(guide_json_path, guide_json)
        elif guide_json_path.exists():
            print("  Deleting public/guide.json (wiki SME guide was removed)")
            guide_json_path.unlink()
        else:
            print("  Skipping guide.json (no wiki SME guide found)")

        # Generate models documentation page if models are provided
        if models:
            print("  Generating pages/models.tsx (Model Documentation)")
            models_page = self.generate_models_page(models, project_name)
            self.write_file(output_dir / "pages" / "models.tsx", models_page)

        # Copy additional static files from reference UI if available
        print("\nCopying static files from reference UI...")
        if reference_ui_dir is not None and reference_ui_dir.exists():
            self.copy_static_files(output_dir, reference_ui_dir)
        elif reference_ui_dir is not None:
            print(f"  Warning: Reference UI directory not found: {reference_ui_dir}")
        else:
            print("  Warning: No reference UI directory specified. Skipping static files.")

        # Update file permissions - make all files owned by current user (Unix only)
        if os.name == "posix":
            try:
                current_uid = os.getuid()
                current_gid = os.getgid()
                for root, dirs, files in os.walk(output_dir):
                    for d in dirs:
                        os.chown(os.path.join(root, d), current_uid, current_gid)
                    for f in files:
                        os.chown(os.path.join(root, f), current_uid, current_gid)
                os.chown(output_dir, current_uid, current_gid)
            except Exception as e:
                print(f"  Warning: Could not update permissions: {e}")
        else:
            print("\nSkipping file permission update (not supported on Windows)")

        print(f"\nUI generated successfully at {output_dir}")

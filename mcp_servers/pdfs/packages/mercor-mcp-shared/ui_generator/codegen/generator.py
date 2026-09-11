"""
Code Generator - Generate TypeScript/JavaScript files from templates.
"""

import json
import shutil
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
        models: list[dict[str, Any]] | None = None,
        personas: list[dict[str, Any]] | None = None,
        hidden_tools: list[str] | None = None,
    ) -> str:
        """
        Generate api-config.ts content.

        Args:
            tools: List of tool configurations
            servers: List of server configurations
            models: Optional list of Pydantic models for documentation
            personas: Optional list of persona configurations from users.json
            hidden_tools: Optional list of tool names to hide from the main UI

        Returns:
            Generated TypeScript code
        """
        template = self.env.get_template("user-api-tool-bench/lib/api-config.ts.j2")

        return template.render(
            tools=tools,
            servers=servers,
            models=models or [],
            personas=personas or [],
            hidden_tools=hidden_tools or [],
            generation_time=datetime.now().isoformat(),
        )

    def get_api_handler(self) -> str:
        """Get static pages/api/call.ts."""
        return self.get_static_file("pages/api/call.ts")

    def _deep_merge(self, base: dict, override: dict) -> dict:
        """Deep merge two dictionaries, with override taking precedence.

        Args:
            base: Base dictionary
            override: Dictionary to merge on top (values take precedence)

        Returns:
            Merged dictionary
        """
        result = base.copy()
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = self._deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    def generate_package_json(
        self,
        project_name: str,
    ) -> dict:
        """Generate package.json content as a dictionary.

        Args:
            project_name: Name for the generated project

        Returns:
            Generated package.json as a dictionary
        """
        template = self.env.get_template("user-api-tool-bench/config/package.json.j2")
        rendered = template.render(project_name=project_name)
        return json.loads(rendered)

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

    def get_api_tool_component(self) -> str:
        """Get the static ApiTool.tsx component content.

        Returns:
            The ApiTool.tsx file content as a string.
        """
        api_tool_path = self.template_dir / "user-api-tool-bench" / "components" / "ApiTool.tsx"
        if api_tool_path.exists():
            return api_tool_path.read_text(encoding="utf-8")
        raise FileNotFoundError(f"ApiTool.tsx not found at {api_tool_path}")

    def get_index_page(self) -> str:
        """Get static index.tsx page."""
        return self.get_static_file("pages/index.tsx")

    def get_data_generator_page(self) -> str:
        """Get static data-generator.tsx page."""
        return self.get_static_file("pages/data-generator.tsx")

    def get_validate_page(self) -> str:
        """Get static validate.tsx page."""
        return self.get_static_file("pages/validate.tsx")

    def get_static_file(self, relative_path: str) -> str:
        """Read a static file from the templates directory.

        Args:
            relative_path: Path relative to user-api-tool-bench directory

        Returns:
            File contents as a string
        """
        file_path = self.template_dir / "user-api-tool-bench" / relative_path
        if file_path.exists():
            return file_path.read_text(encoding="utf-8")
        raise FileNotFoundError(f"Static file not found: {file_path}")

    def get_tsconfig(self) -> str:
        """Get static tsconfig.json."""
        return self.get_static_file("config/tsconfig.json")

    def get_next_config(self) -> str:
        """Get static next.config.js."""
        return self.get_static_file("config/next.config.js")

    def get_tailwind_config(self) -> str:
        """Get static tailwind.config.js."""
        return self.get_static_file("config/tailwind.config.js")

    def get_postcss_config(self) -> str:
        """Get static postcss.config.js."""
        return self.get_static_file("config/postcss.config.js")

    def get_app_page(self, app_imports: list[str] | None = None) -> str:
        """Generate pages/_app.tsx from template.

        Args:
            app_imports: Optional list of additional imports to include

        Returns:
            Generated _app.tsx content
        """
        template = self.env.get_template("user-api-tool-bench/pages/_app.tsx.j2")
        return template.render(app_imports=app_imports or [])

    def get_globals_css(self) -> str:
        """Get static styles/globals.css."""
        return self.get_static_file("styles/globals.css")

    def get_overrides_css(self) -> str:
        """Get static styles/overrides.css for custom style overrides."""
        return self.get_static_file("styles/overrides.css")

    def get_rate_limit(self) -> str:
        """Get static lib/rate-limit.ts."""
        return self.get_static_file("lib/rate-limit.ts")

    def _path_to_title(self, name: str) -> str:
        """
        Convert filename or folder name to display title.

        Examples:
            'GUIDE.md' -> 'Guide'
            'getting-started.md' -> 'Getting Started'
            'api-reference' -> 'API Reference'
            'demo-and-database-workflow' -> 'Demo and Database Workflow'
        """
        from titlecase import titlecase

        # Remove .md extension if present
        if name.endswith(".md"):
            name = name[:-3]

        # Replace hyphens and underscores with spaces
        name = name.replace("-", " ").replace("_", " ")

        # Define callback for common acronyms in our domain
        def acronyms(word: str, **kwargs: dict) -> str | None:
            if word.upper() in ("API", "UI", "MCP", "SDK", "CLI", "HTTP", "URL", "JSON", "CSV"):
                return word.upper()
            return None

        return titlecase(name, callback=acronyms)

    def _scan_docs_directory(self, docs_dir: Path, base_path: str = "") -> list[dict[str, Any]]:
        """
        Recursively scan documentation directory and build manifest items.

        Args:
            docs_dir: Path to directory to scan
            base_path: Base path prefix for file paths in manifest

        Returns:
            List of manifest items
        """
        items = []
        excluded_folders = {"images", "assets", "static", "screenshots"}

        # Get all entries sorted alphabetically
        entries = sorted(docs_dir.iterdir(), key=lambda p: p.name.lower())

        for entry in entries:
            # Skip hidden files/folders and excluded folders
            if entry.name.startswith(".") or entry.name.startswith("_"):
                continue
            if entry.is_dir() and entry.name.lower() in excluded_folders:
                continue

            if entry.is_file() and entry.suffix == ".md":
                # Skip README.md as it's used as folder index
                if entry.name.lower() == "readme.md":
                    continue

                item_id = entry.stem.lower().replace(" ", "-")
                item_path = f"{base_path}{entry.name}" if base_path else entry.name

                items.append(
                    {
                        "type": "file",
                        "id": item_id,
                        "title": self._path_to_title(entry.name),
                        "path": item_path,
                    }
                )

            elif entry.is_dir():
                # Check if folder has a README.md (index) - find actual filename to handle
                # case sensitivity correctly across different filesystems
                readme_path = None
                for child in entry.iterdir():
                    if child.is_file() and child.name.lower() == "readme.md":
                        readme_path = child  # Use actual file path with correct case
                        break

                folder_base = f"{base_path}{entry.name}/" if base_path else f"{entry.name}/"

                # Recursively scan children
                children = self._scan_docs_directory(entry, folder_base)

                # Only include folder if it has README or children
                if readme_path is not None or children:
                    item_id = entry.name.lower().replace(" ", "-")
                    folder_item: dict[str, Any] = {
                        "type": "folder",
                        "id": item_id,
                        "title": self._path_to_title(entry.name),
                    }

                    if readme_path is not None:
                        folder_item["indexPath"] = f"{folder_base}{readme_path.name}"
                    elif children:
                        # Use first child's path as fallback index
                        first_child = children[0]
                        if first_child["type"] == "file":
                            folder_item["indexPath"] = first_child["path"]
                        elif "indexPath" in first_child:
                            folder_item["indexPath"] = first_child["indexPath"]

                    if children:
                        folder_item["children"] = children

                    items.append(folder_item)

        return items

    def copy_end_user_documentation(
        self,
        server_dir: Path | None,
        output_dir: Path,
    ) -> bool:
        """
        Copy end_user_documentation folder from server and generate manifest.

        Args:
            server_dir: Path to MCP server directory
            output_dir: Output directory for generated UI

        Returns:
            True if docs were copied, False otherwise
        """
        if server_dir is None:
            return False

        # Check for documentation folder (try multiple names)
        docs_folder_names = ["end_user_documentation", "end_user_docs", "docs"]
        docs_src = None

        for folder_name in docs_folder_names:
            candidate = server_dir / folder_name
            if candidate.exists() and candidate.is_dir():
                docs_src = candidate
                break

        if docs_src is None:
            return False

        # Destination in UI public folder
        docs_dst = output_dir / "public" / "end_user_documentation"

        # Ensure parent directory exists
        docs_dst.parent.mkdir(parents=True, exist_ok=True)

        # Remove existing docs folder if present
        if docs_dst.exists():
            shutil.rmtree(docs_dst)

        # Copy entire folder
        print(f"  Copying {docs_src.name}/ to public/end_user_documentation/")
        shutil.copytree(docs_src, docs_dst)

        # Generate manifest
        print("  Generating end_user_documentation/manifest.json")
        manifest_items = self._scan_docs_directory(docs_dst)

        manifest = {"items": manifest_items}
        manifest_path = docs_dst / "manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

        return True

    def get_models_page(self) -> str:
        """Get static pages/models.tsx for API models documentation."""
        return self.get_static_file("pages/models.tsx")

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
        models: list[dict[str, Any]] | None = None,
        personas: list[dict[str, Any]] | None = None,
        server_dir: Path | None = None,
    ):
        """
        Generate complete UI application.

        Args:
            output_dir: Output directory path
            reference_ui_dir: Reference UI directory path
            tools: List of tool configurations
            build_spec: Build specification object
            project_name: Name for the generated project
            models: Optional list of Pydantic models for documentation
            personas: Optional list of personas from users.json for login UI
            server_dir: Optional path to MCP server directory for end_user_documentation
        """
        output_dir = Path(output_dir)
        reference_ui_dir = Path(reference_ui_dir) if reference_ui_dir is not None else None

        print("\nGenerating UI files...")

        # Create output directory
        output_dir.mkdir(parents=True, exist_ok=True)

        # Convert build_spec to dicts for templates
        servers = [s.model_dump() for s in build_spec.servers]

        # Extract hidden tools from tool_overrides
        hidden_tools = [override.tool for override in build_spec.tool_overrides if override.hidden]

        # Generate config files
        print("  Generating api-config.ts")
        config_code = self.generate_api_config(
            tools, servers, models, personas=personas, hidden_tools=hidden_tools
        )
        self.write_file(output_dir / "lib" / "api-config.ts", config_code)

        print("  Copying pages/api/call.ts")
        api_code = self.get_api_handler()
        self.write_file(output_dir / "pages" / "api" / "call.ts", api_code)

        # Generate base package.json
        print("  Generating package.json")
        package_json = self.generate_package_json(project_name)

        # Check for ui-config.json and process its sections
        ui_config_path = output_dir / "ui-config.json"
        app_imports: list[str] = []

        if ui_config_path.exists():
            print("  Processing ui-config.json")
            try:
                ui_config = json.loads(ui_config_path.read_text(encoding="utf-8"))
                if not isinstance(ui_config, dict):
                    type_name = type(ui_config).__name__
                    print(f"    Warning: ui-config.json must be an object, got {type_name}")
                else:
                    # Extract 'package' section for merging with package.json
                    package_overrides = ui_config.get("package", {})
                    if package_overrides:
                        package_json = self._deep_merge(package_json, package_overrides)
                        # Log what was overridden/added
                        if "dependencies" in package_overrides:
                            deps = list(package_overrides["dependencies"].keys())
                            print(f"    Merged dependencies: {deps}")
                        if "devDependencies" in package_overrides:
                            dev_deps = list(package_overrides["devDependencies"].keys())
                            print(f"    Merged devDependencies: {dev_deps}")
                        excluded = ("dependencies", "devDependencies")
                        other_keys = [k for k in package_overrides.keys() if k not in excluded]
                        if other_keys:
                            print(f"    Merged other fields: {other_keys}")

                    # Extract 'appImports' for _app.tsx
                    app_imports = ui_config.get("appImports", [])
                    if app_imports:
                        print(f"    App imports: {app_imports}")

            except (json.JSONDecodeError, OSError) as e:
                print(f"    Warning: Could not read ui-config.json: {e}")

        self.write_file(output_dir / "package.json", json.dumps(package_json, indent=2) + "\n")

        print("  Generating .env.local.example")
        env_example = self.generate_env_example(servers)
        self.write_file(output_dir / ".env.local.example", env_example)

        # Create components/overrides directory for local customizations
        # Components are now imported from @mcp-shared/ at runtime
        print("  Creating components/overrides/ directory")
        overrides_dir = output_dir / "components" / "overrides"
        overrides_dir.mkdir(parents=True, exist_ok=True)
        readme_content = """# Component Overrides

Place custom component overrides in this directory to customize the UI.

## How it works

Components imported via `@mcp-shared/` are resolved in this order:
1. First, check `./components/overrides/` for a local override
2. If not found, use the shared component from mercor-mcp-shared

## Example: Custom Main Page

To completely replace the main page (e.g., add a custom GUI with tabs), create `MainPage.tsx`:

```tsx
import { useState } from 'react';
import Head from 'next/head';
import ApiTool from '@mcp-shared/ApiTool';
import { AuthUser } from '@/lib/api-config';
import { MainPageProps } from '@mcp-shared-base/MainPage';

// Import your custom components
import { CustomHeader, CustomSidebar, CustomContent } from '@/components/custom';

export default function MainPage({ appName }: MainPageProps) {
  const [activeTab, setActiveTab] = useState<'gui' | 'mcp'>('gui');
  // ... your custom implementation
}
```

## Example: Custom Header

To add a custom status badge to the header, create `Header.tsx`:

```tsx
import BaseHeader, { HeaderProps } from '@mcp-shared-base/Header';

export default function Header(props: HeaderProps) {
  const customBadge = (
    <span className="px-2 py-1 text-xs font-medium bg-blue-100 text-blue-800 rounded-full">
      Custom
    </span>
  );

  return <BaseHeader {...props} additionalBadges={customBadge} />;
}
```

## Available components to override

- `MainPage.tsx` - The entire main page (most powerful override)
- `Header.tsx` - Main header with login/logout
- `ApiTool.tsx` - The MCP tool testing interface
- `DocsViewer.tsx` - Documentation viewer
- `ui/SearchBar.tsx` - Search input
- `ui/ToolsSidebar.tsx` - Tools sidebar
- And more in the shared components directory

## Customizing CSS Styles

To add custom CSS that overrides the default styles, edit `styles/overrides.css`.
This file is imported after `globals.css`, so your styles will take precedence.

```css
/* styles/overrides.css */

/* Example: Custom button colors */
.custom-button {
  background-color: #your-brand-color;
}

/* Example: Override default spacing */
.container {
  max-width: 1400px;
}
```

The `overrides.css` file is preserved during regeneration if it exists in your
UI directory. If not found, an empty template is created.

## Customizing package.json

To add dependencies or override any package.json fields, create a `ui-config.json`
file in the UI root directory (next to package.json). This file is deep-merged
with the generated package.json, so you can:

- Add new dependencies
- Override existing dependency versions
- Add custom scripts
- Override any other package.json field

```json
{
  "dependencies": {
    "recharts": "^3.7.0",
    "react": "^18.4.0"
  },
  "devDependencies": {
    "@types/recharts": "^2.0.0"
  },
  "scripts": {
    "custom": "echo hello"
  }
}
```

The structure mirrors package.json directly - fields are deep-merged, so you only
need to specify what you want to add or override.
"""
        self.write_file(overrides_dir / "README.md", readme_content)

        print("  Copying pages/index.tsx")
        index_page = self.get_index_page()
        self.write_file(output_dir / "pages" / "index.tsx", index_page)

        # Generate data-generator and validate pages if app-config exists
        app_config_path = output_dir / "config" / "app-config.ts"
        if app_config_path.exists():
            print("  Copying pages/data-generator.tsx (app-config.ts found)")
            self.write_file(
                output_dir / "pages" / "data-generator.tsx", self.get_data_generator_page()
            )

            print("  Copying pages/validate.tsx (app-config.ts found)")
            self.write_file(output_dir / "pages" / "validate.tsx", self.get_validate_page())
        else:
            print("  Skipping data-generator/validate pages (no config/app-config.ts)")

        print("  Copying tsconfig.json")
        tsconfig = self.get_tsconfig()
        self.write_file(output_dir / "tsconfig.json", tsconfig)

        print("  Copying next.config.js")
        next_config = self.get_next_config()
        self.write_file(output_dir / "next.config.js", next_config)

        print("  Copying tailwind.config.js")
        tailwind_config = self.get_tailwind_config()
        self.write_file(output_dir / "tailwind.config.js", tailwind_config)

        print("  Copying postcss.config.js")
        postcss_config = self.get_postcss_config()
        self.write_file(output_dir / "postcss.config.js", postcss_config)

        print("  Generating pages/_app.tsx")
        app_page = self.get_app_page(app_imports=app_imports)
        self.write_file(output_dir / "pages" / "_app.tsx", app_page)

        print("  Copying styles/globals.css")
        globals_css = self.get_globals_css()
        self.write_file(output_dir / "styles" / "globals.css", globals_css)

        # Check for existing custom overrides.css in output directory (preserve on regeneration)
        existing_overrides_path = output_dir / "styles" / "overrides.css"
        default_overrides = self.get_overrides_css()

        # Only overwrite if it doesn't exist or if it's the default empty template
        should_preserve = (
            existing_overrides_path.exists()
            and existing_overrides_path.read_text().strip() != default_overrides.strip()
        )

        if should_preserve:
            print("  Preserving styles/overrides.css (custom content)")
        else:
            print("  Copying styles/overrides.css (default empty)")
            self.write_file(output_dir / "styles" / "overrides.css", default_overrides)

        print("  Copying lib/rate-limit.ts")
        rate_limit = self.get_rate_limit()
        self.write_file(output_dir / "lib" / "rate-limit.ts", rate_limit)

        # Copy models documentation page if models are provided
        if models:
            print("  Copying pages/models.tsx (Model Documentation)")
            models_page = self.get_models_page()
            self.write_file(output_dir / "pages" / "models.tsx", models_page)

        # Copy end_user_documentation from server if available
        self.copy_end_user_documentation(server_dir, output_dir)

        # Copy non-component static files (API routes)
        # Components are now imported from @mcp-shared/ at runtime
        static_api_files = [
            ("pages/api/auth/verify.ts", "pages/api/auth/verify.ts"),
        ]
        for src_rel, dst_rel in static_api_files:
            src_path = self.template_dir / "user-api-tool-bench" / src_rel
            if src_path.exists():
                print(f"  Copying {dst_rel}")
                content = src_path.read_text(encoding="utf-8")
                self.write_file(output_dir / dst_rel, content)
            else:
                print(f"    Warning: {src_rel} not found at {src_path}")

        # Clean up legacy guide.json if it exists
        guide_json_path = output_dir / "public" / "guide.json"
        if guide_json_path.exists():
            print("  Deleting public/guide.json (legacy file, no longer used)")
            guide_json_path.unlink()

        # Copy additional static files from reference UI if available
        print("\nCopying static files from reference UI...")
        if reference_ui_dir is not None and reference_ui_dir.exists():
            self.copy_static_files(output_dir, reference_ui_dir)
        elif reference_ui_dir is not None:
            print(f"  Warning: Reference UI directory not found: {reference_ui_dir}")
        else:
            print("  Warning: No reference UI directory specified. Skipping static files.")

        print(f"\nUI generated successfully at {output_dir}")

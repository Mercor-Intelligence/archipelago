#!/usr/bin/env python3
"""
Apply standard CI templates to an existing MCP server project.

Usage:
    python scripts/apply_ci_templates.py <server_name> [options]

Examples:
    # Apply all templates to SAP server
    python scripts/apply_ci_templates.py sap --all

    # Apply only Makefile
    python scripts/apply_ci_templates.py sap --makefile

    # Apply CI workflow and pre-commit
    python scripts/apply_ci_templates.py sap --ci --precommit
"""

import argparse
import re
import sys
from pathlib import Path

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from logging_config import get_logger

logger = get_logger(__name__)


def to_snake_case(name: str) -> str:
    """Convert a string to snake_case."""
    name = name.replace("-", "_").replace(" ", "_")
    name = re.sub(r"(?<!^)(?=[A-Z])", "_", name)
    return name.lower()


def apply_template(
    template_path: Path,
    output_path: Path,
    replacements: dict[str, str],
    force: bool = False,
) -> bool:
    """Apply a template with variable replacements."""
    if output_path.exists() and not force:
        logger.info("Skipping %s (already exists, use --force to overwrite)", output_path)
        return False

    content = template_path.read_text()

    for key, value in replacements.items():
        content = content.replace(f"{{{{{key}}}}}", value)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content)
    logger.info("Created %s", output_path)
    return True


def main():
    parser = argparse.ArgumentParser(
        description="Apply standard CI templates to an MCP server project"
    )
    parser.add_argument(
        "server_name",
        help="Name of the server (e.g., 'sap', 'weather_api')",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Apply all templates",
    )
    parser.add_argument(
        "--ci",
        action="store_true",
        help="Apply GitHub Actions CI workflow",
    )
    parser.add_argument(
        "--precommit",
        action="store_true",
        help="Apply pre-commit configuration",
    )
    parser.add_argument(
        "--makefile",
        action="store_true",
        help="Apply Makefile",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing files",
    )

    args = parser.parse_args()

    # Determine which templates to apply
    if args.all:
        apply_ci = apply_precommit = apply_makefile = True
    else:
        apply_ci = args.ci
        apply_precommit = args.precommit
        apply_makefile = args.makefile

    if not any([apply_ci, apply_precommit, apply_makefile]):
        logger.error("Specify at least one template (--ci, --precommit, --makefile) or use --all")
        sys.exit(1)

    # Setup paths
    root = Path(__file__).parent.parent
    templates_dir = root / "templates" / "ci"

    if not templates_dir.exists():
        logger.error("Templates directory not found: %s", templates_dir)
        sys.exit(1)

    # Prepare replacements
    snake_name = to_snake_case(args.server_name)
    replacements = {
        "SERVER_NAME": args.server_name,
        "SERVER_NAME_SNAKE": snake_name,
    }

    logger.info("Applying CI templates for '%s' server...", args.server_name)
    logger.info("Snake case: %s", snake_name)

    applied = 0

    # Apply CI workflow
    if apply_ci:
        success = apply_template(
            templates_dir / "mcp-ci.yml",
            root / ".github" / "workflows" / f"{snake_name}-ci.yml",
            replacements,
            args.force,
        )
        if success:
            applied += 1

    # Apply pre-commit config
    if apply_precommit:
        success = apply_template(
            templates_dir / "pre-commit-config.yaml",
            root / ".pre-commit-config.yaml",
            replacements,
            args.force,
        )
        if success:
            applied += 1
            logger.info("Run 'pre-commit install' to activate hooks")

    # Apply Makefile
    if apply_makefile:
        success = apply_template(
            templates_dir / "Makefile",
            root / "Makefile",
            replacements,
            args.force,
        )
        if success:
            applied += 1
            logger.info("Run 'make help' to see available commands")

    if applied > 0:
        logger.info("Applied %s template(s) successfully!", applied)
    else:
        logger.info("No templates applied (files already exist or errors occurred)")

    return 0


if __name__ == "__main__":
    sys.exit(main())

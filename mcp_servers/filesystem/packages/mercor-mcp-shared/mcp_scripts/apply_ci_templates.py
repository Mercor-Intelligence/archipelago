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

    # Apply stage tracker (requires --domain)
    python scripts/apply_ci_templates.py sap --stage-tracker --domain hr
"""

import argparse
import shutil
import sys
from pathlib import Path

from mcp_scripts.logging_config import get_logger
from mcp_scripts.utils import to_snake_case

logger = get_logger(__name__)


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


def copy_support_file(
    src: Path,
    dst: Path,
    force: bool = False,
) -> bool:
    """Copy a support file (no template substitution)."""
    if dst.exists() and not force:
        logger.info("Skipping %s (already exists, use --force to overwrite)", dst)
        return False

    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    logger.info("Copied %s", dst)
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
        "--stage-tracker",
        action="store_true",
        help="Apply stage tracker and publish-status workflows (requires --domain)",
    )
    parser.add_argument(
        "--domain",
        default="hr",
        help="Domain category for stage tracker (e.g., 'hr', 'finance'). Default: hr",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing files",
    )

    args = parser.parse_args()

    # Determine which templates to apply
    if args.all:
        apply_ci = apply_precommit = apply_makefile = apply_stage = True
    else:
        apply_ci = args.ci
        apply_precommit = args.precommit
        apply_makefile = args.makefile
        apply_stage = args.stage_tracker

    if not any([apply_ci, apply_precommit, apply_makefile, apply_stage]):
        logger.error(
            "Specify at least one template (--ci, --precommit, --makefile, --stage-tracker) "
            "or use --all"
        )
        sys.exit(1)

    # Setup paths
    # Templates are in mcp_scripts/templates/ci/ (sibling to this script)
    script_dir = Path(__file__).parent
    templates_dir = script_dir / "templates" / "ci"

    # Output goes to the current working directory (the project being configured)
    project_root = Path.cwd()

    if not templates_dir.exists():
        logger.error("Templates directory not found: %s", templates_dir)
        sys.exit(1)

    # Prepare replacements
    snake_name = to_snake_case(args.server_name)
    replacements = {
        "SERVER_NAME": args.server_name,
        "SERVER_NAME_SNAKE": snake_name,
        "DOMAIN": args.domain,
    }

    logger.info("Applying CI templates for '%s' server...", args.server_name)
    logger.info("Snake case: %s", snake_name)

    applied = 0

    # Apply CI workflow
    if apply_ci:
        success = apply_template(
            templates_dir / "mcp-ci.yml",
            project_root / ".github" / "workflows" / f"{snake_name}-ci.yml",
            replacements,
            args.force,
        )
        if success:
            applied += 1

    # Apply pre-commit config
    if apply_precommit:
        success = apply_template(
            templates_dir / "pre-commit-config.yaml",
            project_root / ".pre-commit-config.yaml",
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
            project_root / "Makefile",
            replacements,
            args.force,
        )
        if success:
            applied += 1
            logger.info("Run 'make help' to see available commands")

    # Apply stage tracker + publish status + support files
    if apply_stage:
        # Stage tracker workflow
        success = apply_template(
            templates_dir / "stage-tracker.yml",
            project_root / ".github" / "workflows" / "stage-tracker.yml",
            replacements,
            args.force,
        )
        if success:
            applied += 1

        # Publish status workflow
        success = apply_template(
            templates_dir / "publish-status.yml",
            project_root / ".github" / "workflows" / "publish-status.yml",
            replacements,
            args.force,
        )
        if success:
            applied += 1

        # Support scripts (copied, not templated)
        scripts_src = templates_dir / "scripts"
        prompts_src = templates_dir / "prompts"

        if scripts_src.exists():
            for src_file in scripts_src.iterdir():
                if src_file.is_file():
                    dst = project_root / "scripts" / "ci" / src_file.name
                    success = copy_support_file(src_file, dst, args.force)
                    if success:
                        applied += 1

        if prompts_src.exists():
            for src_file in prompts_src.iterdir():
                if src_file.is_file():
                    dst = project_root / ".github" / "workflows" / "prompts" / src_file.name
                    success = copy_support_file(src_file, dst, args.force)
                    if success:
                        applied += 1

    if applied > 0:
        logger.info("Applied %s template(s) successfully!", applied)
    else:
        logger.info("No templates applied (files already exist or errors occurred)")

    return 0


if __name__ == "__main__":
    sys.exit(main())

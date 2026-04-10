"""LookML Deployer - Deploy generated LookML to Looker via Git.

Looker requires LookML files to be in a Git repository. This module handles:
1. Cloning/updating the Looker project's Git repository
2. Writing generated LookML files to the repo
3. Committing and pushing changes
4. Triggering Looker to sync via API (deploy webhook or API)

Environment variables required:
- LOOKER_PROJECT_GIT_URL: Git URL for the Looker project repo
- LOOKER_PROJECT_GIT_BRANCH: Branch to push to (default: main)
- LOOKER_PROJECT_NAME: Name of the Looker project
- LOOKER_BASE_URL: Looker instance URL (for deploy webhook)
- LOOKER_WEBHOOK_SECRET: Webhook secret for deployment (optional)
"""

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from http_client import get_http_client
from lookml_generator import generate_all_lookml_from_csv_dir


@dataclass
class DeployConfig:
    """Configuration for LookML deployment."""

    git_url: str
    git_branch: str = "main"
    project_name: str = "seeded_data"
    looker_base_url: str | None = None
    webhook_secret: str | None = None
    connection_name: str = "database"


def get_deploy_config_from_env() -> DeployConfig | None:
    """Get deployment configuration from environment variables.

    Returns:
        DeployConfig if required vars are set, None otherwise
    """
    git_url = os.getenv("LOOKER_PROJECT_GIT_URL")
    if not git_url:
        return None

    return DeployConfig(
        git_url=git_url,
        git_branch=os.getenv("LOOKER_PROJECT_GIT_BRANCH", "main"),
        project_name=os.getenv("LOOKER_PROJECT_NAME", "seeded_data"),
        looker_base_url=os.getenv("LOOKER_BASE_URL"),
        webhook_secret=os.getenv("LOOKER_WEBHOOK_SECRET"),
        connection_name=os.getenv("LOOKER_CONNECTION_NAME", "database"),
    )


def clone_or_update_repo(git_url: str, branch: str, work_dir: Path) -> bool:
    """Clone or update a Git repository.

    Args:
        git_url: Git URL to clone
        branch: Branch to checkout
        work_dir: Directory to clone into

    Returns:
        True if successful, False otherwise
    """
    try:
        if (work_dir / ".git").exists():
            # Update existing repo
            subprocess.run(
                ["git", "fetch", "origin"],
                cwd=work_dir,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "checkout", branch],
                cwd=work_dir,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "pull", "origin", branch],
                cwd=work_dir,
                check=True,
                capture_output=True,
            )
        else:
            # Clone new repo
            subprocess.run(
                ["git", "clone", "-b", branch, git_url, str(work_dir)],
                check=True,
                capture_output=True,
            )
        return True
    except subprocess.CalledProcessError as e:
        print(f"Git error: {e.stderr.decode() if e.stderr else str(e)}")
        return False


def commit_and_push(work_dir: Path, branch: str, message: str) -> bool:
    """Commit all changes and push to remote.

    Args:
        work_dir: Git repo directory
        branch: Branch to push to
        message: Commit message

    Returns:
        True if successful, False otherwise
    """
    try:
        # Check if there are changes
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=work_dir,
            check=True,
            capture_output=True,
            text=True,
        )

        if not result.stdout.strip():
            print("No changes to commit")
            return True

        # Add all changes
        subprocess.run(
            ["git", "add", "-A"],
            cwd=work_dir,
            check=True,
            capture_output=True,
        )

        # Commit
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=work_dir,
            check=True,
            capture_output=True,
        )

        # Push
        subprocess.run(
            ["git", "push", "origin", branch],
            cwd=work_dir,
            check=True,
            capture_output=True,
        )

        return True
    except subprocess.CalledProcessError as e:
        print(f"Git error: {e.stderr.decode() if e.stderr else str(e)}")
        return False


async def trigger_looker_deploy(
    looker_url: str,
    project_name: str,
    branch: str,
    webhook_secret: str | None = None,
) -> bool:
    """Trigger Looker to deploy from Git.

    Uses Looker's webhook endpoint to trigger deployment.

    Args:
        looker_url: Looker instance base URL
        project_name: Name of the Looker project
        branch: Git branch to deploy
        webhook_secret: Optional webhook secret for authentication

    Returns:
        True if successful, False otherwise
    """
    # Looker deploy webhook format
    base = looker_url.rstrip("/")
    webhook_url = f"{base}/webhooks/projects/{project_name}/deploy/branch/{branch}"

    headers = {}
    if webhook_secret:
        headers["X-Looker-Deploy-Secret"] = webhook_secret

    try:
        client = get_http_client()
        response = await client.get(webhook_url, headers=headers, timeout=30)
        if response.status_code == 200:
            return True
        else:
            print(f"Deploy webhook failed: {response.status_code} - {response.text}")
            return False
    except Exception as e:
        print(f"Deploy webhook error: {e}")
        return False


def deploy_lookml_to_git(
    config: DeployConfig,
    csv_dir: Path | None = None,
) -> dict[str, Any]:
    """Deploy generated LookML files to Git repository.

    This is the main deployment function that:
    1. Generates LookML from CSV files
    2. Clones/updates the Looker project repo
    3. Copies generated files to the repo
    4. Commits and pushes changes

    Args:
        config: Deployment configuration
        csv_dir: Optional CSV directory (uses default if not provided)

    Returns:
        Dict with deployment result
    """
    # Create a temporary directory for the Git repo
    with tempfile.TemporaryDirectory() as temp_dir:
        repo_dir = Path(temp_dir) / "repo"

        # Clone the repository
        if not clone_or_update_repo(config.git_url, config.git_branch, repo_dir):
            return {"success": False, "error": "Failed to clone repository"}

        # Generate LookML files directly into the repo
        result = generate_all_lookml_from_csv_dir(
            csv_dir=csv_dir,
            output_dir=repo_dir,
            model_name=config.project_name,
            connection=config.connection_name,
        )

        if "error" in result:
            return {"success": False, "error": result["error"]}

        # Commit and push
        views_str = ", ".join(result["views"])
        commit_message = f"Auto-generated LookML from CSV data\n\nViews: {views_str}"
        if not commit_and_push(repo_dir, config.git_branch, commit_message):
            return {"success": False, "error": "Failed to commit and push"}

        return {
            "success": True,
            "views": result["views"],
            "model": result["model"],
            "files": list(result["files"].keys()),
        }


async def deploy_lookml_full(
    config: DeployConfig | None = None,
    csv_dir: Path | None = None,
    trigger_deploy: bool = True,
) -> dict[str, Any]:
    """Full deployment: generate, push to Git, and trigger Looker deploy.

    Args:
        config: Deployment configuration (uses env vars if not provided)
        csv_dir: Optional CSV directory
        trigger_deploy: Whether to trigger Looker deploy webhook

    Returns:
        Dict with deployment result
    """
    if config is None:
        config = get_deploy_config_from_env()
        if config is None:
            return {
                "success": False,
                "error": "No deployment configuration. Set LOOKER_PROJECT_GIT_URL",
            }

    # Deploy to Git
    result = deploy_lookml_to_git(config, csv_dir)
    if not result["success"]:
        return result

    # Trigger Looker deploy if configured
    if trigger_deploy and config.looker_base_url:
        deploy_success = await trigger_looker_deploy(
            config.looker_base_url,
            config.project_name,
            config.git_branch,
            config.webhook_secret,
        )
        result["looker_deploy_triggered"] = deploy_success

    return result


def generate_lookml_local_only(
    model_name: str = "seeded_data",
    connection: str = "database",
) -> dict[str, Any]:
    """Generate LookML files locally without Git deployment.

    Useful for:
    - Development/testing
    - Manual review of generated LookML
    - Copying files to a Git repo manually

    Args:
        model_name: Name for the generated model
        connection: Database connection name

    Returns:
        Dict with generation result including file paths
    """
    return generate_all_lookml_from_csv_dir(
        model_name=model_name,
        connection=connection,
    )

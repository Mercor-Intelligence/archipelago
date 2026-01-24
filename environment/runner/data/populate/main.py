"""Populate subsystems with data from S3-compatible storage.

This module handles downloading objects from S3 (either single objects or
prefixes containing multiple objects) and placing them into subsystem
directories. Supports overwrite semantics where later sources overwrite
earlier ones with the same destination path.

Also supports post-populate hooks that run shell commands after data extraction.
"""

import asyncio
import os

from fastapi import HTTPException
from loguru import logger

from runner.utils.settings import get_settings

from .models import LifecycleHook, PopulateRequest, PopulateResult
from .utils import populate_data

settings = get_settings()


async def run_lifecycle_hook(hook: LifecycleHook) -> None:
    """Run a lifecycle hook command.

    Executes a shell command with optional environment variables.
    Secrets are already resolved by the agent before being sent to the environment.

    Args:
        hook: The lifecycle hook to execute

    Raises:
        RuntimeError: If the command fails (non-zero exit code)
    """
    logger.info(f"Running lifecycle hook for service '{hook.name}'")
    logger.debug(f"Hook command: {hook.command}")

    # Build environment: start with container env, add hook-specific vars
    run_env = dict(os.environ)
    if hook.env:
        run_env.update(hook.env)

    proc = await asyncio.create_subprocess_shell(
        hook.command,
        env=run_env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        # Use errors='replace' to handle binary output gracefully
        error_msg = stderr.decode(errors="replace") if stderr else "No error output"
        logger.error(
            f"Lifecycle hook '{hook.name}' failed with exit code {proc.returncode}: {error_msg}"
        )
        raise RuntimeError(
            f"Lifecycle hook '{hook.name}' failed with exit code {proc.returncode}: {error_msg}"
        )

    if stdout:
        # Use errors='replace' to handle binary output (e.g., database dumps)
        logger.debug(f"Hook '{hook.name}' stdout: {stdout.decode(errors='replace')}")

    logger.info(f"Lifecycle hook '{hook.name}' completed successfully")


async def handle_populate(request: PopulateRequest) -> PopulateResult:
    """Handle populate endpoint request.

    Entry point for the /data/populate endpoint. Validates settings,
    processes the request, runs post-populate hooks, and returns results.

    Args:
        request: PopulateRequest containing list of S3 sources to download
            and optional post-populate hooks

    Returns:
        PopulateResult with total number of objects added

    Raises:
        HTTPException: If populate operation fails or S3 configuration is invalid
    """
    logger.debug(f"Processing populate request with {len(request.sources)} source(s)")

    try:
        # 1. Extract data from S3
        result = await populate_data(
            sources=request.sources,
        )

        logger.info(
            f"Populated {result.objects_added} object(s) from {len(request.sources)} source(s)"
        )

        # 2. Run post-populate hooks
        if request.post_populate_hooks:
            logger.info(
                f"Running {len(request.post_populate_hooks)} post-populate hook(s)"
            )
            for hook in request.post_populate_hooks:
                await run_lifecycle_hook(hook)
            logger.info("All post-populate hooks completed")

        return result
    except HTTPException:
        raise
    except RuntimeError as e:
        # Hook failure
        logger.error(f"Post-populate hook failed: {repr(e)}")
        raise HTTPException(
            status_code=500,
            detail=str(e),
        ) from e
    except Exception as e:
        source_count = len(request.sources)
        logger.error(f"Error populating data from {source_count} source(s): {repr(e)}")
        raise HTTPException(
            status_code=500,
            detail=f"Failed to populate {source_count} source(s): {str(e)}",
        ) from e

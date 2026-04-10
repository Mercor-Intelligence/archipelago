"""
Application lifespan management for OpenBB service startup and shutdown.
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from openbb_app.models import InitializationError, ServiceStatus
from openbb_app.openbb_client import (
    initialize_openbb_client,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
logger.propagate = True


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manage application startup and shutdown.

    Startup: Initialize OpenBB with retries
    Shutdown: Clean up resources
    """
    # ========================================================================
    # STARTUP
    # ========================================================================
    logger.info("=" * 70)
    logger.info("Starting OpenBB...")
    logger.info("=" * 70)

    try:
        # Initialize OpenBB with retry logic
        client = initialize_openbb_client(
            fail_on_error=False,  # Continue even if degraded
        )

        # Log status
        health = client.get_health()

        if health.status == ServiceStatus.HEALTHY:
            logger.info("✅ OpenBB service: HEALTHY")
        elif health.status == ServiceStatus.FAILED:
            logger.error("❌ OpenBB service: FAILED")
            logger.error("❌ Endpoints will return 503")

        logger.info("=" * 70)
        logger.info("Application ready")
        logger.info("=" * 70 + "\n")

    except InitializationError as e:
        logger.critical(f"💥 Startup failed: {e}")
        raise
    except Exception as e:
        logger.critical(f"💥 Unexpected startup error: {e}", exc_info=True)
        raise

    # Application is running
    yield

    # ========================================================================
    # SHUTDOWN
    # ========================================================================
    logger.info("\n" + "=" * 70)
    logger.info("Shutting down...")
    logger.info("=" * 70)

    # Clean up service manager (closes FMPClient HTTP connections)
    try:
        from fastapi_app.services.service_manager import get_service_manager

        manager = get_service_manager()
        await manager.cleanup()
        logger.info("✅ Service manager cleanup complete")
    except Exception as e:
        logger.warning(f"Service manager cleanup error: {e}")

    logger.info("=" * 70 + "\n")

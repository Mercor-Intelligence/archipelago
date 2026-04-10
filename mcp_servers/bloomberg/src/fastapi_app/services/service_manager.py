# mcp_servers/blpapi/fastapi_app/services/service_manager.py
import os
import threading

# Load .env.local for FMP key
from pathlib import Path

from fastapi_app.clients import mock_openbb_client
from fastapi_app.config import settings
from fastapi_app.handlers import IntradayBarHandler, ReferenceDataHandler
from fastapi_app.handlers.beqs_handler import BeqsHandler
from fastapi_app.handlers.historical_data_handler import (
    HISTORICAL_DATA_REQUEST,
    HistoricalDataHandler,
)
from fastapi_app.handlers.intraday_tick_handler import IntradayTickHandler
from fastapi_app.models import (
    INTRADAY_BAR_REQUEST,
    IntradayBarRequest,
    ReferenceDataRequest,
)
from fastapi_app.models.beqs import BeqsRequest
from fastapi_app.models.historical_data import HistoricalDataRequest
from fastapi_app.models.intraday_tick import (
    INTRADAY_TICK_REQUEST,
    IntradayTickRequest,
)
from fastapi_app.services import OpenBBAdapter, RequestDispatcher
from openbb_app.openbb_client import initialize_openbb_client

env_file = Path.cwd() / ".env.local"
if env_file.exists():
    try:
        from dotenv import load_dotenv

        load_dotenv(env_file)
    except ImportError:
        pass


class BloombergServiceManager:
    """Manages Bloomberg service initialization and handler registration."""

    def __init__(self):
        self.dispatcher = RequestDispatcher()
        self._initialized = False
        self._lock = threading.Lock()
        self.use_mock = os.getenv("USE_MOCK", "false").lower() == "true"
        self._client = None  # Store reference for cleanup

    async def cleanup(self):
        """Cleanup resources (close HTTP clients, etc.).

        Note: This is optional for the singleton pattern. The service manager
        persists for the application lifetime by design. This method is provided
        for testing scenarios or explicit cleanup if needed.
        """
        if self._client is not None:
            # Close FMPClient if it has a close method
            if hasattr(self._client, "close"):
                await self._client.close()
            self._client = None

    def initialize(self):
        """Initialize all handlers. Call after OpenBB client is ready."""
        with self._lock:
            if self._initialized:
                return

            # Perform initialization within lock (prevents race conditions)
            # Client selection priority: USE_MOCK > MODE > FMP_API_KEY > OpenBB

            if self.use_mock:
                # Priority 1: Use mock client for testing
                obb_service = mock_openbb_client
            elif settings.mode.lower() == "offline":
                # Priority 2: Use offline client when MODE=offline
                from fastapi_app.clients.offline_client import OfflineClient

                obb_service = OfflineClient(db_path=settings.duckdb_path)
                self._client = obb_service  # Store for cleanup
            else:
                # Priority 3 & 4: Online mode - use FMP or OpenBB
                fmp_key = os.environ.get("FMP_API_KEY")

                if fmp_key:
                    # Use direct FMP client (bypasses OpenBB Platform issues)
                    from fastapi_app.clients.fmp_client import FMPClient

                    obb_service = FMPClient(api_key=fmp_key)
                    self._client = obb_service  # Store for cleanup
                else:
                    # Fall back to OpenBB Platform
                    obb_service = initialize_openbb_client()

            # Register handlers
            service_adapter = OpenBBAdapter(client=obb_service)

            self.dispatcher.register_handler(
                request_type="BeqsRequest",
                handler=BeqsHandler(service_adapter),
                request_model=BeqsRequest,
            )

            self.dispatcher.register_handler(
                request_type="ReferenceDataRequest",
                handler=ReferenceDataHandler(service_adapter),
                request_model=ReferenceDataRequest,
            )

            self.dispatcher.register_handler(
                request_type=INTRADAY_BAR_REQUEST,
                handler=IntradayBarHandler(service_adapter),
                request_model=IntradayBarRequest,
            )

            self.dispatcher.register_handler(
                request_type=INTRADAY_TICK_REQUEST,
                handler=IntradayTickHandler(service_adapter),
                request_model=IntradayTickRequest,
            )

            self.dispatcher.register_handler(
                request_type=HISTORICAL_DATA_REQUEST,
                handler=HistoricalDataHandler(service_adapter),
                request_model=HistoricalDataRequest,
            )

            # Only set flag after successful completion (allows retry on failure)
            self._initialized = True


# Global instance
_service_manager: BloombergServiceManager | None = None


def get_service_manager() -> BloombergServiceManager:
    """Get or create the service manager singleton."""
    global _service_manager
    if _service_manager is None:
        _service_manager = BloombergServiceManager()
    return _service_manager

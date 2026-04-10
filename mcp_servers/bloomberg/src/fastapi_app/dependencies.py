"""FastAPI dependency injection for shared resources."""

import os
from typing import Annotated

from fastapi import Depends

from .clients.openbb_client import OpenBBClient
from .services.mock_adapter import MockAdapter
from .services.openbb_adapter import OpenBBAdapter

# Module-level singleton instance
_adapter_instance: OpenBBAdapter | MockAdapter | None = None


def get_data_adapter() -> OpenBBAdapter | MockAdapter:
    """
    Get the appropriate data adapter based on configuration.

    Returns MockAdapter if MOCK_OPENBB env var is set to "true",
    otherwise returns OpenBBAdapter with OpenBBClient.

    Uses a module-level singleton to ensure only one adapter instance
    is created throughout the application lifecycle.

    Returns:
        OpenBBAdapter or MockAdapter instance

    Example:
        @router.post("/endpoint")
        async def handler(adapter: Annotated[OpenBBAdapter | MockAdapter, Depends(get_data_adapter)]):
            # Use adapter...
    """
    global _adapter_instance

    if _adapter_instance is None:
        use_mock = os.getenv("MOCK_OPENBB", "false").lower() == "true"

        if use_mock:
            _adapter_instance = MockAdapter()
        else:
            client = OpenBBClient()
            _adapter_instance = OpenBBAdapter(client=client)

    return _adapter_instance


# Type alias for convenience
DataAdapter = Annotated[OpenBBAdapter | MockAdapter, Depends(get_data_adapter)]

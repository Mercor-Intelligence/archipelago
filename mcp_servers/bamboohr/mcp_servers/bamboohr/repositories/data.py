"""Data repository pattern for bamboohr.

This module provides the repository abstraction for loading and querying data
from various sources (JSON files, live APIs, user data).

The repository pattern enables:
- Online mode: Live API calls to external services
- Offline mode: Synthetic/mock data from JSON files
- User data: User-created data that overlays synthetic data

Usage:
    from repositories.data import get_repository

    # Get repository (auto-detects mode from environment)
    repo = get_repository()

    # Query data
    result = await repo.get(MyInput(param="value"))
"""

import json
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import httpx
from loguru import logger
from mcp_schema import GeminiBaseModel as BaseModel

# =============================================================================
# Data Path Configuration
# =============================================================================
#
# Path resolution order:
#   1. {APPS_DATA_ROOT}/bamboohr/synthetic/ (production)
#   2. ./data/synthetic/ relative to this file (local development)
#
# Production: APPS_DATA_ROOT=/.apps_data (default)
# Local dev:  APPS_DATA_ROOT=./.apps_data OR just run from server directory
#
# =============================================================================

APP_NAME = "bamboohr"
APPS_DATA_ROOT = os.environ.get("APPS_DATA_ROOT", "")

# Local data directory (relative to this file's parent = server root)
_LOCAL_DATA_DIR = Path(__file__).parent.parent / "data"


def get_apps_data_dir() -> Path:
    """Get app data directory with fallback to local data/ directory.

    Priority:
    1. If APPS_DATA_ROOT is set, use it (regardless of existence)
    2. Otherwise, fall back to local data/ directory
    """
    if APPS_DATA_ROOT:
        # Use configured path regardless of existence - let callers handle missing dirs
        return Path(APPS_DATA_ROOT) / APP_NAME
    # Fall back to local data directory
    return _LOCAL_DATA_DIR


def get_synthetic_data_dir() -> Path:
    """Get synthetic data directory."""
    return get_apps_data_dir() / "synthetic"


def get_user_data_dir() -> Path:
    """Get user data directory."""
    return get_apps_data_dir() / "user"


class Repository[T: BaseModel](ABC):
    """Abstract base class for data repositories."""

    def __init__(self, response_model: type[T], data_key: str):
        """Initialize repository.

        Args:
            response_model: Pydantic model for response validation
            data_key: Key name for this data type (e.g., "orders", "customers")
        """
        self.response_model = response_model
        self.data_key = data_key

    @abstractmethod
    async def get(self, input_model: BaseModel) -> T:
        """Get data based on input model.

        Args:
            input_model: Input parameters for the query

        Returns:
            Response matching response_model schema
        """
        pass


class SyntheticDataRepository[T: BaseModel](Repository[T]):
    """Repository that loads data from JSON files.

    Supports user data overlay - user-created data takes precedence
    over synthetic/seed data.
    """

    def __init__(
        self,
        response_model: type[T],
        data_key: str,
        synthetic_dir: Path | None = None,
        user_dir: Path | None = None,
    ):
        super().__init__(response_model, data_key)
        self.synthetic_dir = synthetic_dir or get_synthetic_data_dir()
        self.user_dir = user_dir or get_user_data_dir()
        self._data: list[dict] | None = None

    def _load_data(self) -> list[dict]:
        """Load and merge synthetic + user data."""
        if self._data is not None:
            return self._data

        data = []

        # Load synthetic data
        synthetic_file = self.synthetic_dir / f"{self.data_key}.json"
        if synthetic_file.exists():
            with open(synthetic_file) as f:
                synthetic = json.load(f)
                if isinstance(synthetic, list):
                    data.extend(synthetic)
                elif isinstance(synthetic, dict) and self.data_key in synthetic:
                    data.extend(synthetic[self.data_key])
            logger.debug(f"Loaded {len(data)} items from {synthetic_file}")

        # Load user data (takes precedence over synthetic)
        # Insert at beginning so get() finds user data first
        user_file = self.user_dir / f"{self.data_key}.json"
        if user_file.exists():
            with open(user_file) as f:
                user_data = json.load(f)
                user_items = []
                if isinstance(user_data, list):
                    user_items = user_data
                elif isinstance(user_data, dict) and self.data_key in user_data:
                    user_items = user_data[self.data_key]
                # Prepend user data so it takes precedence
                data = user_items + data
            logger.debug(f"Loaded user data from {user_file}")

        self._data = data
        return data

    async def get(self, input_model: BaseModel) -> T:
        """Get data matching input criteria."""
        data = self._load_data()

        # Convert input to dict for matching
        input_dict = input_model.model_dump(exclude_none=True)

        # Find matching items
        for item in data:
            if self._matches(item, input_dict):
                return self.response_model.model_validate(item)

        raise ValueError(f"No matching {self.data_key} found for {input_dict}")

    def _matches(self, item: dict, criteria: dict) -> bool:
        """Check if item matches all criteria."""
        for key, value in criteria.items():
            if key not in item or item[key] != value:
                return False
        return True


class LiveDataRepository[T: BaseModel](Repository[T]):
    """Repository that makes live API calls."""

    def __init__(
        self,
        response_model: type[T],
        data_key: str,
        base_url: str,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(response_model, data_key)
        self.base_url = base_url.rstrip("/")
        self.headers = headers or {}

    async def get(self, input_model: BaseModel) -> T:
        """Make API call based on input model."""
        # Build URL from input
        url = self._build_url(input_model)

        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=self.headers)
            response.raise_for_status()
            return self.response_model.model_validate(response.json())

    def _build_url(self, input_model: BaseModel) -> str:
        """Build API URL from input model.

        Override this method to customize URL construction.
        """
        # Default: append input params as query string
        params = input_model.model_dump(exclude_none=True)
        query = "&".join(f"{k}={v}" for k, v in params.items())
        if query:
            return f"{self.base_url}/{self.data_key}?{query}"
        return f"{self.base_url}/{self.data_key}"


def get_repository[T: BaseModel](
    response_model: type[T],
    data_key: str,
    mode: str | None = None,
    **kwargs: Any,
) -> Repository[T]:
    """Factory function to get appropriate repository.

    Args:
        response_model: Pydantic model for responses
        data_key: Data type key (e.g., "orders")
        mode: "online" or "offline" (defaults to BAMBOOHR_MODE env var)
        **kwargs: Additional arguments passed to repository

    Returns:
        Repository instance for the specified mode
    """
    if mode is None:
        mode = os.environ.get("BAMBOOHR_MODE", "offline").lower()

    if mode == "online":
        base_url = kwargs.pop("base_url", os.environ.get("BAMBOOHR_API_URL", ""))
        if not base_url:
            raise ValueError("BAMBOOHR_API_URL environment variable required for online mode")
        return LiveDataRepository(response_model, data_key, base_url, **kwargs)

    return SyntheticDataRepository(response_model, data_key, **kwargs)

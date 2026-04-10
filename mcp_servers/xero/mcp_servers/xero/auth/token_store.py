"""Token storage with rotation-safe handling for refresh tokens."""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from loguru import logger
from mcp_schema import GeminiBaseModel as BaseModel


class TokenData(BaseModel):
    """OAuth token data."""

    access_token: str
    refresh_token: str
    token_type: str = "Bearer"
    expires_in: int
    expires_at: datetime | None = None
    scope: str | None = None

    def is_expired(self) -> bool:
        """Check if access token is expired."""
        if not self.expires_at:
            return True
        # Add 60 second buffer
        return datetime.now(UTC) >= (self.expires_at - timedelta(seconds=60))

    def update_expiry(self) -> None:
        """Update expiry timestamp based on expires_in."""
        self.expires_at = datetime.now(UTC) + timedelta(seconds=self.expires_in)


class TokenStore:
    """
    Manages OAuth token storage with rotation-safe refresh token handling.

    Ensures that refresh tokens are always persisted immediately after
    being received to prevent token rotation issues.
    """

    def __init__(self, storage_path: Path):
        """
        Initialize token store.

        Args:
            storage_path: Path to token storage file
        """
        self.storage_path = storage_path
        self._ensure_storage_dir()

    def _ensure_storage_dir(self) -> None:
        """Ensure storage directory exists."""
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)

    def save_tokens(self, token_data: TokenData) -> None:
        """
        Save tokens to storage with secure file permissions.

        Args:
            token_data: Token data to save
        """
        try:
            # Update expiry timestamp only if not already set
            # This prevents drift when expires_at is provided from OAuth response
            if token_data.expires_at is None:
                token_data.update_expiry()

            # Save to file
            with open(self.storage_path, "w") as f:
                json.dump(token_data.model_dump(mode="json"), f, indent=2, default=str)

            # Set restrictive file permissions (0600 = rw-------)
            # Only the owner can read/write, no access for group/others
            os.chmod(self.storage_path, 0o600)

            logger.info(f"Tokens saved to {self.storage_path} with secure permissions")
        except Exception as e:
            logger.error(f"Failed to save tokens: {e}")
            raise

    def load_tokens(self) -> TokenData | None:
        """
        Load tokens from storage.

        Returns:
            Token data if found, None otherwise
        """
        if not self.storage_path.exists():
            logger.debug("No stored tokens found")
            return None

        try:
            with open(self.storage_path) as f:
                data = json.load(f)

            # Parse expires_at if present
            if "expires_at" in data and data["expires_at"]:
                data["expires_at"] = datetime.fromisoformat(
                    data["expires_at"].replace("Z", "+00:00")
                )

            token_data = TokenData(**data)
            logger.info("Tokens loaded from storage")
            return token_data
        except Exception as e:
            logger.error(f"Failed to load tokens: {e}")
            return None

    def delete_tokens(self) -> None:
        """Delete stored tokens."""
        if self.storage_path.exists():
            self.storage_path.unlink()
            logger.info("Tokens deleted from storage")

    def has_valid_tokens(self) -> bool:
        """
        Check if valid tokens exist.

        Returns:
            True if valid tokens exist, False otherwise
        """
        token_data = self.load_tokens()
        if not token_data:
            return False

        if token_data.is_expired():
            logger.debug("Stored access token is expired")
            return False

        return True

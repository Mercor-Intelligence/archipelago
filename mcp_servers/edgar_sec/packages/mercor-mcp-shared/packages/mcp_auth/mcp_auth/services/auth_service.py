import json
import secrets
import time
from pathlib import Path

from loguru import logger


class AuthService:
    """Service for authentication and token management."""

    TOKEN_EXPIRY_SECONDS = 3600  # 1 hour

    def __init__(self, users_file: Path, token_prefix: str | None = None):
        """Initialize with users from JSON file."""
        self.users = self._load_users(users_file)
        self.tokens: dict[str, dict] = {}
        self.token_prefix = token_prefix

    def _load_users(self, users_file: Path) -> dict[str, dict]:
        """Load users from JSON file."""
        try:
            with open(users_file) as f:
                users = json.load(f)
            logger.info(f"[mcp-auth] Loaded {len(users)} users")
            return users
        except Exception as e:
            logger.error(f"[mcp-auth] Failed to load users: {e}")
            return {}

    def _clean_expired_tokens(self):
        """Remove expired tokens from memory."""
        now = time.time()
        expired = [token for token, data in self.tokens.items() if data.get("expires_at", 0) < now]
        for token in expired:
            del self.tokens[token]
            logger.debug("[mcp-auth] Cleaned expired token")

    def validate_user(self, username: str, password: str) -> dict | None:
        """Validate username/password combination."""
        if not username or not password:
            return None

        user = self.users.get(username)
        if not user:
            logger.warning(f"[mcp-auth] User not found: {username}")
            return None

        if user.get("password") != password:
            logger.warning(f"[mcp-auth] Invalid password for: {username}")
            return None

        return {
            "userId": user["userId"],
            "username": username,
            "roles": user.get("roles", []),
            "scopes": user.get("scopes", []),
        }

    def create_token(self, username: str, user_data: dict) -> str:
        """Create a new secure token for the user.

        If token_prefix was configured, the token will be prefixed with it
        (e.g., "myprefix_abc123...") for easier identification in tests.
        """
        self._clean_expired_tokens()

        random_part = secrets.token_urlsafe(32)
        token = f"{self.token_prefix}_{random_part}" if self.token_prefix else random_part

        self.tokens[token] = {
            "userId": user_data["userId"],
            "username": username,
            "roles": user_data.get("roles", []),
            "scopes": user_data.get("scopes", []),
            "created_at": time.time(),
            "expires_at": time.time() + self.TOKEN_EXPIRY_SECONDS,
        }

        logger.info(f"[mcp-auth] Created token for user: {username}")
        return token

    def get_or_create_token(self, username: str, user_data: dict) -> str:
        """Return existing valid token or create new one."""
        self._clean_expired_tokens()

        now = time.time()
        for token, info in self.tokens.items():
            if info["username"] == username and info.get("expires_at", 0) > now:
                logger.debug(f"[mcp-auth] Reusing token for: {username}")
                return token

        return self.create_token(username, user_data)

    def validate_token(self, token: str) -> dict | None:
        """Validate token and return user metadata if valid."""
        if not token or len(token) > 500:
            return None

        token_data = self.tokens.get(token)
        if not token_data:
            return None

        if token_data.get("expires_at", 0) < time.time():
            del self.tokens[token]
            logger.debug("[mcp-auth] Token expired")
            return None

        return {
            "userId": token_data["userId"],
            "username": token_data["username"],
            "roles": token_data["roles"],
            "scopes": token_data["scopes"],
        }

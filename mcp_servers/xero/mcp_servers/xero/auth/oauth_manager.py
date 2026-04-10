"""
OAuth 2.0 Authorization Code with PKCE flow manager for Xero.

Integrates the shared OAuthPKCEManager with persistent TokenStore for Xero API.
Implements the complete OAuth flow as per Xero's PKCE specification:
https://developer.xero.com/documentation/guides/oauth2/pkce-flow

Key Features:
- Uses shared OAuthPKCEManager for OAuth protocol handling
- Bridges in-memory tokens with persistent TokenStore
- Handles Xero's refresh token rotation
- Auto-refreshes expired tokens
- Manages tenant/connection information
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from loguru import logger

# Import shared OAuth PKCE component using importlib to avoid namespace collision
# (both xero and mercor-mcp use 'mcp_servers' as package name)
import importlib.util
import os
import sys
from pathlib import Path

# Path to OAuth PKCE module in mercor-mcp
# Uses environment variable to allow flexible repository locations
OAUTH_PKCE_PATH = os.getenv("OAUTH_PKCE_MODULE_PATH")

try:
    if not OAUTH_PKCE_PATH:
        raise EnvironmentError(
            "OAUTH_PKCE_MODULE_PATH environment variable not set.\n"
            "Please set it to the oauth_pkce.py file path (absolute or relative):\n"
            "\n"
            "For sibling repositories (recommended):\n"
            "  export OAUTH_PKCE_MODULE_PATH='mercor-mcp/mcp_servers/auth_server/oauth_pkce.py'\n"
            "\n"
            "Or use absolute path:\n"
            "  export OAUTH_PKCE_MODULE_PATH='/path/to/mercor-mcp/mcp_servers/auth_server/oauth_pkce.py'"
        )

    OAUTH_PKCE_PATH = Path(OAUTH_PKCE_PATH)

    # Handle relative paths by resolving from workspace parent directory
    # This allows mercor-mcp to be a sibling directory to mercor-xero
    if not OAUTH_PKCE_PATH.is_absolute():
        current_file = Path(__file__).resolve()
        project_root = current_file.parent.parent.parent.parent  # mercor-xero/
        workspace_parent = project_root
        OAUTH_PKCE_PATH = (workspace_parent / OAUTH_PKCE_PATH).resolve()
        logger.debug(f"Resolved relative path to: {OAUTH_PKCE_PATH}")

    if not OAUTH_PKCE_PATH.exists():
        raise FileNotFoundError(
            f"OAuth PKCE module not found at: {OAUTH_PKCE_PATH}\n"
            f"For relative paths, resolution is from workspace parent directory."
        )

    # Load module directly using importlib to avoid namespace collision
    spec = importlib.util.spec_from_file_location("oauth_pkce_module", OAUTH_PKCE_PATH)
    if spec is None or spec.loader is None:
        raise ValueError(f"Failed to load spec from {OAUTH_PKCE_PATH}")
    oauth_pkce_module = importlib.util.module_from_spec(spec)
    sys.modules["oauth_pkce_module"] = oauth_pkce_module
    spec.loader.exec_module(oauth_pkce_module)

    # Import the class
    OAuthPKCEManager = oauth_pkce_module.OAuthPKCEManager
    logger.debug(f"Successfully imported OAuthPKCEManager from {OAUTH_PKCE_PATH}")

except Exception as e:
    logger.warning(
        f"Failed to import OAuthPKCEManager: {e}\n"
        f"Expected path: {OAUTH_PKCE_PATH}\n"
        "OAuthPKCEManager will not be available. This is fine for offline mode."
    )
    OAuthPKCEManager = None

from mcp_servers.xero.config import Config
from mcp_servers.xero.auth.token_store import TokenData, TokenStore


class OAuthManager:
    """
    Manages OAuth 2.0 Authorization Code with PKCE flow for Xero.

    This class wraps the shared OAuthPKCEManager and integrates it with
    Xero-specific requirements and persistent token storage.

    Xero-Specific Features:
    - Handles Xero's tenant/connection model
    - Manages refresh token rotation (Xero rotates on each refresh)
    - Supports Xero's scopes: offline_access, accounting.*, etc.
    - Integrates with Xero Connections API for multi-tenant support

    Architecture:
    ┌─────────────────┐
    │   TokenStore    │  (Persistent - disk)
    │   (.json file)  │
    └────────┬────────┘
             │ load/save
             ▼
    ┌─────────────────┐
    │ OAuthPKCEManager│  (In-memory - shared component)
    │  (OAuth logic)  │
    └────────┬────────┘
             │ delegates to
             ▼
    ┌─────────────────┐
    │  OAuthManager   │  (This class - Xero integration)
    │ (Xero-specific) │
    └─────────────────┘
    """

    def __init__(self, config: Config, token_store: TokenStore):
        """
        Initialize OAuth manager with Xero configuration.

        Args:
            config: Application configuration with Xero endpoints and settings
            token_store: Token storage manager for persistence
        """
        self.config = config
        self.token_store = token_store

        # Check if OAuthPKCEManager is available
        if OAuthPKCEManager is None:
            raise RuntimeError(
                "OAuthPKCEManager is not available. Cannot initialize OAuthManager.\n"
                "This is required for online mode. Please ensure:\n"
                "1. OAUTH_PKCE_MODULE_PATH environment variable is set\n"
                "2. The oauth_pkce.py module exists at the specified path\n"
                "3. All dependencies are installed\n"
                "\n"
                "For offline mode, use offline provider instead."
            )

        # Initialize shared OAuth PKCE manager with Xero endpoints
        self.oauth_pkce = OAuthPKCEManager(
            client_id=config.xero_client_id,
            authorization_endpoint=config.xero_authorization_endpoint,
            token_endpoint=config.xero_token_endpoint,
            redirect_uri=config.xero_redirect_uri,
            scopes=config.scopes_list,
            client_secret=os.getenv("XERO_CLIENT_SECRET") or None,
        )

        # Load saved tokens from persistent storage (if available)
        self._load_tokens_from_storage()

        logger.info("OAuth manager initialized with shared PKCE implementation")
        logger.debug(f"Scopes: {', '.join(config.scopes_list)}")

    def _load_tokens_from_storage(self) -> None:
        """
        Load tokens from persistent storage into OAuth manager.

        Syncs: TokenStore (disk) → OAuthPKCEManager (memory)
        """
        token_data = self.token_store.load_tokens()
        if token_data:
            # Populate OAuth manager with saved tokens
            self.oauth_pkce.access_token = token_data.access_token
            self.oauth_pkce.refresh_token = token_data.refresh_token
            self.oauth_pkce.token_expiry = token_data.expires_at

            logger.info("Loaded saved tokens from storage")
            if token_data.expires_at:
                time_until_expiry = (
                    token_data.expires_at - datetime.now(timezone.utc)
                ).total_seconds()
                logger.debug(f"Token expires in {time_until_expiry:.0f}s")
        else:
            logger.debug("No saved tokens found in storage")

    def _save_tokens_to_storage(self) -> None:
        """
        Save tokens from OAuth manager to persistent storage.

        Syncs: OAuthPKCEManager (memory) → TokenStore (disk)

        CRITICAL for Xero: This must be called immediately after token refresh
        because Xero rotates refresh tokens on each use!
        """
        if not self.oauth_pkce.access_token:
            logger.warning("No tokens to save")
            return

        # Create TokenData and save
        # Use configured token type and expiry (defaults: Bearer, 1800 seconds)
        token_data = TokenData(
            access_token=self.oauth_pkce.access_token,
            refresh_token=self.oauth_pkce.refresh_token or "",
            token_type=self.config.token_type,
            expires_in=self.config.token_expiry_seconds,
            expires_at=self.oauth_pkce.token_expiry,
            scope=" ".join(self.config.scopes_list),
        )

        self.token_store.save_tokens(token_data)
        logger.info("Saved tokens to persistent storage")

    async def start_oauth_flow(self) -> Optional[TokenData]:
        """
        Start the OAuth 2.0 PKCE authorization flow for Xero.

        This method:
        1. Generates PKCE code_verifier and code_challenge
        2. Opens browser to Xero authorization page
        3. Starts local HTTP server on callback port
        4. Waits for user to authorize (up to timeout)
        5. Exchanges authorization code for tokens
        6. Saves tokens to persistent storage

        User will see Xero's login page and must:
        - Sign in to their Xero account
        - Select which organization to connect
        - Authorize the requested scopes

        Returns:
            TokenData if successful, None otherwise

        Example:
            >>> oauth_manager = OAuthManager(config, token_store)
            >>> tokens = await oauth_manager.start_oauth_flow()
            >>> if tokens:
            >>>     print(f"Authorized! Token expires at: {tokens.expires_at}")
        """
        try:
            # Extract port from redirect URI
            port = urlparse(self.config.xero_redirect_uri).port or self.config.oauth_callback_port

            logger.info("Starting Xero OAuth 2.0 PKCE authorization flow")
            logger.info(f"Callback server will run on port {port}")
            logger.info(f"Timeout: {self.config.oauth_callback_timeout}s")
            logger.info("Opening browser for authorization...")

            # Run async OAuth flow
            token_response = await self.oauth_pkce.start_oauth_flow(
                port=port,
                timeout=self.config.oauth_callback_timeout
            )

            # Save tokens to persistent storage
            self._save_tokens_to_storage()

            logger.info("OAuth flow completed successfully!")
            logger.info(f"Access token obtained (expires in {token_response.get('expires_in', 0)}s)")
            logger.info(f"Refresh token: {'present' if token_response.get('refresh_token') else 'absent'}")

            return self.token_store.load_tokens()

        except TimeoutError as e:
            logger.error("OAuth authorization timeout")
            logger.error(f"User did not authorize within {self.config.oauth_callback_timeout}s")
            logger.error("Please run start_oauth_flow() again to retry")
            return None

        except ValueError as e:
            logger.error("OAuth authorization failed")
            logger.error(f"Error: {e}")
            return None

        except Exception as e:
            logger.error("Unexpected error during OAuth flow")
            logger.exception(f"Error: {e}")
            return None

    async def exchange_code_for_tokens(
        self, authorization_code: str, code_verifier: str
    ) -> Optional[TokenData]:
        """
        Exchange authorization code for access and refresh tokens.

        This is typically called internally by start_oauth_flow().
        Exposed for advanced use cases or custom OAuth flows.

        Args:
            authorization_code: Authorization code from OAuth callback
            code_verifier: PKCE code verifier generated earlier

        Returns:
            Token data if successful, None otherwise

        Reference:
            https://developer.xero.com/documentation/guides/oauth2/pkce-flow#token-exchange
        """
        try:
            # Set code verifier (normally done by start_oauth_flow)
            self.oauth_pkce.code_verifier = code_verifier

            logger.info("Exchanging authorization code for tokens...")

            # Exchange code for tokens
            await self.oauth_pkce.exchange_code_for_tokens(authorization_code)

            # Save to persistent storage
            self._save_tokens_to_storage()

            logger.info("Token exchange successful")
            return self.token_store.load_tokens()

        except ValueError as e:
            logger.error(f"Token exchange failed: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error during token exchange: {e}")
            return None

    async def refresh_access_token(self) -> Optional[TokenData]:
        """
        Refresh access token using stored refresh token.

        IMPORTANT for Xero: Xero rotates refresh tokens on each refresh!
        The old refresh token becomes invalid after use, so we must save
        the new refresh token immediately.

        Returns:
            New token data if successful, None otherwise
        Reference:
            https://developer.xero.com/documentation/guides/oauth2/pkce-flow#token-refresh
        """
        try:
            logger.info("Refreshing access token...")

            current = self.token_store.load_tokens()
            if current and current.refresh_token:
                self.oauth_pkce.refresh_token = current.refresh_token
            if not getattr(self.oauth_pkce, "client_secret", None):
                self.oauth_pkce.client_secret = os.getenv("XERO_CLIENT_SECRET") or None

            # Refresh token using stored refresh token
            await self.oauth_pkce.refresh_access_token()

            # CRITICAL: Save immediately! Xero rotates refresh tokens!
            # If we don't save now and app crashes, we lose the new refresh token
            self._save_tokens_to_storage()

            logger.info("Access token refreshed successfully")
            logger.debug("New refresh token saved (Xero token rotation)")

            return self.token_store.load_tokens()

        except ValueError as e:
            logger.error(f"Token refresh failed: {e}")
            logger.warning("You may need to re-authorize using start_oauth_flow()")
            return None
        except Exception as e:
            logger.error(f"Unexpected error during token refresh: {e}")
            return None

    async def get_valid_access_token(self) -> Optional[str]:
        """
        Get a valid access token, automatically refreshing if expired.

        This is the primary method to use when making Xero API calls.
        It handles token expiry automatically:
        - If token is valid: returns it immediately
        - If token is expired/expiring soon: refreshes first, then returns
        - If no token exists: returns None (must call start_oauth_flow first)

        Returns:
            Valid access token if available, None otherwise

        Example:
            >>> token = await oauth_manager.get_valid_access_token()
            >>> if token:
            >>>     # Make Xero API call
            >>>     headers = {"Authorization": f"Bearer {token}"}
            >>>     response = await client.get(xero_api_url, headers=headers)
        """
        try:
            # ensure refresh_token and client_secret are set before auto-refresh
            current = self.token_store.load_tokens()
            if current and current.refresh_token:
                self.oauth_pkce.refresh_token = current.refresh_token
            if not getattr(self.oauth_pkce, "client_secret", None):
                self.oauth_pkce.client_secret = os.getenv("XERO_CLIENT_SECRET") or None

            # Track token expiry before refresh to detect if refresh occurred
            old_expiry = self.oauth_pkce.token_expiry

            # Try to get valid token (auto-refreshes if expired)
            access_token = await self.oauth_pkce.get_valid_access_token()

            # Save tokens only if they changed (refresh occurred)
            if self.oauth_pkce.token_expiry != old_expiry:
                self._save_tokens_to_storage()
                logger.debug("Tokens refreshed and saved to storage")

            return access_token

        except ValueError as e:
            logger.error(f"No valid access token available: {e}")
            logger.info("Please run start_oauth_flow() to authorize with Xero")
            return None
        except Exception as e:
            logger.error(f"Error getting valid access token: {e}")
            return None

    def has_valid_tokens(self) -> bool:
        """
        Check if valid tokens exist (not expired).

        Returns:
            True if valid tokens exist and are not expired, False otherwise
        """
        return self.oauth_pkce.is_token_valid()

    def get_authorization_url(self) -> str:
        """
        Get the authorization URL without starting the full OAuth flow.

        Useful for manual OAuth flows or debugging.

        Returns:
            Authorization URL with PKCE parameters
        """
        return self.oauth_pkce.get_authorization_url()

    def clear_tokens(self) -> None:
        """
        Clear all tokens from both memory and persistent storage.

        Use this to log out the user or when switching Xero organizations.
        """
        # Clear in-memory tokens
        self.oauth_pkce.clear_tokens()

        # Clear persistent storage
        self.token_store.delete_tokens()

        logger.info("Cleared all tokens (user logged out)")

    async def close(self) -> None:
        """
        Cleanup resources.

        Call this when shutting down the application.
        """
        logger.info("OAuth manager cleanup complete")

"""
Example: Using OAuth PKCE Manager with Xero API

This demonstrates how to use the generic OAuthPKCEManager with Xero's OAuth 2.0 endpoints.
The OAuthPKCEManager is provider-agnostic and works with any OAuth 2.0 PKCE provider.
"""

import asyncio
import os
from datetime import UTC, datetime, timedelta

from mcp_servers.auth_server.oauth_pkce import OAuthPKCEManager


class XeroOAuthClient:
    """
    Xero-specific OAuth client using the generic OAuthPKCEManager.

    This is a thin wrapper that configures the generic OAuth manager
    with Xero's specific endpoints and requirements.
    """

    # Xero OAuth endpoints
    XERO_AUTH_URL = "https://login.xero.com/identity/connect/authorize"
    XERO_TOKEN_URL = "https://identity.xero.com/connect/token"

    # Xero OAuth scopes
    # See: https://developer.xero.com/documentation/guides/oauth2/scopes
    DEFAULT_SCOPES = [
        "offline_access",  # Required for refresh tokens
        "accounting.transactions",
        "accounting.contacts",
        "accounting.settings",
    ]

    def __init__(
        self,
        client_id: str,
        redirect_uri: str = "http://localhost:8080/callback",
        scopes: list[str] = None,
    ):
        """
        Initialize Xero OAuth client.

        Args:
            client_id: Your Xero app's client ID
            redirect_uri: OAuth callback URL (must match Xero app config)
            scopes: List of Xero OAuth scopes (uses defaults if not provided)
        """
        self.oauth_manager = OAuthPKCEManager(
            client_id=client_id,
            authorization_endpoint=self.XERO_AUTH_URL,
            token_endpoint=self.XERO_TOKEN_URL,
            redirect_uri=redirect_uri,
            scopes=scopes or self.DEFAULT_SCOPES,
        )

    async def authorize(self, port: int = 8080) -> dict:
        """
        Start Xero OAuth authorization flow.

        Opens browser for user to authorize, handles callback, and exchanges code for tokens.

        Args:
            port: Port for local callback server

        Returns:
            Token data including access_token, refresh_token, etc.
        """
        return await self.oauth_manager.start_oauth_flow(port=port)

    async def get_access_token(self) -> str:
        """
        Get valid access token, refreshing if necessary.

        Returns:
            Valid Xero access token
        """
        return await self.oauth_manager.get_valid_access_token()

    async def refresh_token(self) -> dict:
        """
        Manually refresh the access token.

        Returns:
            New token data
        """
        return await self.oauth_manager.refresh_access_token()

    def is_authorized(self) -> bool:
        """
        Check if user is currently authorized with valid token.

        Returns:
            True if authorized with valid token, False otherwise
        """
        return self.oauth_manager.is_token_valid()

    def logout(self) -> None:
        """Clear all stored tokens (logout)."""
        self.oauth_manager.clear_tokens()


async def main():
    """
    Example usage of XeroOAuthClient.

    Demonstrates:
    1. Initializing the client
    2. Starting OAuth flow
    3. Getting valid access tokens
    4. Refreshing tokens
    """
    # Get Xero client ID from environment
    client_id = os.getenv("XERO_CLIENT_ID")
    if not client_id:
        print("Error: XERO_CLIENT_ID environment variable not set")
        print("\nSet your Xero client ID:")
        print("  export XERO_CLIENT_ID='your-client-id-here'")
        return

    print("Xero OAuth 2.0 PKCE Example")
    print("=" * 60)

    # Initialize Xero OAuth client
    xero = XeroOAuthClient(
        client_id=client_id,
        redirect_uri="http://localhost:8080/callback",
        scopes=[
            "offline_access",
            "accounting.transactions",
            "accounting.contacts",
            "accounting.settings.read",
        ],
    )

    print("\nInitialized Xero OAuth client")
    print(f"   Client ID: {client_id[:8]}...")

    # Start OAuth flow
    print("\nStarting OAuth authorization flow...")
    print("   1. Opening browser for authorization")
    print("   2. Starting local callback server on port 8080")
    print("   3. Waiting for user authorization...")

    try:
        tokens = await xero.authorize(port=8080)

        print("\nAuthorization successful!")
        print(f"   Access token: {tokens['access_token'][:20]}...")
        print(f"   Token type: {tokens.get('token_type', 'Bearer')}")
        print(f"   Expires in: {tokens.get('expires_in', 0)} seconds")
        print(f"   Has refresh token: {bool(tokens.get('refresh_token'))}")

        # Use the access token
        print("\nGetting valid access token...")
        access_token = await xero.get_access_token()
        print(f"   Token: {access_token[:20]}...")

        # Check authorization status
        print(f"\nAuthorization status: {xero.is_authorized()}")

        # Simulate waiting for token to expire to test refresh
        print("\nSimulating token expiration...")
        print("   (In production, this happens automatically after ~30 minutes)")
        xero.oauth_manager.token_expiry = datetime.now(UTC) - timedelta(seconds=1)

        # Get token again (will auto-refresh if expired)
        print("\nGetting token (will refresh if expired)...")
        access_token = await xero.get_access_token()
        print(f"   Token: {access_token[:20]}...")

        print("\nOAuth flow completed successfully!")
        print("\nNext steps:")
        print("   - Use access_token to make Xero API calls")
        print("   - Token will auto-refresh when needed")
        print("   - Call xero.logout() to clear tokens")

    except TimeoutError as e:
        print(f"\nAuthorization timeout: {e}")
        print("   User did not authorize within the timeout period")

    except ValueError as e:
        print(f"\nAuthorization error: {e}")

    except Exception as e:
        print(f"\nUnexpected error: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())

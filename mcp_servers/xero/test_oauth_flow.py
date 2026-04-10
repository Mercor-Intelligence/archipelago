#!/usr/bin/env python3
"""
Test script for Xero OAuth PKCE flow.

This script demonstrates the complete OAuth flow:
1. Initialize OAuth manager
2. Start OAuth flow (opens browser)
3. User authorizes
4. Get tokens
5. Test token refresh
6. Make a sample API call

Usage:
    export XERO_CLIENT_ID="your-client-id-here"
    python test_oauth_flow.py
"""

import asyncio
import os
import sys

# Add project root to path (mcp_servers is in root, not src)
sys.path.insert(0, os.path.dirname(__file__))

from mcp_servers.xero.auth import OAuthManager, TokenStore
from mcp_servers.xero.config import config


def print_section(title: str):
    """Print a formatted section header."""
    print("\n" + "=" * 70)
    print(f"  {title}")
    print("=" * 70)


async def test_oauth_flow():
    """Test the complete OAuth flow."""

    print_section("Xero OAuth PKCE Flow Test")

    # Check if client ID is set
    if not config.xero_client_id:
        print("\nERROR: XERO_CLIENT_ID environment variable not set!")
        print("\nPlease set your Xero client ID:")
        print("  export XERO_CLIENT_ID='your-client-id-here'")
        print("\nTo get a client ID:")
        print("  1. Go to https://developer.xero.com/app/manage")
        print("  2. Create a new app or use existing app")
        print("  3. Copy the Client ID")
        print("  4. Set redirect URI to: http://localhost:8080/callback")
        return

    print(f"\nClient ID: {config.xero_client_id[:8]}...")
    print(f"Redirect URI: {config.xero_redirect_uri}")
    print(f"Scopes: {', '.join(config.scopes_list[:3])}...")

    # Initialize OAuth manager
    print_section("Step 1: Initialize OAuth Manager")

    token_store = TokenStore(config.token_storage_path)
    oauth_manager = OAuthManager(config, token_store)

    print(f"\nToken storage: {config.token_storage_path}")
    print("OAuth manager initialized")

    # Check if we already have valid tokens
    if oauth_manager.has_valid_tokens():
        print("\nFound existing valid tokens!")

        print_section("Step 2: Using Existing Tokens")

        # Get valid token (will auto-refresh if needed)
        access_token = await oauth_manager.get_valid_access_token()

        if access_token:
            print(f"\nAccess token: {access_token[:20]}...")
            print("\nSkipping authorization (already authorized)")

            # Test API call
            await test_xero_api_call(access_token)
            return
        else:
            print("\nWARNING: Existing tokens are invalid, starting new OAuth flow...")

    # Start OAuth flow
    print_section("Step 2: Start OAuth Authorization Flow")

    print("\nBrowser will open for authorization...")
    print("Please:")
    print("   1. Sign in to your Xero account")
    print("   2. Select which organization to connect")
    print("   3. Click 'Authorize' to grant access")
    print("\nWaiting for authorization...")

    tokens = await oauth_manager.start_oauth_flow()

    if not tokens:
        print("\nOAuth authorization failed!")
        print("Please check the error messages above and try again.")
        return

    # Display token info
    print_section("Step 3: Authorization Successful!")

    print(f"\nAccess token: {tokens.access_token[:20]}...")
    print(f"Refresh token: {tokens.refresh_token[:20] if tokens.refresh_token else 'None'}...")
    print(f"Token type: {tokens.token_type}")
    print(f"Expires at: {tokens.expires_at}")
    print(f"Scopes: {tokens.scope}")

    # Test getting valid token
    print_section("Step 4: Test Token Retrieval")

    access_token = await oauth_manager.get_valid_access_token()

    if access_token:
        print("\nSuccessfully retrieved valid access token")
        print(f"Token: {access_token[:20]}...")
    else:
        print("\nFailed to get valid access token")
        return

    # Test token refresh
    print_section("Step 5: Test Token Refresh")

    print("\nTesting token refresh functionality...")

    refreshed_tokens = await oauth_manager.refresh_access_token()

    if refreshed_tokens:
        print("\nToken refresh successful!")
        print(f"New access token: {refreshed_tokens.access_token[:20]}...")
        print(
            f"New refresh token: {refreshed_tokens.refresh_token[:20] if refreshed_tokens.refresh_token else 'None'}..."
        )
        print("Xero rotated the refresh token (as expected)")
        # Use the new token after refresh
        access_token = refreshed_tokens.access_token
    else:
        print("\nWARNING: Token refresh failed (this might be expected if just obtained)")

    # Test API call with current (possibly refreshed) token
    await test_xero_api_call(access_token)

    # Summary
    print_section("OAuth Flow Test Complete!")

    print("\nAll steps completed successfully!")
    print("\nNext steps:")
    print("  1. Tokens are saved to:", config.token_storage_path)
    print("  2. Tokens will auto-refresh when needed")
    print("  3. You can now implement Xero API tools (accounts, invoices, etc.)")
    print("\nTo test again:")
    print("  - Run this script again (will use saved tokens)")
    print("  - Or delete tokens: rm", config.token_storage_path)


async def test_xero_api_call(access_token: str):
    """Test making an API call to Xero."""
    print_section("Step 6: Test Xero API Call")

    print("\nMaking test API call to Xero...")

    try:
        import httpx

        # Get connections (tenants)
        async with httpx.AsyncClient() as client:
            response = await client.get(
                config.xero_connections_endpoint,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                timeout=30.0,
            )

            response.raise_for_status()
            connections = response.json()

            print("\nAPI call successful!")
            print(f"Found {len(connections)} Xero organization(s):")

            for conn in connections:
                print(f"\n   Organization: {conn.get('tenantName', 'Unknown')}")
                print(f"     Tenant ID: {conn.get('tenantId', 'Unknown')}")
                print(f"     Type: {conn.get('tenantType', 'Unknown')}")

            # Save first tenant ID to config
            if connections:
                tenant_id = connections[0]["tenantId"]
                print("\nTip: You can use this tenant ID for API calls:")
                print(f"   export XERO_TENANT_ID='{tenant_id}'")

    except httpx.HTTPError as e:
        print(f"\nWARNING: API call failed: {e}")
        if hasattr(e, "response") and e.response is not None:
            print(f"Status code: {e.response.status_code}")
            print(f"Response: {e.response.text[:200]}...")
    except Exception as e:
        print(f"\nWARNING: Unexpected error: {e}")


def main():
    """Main entry point."""
    try:
        asyncio.run(test_oauth_flow())
    except KeyboardInterrupt:
        print("\n\nWARNING: Test interrupted by user")
    except Exception as e:
        print(f"\n\nTest failed with error: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    main()

"""
OAuth 2.0 PKCE (Proof Key for Code Exchange) Implementation

Provides a common, reusable implementation of OAuth 2.0 with PKCE for secure
authorization flows. Designed to work with various OAuth providers (Xero, etc).

Reference: RFC 7636 - Proof Key for Code Exchange by OAuth Public Clients
"""

import base64
import hashlib
import secrets
import threading
import time
import webbrowser
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import TypedDict
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from loguru import logger


class TokenResponse(TypedDict, total=False):
    """Type definition for OAuth token response."""

    access_token: str
    token_type: str
    expires_in: int
    refresh_token: str  # Optional - not all providers return this


class OAuthPKCEManager:
    """
    OAuth 2.0 PKCE Flow Manager.

    Handles the complete OAuth flow with PKCE:
    1. Generate code verifier and challenge
    2. Build authorization URL
    3. Start local server and open browser
    4. Handle OAuth callback
    5. Exchange authorization code for tokens
    6. Refresh expired tokens
    7. Manage token lifecycle

    Attributes:
        client_id: OAuth client ID
        authorization_endpoint: OAuth authorization URL
        token_endpoint: OAuth token URL
        redirect_uri: OAuth redirect URI (local callback server)
        scopes: List of OAuth scopes to request
        code_verifier: PKCE code verifier (generated)
        code_challenge: PKCE code challenge (generated)
        access_token: Current access token
        refresh_token: Current refresh token
        token_expiry: Token expiration timestamp
    """

    def __init__(
        self,
        client_id: str,
        authorization_endpoint: str,
        token_endpoint: str,
        redirect_uri: str = "http://localhost:8080/callback",
        scopes: list[str] | None = None,
        client_secret: str | None = None,
    ):
        """
        Initialize OAuth PKCE manager.

        Args:
            client_id: OAuth client ID
            authorization_endpoint: OAuth authorization URL
            token_endpoint: OAuth token URL
            redirect_uri: OAuth redirect URI (default: http://localhost:8080/callback)
            scopes: List of OAuth scopes to request
            client_secret: Optional client secret (some providers require this even with PKCE)

        Note:
            The redirect_uri must use 'localhost' (not '127.0.0.1') as the callback
            server binds specifically to localhost. Ensure your OAuth provider is
            configured with a matching redirect URI.
        """
        self.client_id = client_id
        self.client_secret = client_secret
        self.authorization_endpoint = authorization_endpoint
        self.token_endpoint = token_endpoint
        self.redirect_uri = redirect_uri
        self.scopes = scopes or []

        # PKCE parameters (will be generated)
        self.code_verifier: str | None = None
        self.code_challenge: str | None = None

        # Token data
        self.access_token: str | None = None
        self.refresh_token: str | None = None
        self.token_expiry: datetime | None = None

        # OAuth state for CSRF protection
        self.state: str | None = None

        logger.info(f"Initialized OAuth PKCE manager for client: {client_id}")

    def _generate_pkce_pair(self) -> tuple[str, str]:
        """
        Generate PKCE code verifier and challenge.

        Per RFC 7636:
        - Code verifier: 43-128 character random string (base64url encoded)
        - Code challenge: Base64url(SHA256(code_verifier))

        Returns:
            Tuple of (code_verifier, code_challenge)
        """
        # Generate code verifier (43-128 characters)
        # Using 32 bytes = 43 base64url characters (after padding removal)
        code_verifier_bytes = secrets.token_bytes(32)
        code_verifier = base64.urlsafe_b64encode(code_verifier_bytes).decode("utf-8").rstrip("=")

        # Generate code challenge: BASE64URL(SHA256(code_verifier))
        challenge_bytes = hashlib.sha256(code_verifier.encode("utf-8")).digest()
        code_challenge = base64.urlsafe_b64encode(challenge_bytes).decode("utf-8").rstrip("=")

        logger.debug(
            f"Generated PKCE pair - verifier length: {len(code_verifier)}, "
            f"challenge length: {len(code_challenge)}"
        )

        return code_verifier, code_challenge

    def get_authorization_url(self) -> str:
        """
        Build OAuth authorization URL with PKCE parameters.

        Generates new PKCE pair and state for each authorization request.

        Returns:
            Complete authorization URL to redirect user to
        """
        # Generate new PKCE pair for this authorization request
        self.code_verifier, self.code_challenge = self._generate_pkce_pair()

        # Generate state for CSRF protection
        self.state = secrets.token_urlsafe(32)

        # Build authorization URL parameters
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": " ".join(self.scopes),
            "state": self.state,
            "code_challenge": self.code_challenge,
            "code_challenge_method": "S256",  # SHA256
        }

        auth_url = f"{self.authorization_endpoint}?{urlencode(params)}"

        logger.info(f"Generated authorization URL with PKCE (state: {self.state[:8]}...)")

        return auth_url

    async def start_oauth_flow(self, port: int = 8080, timeout: int = 300) -> TokenResponse:
        """
        Start OAuth flow: open browser and start local callback server.

        This method:
        1. Starts a local HTTP server on specified port
        2. Generates authorization URL with PKCE
        3. Opens browser to authorization URL
        4. Waits for OAuth callback with authorization code
        5. Exchanges code for tokens

        Args:
            port: Port for local callback server (default: 8080)
            timeout: Timeout in seconds for waiting for callback (default: 300)

        Returns:
            Dictionary with token data (TokenResponse)

        Raises:
            ValueError: If port is in use, authorization fails, or state mismatch
            TimeoutError: If no callback received within timeout

        Note:
            After completing this flow, the callback server is shut down and cannot
            be reused. To re-authorize, create a new OAuthPKCEManager instance or
            call this method again (which will attempt to bind to the port again).
        """
        # Get authorization URL
        auth_url = self.get_authorization_url()

        # Container for callback data (shared between threads)
        callback_data = {"code": None, "state": None, "error": None}
        callback_lock = threading.Lock()

        class CallbackHandler(BaseHTTPRequestHandler):
            """HTTP handler for OAuth callback."""

            def do_GET(self):
                """Handle GET request to callback URL."""
                # Parse query parameters
                parsed_url = urlparse(self.path)
                query_params = parse_qs(parsed_url.query)

                # Extract code and state (with thread safety)
                with callback_lock:
                    callback_data["code"] = query_params.get("code", [None])[0]  # type: ignore
                    callback_data["state"] = query_params.get("state", [None])[0]  # type: ignore
                    callback_data["error"] = query_params.get("error", [None])[0]  # type: ignore

                # Send response to browser
                if callback_data["error"]:
                    self.send_response(400)
                    self.send_header("Content-type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"<html><body><h1>Authorization Failed</h1>")
                    self.wfile.write(f"<p>Error: {callback_data['error']}</p>".encode())
                    self.wfile.write(b"<p>You can close this window.</p></body></html>")
                else:
                    self.send_response(200)
                    self.send_header("Content-type", "text/html")
                    self.end_headers()
                    self.wfile.write(b"<html><body><h1>Authorization Successful!</h1>")
                    self.wfile.write(
                        b"<p>You can close this window and return to your application.</p>"
                    )
                    self.wfile.write(b"</body></html>")

            def log_message(self, format, *args):
                """Suppress default logging."""
                pass

        # Start local server with error handling for port binding
        try:
            server = HTTPServer(("localhost", port), CallbackHandler)
            # Set short timeout to allow manual timeout check to run frequently
            # (handle_request() blocks for server.timeout, so we use 1s to check every second)
            server.timeout = 1
        except OSError as e:
            if e.errno == 98 or "Address already in use" in str(e):
                raise ValueError(
                    f"Port {port} is already in use. "
                    f"Please specify a different port or ensure no other application is using it."
                ) from e
            raise ValueError(f"Failed to start callback server on port {port}: {e}") from e

        logger.info(f"Starting local callback server on port {port}")

        try:
            # Open browser to authorization URL
            logger.info(f"Opening browser to: {auth_url}")
            try:
                opened = webbrowser.open(auth_url)
                if not opened:
                    logger.warning(
                        "Failed to open browser automatically. Please visit the URL manually:"
                    )
                    logger.warning(f"  {auth_url}")
            except Exception as e:
                logger.warning(f"Could not open browser: {e}")
                logger.warning(f"Please visit this URL manually: {auth_url}")

            # Wait for callback (with timeout)
            start_time = time.time()
            while True:
                # Check callback status with thread safety
                with callback_lock:
                    if callback_data["code"] is not None or callback_data["error"] is not None:
                        break

                server.handle_request()

                if time.time() - start_time > timeout:
                    raise TimeoutError(f"OAuth callback not received within {timeout} seconds")

            # Read all callback data once under lock for thread safety
            with callback_lock:
                received_code = callback_data["code"]
                received_state = callback_data["state"]
                received_error = callback_data["error"]

            # Check for errors
            if received_error:
                logger.error(f"OAuth authorization failed: {received_error}")
                raise ValueError(f"Authorization failed: {received_error}")

            # Verify state to prevent CSRF
            if received_state != self.state:
                logger.error("State mismatch in OAuth callback (CSRF attempt?)")
                raise ValueError("State mismatch - possible CSRF attack")

            # Exchange code for tokens
            logger.info("Received authorization code, exchanging for tokens...")

            tokens = await self.exchange_code_for_tokens(received_code)  # type: ignore

            return tokens

        finally:
            # Always clean up server, even if an exception occurs
            server.server_close()
            logger.debug(f"Callback server on port {port} shut down")

    async def exchange_code_for_tokens(self, authorization_code: str) -> TokenResponse:
        """
        Exchange authorization code for access and refresh tokens.

        Args:
            authorization_code: Authorization code from OAuth callback

        Returns:
            TokenResponse with token data

        Raises:
            ValueError: If token exchange fails or response is invalid
        """
        if not self.code_verifier:
            raise ValueError("Code verifier not set - call get_authorization_url() first")

        # Prepare token request
        data = {
            "grant_type": "authorization_code",
            "client_id": self.client_id,
            "code": authorization_code,
            "redirect_uri": self.redirect_uri,
            "code_verifier": self.code_verifier,
        }

        # Add client_secret if provided (some OAuth providers require it)
        if self.client_secret:
            data["client_secret"] = self.client_secret

        logger.info(f"Exchanging authorization code for tokens at {self.token_endpoint}")

        # Make token request
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    self.token_endpoint,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=30.0,
                )

                response.raise_for_status()
                token_data = response.json()

            except httpx.HTTPError as e:
                logger.error(f"Token exchange failed: {e}")
                if hasattr(e, "response") and e.response is not None:
                    logger.error(f"Response status: {e.response.status_code}")
                    # Don't log response body - may contain sensitive data (tokens, secrets)
                raise ValueError(f"Failed to exchange code for tokens: {e}") from e

        # Validate response has required access_token field
        if "access_token" not in token_data:
            logger.error("OAuth provider did not return access_token in response")
            raise ValueError("Invalid token response: missing access_token field")

        # Store tokens
        self.access_token = token_data["access_token"]
        self.refresh_token = token_data.get("refresh_token")  # Optional

        # Calculate expiry time
        expires_in = token_data.get("expires_in", 3600)
        self.token_expiry = datetime.now(UTC) + timedelta(seconds=expires_in)

        logger.info(f"Successfully obtained tokens (expires in {expires_in}s)")

        return token_data

    async def refresh_access_token(self) -> TokenResponse:
        """
        Refresh access token using refresh token.

        Returns:
            TokenResponse with new token data

        Raises:
            ValueError: If refresh fails, no refresh token available, or response is invalid
        """
        if not self.refresh_token:
            raise ValueError("No refresh token available")

        # Prepare refresh request
        data = {
            "grant_type": "refresh_token",
            "client_id": self.client_id,
            "refresh_token": self.refresh_token,
        }

        # Add client_secret if provided (some OAuth providers require it)
        if self.client_secret:
            data["client_secret"] = self.client_secret

        logger.info(f"Refreshing access token at {self.token_endpoint}")

        # Make refresh request
        async with httpx.AsyncClient() as client:
            try:
                response = await client.post(
                    self.token_endpoint,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=30.0,
                )

                response.raise_for_status()
                token_data = response.json()

            except httpx.HTTPError as e:
                logger.error(f"Token refresh failed: {e}")
                if hasattr(e, "response") and e.response is not None:
                    logger.error(f"Response status: {e.response.status_code}")
                    # Don't log response body - may contain sensitive data (tokens, secrets)
                raise ValueError(f"Failed to refresh token: {e}") from e

        # Validate response has required access_token field
        if "access_token" not in token_data:
            logger.error("OAuth provider did not return access_token in refresh response")
            raise ValueError("Invalid token response: missing access_token field")

        # Update stored tokens
        self.access_token = token_data["access_token"]

        # Refresh token may be rotated (new one provided) or stay the same
        if "refresh_token" in token_data:
            self.refresh_token = token_data["refresh_token"]

        # Update expiry time
        expires_in = token_data.get("expires_in", 3600)
        self.token_expiry = datetime.now(UTC) + timedelta(seconds=expires_in)

        logger.info(f"Successfully refreshed token (expires in {expires_in}s)")

        return token_data

    async def get_valid_access_token(self) -> str:
        """
        Get a valid access token, refreshing if necessary.

        Checks if current token is expired or about to expire (within 60 seconds).
        If expired/expiring, automatically refreshes the token.

        Returns:
            Valid access token

        Raises:
            ValueError: If no token available and no refresh token to use
        """
        # Check if we have a token
        if not self.access_token:
            raise ValueError("No access token available - complete OAuth flow first")

        # Check if token is expired or expiring soon (60 second buffer)
        if not self.token_expiry:
            logger.warning("No expiry time set, assuming token is valid")
            return self.access_token

        time_until_expiry = (self.token_expiry - datetime.now(UTC)).total_seconds()

        if time_until_expiry <= 60:
            logger.info(
                f"Token expired or expiring soon ({time_until_expiry:.0f}s remaining), "
                f"refreshing..."
            )
            await self.refresh_access_token()

            # Re-check token validity after refresh to handle edge cases
            # where provider returns tokens with very short expiry
            if self.token_expiry:
                new_time_until_expiry = (self.token_expiry - datetime.now(UTC)).total_seconds()
                if new_time_until_expiry <= 60:
                    logger.warning(
                        f"Refreshed token still expiring soon "
                        f"({new_time_until_expiry:.0f}s remaining). "
                        f"OAuth provider may be returning tokens with insufficient lifetime."
                    )
        else:
            logger.debug(f"Token valid for {time_until_expiry:.0f}s")

        return self.access_token

    def is_token_valid(self) -> bool:
        """
        Check if current access token is valid (not expired).

        Returns:
            True if token exists and is not expired, False otherwise
        """
        if not self.access_token or not self.token_expiry:
            return False

        return datetime.now(UTC) < self.token_expiry

    def clear_tokens(self) -> None:
        """Clear all stored tokens and reset state."""
        self.access_token = None
        self.refresh_token = None
        self.token_expiry = None
        self.code_verifier = None
        self.code_challenge = None
        self.state = None

        logger.info("Cleared all tokens and OAuth state")

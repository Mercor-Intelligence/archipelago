"""Shared helpers and constants for integration tests."""

import json
import os
import time
from pathlib import Path

import requests

# Default REST bridge URL
REST_BRIDGE_URL = os.environ.get("REST_BRIDGE_URL", "http://127.0.0.1:8000")

# Demo data IDs from seed migration
DEMO_JOB_PROFILES = ["JP-CEO", "JP-VP-ENG", "JP-SWE-SR", "JP-SWE-MID", "JP-SWE-JR"]
DEMO_LOCATIONS = ["LOC-SF", "LOC-NYC", "LOC-REMOTE"]
DEMO_ORGS = ["ORG-COMPANY", "ORG-ENG", "ORG-ENG-BACKEND", "ORG-ENG-FRONTEND"]
DEMO_COST_CENTERS = ["CC-1000", "CC-2000", "CC-2100", "CC-2200"]

# Load user credentials from users.json (single source of truth)
_USERS_FILE = Path(__file__).parent.parent / "mcp_servers" / "workday" / "users.json"
_USERS: dict = {}
if _USERS_FILE.exists():
    with open(_USERS_FILE) as f:
        _USERS = json.load(f)

# Default credentials for integration tests (loaded from users.json)
DEFAULT_USER = "coordinator"
DEFAULT_PASSWORD = _USERS.get("coordinator", {}).get("password", "coordinator")


def get_user_credentials(username: str) -> tuple[str, str]:
    """Get credentials for a user from users.json.

    Args:
        username: The username to get credentials for

    Returns:
        Tuple of (username, password)

    Raises:
        KeyError: If user not found in users.json
    """
    if username not in _USERS:
        raise KeyError(f"User '{username}' not found in users.json")
    return username, _USERS[username]["password"]


def wait_for_server(url: str, timeout: int = 30) -> bool:
    """Wait for the REST bridge server to be ready.

    Args:
        url: Base URL of the REST bridge
        timeout: Maximum seconds to wait

    Returns:
        True if server is ready, False if timeout
    """
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            response = requests.get(f"{url}/", timeout=2)
            if response.status_code == 200:
                return True
        except requests.exceptions.ConnectionError:
            pass
        time.sleep(0.5)
    return False


class RestClient:
    """HTTP client for calling REST bridge endpoints with authentication support."""

    def __init__(self, base_url: str):
        self.base_url = base_url
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})
        self._auth_token: str | None = None
        self._current_user: str | None = None

    def login(self, username: str, password: str) -> dict:
        """Login to get an authentication token.

        Args:
            username: Username to login with
            password: Password for the user

        Returns:
            Login response containing token info

        Raises:
            AssertionError: If login fails
        """
        response = self.call_tool(
            "login_tool",
            {"username": username, "password": password},
            skip_auth=True,  # Don't require auth for login
        )

        # Extract token from response
        if "token" in response:
            self._auth_token = response["token"]
            self._current_user = username
            # Set Authorization header for subsequent requests
            self.session.headers.update({"Authorization": f"Bearer {self._auth_token}"})

        return response

    def logout(self) -> None:
        """Clear authentication state."""
        self._auth_token = None
        self._current_user = None
        self.session.headers.pop("Authorization", None)

    def switch_user(self, username: str, password: str) -> dict:
        """Switch to a different user.

        Args:
            username: Username to switch to
            password: Password for the user

        Returns:
            Login response for new user
        """
        self.logout()
        return self.login(username, password)

    @property
    def current_user(self) -> str | None:
        """Get the currently logged-in username."""
        return self._current_user

    @property
    def is_authenticated(self) -> bool:
        """Check if client has a valid auth token."""
        return self._auth_token is not None

    def call_tool(
        self, tool_name: str, params: dict | None = None, *, skip_auth: bool = False
    ) -> dict:
        """Call a tool on the REST bridge.

        Args:
            tool_name: Name of the tool to call
            params: Tool parameters (for POST requests)
            skip_auth: If True, don't check for authentication (used for login_tool)

        Returns:
            Tool response as dict

        Raises:
            AssertionError: If the request fails
        """
        url = f"{self.base_url}/tools/{tool_name}"

        if params is None:
            params = {}

        response = self.session.post(url, json=params)

        if response.status_code != 200:
            raise AssertionError(
                f"Tool {tool_name} failed with status {response.status_code}: {response.text}"
            )

        return response.json()

    def discover_tools(self) -> list[dict]:
        """Get list of all available tools.

        Returns:
            List of tool metadata dicts
        """
        response = self.session.get(f"{self.base_url}/api/discover")
        response.raise_for_status()
        return response.json().get("tools", [])

    def get_root(self) -> dict:
        """Get root endpoint info.

        Returns:
            Server info dict
        """
        response = self.session.get(f"{self.base_url}/")
        response.raise_for_status()
        return response.json()

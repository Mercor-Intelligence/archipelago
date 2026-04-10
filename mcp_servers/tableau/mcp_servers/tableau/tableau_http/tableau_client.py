"""HTTP client for Tableau Server REST API.

This module provides a reusable HTTP client for making authenticated requests
to Tableau Server REST API v3.x endpoints.
"""

from typing import Any

import httpx


class TableauHTTPClient:
    """HTTP client for Tableau Server REST API."""

    def __init__(
        self,
        base_url: str,
        site_id: str,
        api_version: str = "3.21",
        auth_token: str | None = None,
        personal_access_token: str | None = None,
        timeout: float = 30.0,
    ):
        """Initialize Tableau HTTP client.

        Args:
            base_url: Base URL of Tableau Server (e.g., "https://tableau.example.com")
            site_id: Site identifier (content URL name)
            api_version: Tableau REST API version (default: 3.21)
            auth_token: Session authentication token (X-Tableau-Auth header)
            personal_access_token: PAT in format "name:secret" for sign-in
            timeout: Request timeout in seconds
        """
        self.base_url = base_url.rstrip("/")
        self.site_id = site_id
        self.api_version = api_version
        self.auth_token = auth_token
        self.personal_access_token = personal_access_token
        self.timeout = timeout

        # Site info populated after sign_in
        self.site_name: str | None = None
        self.site_content_url: str | None = None

        # Build base API URL
        self.api_base = f"{self.base_url}/api/{self.api_version}"

    def _get_headers(self) -> dict[str, str]:
        """Get request headers including auth token if available.

        Returns:
            Dictionary of HTTP headers
        """
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self.auth_token:
            headers["X-Tableau-Auth"] = self.auth_token
        return headers

    async def sign_in(self) -> None:
        """Sign in using Personal Access Token to get session token.

        Uses the personal_access_token to authenticate and stores the
        resulting session token in auth_token.

        Raises:
            ValueError: If personal_access_token is not set
            httpx.HTTPStatusError: If sign-in fails
        """
        if not self.personal_access_token:
            raise ValueError("personal_access_token must be set to sign in")

        token_name, token_secret = self.personal_access_token.split(":", 1)

        payload = {
            "credentials": {
                "personalAccessTokenName": token_name,
                "personalAccessTokenSecret": token_secret,
                "site": {"contentUrl": self.site_id},
            }
        }

        url = f"{self.api_base}/auth/signin"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                url,
                json=payload,
                headers={"Accept": "application/json", "Content-Type": "application/json"},
            )
            response.raise_for_status()
            data = response.json()

        self.auth_token = data["credentials"]["token"]
        site_data = data["credentials"]["site"]
        self.site_id = site_data["id"]
        self.site_name = site_data.get("name")
        self.site_content_url = site_data.get("contentUrl", "")

    async def get(self, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Make GET request to Tableau API.

        Args:
            endpoint: API endpoint path (relative to api_base, without leading /)
            params: Query parameters

        Returns:
            Response JSON data

        Raises:
            httpx.HTTPStatusError: If request fails
        """
        url = f"{self.api_base}/{endpoint}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(url, headers=self._get_headers(), params=params)
            response.raise_for_status()
            return response.json()

    async def post(self, endpoint: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Make POST request to Tableau API.

        Args:
            endpoint: API endpoint path (relative to api_base, without leading /)
            data: Request body JSON data

        Returns:
            Response JSON data

        Raises:
            httpx.HTTPStatusError: If request fails
        """
        url = f"{self.api_base}/{endpoint}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(url, headers=self._get_headers(), json=data)
            response.raise_for_status()
            return response.json()

    async def put(self, endpoint: str, data: dict[str, Any] | None = None) -> dict[str, Any]:
        """Make PUT request to Tableau API.

        Args:
            endpoint: API endpoint path (relative to api_base, without leading /)
            data: Request body JSON data

        Returns:
            Response JSON data

        Raises:
            httpx.HTTPStatusError: If request fails
        """
        url = f"{self.api_base}/{endpoint}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.put(url, headers=self._get_headers(), json=data)
            response.raise_for_status()
            return response.json()

    async def delete(
        self, endpoint: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        """Make DELETE request to Tableau API.

        Args:
            endpoint: API endpoint path (relative to api_base, without leading /)
            params: Query parameters (e.g., mapAssetsTo)

        Returns:
            Response JSON data (if any)

        Raises:
            httpx.HTTPStatusError: If request fails
        """
        url = f"{self.api_base}/{endpoint}"
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.delete(url, headers=self._get_headers(), params=params)
            response.raise_for_status()
            # DELETE may return empty response
            if response.content:
                return response.json()
            return None

    # ========================================================================
    # CONVENIENCE METHODS FOR COMMON ENDPOINTS
    # ========================================================================

    def get_project_endpoint(self, project_id: str | None = None) -> str:
        """Get project endpoint path.

        Args:
            project_id: Project ID (if None, returns list endpoint)

        Returns:
            Endpoint path
        """
        suffix = f"/{project_id}" if project_id else ""
        return f"sites/{self.site_id}/projects" + suffix

    def get_user_endpoint(self, user_id: str | None = None) -> str:
        """Get user endpoint path.

        Args:
            user_id: User ID (if None, returns list endpoint)

        Returns:
            Endpoint path
        """
        suffix = f"/{user_id}" if user_id else ""
        return f"sites/{self.site_id}/users" + suffix

    def get_workbook_endpoint(self, workbook_id: str | None = None) -> str:
        """Get workbook endpoint path.

        Args:
            workbook_id: Workbook ID (if None, returns list endpoint)

        Returns:
            Endpoint path
        """
        suffix = f"/{workbook_id}" if workbook_id else ""
        return f"sites/{self.site_id}/workbooks" + suffix

    def get_datasource_endpoint(self, datasource_id: str | None = None) -> str:
        """Get datasource endpoint path.

        Args:
            datasource_id: Datasource ID (if None, returns list endpoint)

        Returns:
            Endpoint path
        """
        suffix = f"/{datasource_id}" if datasource_id else ""
        return f"sites/{self.site_id}/datasources" + suffix

    def get_view_endpoint(self, view_id: str | None = None) -> str:
        """Get view endpoint path.

        Args:
            view_id: View ID (if None, returns list endpoint)

        Returns:
            Endpoint path
        """
        suffix = f"/{view_id}" if view_id else ""
        return f"sites/{self.site_id}/views" + suffix

    def get_workbook_views_endpoint(self, workbook_id: str) -> str:
        """Get workbook views endpoint path.

        Args:
            workbook_id: Workbook ID

        Returns:
            Endpoint path for listing views in a workbook
        """
        return f"sites/{self.site_id}/workbooks/{workbook_id}/views"

    def get_view_data_endpoint(self, view_id: str) -> str:
        """Get view data export endpoint path.

        Args:
            view_id: View ID

        Returns:
            Endpoint path for CSV data export
        """
        return f"sites/{self.site_id}/views/{view_id}/data"

    def get_view_image_endpoint(self, view_id: str) -> str:
        """Get view image export endpoint path.

        Args:
            view_id: View ID

        Returns:
            Endpoint path for PNG image export
        """
        return f"sites/{self.site_id}/views/{view_id}/image"

    def get_group_endpoint(self, group_id: str | None = None) -> str:
        """Get group endpoint path.

        Args:
            group_id: Group ID (if None, returns list endpoint)

        Returns:
            Endpoint path
        """
        suffix = f"/{group_id}" if group_id else ""
        return f"sites/{self.site_id}/groups" + suffix

    def get_group_user_endpoint(self, group_id: str, user_id: str | None = None) -> str:
        """Get group user endpoint path.

        Args:
            group_id: Group ID
            user_id: User ID (if None, returns list endpoint for group users)

        Returns:
            Endpoint path
        """
        suffix = f"/{user_id}" if user_id else ""
        return f"sites/{self.site_id}/groups/{group_id}/users" + suffix

    async def get_raw(self, endpoint: str, params: dict[str, Any] | None = None) -> bytes:
        """Make GET request and return raw bytes (for binary data like images).

        Args:
            endpoint: API endpoint path (relative to api_base, without leading /)
            params: Query parameters

        Returns:
            Response body as bytes

        Raises:
            httpx.HTTPStatusError: If request fails
        """
        url = f"{self.api_base}/{endpoint}"
        # Note: Tableau API returns 406 if Accept header is set for binary endpoints
        headers = {"X-Tableau-Auth": self.auth_token} if self.auth_token else {}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()
            return response.content

    async def get_text(self, endpoint: str, params: dict[str, Any] | None = None) -> str:
        """Make GET request and return text (for CSV data).

        Args:
            endpoint: API endpoint path (relative to api_base, without leading /)
            params: Query parameters

        Returns:
            Response body as text

        Raises:
            httpx.HTTPStatusError: If request fails
        """
        url = f"{self.api_base}/{endpoint}"
        # Note: Tableau API returns 406 if Accept header is set for data endpoints
        headers = {"X-Tableau-Auth": self.auth_token} if self.auth_token else {}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(url, headers=headers, params=params)
            response.raise_for_status()
            return response.text

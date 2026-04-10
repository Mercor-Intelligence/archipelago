"""Configuration module for Tableau MCP Server.

Loads configuration from environment variables, with support for .env files.
"""

import os
from dataclasses import dataclass

from dotenv import load_dotenv

# Load .env file from project root if it exists
load_dotenv()


@dataclass
class TableauConfig:
    """Configuration for Tableau Server connection."""

    server_url: str
    site_id: str
    token_name: str
    token_secret: str
    api_version: str = "3.21"

    @classmethod
    def from_env(cls) -> "TableauConfig":
        """Load configuration from environment variables.

        Required environment variables:
        - TABLEAU_SERVER_URL: Tableau Server/Cloud URL
        - TABLEAU_SITE_ID: Site content URL or ID
        - TABLEAU_TOKEN_NAME: Personal access token name
        - TABLEAU_TOKEN_SECRET: Personal access token secret

        Optional:
        - TABLEAU_API_VERSION: API version (default: 3.21)

        Returns:
            TableauConfig instance

        Raises:
            ValueError: If required environment variables are missing
        """
        server_url = os.getenv("TABLEAU_SERVER_URL")
        site_id = os.getenv("TABLEAU_SITE_ID")
        token_name = os.getenv("TABLEAU_TOKEN_NAME")
        token_secret = os.getenv("TABLEAU_TOKEN_SECRET")
        api_version = os.getenv("TABLEAU_API_VERSION", "3.21")

        missing = []
        if not server_url:
            missing.append("TABLEAU_SERVER_URL")
        if not site_id:
            missing.append("TABLEAU_SITE_ID")
        if not token_name:
            missing.append("TABLEAU_TOKEN_NAME")
        if not token_secret:
            missing.append("TABLEAU_TOKEN_SECRET")

        if missing:
            raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

        return cls(
            server_url=server_url,
            site_id=site_id,
            token_name=token_name,
            token_secret=token_secret,
            api_version=api_version,
        )

    @property
    def personal_access_token(self) -> str:
        """Get PAT in format expected by TableauHTTPClient."""
        return f"{self.token_name}:{self.token_secret}"

    def is_configured(self) -> bool:
        """Check if all required configuration is present."""
        return all([self.server_url, self.site_id, self.token_name, self.token_secret])


def get_config() -> TableauConfig | None:
    """Get Tableau configuration if available.

    Returns:
        TableauConfig if all required env vars are set, None otherwise
    """
    try:
        return TableauConfig.from_env()
    except ValueError:
        return None

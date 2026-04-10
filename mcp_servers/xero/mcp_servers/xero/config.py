"""Configuration management for Xero MCP server."""

from enum import Enum
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Mode(str, Enum):
    """Server mode."""

    ONLINE = "online"
    OFFLINE = "offline"


class Config(BaseSettings):
    """Configuration for Xero MCP server."""

    _env_default = Path(__file__).parent / ".env"
    _env_root = Path(__file__).parents[3] / ".env" if len(Path(__file__).parents) >= 3 else None
    _env_file = _env_default if _env_default.exists() else _env_root

    model_config = SettingsConfigDict(
        env_file=str(_env_file) if _env_file else None,
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Mode — keyed to XERO_OFFLINE_MODE so it won't collide with other
    # services that share the same container image.
    xero_offline_mode: bool = Field(
        default=True,
        description="Run in offline mode (true) or online mode (false)",
    )
    xero_seed_demo_data: bool = Field(
        default=False,
        description="Seed demo data when offline, enable via XERO_SEED_DEMO_DATA=true",
    )

    # Xero OAuth Configuration
    xero_client_id: str | None = Field(default=None, description="Xero OAuth client ID")
    xero_redirect_uri: str = Field(
        default="http://localhost:8080/callback", description="OAuth redirect URI"
    )
    xero_scopes: str = Field(
        default=(
            "openid profile email offline_access "
            "accounting.settings.read accounting.transactions.read accounting.reports.read"
        ),
        description="OAuth scopes",
    )
    xero_authorization_endpoint: str = Field(
        default="https://login.xero.com/identity/connect/authorize",
        description="Xero authorization endpoint",
    )
    xero_token_endpoint: str = Field(
        default="https://identity.xero.com/connect/token",
        description="Xero token endpoint",
    )
    xero_api_base_url: str = Field(
        default="https://api.xero.com/api.xro/2.0", description="Xero API base URL"
    )
    xero_assets_api_base_url: str = Field(
        default="https://api.xero.com/assets.xro/1.0",
        description="Xero Assets API base URL",
    )
    xero_files_api_base_url: str = Field(
        default="https://api.xero.com/files.xro/1.0",
        description="Xero Files API base URL",
    )
    xero_connections_endpoint: str = Field(
        default="https://api.xero.com/connections", description="Xero connections endpoint"
    )

    # Token storage
    token_storage_path: Path = Field(
        default=Path(".xero_tokens.json"), description="Path to token storage file"
    )

    # Selected tenant
    xero_tenant_id: str | None = Field(default=None, description="Selected Xero tenant ID")

    # OAuth Server Configuration
    oauth_callback_port: int = Field(default=8080, description="Port for OAuth callback server")
    oauth_callback_timeout: int = Field(
        default=300, description="OAuth callback timeout in seconds (5 minutes)"
    )

    # Token Configuration
    token_type: str = Field(default="Bearer", description="OAuth token type (typically 'Bearer')")
    token_expiry_seconds: int = Field(
        default=1800,
        description="Token expiry duration in seconds (Xero default: 1800 = 30 minutes)",
    )

    # Rate limiting
    rate_limit_per_minute: int = Field(
        default=60, description="Maximum requests per minute per tenant"
    )
    rate_limit_per_day: int = Field(default=5000, description="Maximum requests per day per tenant")

    # Retry configuration
    max_retries: int = Field(default=3, description="Maximum retry attempts")
    base_backoff: float = Field(default=1.5, description="Base backoff time in seconds")
    max_backoff: float = Field(default=30.0, description="Maximum backoff cap in seconds")
    backoff_jitter_seed: int | None = Field(
        default=None,
        description="Seed for deterministic jitter during tests; None = random.",
    )

    # Logging
    log_level: str = Field(default="INFO", description="Logging level")
    log_file: str | None = Field(default=None, description="Log file path")

    # MCP Server
    mcp_server_port: int = Field(default=8002, description="MCP server port")
    mcp_server_host: str = Field(default="0.0.0.0", description="MCP server host")

    @property
    def mode(self) -> Mode:
        """Derive Mode from the xero_offline_mode flag."""
        return Mode.OFFLINE if self.xero_offline_mode else Mode.ONLINE

    @property
    def is_online_mode(self) -> bool:
        """Check if server is in online mode."""
        return self.mode == Mode.ONLINE

    @property
    def is_offline_mode(self) -> bool:
        """Check if server is in offline mode."""
        return self.mode == Mode.OFFLINE

    @property
    def scopes_list(self) -> list[str]:
        """Get OAuth scopes as a list."""
        return self.xero_scopes.split()

    def validate_online_config(self) -> None:
        """Validate configuration for online mode."""
        if self.is_online_mode and not self.xero_client_id:
            raise ValueError("XERO_CLIENT_ID must be set for online mode")


# Global config instance (lazy default)
config = Config()


def get_config(mode: Mode | None = None) -> Config:
    """
    Return a fresh Config object.
    - If mode is provided, override the xero_offline_mode flag.
    - Otherwise, use the XERO_OFFLINE_MODE env var / default.
    """
    cfg = Config()

    if mode:
        cfg.xero_offline_mode = mode == Mode.OFFLINE

    return cfg

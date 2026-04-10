import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class OpenBBSettings(BaseSettings):
    """Settings for OpenBB API keys and integrations."""

    model_config = SettingsConfigDict(
        env_file=os.environ.get("ENV_FILE", ".env.local"),
        env_file_encoding="utf-8",
        extra="ignore",  # Ignore extra fields in .env files
        case_sensitive=False,  # Allow case-insensitive environment variable matching
    )

    # OpenBB data provider API keys
    alpha_vantage_key: str = ""
    benzinga_key: str = ""
    biztoc_key: str = ""
    bls_key: str = ""
    cboe_key: str = ""
    cftc_key: str = ""
    congress_key: str = ""
    deribit_key: str = ""
    ecb_key: str = ""
    econdb_key: str = ""
    fama_french_key: str = ""
    federal_reserve_key: str = ""
    finra_key: str = ""
    finviz_key: str = ""
    fmp_api_key: str = ""
    fred_key: str = ""
    gov_us_key: str = ""
    imf_key: str = ""
    intrinio_key: str = ""
    multpl_key: str = ""
    nasdaq_key: str = ""
    oecd_key: str = ""
    polygon_key: str = ""
    sec_key: str = ""
    seeking_alpha_key: str = ""
    stockgrid_key: str = ""
    tiingo_key: str = ""
    tmx_key: str = ""
    tradier_key: str = ""
    tradingeconomics_key: str = ""
    us_eia_key: str = ""
    wsj_key: str = ""
    yfinance_key: str = ""


openbb_settings = OpenBBSettings()


def active_openbb_providers(settings: OpenBBSettings) -> dict[str, str]:
    """
    Returns a dictionary of active OpenBB providers.
    Only includes providers with a non-empty API key or enabled by default (e.g., yfinance).
    """
    # Providers that don't require API keys and are always available
    FREE_PROVIDERS = ["yfinance"]  # Add others as needed

    creds = settings.model_dump()

    # Include providers with non-empty API keys OR providers that are free/don't need keys
    active_providers = {}
    for k, v in creds.items():
        provider_name = k.removesuffix("_api_key").removesuffix("_key")
        # Include if: has a key value OR is in the free providers list
        if v or provider_name in FREE_PROVIDERS:
            active_providers[provider_name] = (
                v if v else "free"
            )  # Use "free" as placeholder for keyless providers
    return active_providers

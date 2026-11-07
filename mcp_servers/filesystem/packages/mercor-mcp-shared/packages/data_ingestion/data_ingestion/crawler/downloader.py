"""HTTP client for fetching URLs and downloading files.

This module provides HTTP functionality for the crawler, including
fetching JSON API responses and downloading files to disk.
"""

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from ..exceptions import ConfigurationError, SourceError

logger = logging.getLogger(__name__)


@dataclass
class HttpConfig:
    """HTTP client configuration.

    Attributes:
        headers: HTTP headers to send with requests
        timeout_connect: Connection timeout in seconds
        timeout_read: Read timeout in seconds
        retry_max_attempts: Maximum number of retry attempts
        retry_backoff_factor: Backoff factor for retries
    """

    headers: dict[str, str] = field(default_factory=dict)
    timeout_connect: int = 10
    timeout_read: int = 60
    retry_max_attempts: int = 3
    retry_backoff_factor: float = 0.5

    @classmethod
    def from_dict(cls, config: dict | None) -> "HttpConfig":
        """Create HttpConfig from dictionary (parsed YAML).

        Args:
            config: Dictionary with HTTP configuration (can be None)

        Returns:
            HttpConfig instance
        """
        if not config:
            return cls()

        headers = config.get("headers", {})

        timeout = config.get("timeout", {})
        timeout_connect = timeout.get("connect", 10) if isinstance(timeout, dict) else 10
        timeout_read = timeout.get("read", 60) if isinstance(timeout, dict) else 60

        retry = config.get("retry", {})
        retry_max_attempts = retry.get("max_attempts", 3) if isinstance(retry, dict) else 3
        retry_backoff_factor = retry.get("backoff_factor", 0.5) if isinstance(retry, dict) else 0.5

        return cls(
            headers=headers,
            timeout_connect=timeout_connect,
            timeout_read=timeout_read,
            retry_max_attempts=retry_max_attempts,
            retry_backoff_factor=retry_backoff_factor,
        )


@dataclass
class RateLimitConfig:
    """Rate limiting configuration.

    Attributes:
        requests_per_second: Maximum requests per second (must be > 0)
    """

    requests_per_second: float = 5.0

    def __post_init__(self):
        if self.requests_per_second <= 0:
            raise ConfigurationError(
                f"requests_per_second must be greater than 0, got {self.requests_per_second}"
            )

    @classmethod
    def from_dict(cls, config: dict | None) -> "RateLimitConfig":
        """Create RateLimitConfig from dictionary.

        Args:
            config: Dictionary with rate limit configuration

        Returns:
            RateLimitConfig instance
        """
        if not config:
            return cls()

        return cls(requests_per_second=config.get("requests_per_second", 5.0))


class Downloader:
    """HTTP client for fetching URLs and downloading files.

    Provides:
    - JSON API response fetching
    - File downloads with progress tracking
    - Rate limiting
    - Automatic retries with backoff
    - Configurable timeouts and headers

    Example:
        >>> config = HttpConfig(headers={"Accept": "application/json"})
        >>> rate_limit = RateLimitConfig(requests_per_second=5)
        >>> downloader = Downloader(config, rate_limit)
        >>> data = downloader.fetch_json("https://api.example.com/data")
    """

    def __init__(
        self,
        http_config: HttpConfig | None = None,
        rate_limit_config: RateLimitConfig | None = None,
    ):
        """Initialize Downloader.

        Args:
            http_config: HTTP configuration (uses defaults if None)
            rate_limit_config: Rate limiting configuration (uses defaults if None)
        """
        self.http_config = http_config or HttpConfig()
        self.rate_limit_config = rate_limit_config or RateLimitConfig()

        # Create session with retry logic
        self._session = self._create_session()

        # Rate limiting state
        self._last_request_time: float = 0
        self._min_interval = 1.0 / self.rate_limit_config.requests_per_second

    def _create_session(self) -> requests.Session:
        """Create requests session with retry configuration.

        Returns:
            Configured requests.Session
        """
        session = requests.Session()

        # Configure retries
        retry_strategy = Retry(
            total=self.http_config.retry_max_attempts,
            backoff_factor=self.http_config.retry_backoff_factor,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=["GET", "HEAD"],
        )

        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)

        # Set default headers
        session.headers.update(self.http_config.headers)

        return session

    def _wait_for_rate_limit(self) -> None:
        """Wait if necessary to respect rate limiting."""
        now = time.time()
        elapsed = now - self._last_request_time

        if elapsed < self._min_interval:
            sleep_time = self._min_interval - elapsed
            time.sleep(sleep_time)

        self._last_request_time = time.time()

    def fetch_json(self, url: str) -> dict[str, Any]:
        """Fetch JSON data from URL.

        Args:
            url: URL to fetch

        Returns:
            Parsed JSON response as dictionary

        Raises:
            SourceError: If request fails or response is not valid JSON
        """
        self._wait_for_rate_limit()

        try:
            logger.debug(f"Fetching JSON from: {url}")

            response = self._session.get(
                url,
                timeout=(self.http_config.timeout_connect, self.http_config.timeout_read),
            )
            response.raise_for_status()

            return response.json()

        except requests.exceptions.JSONDecodeError as e:
            raise SourceError(f"Invalid JSON response from {url}: {e}") from e
        except requests.exceptions.HTTPError as e:
            raise SourceError(f"HTTP error fetching {url}: {e}") from e
        except requests.exceptions.ConnectionError as e:
            raise SourceError(f"Connection error fetching {url}: {e}") from e
        except requests.exceptions.Timeout as e:
            raise SourceError(f"Timeout fetching {url}: {e}") from e
        except requests.exceptions.RequestException as e:
            raise SourceError(f"Request failed for {url}: {e}") from e

    def download_file(
        self,
        url: str,
        destination: Path,
        chunk_size: int = 8192,
    ) -> Path:
        """Download file from URL to local path.

        Args:
            url: URL to download from
            destination: Local path to save file
            chunk_size: Size of chunks for streaming download

        Returns:
            Path to downloaded file

        Raises:
            SourceError: If download fails
        """
        self._wait_for_rate_limit()

        try:
            logger.debug(f"Downloading: {url} -> {destination}")

            # Ensure parent directory exists
            destination.parent.mkdir(parents=True, exist_ok=True)

            with self._session.get(
                url,
                stream=True,
                timeout=(self.http_config.timeout_connect, self.http_config.timeout_read),
            ) as response:
                response.raise_for_status()

                # Download with streaming
                downloaded = 0
                with open(destination, "wb") as f:
                    for chunk in response.iter_content(chunk_size=chunk_size):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)

                logger.debug(f"Downloaded {downloaded} bytes to {destination}")
                return destination

        except requests.exceptions.HTTPError as e:
            raise SourceError(f"HTTP error downloading {url}: {e}") from e
        except requests.exceptions.ConnectionError as e:
            raise SourceError(f"Connection error downloading {url}: {e}") from e
        except requests.exceptions.Timeout as e:
            raise SourceError(f"Timeout downloading {url}: {e}") from e
        except requests.exceptions.RequestException as e:
            raise SourceError(f"Download failed for {url}: {e}") from e
        except OSError as e:
            raise SourceError(f"Failed to write file {destination}: {e}") from e

    def close(self) -> None:
        """Close the HTTP session."""
        self._session.close()

    def __enter__(self) -> "Downloader":
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Context manager exit."""
        self.close()

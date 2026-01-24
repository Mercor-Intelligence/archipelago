"""
Data Delivery API client for document parsing with S3-backed caching.

This is Mercor's internal delivery API that wraps Reducto with persistent storage.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from loguru import logger
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from runner.utils.settings import get_settings

from ...constants import MULTI_PART_FILE_TYPES
from ..reducto.client import ReductoClient, _is_retryable_error
from ..reducto.types import ReductoExtractedContent


@dataclass
class DataDeliveryConfig:
    """Configuration for the Mercor Data Delivery API."""

    # Hard-coded configuration for Mercor Delivery
    base_url: str = "https://delivery-api.mercor.com"
    # base_url: str = "http://localhost:8001"  # For local development
    upload_timeout_sec: int = 300
    parse_timeout_sec: int = 600
    max_retries: int = 3
    backoff_base_sec: float = 3.0
    user_email: str = "rl-studio@mercor.com"
    user_name: str = "rl-studio"
    user_id: str = "rl-studio"
    user_role: str = "admin"


class DataDeliveryClient(ReductoClient):
    """
    Client for Mercor Data Delivery document parsing API.

    Inherits from ReductoClient and overrides upload/parse methods to use
    Mercor's internal delivery API with S3+SQLite caching.

    All extraction methods (extract_md, extract_content_with_images, etc.)
    are inherited from ReductoClient and work identically.

    Environment Variables:
        MERCOR_DELIVERY_API_KEY: API key for Mercor Delivery API (required)
    """

    def __init__(
        self,
        api_key: str | None = None,
        config: DataDeliveryConfig | None = None,
    ):
        # Set our config
        delivery_cfg = config or DataDeliveryConfig()

        # Read API key: use parameter if provided, otherwise from settings
        settings = get_settings()
        delivery_api_key = api_key or settings.MERCOR_DELIVERY_API_KEY

        if not delivery_api_key:
            raise RuntimeError(
                "MERCOR_DELIVERY_API_KEY not configured. "
                "Either set it in settings/environment or pass api_key parameter."
            )

        # Store config
        self._delivery_cfg = delivery_cfg

        # Initialize parent - will store api_key as self.api_key
        super().__init__(api_key=delivery_api_key)

        # Override with our delivery config
        self.cfg = delivery_cfg

    def _headers(self, is_json: bool = False) -> dict[str, str]:
        """
        Build headers with authentication and user context.

        Args:
            is_json: Whether to add Content-Type: application/json
        """
        # Mercor Delivery headers with user context
        h = {
            "Authorization": f"Bearer {self.api_key}",
            "X-User-email": self._delivery_cfg.user_email,
            "X-User-name": self._delivery_cfg.user_name,
            "X-User-id": self._delivery_cfg.user_id,
            "X-User-role": self._delivery_cfg.user_role,
        }

        if is_json:
            h["Content-Type"] = "application/json"
        return h

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception(_is_retryable_error),
    )
    async def upload_and_parse(
        self,
        file_path: Path,
        *,
        is_csv: bool,
        page_range: tuple[int, int] | None = None,
    ) -> dict[str, Any]:
        """
        Parse a document using Mercor Data Delivery API.

        Directly uploads the file to the parse endpoint (no separate upload step needed).

        Args:
            file_path: Path to the local file to parse
            is_csv: Whether to use CSV table output format
            page_range: Optional tuple of (start_page, end_page) to extract only specific pages (1-indexed, inclusive)

        Returns:
            Parsed document result from Mercor Data Delivery API
        """
        url = f"{self._delivery_cfg.base_url}/api/v1/parsed-files/v2/parse"

        logger.debug(f"Parsing file with Mercor Data Delivery API: {file_path}")

        # Build form data
        with file_path.open("rb") as f:
            files = {"file": (file_path.name, f, "application/octet-stream")}

            # Build form fields
            data = {
                "is_csv": "true" if is_csv else "false",
            }

            # Add page range if specified - pass as string format "start-end"
            if page_range is not None:
                start_page, end_page = page_range
                logger.debug(
                    f"DataDelivery: Extracting pages {start_page} to {end_page}"
                )
                # Pass page_range as string format "start-end"
                data["page_range"] = f"{start_page}-{end_page}"
                logger.debug(f"DataDelivery: Sending page_range: {data['page_range']}")

            async with httpx.AsyncClient() as client:
                resp = await client.post(
                    url,
                    files=files,
                    data=data,
                    headers=self._headers(is_json=False),
                    timeout=self._delivery_cfg.parse_timeout_sec,
                )

        resp.raise_for_status()
        result = resp.json()

        logger.debug(
            f"Document parsed successfully with Mercor Data Delivery: {file_path.name}"
        )

        return result

    async def extract_from_file(
        self,
        file_path: Path,
        *,
        include_images: bool = True,
        sub_artifact_index: int | None = None,
    ):
        """
        Extract text and optionally images from a local file using Mercor Data Delivery API.

        Overrides parent class to skip the upload step and directly call parse with file_path.

        Args:
            file_path: Path to the document file
            include_images: Whether to extract images
            sub_artifact_index: Optional 0-based index of specific sub-artifact to extract

        Returns:
            ReductoExtractedContent with text, optional images, and sub-artifacts
        """
        # Determine page range for API (if extracting a specific sub-artifact)
        page_range = None
        if sub_artifact_index is not None:
            # API uses 1-indexed pages, our sub_artifact_index is 0-based
            page_num = sub_artifact_index + 1
            page_range = (page_num, page_num)
            logger.debug(
                f"Extracting only sub-artifact at index {sub_artifact_index} (page {page_num})"
            )

        # Parse with Mercor Data Delivery (no upload step needed)
        is_csv = file_path.suffix.lower() == ".csv"
        result = await self.upload_and_parse(
            file_path, is_csv=is_csv, page_range=page_range
        )

        # Determine if this is a multi-part document
        file_type = file_path.suffix.lower()
        is_multi_part = file_type in MULTI_PART_FILE_TYPES

        # Extract content using parent class methods
        if include_images:
            if is_multi_part:
                (
                    text,
                    images,
                    sub_artifacts,
                ) = await self.extract_content_with_sub_artifacts(
                    result, file_type, file_path=str(file_path)
                )
                return ReductoExtractedContent(
                    text=text, images=images, sub_artifacts=sub_artifacts
                )
            else:
                text, images = await self.extract_content_with_images(result)
                return ReductoExtractedContent(text=text, images=images)
        else:
            text = self.extract_md(result)
            return ReductoExtractedContent(text=text, images=[])

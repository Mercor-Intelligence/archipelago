"""Generic web crawler module for data ingestion.

This module provides a config-driven web crawler that can discover
and download files from any JSON-based API.

Example:
    >>> from data_ingestion.crawler import Crawler, CrawlerConfig
    >>> config = CrawlerConfig.from_yaml(Path("crawler_config.yaml"))
    >>> crawler = Crawler(config)
    >>> crawler.run()
"""

from ..checkpoint import Checkpoint, FailedItem
from .crawler import (
    Crawler,
    CrawlerConfig,
    CrawlOptions,
    CrawlResult,
    DownloadResult,
    InputConfig,
    OutputConfig,
)
from .downloader import Downloader, HttpConfig, RateLimitConfig
from .manifest import Manifest, ManifestItem
from .parser import FieldConfig, ParsedItem, ParserConfig, ResponseParser

__all__ = [
    # Main crawler
    "Crawler",
    "CrawlerConfig",
    "CrawlResult",
    "DownloadResult",
    # Config classes
    "InputConfig",
    "OutputConfig",
    "CrawlOptions",
    "ParserConfig",
    "FieldConfig",
    "HttpConfig",
    "RateLimitConfig",
    # Parser
    "ResponseParser",
    "ParsedItem",
    # Downloader
    "Downloader",
    # Manifest
    "Manifest",
    "ManifestItem",
    # Checkpoint
    "Checkpoint",
    "FailedItem",
]

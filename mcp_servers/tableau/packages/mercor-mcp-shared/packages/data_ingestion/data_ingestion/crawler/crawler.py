"""Core crawler orchestration.

This module provides the main Crawler class that orchestrates
crawling JSON APIs and downloading files.
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import yaml

from ..checkpoint import Checkpoint
from ..exceptions import ConfigurationError, SourceError
from .downloader import Downloader, HttpConfig, RateLimitConfig
from .manifest import Manifest, ManifestItem
from .parser import ParsedItem, ParserConfig, ResponseParser

logger = logging.getLogger(__name__)


@dataclass
class CrawlOptions:
    """Options controlling crawl behavior.

    Attributes:
        follow_folders: Whether to recursively crawl folders
        max_depth: Maximum crawl depth (-1 for unlimited)
        file_extensions: Only include files with these extensions
        exclude_patterns: Glob patterns to exclude URLs
    """

    follow_folders: bool = True
    max_depth: int = -1
    file_extensions: list[str] = field(default_factory=list)
    exclude_patterns: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, config: dict | None) -> "CrawlOptions":
        """Create CrawlOptions from dictionary.

        Args:
            config: Dictionary with options configuration

        Returns:
            CrawlOptions instance
        """
        if not config:
            return cls()

        return cls(
            follow_folders=config.get("follow_folders", True),
            max_depth=config.get("max_depth", -1),
            file_extensions=config.get("file_extensions", []),
            exclude_patterns=config.get("exclude_patterns", []),
        )


@dataclass
class OutputConfig:
    """Output configuration.

    Attributes:
        mode: Output mode ("manifest" or "download")
        directory: Output directory path
        confirm_before_download: If True, show summary and prompt before downloading
    """

    mode: str = "manifest"
    directory: Path = field(default_factory=lambda: Path("./output"))
    confirm_before_download: bool = False

    def __post_init__(self):
        if self.mode not in ("manifest", "download"):
            raise ConfigurationError(
                f"Invalid output mode '{self.mode}'. Valid modes: manifest, download"
            )
        if isinstance(self.directory, str):
            self.directory = Path(self.directory)

    @classmethod
    def from_dict(cls, config: dict | None) -> "OutputConfig":
        """Create OutputConfig from dictionary.

        Args:
            config: Dictionary with output configuration

        Returns:
            OutputConfig instance
        """
        if not config:
            return cls()

        return cls(
            mode=config.get("mode", "manifest"),
            directory=Path(config.get("directory", "./output")),
            confirm_before_download=config.get("confirm_before_download", False),
        )


@dataclass
class InputConfig:
    """Input configuration.

    Attributes:
        input_type: Input type ("crawl" or "manifest")
        root_url: Root URL to start crawling (when type=crawl)
        manifest_file: Path to manifest file (when type=manifest)
        parser_config: Parser configuration (when type=crawl)
        options: Crawl options (when type=crawl)
        http_config: HTTP configuration (when type=crawl)
        rate_limit_config: Rate limit configuration (when type=crawl)
    """

    input_type: str = "crawl"
    root_url: str | None = None
    manifest_file: Path | None = None
    parser_config: ParserConfig | None = None
    options: CrawlOptions = field(default_factory=CrawlOptions)
    http_config: HttpConfig = field(default_factory=HttpConfig)
    rate_limit_config: RateLimitConfig = field(default_factory=RateLimitConfig)

    def __post_init__(self):
        if self.input_type not in ("crawl", "manifest"):
            raise ConfigurationError(
                f"Invalid input type '{self.input_type}'. Valid types: crawl, manifest"
            )

        if self.input_type == "crawl" and not self.root_url:
            raise ConfigurationError("input.root_url is required when input.type=crawl")

        if self.input_type == "crawl" and not self.parser_config:
            raise ConfigurationError("input.parser is required when input.type=crawl")

        if self.input_type == "manifest" and not self.manifest_file:
            raise ConfigurationError("input.file is required when input.type=manifest")

    @classmethod
    def from_dict(cls, config: dict) -> "InputConfig":
        """Create InputConfig from dictionary.

        Args:
            config: Dictionary with input configuration

        Returns:
            InputConfig instance

        Raises:
            ConfigurationError: If configuration is invalid
        """
        if not config:
            raise ConfigurationError("input config cannot be empty")

        input_type = config.get("type", "crawl")

        parser_config = None
        if "parser" in config:
            parser_config = ParserConfig.from_dict(config["parser"])

        manifest_file = None
        if "file" in config:
            manifest_file = Path(config["file"])

        return cls(
            input_type=input_type,
            root_url=config.get("root_url"),
            manifest_file=manifest_file,
            parser_config=parser_config,
            options=CrawlOptions.from_dict(config.get("options")),
            http_config=HttpConfig.from_dict(config.get("http")),
            rate_limit_config=RateLimitConfig.from_dict(
                config.get("options", {}).get("rate_limit") if config.get("options") else None
            ),
        )


@dataclass
class CrawlerConfig:
    """Complete crawler configuration.

    Attributes:
        input_config: Input configuration
        output_config: Output configuration
    """

    input_config: InputConfig
    output_config: OutputConfig

    @classmethod
    def from_dict(cls, config: dict) -> "CrawlerConfig":
        """Create CrawlerConfig from dictionary.

        Args:
            config: Dictionary with crawler configuration

        Returns:
            CrawlerConfig instance
        """
        if not isinstance(config, dict):
            raise ConfigurationError(
                "Configuration must be a YAML mapping, got empty or invalid file"
            )
        crawler_config = config.get("crawler", config)

        return cls(
            input_config=InputConfig.from_dict(crawler_config.get("input", {})),
            output_config=OutputConfig.from_dict(crawler_config.get("output")),
        )

    @classmethod
    def from_yaml(cls, config_path: Path) -> "CrawlerConfig":
        """Load CrawlerConfig from YAML file.

        Args:
            config_path: Path to YAML configuration file

        Returns:
            CrawlerConfig instance

        Raises:
            ConfigurationError: If file cannot be read or parsed
        """
        try:
            with open(config_path) as f:
                config = yaml.safe_load(f)
            return cls.from_dict(config)
        except FileNotFoundError:
            raise ConfigurationError(f"Config file not found: {config_path}")
        except yaml.YAMLError as e:
            raise ConfigurationError(f"Invalid YAML in {config_path}: {e}")


@dataclass
class CrawlResult:
    """Result of a crawl operation.

    Attributes:
        files: List of discovered files
        folders_crawled: Number of folders crawled
        files_discovered: Number of files discovered
        folders_excluded: Number of folders excluded by patterns
        errors: List of errors encountered during crawl
    """

    files: list[ParsedItem] = field(default_factory=list)
    folders_crawled: int = 0
    files_discovered: int = 0
    folders_excluded: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class DownloadResult:
    """Result of a download operation.

    Attributes:
        downloaded: Number of files successfully downloaded
        skipped: Number of files skipped (already on disk)
        failed: Number of files that failed to download
        total: Total number of files in manifest
        errors: List of error messages
    """

    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    total: int = 0
    errors: list[str] = field(default_factory=list)


class Crawler:
    """Config-driven web crawler.

    Orchestrates crawling JSON APIs and downloading files based on
    configuration. Supports two-phase operation:

    1. Crawl phase: Discover files from API, save manifest
    2. Download phase: Download files from manifest

    Example:
        >>> config = CrawlerConfig.from_yaml(Path("crawler_config.yaml"))
        >>> crawler = Crawler(config)
        >>> crawler.run()
    """

    def __init__(self, config: CrawlerConfig):
        """Initialize Crawler.

        Args:
            config: Crawler configuration
        """
        self.config = config
        self._downloader: Downloader | None = None
        self._parser: ResponseParser | None = None

    def run(self) -> None:
        """Run the crawler based on configuration.

        Executes the appropriate workflow based on input.type and output.mode:
        - crawl + manifest: Crawl and save manifest only
        - crawl + download: Crawl, save manifest, then download
        - manifest + download: Read manifest and download

        When input.type=crawl and output.mode=download, if an existing manifest
        is found, the user is prompted to choose between using the existing
        manifest (resume) or re-crawling.
        """
        input_config = self.config.input_config
        output_config = self.config.output_config

        # Ensure output directory exists
        output_config.directory.mkdir(parents=True, exist_ok=True)

        if input_config.input_type == "crawl":
            manifest_path = output_config.directory / "manifest.json"

            # If mode=download and manifest exists, ask user whether to re-crawl
            use_existing_manifest = False
            if output_config.mode == "download" and manifest_path.exists():
                existing_manifest = Manifest.load(manifest_path)
                use_existing_manifest = self._prompt_use_existing_manifest(
                    existing_manifest, manifest_path
                )

            if use_existing_manifest:
                manifest = existing_manifest
                logger.info(f"Using existing manifest: {len(manifest.files)} files")
            else:
                # Phase 1: Crawl and discover files
                logger.info(f"Starting crawl from: {input_config.root_url}")
                crawl_result = self.crawl()

                excluded_msg = (
                    f", {crawl_result.folders_excluded} excluded"
                    if crawl_result.folders_excluded > 0
                    else ""
                )
                logger.info(
                    f"Crawl complete: {crawl_result.files_discovered} files discovered, "
                    f"{crawl_result.folders_crawled} folders crawled{excluded_msg}"
                )

                if crawl_result.errors:
                    logger.warning(f"Crawl had {len(crawl_result.errors)} errors")

                # Save manifest
                manifest = self._create_manifest(crawl_result.files, root_url=input_config.root_url)
                manifest.save(manifest_path)
                logger.info(f"Manifest saved to: {manifest_path}")

            # Phase 2: Download if mode=download
            if output_config.mode == "download":
                if output_config.confirm_before_download:
                    if not self._prompt_confirm_download(manifest):
                        logger.info("Download cancelled by user. Manifest saved.")
                        return
                logger.info("Starting download phase...")
                self.download_all(manifest)

        elif input_config.input_type == "manifest":
            # Read existing manifest and download
            manifest_path = input_config.manifest_file
            logger.info(f"Loading manifest from: {manifest_path}")
            manifest = Manifest.load(manifest_path)

            logger.info(f"Manifest contains {len(manifest.files)} files")

            if output_config.mode == "download":
                if output_config.confirm_before_download:
                    if not self._prompt_confirm_download(manifest):
                        logger.info("Download cancelled by user.")
                        return
                logger.info("Starting download phase...")
                self.download_all(manifest)
            else:
                logger.warning("input.type=manifest with output.mode=manifest does nothing")

    @staticmethod
    def _prompt_use_existing_manifest(manifest: Manifest, manifest_path: Path) -> bool:
        """Prompt user to choose between existing manifest and re-crawling.

        Args:
            manifest: Existing manifest loaded from disk
            manifest_path: Path to the manifest file

        Returns:
            True if user wants to use existing manifest, False to re-crawl
        """
        print(f"\nExisting manifest found: {manifest_path}")
        print(f"  Files: {len(manifest.files)}")
        print(f"  Created: {manifest.created_at}")
        print()

        try:
            response = input("Use existing manifest? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return True

        return response in ("", "y", "yes")

    @staticmethod
    def _prompt_confirm_download(manifest: Manifest) -> bool:
        """Show download summary and prompt user to confirm.

        Args:
            manifest: Manifest with files to download

        Returns:
            True if user confirms, False to cancel
        """
        total_files = len(manifest.files)
        total_bytes = sum(f.size for f in manifest.files if f.size is not None)
        files_with_size = sum(1 for f in manifest.files if f.size is not None)

        print(f"\n{'=' * 50}")
        print("Download Summary")
        print(f"{'=' * 50}")
        print(f"  Files to download: {total_files:,}")

        if files_with_size > 0:
            if total_bytes >= 1_073_741_824:
                size_str = f"{total_bytes / 1_073_741_824:.1f} GB"
            elif total_bytes >= 1_048_576:
                size_str = f"{total_bytes / 1_048_576:.1f} MB"
            elif total_bytes >= 1_024:
                size_str = f"{total_bytes / 1_024:.1f} KB"
            else:
                size_str = f"{total_bytes} bytes"
            print(f"  Total size: {size_str}")

            if files_with_size < total_files:
                print(f"  (size known for {files_with_size:,} of {total_files:,} files)")
        else:
            print("  Total size: unknown")

        print(f"{'=' * 50}\n")

        try:
            response = input("Proceed with download? [Y/n]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return False

        return response in ("", "y", "yes")

    def crawl(self) -> CrawlResult:
        """Crawl from root_url and discover all files.

        Returns:
            CrawlResult with discovered files and statistics
        """
        input_config = self.config.input_config

        if input_config.input_type != "crawl":
            raise ConfigurationError("Cannot crawl when input.type is not 'crawl'")

        # Initialize parser and downloader
        self._parser = ResponseParser(input_config.parser_config)
        self._downloader = Downloader(
            input_config.http_config,
            input_config.rate_limit_config,
        )

        result = CrawlResult()
        options = input_config.options

        # BFS queue: (url, depth)
        queue: list[tuple[str, int]] = [(input_config.root_url, 0)]
        visited: set[str] = set()

        try:
            while queue:
                url, depth = queue.pop(0)

                # Skip if already visited
                if url in visited:
                    continue
                visited.add(url)

                # Check max depth
                if options.max_depth != -1 and depth > options.max_depth:
                    continue

                # Check exclude patterns
                if self._should_exclude(url, options.exclude_patterns):
                    result.folders_excluded += 1
                    logger.debug(f"Excluding URL: {url}")
                    continue

                try:
                    # Fetch and parse
                    response = self._downloader.fetch_json(url)
                    items = self._parser.parse(response)

                    for item in items:
                        if item.is_folder and options.follow_folders:
                            # Add folder to queue for further crawling
                            queue.append((item.url, depth + 1))
                        elif not item.is_folder:
                            # Check file extension filter
                            if self._should_include_file(item.name, options.file_extensions):
                                result.files.append(item)
                                result.files_discovered += 1

                    result.folders_crawled += 1

                    # Progress log every 10 folders at INFO level
                    if result.folders_crawled % 10 == 0:
                        logger.info(
                            f"Progress: {result.folders_crawled} folders crawled, "
                            f"{result.files_discovered} files found, {len(queue)} folders queued"
                        )

                    logger.debug(
                        f"Crawled: {url} - found {len(items)} items "
                        f"(depth={depth}, total_files={result.files_discovered})"
                    )

                except SourceError as e:
                    error_msg = f"Error crawling {url}: {e}"
                    logger.error(error_msg)
                    result.errors.append(error_msg)

        finally:
            if self._downloader:
                self._downloader.close()
                self._downloader = None

        return result

    def download_all(self, manifest: Manifest) -> DownloadResult:
        """Download all files from manifest with checkpoint-based resume.

        Supports resumable downloads: files already on disk with correct size
        are skipped. Progress is tracked via a checkpoint file so interrupted
        downloads can be resumed by re-running the same command.

        Args:
            manifest: Manifest containing files to download

        Returns:
            DownloadResult with download statistics
        """
        input_config = self.config.input_config
        output_config = self.config.output_config

        # Create downloader if not already created
        if not self._downloader:
            self._downloader = Downloader(
                input_config.http_config,
                input_config.rate_limit_config,
            )

        result = DownloadResult(total=len(manifest.files))
        data_dir = output_config.directory / "data"
        data_dir.mkdir(parents=True, exist_ok=True)

        # Load checkpoint for resume support
        checkpoint = Checkpoint(output_config.directory / "checkpoint.json")
        checkpoint.load()

        try:
            for item in manifest.files:
                # Compute local path preserving URL directory structure
                parsed_url = urlparse(item.url)
                relative_path = parsed_url.path.lstrip("/")
                local_path = (data_dir / relative_path).resolve()
                filename = local_path.name

                # Prevent path traversal attacks
                if not str(local_path).startswith(str(data_dir.resolve())):
                    logger.warning(f"Skipping path traversal attempt: {item.url}")
                    result.failed += 1
                    continue

                # Decide whether to skip, delete-and-redownload, or download
                action = self._decide_download_action(
                    relative_path, local_path, item.size, checkpoint
                )

                if action == "skip":
                    result.skipped += 1
                    logger.debug(f"Skipped (exists): {filename}")
                    continue

                if action == "redownload":
                    # Remove partial/mismatched file
                    try:
                        local_path.unlink()
                        logger.debug(f"Removed partial file: {filename}")
                    except OSError:
                        pass
                    # Also remove stale checkpoint entry
                    checkpoint.remove_completed(relative_path)

                # Download the file
                try:
                    local_path.parent.mkdir(parents=True, exist_ok=True)
                    self._downloader.download_file(item.url, local_path)

                    # Verify downloaded size matches manifest if size is known
                    actual_size = local_path.stat().st_size
                    if item.size is not None and actual_size != item.size:
                        logger.warning(
                            f"Size mismatch for {filename}: expected {item.size}, got {actual_size}"
                        )

                    checkpoint.mark_completed(relative_path, {"size": actual_size})
                    result.downloaded += 1

                    # Progress log every 10 downloaded files
                    if result.downloaded % 10 == 0:
                        logger.info(
                            f"Progress: {result.downloaded} downloaded, "
                            f"{result.skipped} skipped, "
                            f"{result.failed} failed / {result.total} total"
                        )

                    # Flush checkpoint periodically
                    if checkpoint.should_flush(interval=10):
                        checkpoint.flush()

                    logger.debug(f"Downloaded: {filename}")

                except SourceError as e:
                    error_msg = f"Failed to download {item.url}: {e}"
                    logger.error(error_msg)
                    result.errors.append(error_msg)
                    result.failed += 1

                    checkpoint.mark_failed(relative_path, str(e), context={"url": item.url})
                    # Always flush on failure so error is recorded
                    checkpoint.flush(force=True)

                    # Clean up partial file if it was created
                    if local_path.exists():
                        try:
                            local_path.unlink()
                        except OSError:
                            pass

        finally:
            # Final checkpoint flush and cleanup
            checkpoint.flush(force=True)
            if self._downloader:
                self._downloader.close()
                self._downloader = None

        # Log summary
        logger.info("=" * 50)
        logger.info("Download summary:")
        logger.info(f"  Total in manifest:     {result.total}")
        logger.info(f"  Downloaded this run:    {result.downloaded}")
        logger.info(f"  Skipped (already done): {result.skipped}")
        logger.info(f"  Failed this run:        {result.failed}")
        logger.info(f"  Total on disk:          {result.skipped + result.downloaded}")
        logger.info("=" * 50)

        return result

    @staticmethod
    def _decide_download_action(
        relative_path: str,
        local_path: Path,
        manifest_size: int | None,
        checkpoint: Checkpoint,
    ) -> str:
        """Decide whether to skip, redownload, or download a file.

        Decision matrix:
        - Checkpoint completed + file exists + size matches manifest → skip
        - Checkpoint completed + file exists + no manifest size → skip (trust checkpoint)
        - Checkpoint completed + file missing from disk → download (was deleted)
        - File exists + not in checkpoint → redownload (partial from interrupted run)
        - File exists + in checkpoint + size mismatch → redownload
        - File doesn't exist + not in checkpoint → download

        Args:
            relative_path: Relative path key for checkpoint lookup
            local_path: Local file path on disk
            manifest_size: Expected file size from manifest (may be None)
            checkpoint: Checkpoint instance

        Returns:
            One of "skip", "redownload", or "download"
        """
        in_checkpoint = checkpoint.is_completed(relative_path)

        try:
            local_size = local_path.stat().st_size
            file_exists = True
        except FileNotFoundError:
            file_exists = False
            local_size = None

        if in_checkpoint and file_exists:
            if manifest_size is not None:
                if local_size == manifest_size:
                    return "skip"
                else:
                    # Size mismatch — file is corrupted or partial
                    return "redownload"
            else:
                # No manifest size to verify, trust the checkpoint
                return "skip"

        if in_checkpoint and not file_exists:
            # Was in checkpoint but file was deleted from disk
            checkpoint.remove_completed(relative_path)
            return "download"

        if file_exists and not in_checkpoint:
            # File exists but wasn't checkpointed — likely partial from crash
            return "redownload"

        return "download"

    def _create_manifest(self, files: list[ParsedItem], root_url: str | None = None) -> Manifest:
        """Create manifest from parsed items.

        Args:
            files: List of parsed file items
            root_url: Root URL that was crawled

        Returns:
            Manifest instance
        """
        manifest_items = [
            ManifestItem(
                name=f.name,
                url=f.url,
                size=f.size,
                last_modified=f.last_modified,
            )
            for f in files
        ]
        return Manifest(files=manifest_items, root_url=root_url)

    def _should_exclude(self, url: str, patterns: list[str]) -> bool:
        """Check if URL should be excluded based on patterns.

        Args:
            url: URL to check
            patterns: List of regex patterns (searched anywhere in URL)

        Returns:
            True if URL should be excluded
        """
        for pattern in patterns:
            if re.search(pattern, url):
                return True
        return False

    def _should_include_file(self, filename: str, extensions: list[str]) -> bool:
        """Check if file should be included based on extension filter.

        Args:
            filename: Filename to check
            extensions: List of allowed extensions (empty = all)

        Returns:
            True if file should be included
        """
        if not extensions:
            return True

        for ext in extensions:
            if filename.lower().endswith(ext.lower()):
                return True
        return False

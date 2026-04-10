"""Seed pipeline that orchestrates seeding operations."""

from dataclasses import dataclass, field

from .fetchers import BaseFetcher
from .loaders import BaseLoader
from .seeders import HistoricalSeeder, IntradaySeeder, ProfileSeeder
from .storage import RawStorage


@dataclass
class PipelineStats:
    """Statistics from a pipeline run."""

    historical_rows: int = 0
    intraday_rows: int = 0
    profiles_count: int = 0
    raw_files_saved: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return self.historical_rows + self.intraday_rows + self.profiles_count


class SeedPipeline:
    """Thin orchestrator that delegates to specialized seeders.

    This class simply coordinates which seeders to run and collects stats.
    All seeding logic lives in the individual seeder classes.
    """

    def __init__(
        self,
        fetcher: BaseFetcher | None,
        storage: RawStorage,
        loader: BaseLoader,
    ):
        """Initialize the pipeline.

        Args:
            fetcher: Data fetcher (can be None if only loading from raw files)
            storage: Raw data storage
            loader: Data loader
        """
        self.fetcher = fetcher
        self.storage = storage
        self.loader = loader

        # Create seeders
        self.historical_seeder = HistoricalSeeder(fetcher, storage, loader)
        self.intraday_seeder = IntradaySeeder(fetcher, storage, loader)
        self.profile_seeder = ProfileSeeder(fetcher, storage, loader)

    def run(
        self,
        symbols: list[str],
        historical_config: dict | None = None,
        intraday_config: dict | None = None,
        profiles_config: dict | None = None,
        save_raw: bool = True,
        from_raw: bool = False,
        raw_only: bool = False,
        force: bool = False,
        verbose: bool = True,
    ) -> PipelineStats:
        """Run the full seeding pipeline.

        Args:
            symbols: List of stock symbols to seed
            historical_config: Config for historical seeding (enabled, days)
            intraday_config: Config for intraday seeding (enabled, intervals)
            profiles_config: Config for profile seeding (enabled)
            save_raw: Whether to save raw JSON files
            from_raw: Load from existing raw files instead of API
            raw_only: Only save raw files, don't load to DB
            force: Force re-fetch even if data exists
            verbose: Print progress

        Returns:
            Statistics from the pipeline run
        """
        stats = PipelineStats()

        historical_config = historical_config or {}
        intraday_config = intraday_config or {}
        profiles_config = profiles_config or {}

        if profiles_config.get("enabled", False):
            stats.profiles_count = self.profile_seeder.seed(
                symbols,
                save_raw=save_raw,
                from_raw=from_raw,
                raw_only=raw_only,
                force=force,
                verbose=verbose,
            )

        if historical_config.get("enabled", False):
            stats.historical_rows = self.historical_seeder.seed(
                symbols,
                days=historical_config.get("days", 90),
                save_raw=save_raw,
                from_raw=from_raw,
                raw_only=raw_only,
                force=force,
                verbose=verbose,
            )

        if intraday_config.get("enabled", False):
            stats.intraday_rows = self.intraday_seeder.seed(
                symbols,
                intervals=intraday_config.get("intervals", ["5min"]),
                save_raw=save_raw,
                from_raw=from_raw,
                raw_only=raw_only,
                force=force,
                verbose=verbose,
            )

        return stats

    # Convenience methods for seeding individual data types
    def seed_historical(
        self,
        symbols: list[str],
        days: int = 90,
        save_raw: bool = True,
        from_raw: bool = False,
        raw_only: bool = False,
        force: bool = False,
        verbose: bool = True,
    ) -> int:
        """Seed historical data for all symbols."""
        return self.historical_seeder.seed(
            symbols,
            days=days,
            save_raw=save_raw,
            from_raw=from_raw,
            raw_only=raw_only,
            force=force,
            verbose=verbose,
        )

    def seed_intraday(
        self,
        symbols: list[str],
        intervals: list[str] | None = None,
        save_raw: bool = True,
        from_raw: bool = False,
        raw_only: bool = False,
        force: bool = False,
        verbose: bool = True,
    ) -> int:
        """Seed intraday data for all symbols and intervals."""
        return self.intraday_seeder.seed(
            symbols,
            intervals=intervals,
            save_raw=save_raw,
            from_raw=from_raw,
            raw_only=raw_only,
            force=force,
            verbose=verbose,
        )

    def seed_profiles(
        self,
        symbols: list[str],
        save_raw: bool = True,
        from_raw: bool = False,
        raw_only: bool = False,
        force: bool = False,
        verbose: bool = True,
    ) -> int:
        """Seed company profiles for all symbols."""
        return self.profile_seeder.seed(
            symbols,
            save_raw=save_raw,
            from_raw=from_raw,
            raw_only=raw_only,
            force=force,
            verbose=verbose,
        )

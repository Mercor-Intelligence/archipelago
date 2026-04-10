"""Company profiles seeder."""

import logging

from .base import BaseSeeder

logger = logging.getLogger(__name__)


class ProfileSeeder(BaseSeeder):
    """Seeds company profile data."""

    def seed(
        self,
        symbols: list[str],
        save_raw: bool = True,
        from_raw: bool = False,
        raw_only: bool = False,
        force: bool = False,
        verbose: bool = True,
        **kwargs,
    ) -> int:
        """Seed company profiles for all symbols.

        Args:
            symbols: List of stock symbols
            save_raw: Whether to save raw JSON files
            from_raw: Load from existing raw files instead of API
            raw_only: Only save raw files, don't load to DB
            force: Force re-fetch even if data exists
            verbose: Print progress

        Returns:
            Total profiles inserted
        """
        if verbose:
            mode = "RAW ONLY" if raw_only else f"{len(symbols)} symbols"
            logger.info(f"=== Company Profiles ({mode}) ===")

        # Determine which symbols need profiles
        symbols_to_process = self._get_missing_symbols(symbols, force, raw_only, from_raw, verbose)
        if not symbols_to_process:
            return 0

        try:
            if from_raw:
                return self._load_from_raw(verbose)

            return self._fetch_and_process(symbols_to_process, save_raw, raw_only, verbose)
        except Exception as e:
            if verbose:
                logger.error(f"  error: {e}")
            return 0

    def _get_missing_symbols(
        self,
        symbols: list[str],
        force: bool,
        raw_only: bool,
        from_raw: bool,
        verbose: bool,
    ) -> list[str]:
        """Get list of symbols that need profile fetching."""
        if force or raw_only or from_raw:
            return symbols

        existing = self.loader.get_profile_symbols()
        missing = [s for s in symbols if s not in existing]

        if not missing:
            if verbose:
                logger.info("  Skipped (all profiles exist)")
            return []

        if verbose:
            logger.info(f"  {len(missing)} profiles missing")
        return missing

    def _load_from_raw(self, verbose: bool) -> int:
        """Load profiles from raw storage."""
        raw_data = self.storage.load("profiles")
        if not raw_data:
            if verbose:
                logger.warning("  No raw profiles file")
            return 0

        count = self.loader.load_profiles_batch(raw_data)

        if verbose:
            logger.info(f"  Inserted {count} profiles (raw)")
        return count

    def _fetch_and_process(
        self,
        symbols: list[str],
        save_raw: bool,
        raw_only: bool,
        verbose: bool,
    ) -> int:
        """Fetch profiles from API and process them."""
        fetcher = self._require_fetcher()
        raw_data = fetcher.fetch_profiles_batch(symbols)
        profiles_count = len(raw_data.get("data", {}).get("profiles", []))

        if verbose:
            logger.info(f"  Fetched {profiles_count} profiles from API")

        # Save raw data
        if save_raw or raw_only:
            self.storage.save("profiles", raw_data)

        # Handle raw_only mode
        if raw_only:
            if verbose:
                logger.info(f"  Saved {profiles_count} profiles (raw only)")
            return 0

        # Load to database
        count = self.loader.load_profiles_batch(raw_data)

        if verbose:
            logger.info(f"  Inserted {count} profiles (api)")
        return count

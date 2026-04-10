"""Intraday data seeder."""

import logging
from datetime import datetime

from .base import BaseSeeder, SymbolResult

logger = logging.getLogger(__name__)


class IntradaySeeder(BaseSeeder):
    """Seeds intraday bar data."""

    def seed(
        self,
        symbols: list[str],
        save_raw: bool = True,
        from_raw: bool = False,
        raw_only: bool = False,
        force: bool = False,
        verbose: bool = True,
        intervals: list[str] | None = None,
        **kwargs,
    ) -> int:
        """Seed intraday data for all symbols and intervals.

        Args:
            symbols: List of stock symbols
            intervals: List of intervals (e.g., ["5min", "15min"])
            save_raw: Whether to save raw JSON files
            from_raw: Load from existing raw files instead of API
            raw_only: Only save raw files, don't load to DB
            force: Force re-fetch even if data exists
            verbose: Print progress

        Returns:
            Total rows inserted
        """
        if intervals is None:
            intervals = ["5min"]

        if verbose:
            mode = "RAW ONLY" if raw_only else ", ".join(intervals)
            logger.info(f"=== Intraday Bars ({mode}) ===")

        total_rows = 0
        for symbol in symbols:
            for interval in intervals:
                _result, rows, msg = self._process_symbol(
                    symbol, interval, save_raw, from_raw, raw_only, force
                )
                total_rows += rows

                if verbose:
                    logger.info(f"  {symbol} @ {interval}: {msg}")

        return total_rows

    def _process_symbol(
        self,
        symbol: str,
        interval: str,
        save_raw: bool,
        from_raw: bool,
        raw_only: bool,
        force: bool,
    ) -> tuple[SymbolResult, int, str]:
        """Process intraday data for a single symbol/interval."""
        try:
            if from_raw:
                return self._load_from_raw(symbol, interval)

            return self._fetch_and_process(symbol, interval, save_raw, raw_only, force)
        except Exception as e:
            return SymbolResult.ERROR, 0, f"error: {e}"

    def _load_from_raw(self, symbol: str, interval: str) -> tuple[SymbolResult, int, str]:
        """Load intraday data from raw storage."""
        raw_data = self.storage.load("intraday", symbol, interval)
        if not raw_data:
            return SymbolResult.NO_DATA, 0, "no raw file"

        inserted = self.loader.load_intraday(symbol, interval, raw_data)
        return SymbolResult.SUCCESS, inserted, f"{inserted} rows (raw)"

    def _fetch_and_process(
        self,
        symbol: str,
        interval: str,
        save_raw: bool,
        raw_only: bool,
        force: bool,
    ) -> tuple[SymbolResult, int, str]:
        """Fetch intraday data from API and process it."""
        # Determine fetch range
        from_ts, last_ts = self._get_fetch_range(symbol, interval, raw_only, force)

        # Fetch from API
        fetcher = self._require_fetcher()
        raw_data = fetcher.fetch_intraday(symbol, interval, from_timestamp=from_ts)

        # Check for empty response
        bars = raw_data.get("data", {}).get("bars", [])
        if not bars:
            if from_ts:
                return SymbolResult.SKIPPED, 0, f"up to date (last: {last_ts})"
            return SymbolResult.NO_DATA, 0, "no data"

        # Save raw data (may merge with existing)
        if save_raw or raw_only:
            if self.storage.exists("intraday", symbol, interval):
                raw_data = self.storage.merge_intraday(symbol, interval, raw_data)
            self.storage.save("intraday", raw_data, symbol, interval)

        # Handle raw_only mode
        if raw_only:
            bar_count = len(raw_data.get("data", {}).get("bars", []))
            new_count = len(bars)
            if from_ts:
                return (
                    SymbolResult.SUCCESS,
                    0,
                    f"+{new_count} bars, {bar_count} total (raw only)",
                )
            return SymbolResult.SUCCESS, 0, f"saved {bar_count} bars (raw only)"

        # Load to database
        inserted = self.loader.load_intraday(symbol, interval, raw_data)
        mode = "incremental" if from_ts else ""
        msg = f"{inserted} rows (api{', ' + mode if mode else ''})"
        return SymbolResult.SUCCESS, inserted, msg

    def _get_fetch_range(
        self, symbol: str, interval: str, raw_only: bool, force: bool
    ) -> tuple[datetime | None, datetime | None]:
        """Determine the timestamp range to fetch."""
        if force:
            return None, None

        if raw_only:
            raw_last = self.storage.get_last_intraday_timestamp(symbol, interval)
            db_last = self.loader.get_last_intraday_timestamp(symbol, interval)
            if raw_last and db_last:
                last_ts = max(raw_last, db_last)
            else:
                last_ts = raw_last or db_last
        else:
            last_ts = self.loader.get_last_intraday_timestamp(symbol, interval)

        return last_ts, last_ts

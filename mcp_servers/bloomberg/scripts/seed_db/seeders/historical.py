"""Historical data seeder."""

import logging
from datetime import date, timedelta

from .base import BaseSeeder, SymbolResult

logger = logging.getLogger(__name__)


class HistoricalSeeder(BaseSeeder):
    """Seeds historical daily price data."""

    def seed(
        self,
        symbols: list[str],
        save_raw: bool = True,
        from_raw: bool = False,
        raw_only: bool = False,
        force: bool = False,
        verbose: bool = True,
        days: int = 90,
        **kwargs,
    ) -> int:
        """Seed historical data for all symbols.

        Args:
            symbols: List of stock symbols
            days: Number of days of history to fetch
            save_raw: Whether to save raw JSON files
            from_raw: Load from existing raw files instead of API
            raw_only: Only save raw files, don't load to DB
            force: Force re-fetch even if data exists
            verbose: Print progress

        Returns:
            Total rows inserted
        """
        if verbose:
            mode = "RAW ONLY" if raw_only else f"{days} days"
            logger.info(f"=== Historical Data ({mode}) ===")

        total_rows = 0
        for symbol in symbols:
            _result, rows, msg = self._process_symbol(
                symbol, days, save_raw, from_raw, raw_only, force
            )
            total_rows += rows

            if verbose:
                logger.info(f"  {symbol}: {msg}")

        return total_rows

    def _process_symbol(
        self,
        symbol: str,
        days: int,
        save_raw: bool,
        from_raw: bool,
        raw_only: bool,
        force: bool,
    ) -> tuple[SymbolResult, int, str]:
        """Process historical data for a single symbol."""
        try:
            if from_raw:
                return self._load_from_raw(symbol)

            return self._fetch_and_process(symbol, days, save_raw, raw_only, force)
        except Exception as e:
            return SymbolResult.ERROR, 0, f"error: {e}"

    def _load_from_raw(self, symbol: str) -> tuple[SymbolResult, int, str]:
        """Load historical data from raw storage."""
        raw_data = self.storage.load("historical", symbol)
        if not raw_data:
            return SymbolResult.NO_DATA, 0, "no raw file"

        inserted = self.loader.load_historical(symbol, raw_data)
        return SymbolResult.SUCCESS, inserted, f"{inserted} rows (raw)"

    def _fetch_and_process(
        self,
        symbol: str,
        days: int,
        save_raw: bool,
        raw_only: bool,
        force: bool,
    ) -> tuple[SymbolResult, int, str]:
        """Fetch historical data from API and process it."""
        # Determine fetch range
        from_date, last_date = self._get_fetch_range(symbol, raw_only, force)

        # Check if up to date
        if last_date is not None and last_date >= date.today():
            return SymbolResult.SKIPPED, 0, f"up to date (last: {last_date})"

        # Fetch from API
        fetcher = self._require_fetcher()
        raw_data = fetcher.fetch_historical(
            symbol,
            days=days if from_date is None else None,
            from_date=from_date,
        )

        # Check for empty response
        historical = raw_data.get("data", {}).get("historical", [])
        if not historical:
            if from_date:
                return SymbolResult.SKIPPED, 0, f"up to date (last: {last_date})"
            return SymbolResult.NO_DATA, 0, "no data"

        # Save raw data (may merge with existing)
        if save_raw or raw_only:
            if self.storage.exists("historical", symbol):
                raw_data = self.storage.merge_historical(symbol, raw_data)
            self.storage.save("historical", raw_data, symbol)

        # Handle raw_only mode
        if raw_only:
            row_count = len(raw_data.get("data", {}).get("historical", []))
            new_count = len(historical)
            if from_date:
                return (
                    SymbolResult.SUCCESS,
                    0,
                    f"+{new_count} rows, {row_count} total (raw only)",
                )
            return SymbolResult.SUCCESS, 0, f"saved {row_count} rows (raw only)"

        # Load to database
        inserted = self.loader.load_historical(symbol, raw_data)
        mode = "incremental" if from_date else ""
        msg = f"{inserted} rows (api{', ' + mode if mode else ''})"
        return SymbolResult.SUCCESS, inserted, msg

    def _get_fetch_range(
        self, symbol: str, raw_only: bool, force: bool
    ) -> tuple[date | None, date | None]:
        """Determine the date range to fetch."""
        if force:
            return None, None

        if raw_only:
            raw_last = self.storage.get_last_historical_date(symbol)
            db_last = self.loader.get_last_historical_date(symbol)
            if raw_last and db_last:
                last_date = max(raw_last, db_last)
            else:
                last_date = raw_last or db_last
        else:
            last_date = self.loader.get_last_historical_date(symbol)

        if last_date is None:
            return None, None

        from_date = last_date + timedelta(days=1)
        return from_date, last_date

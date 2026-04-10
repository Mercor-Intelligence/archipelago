"""Raw data storage for JSON files."""

import json
from datetime import date, datetime
from pathlib import Path


class RawStorage:
    """Handles saving and loading raw API responses as JSON files.

    Directory structure:
        base_path/
        ├── historical/
        │   ├── AAPL.json
        │   └── MSFT.json
        ├── intraday/
        │   ├── 5min/
        │   │   ├── AAPL.json
        │   │   └── MSFT.json
        │   ├── 15min/
        │   └── 1hour/
        └── profiles/
            └── profiles.json
    """

    def __init__(self, base_path: str | Path):
        self.base_path = Path(base_path)

    def _get_path(
        self, data_type: str, symbol: str | None = None, interval: str | None = None
    ) -> Path:
        """Get the file path for a given data type and symbol.

        Args:
            data_type: One of 'historical', 'intraday', 'profiles'
            symbol: Stock symbol (not needed for batch profiles)
            interval: Interval for intraday data (e.g., '5min')

        Returns:
            Path to the JSON file
        """
        if data_type == "historical":
            return self.base_path / "historical" / f"{symbol}.json"
        elif data_type == "intraday":
            if not interval:
                raise ValueError("interval required for intraday data")
            return self.base_path / "intraday" / interval / f"{symbol}.json"
        elif data_type == "profiles":
            if symbol:
                return self.base_path / "profiles" / f"{symbol}.json"
            return self.base_path / "profiles" / "profiles.json"
        else:
            raise ValueError(f"Unknown data_type: {data_type}")

    def save(
        self,
        data_type: str,
        data: dict,
        symbol: str | None = None,
        interval: str | None = None,
    ) -> Path:
        """Save raw data to a JSON file.

        Args:
            data_type: One of 'historical', 'intraday', 'profiles'
            data: Raw API response dict to save
            symbol: Stock symbol (optional for batch profiles)
            interval: Interval for intraday data

        Returns:
            Path to the saved file
        """
        path = self._get_path(data_type, symbol, interval)
        path.parent.mkdir(parents=True, exist_ok=True)

        with open(path, "w") as f:
            json.dump(data, f, indent=2)

        return path

    def load(
        self,
        data_type: str,
        symbol: str | None = None,
        interval: str | None = None,
    ) -> dict | None:
        """Load raw data from a JSON file.

        Args:
            data_type: One of 'historical', 'intraday', 'profiles'
            symbol: Stock symbol (optional for batch profiles)
            interval: Interval for intraday data

        Returns:
            Raw data dict or None if file doesn't exist
        """
        path = self._get_path(data_type, symbol, interval)

        if not path.exists():
            return None

        with open(path) as f:
            return json.load(f)

    def exists(
        self,
        data_type: str,
        symbol: str | None = None,
        interval: str | None = None,
    ) -> bool:
        """Check if raw data exists for a given data type and symbol.

        Args:
            data_type: One of 'historical', 'intraday', 'profiles'
            symbol: Stock symbol (optional for batch profiles)
            interval: Interval for intraday data

        Returns:
            True if the file exists
        """
        path = self._get_path(data_type, symbol, interval)
        return path.exists()

    def list_symbols(self, data_type: str, interval: str | None = None) -> list[str]:
        """List all symbols that have raw data saved.

        Args:
            data_type: One of 'historical', 'intraday', 'profiles'
            interval: Interval for intraday data

        Returns:
            List of symbols with saved data
        """
        if data_type == "historical":
            dir_path = self.base_path / "historical"
        elif data_type == "intraday":
            if not interval:
                raise ValueError("interval required for intraday data")
            dir_path = self.base_path / "intraday" / interval
        elif data_type == "profiles":
            dir_path = self.base_path / "profiles"
        else:
            raise ValueError(f"Unknown data_type: {data_type}")

        if not dir_path.exists():
            return []

        # Get all .json files and extract symbol from filename
        symbols = []
        for path in dir_path.glob("*.json"):
            # Skip batch profiles file
            if path.stem == "profiles":
                continue
            symbols.append(path.stem)

        return sorted(symbols)

    def get_metadata(
        self, data_type: str, symbol: str | None = None, interval: str | None = None
    ) -> dict | None:
        """Get metadata from a saved raw file without loading full data.

        Args:
            data_type: One of 'historical', 'intraday', 'profiles'
            symbol: Stock symbol
            interval: Interval for intraday data

        Returns:
            Dict with source, endpoint, fetched_at, etc. or None
        """
        data = self.load(data_type, symbol, interval)
        if not data:
            return None

        # Return metadata fields only
        return {
            "source": data.get("source"),
            "endpoint": data.get("endpoint"),
            "symbol": data.get("symbol"),
            "fetched_at": data.get("fetched_at"),
            "params": data.get("params"),
        }

    def delete(
        self,
        data_type: str,
        symbol: str | None = None,
        interval: str | None = None,
    ) -> bool:
        """Delete a raw data file.

        Args:
            data_type: One of 'historical', 'intraday', 'profiles'
            symbol: Stock symbol
            interval: Interval for intraday data

        Returns:
            True if file was deleted, False if it didn't exist
        """
        path = self._get_path(data_type, symbol, interval)

        if path.exists():
            path.unlink()
            return True
        return False

    def clear_all(self) -> int:
        """Delete all raw data files.

        Returns:
            Number of files deleted
        """
        count = 0
        for path in self.base_path.rglob("*.json"):
            path.unlink()
            count += 1
        return count

    def get_last_historical_date(self, symbol: str) -> date | None:
        """Get the most recent date from a historical raw file.

        Args:
            symbol: Stock symbol

        Returns:
            Most recent date or None if file doesn't exist or has no data
        """
        raw_data = self.load("historical", symbol)
        if not raw_data:
            return None

        historical = raw_data.get("data", {}).get("historical", [])
        if not historical:
            return None

        # Find the max date
        dates = [row["date"] for row in historical]
        if not dates:
            return None

        max_date_str = max(dates)
        return datetime.fromisoformat(max_date_str).date()

    def get_last_intraday_timestamp(self, symbol: str, interval: str) -> datetime | None:
        """Get the most recent timestamp from an intraday raw file.

        Args:
            symbol: Stock symbol
            interval: Bar interval (e.g., "5min")

        Returns:
            Most recent timestamp or None if file doesn't exist or has no data
        """
        raw_data = self.load("intraday", symbol, interval)
        if not raw_data:
            return None

        bars = raw_data.get("data", {}).get("bars", [])
        if not bars:
            return None

        # Find the max timestamp
        timestamps = [row["date"] for row in bars]
        if not timestamps:
            return None

        max_ts_str = max(timestamps)
        return datetime.fromisoformat(max_ts_str)

    def merge_historical(self, symbol: str, new_data: dict) -> dict:
        """Merge new historical data with existing raw file data.

        Args:
            symbol: Stock symbol
            new_data: New raw data dict to merge

        Returns:
            Merged raw data dict
        """
        existing = self.load("historical", symbol)
        if not existing:
            return new_data

        # Get existing and new rows
        existing_rows = existing.get("data", {}).get("historical", [])
        new_rows = new_data.get("data", {}).get("historical", [])

        # Create a dict keyed by date for deduplication
        rows_by_date = {row["date"]: row for row in existing_rows}

        # Add/update with new rows
        for row in new_rows:
            rows_by_date[row["date"]] = row

        # Sort by date descending (most recent first, like FMP returns)
        merged_rows = sorted(rows_by_date.values(), key=lambda x: x["date"], reverse=True)

        # Create merged result with updated metadata
        merged = {
            "source": new_data.get("source", existing.get("source")),
            "endpoint": new_data.get("endpoint", existing.get("endpoint")),
            "symbol": symbol,
            "params": {
                "merged": True,
                "total_rows": len(merged_rows),
            },
            "fetched_at": new_data.get("fetched_at", datetime.now().isoformat()),
            "data": {
                "symbol": symbol,
                "historical": merged_rows,
            },
        }

        return merged

    def merge_intraday(self, symbol: str, interval: str, new_data: dict) -> dict:
        """Merge new intraday data with existing raw file data.

        Args:
            symbol: Stock symbol
            interval: Bar interval (e.g., "5min")
            new_data: New raw data dict to merge

        Returns:
            Merged raw data dict
        """
        existing = self.load("intraday", symbol, interval)
        if not existing:
            return new_data

        # Get existing and new bars
        existing_bars = existing.get("data", {}).get("bars", [])
        new_bars = new_data.get("data", {}).get("bars", [])

        # Create a dict keyed by timestamp for deduplication
        bars_by_ts = {bar["date"]: bar for bar in existing_bars}

        # Add/update with new bars
        for bar in new_bars:
            bars_by_ts[bar["date"]] = bar

        # Sort by timestamp descending (most recent first)
        merged_bars = sorted(bars_by_ts.values(), key=lambda x: x["date"], reverse=True)

        # Create merged result with updated metadata
        merged = {
            "source": new_data.get("source", existing.get("source")),
            "endpoint": new_data.get("endpoint", existing.get("endpoint")),
            "symbol": symbol,
            "params": {
                "interval": interval,
                "merged": True,
                "total_bars": len(merged_bars),
            },
            "fetched_at": new_data.get("fetched_at", datetime.now().isoformat()),
            "data": {
                "bars": merged_bars,
            },
        }

        return merged

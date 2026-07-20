"""Generic checkpoint for tracking progress of multi-item operations.

This module provides a reusable checkpoint mechanism that can track progress
of any multi-item operation (downloads, ingestion, batch processing) with
resume support. Items are identified by string keys and can carry arbitrary
metadata.

Example (download):
    >>> cp = Checkpoint(Path("./data/checkpoint.json"))
    >>> cp.load()
    >>> cp.mark_completed("bulkdata/PLAW/119/file.xml", {"size": 23000})
    >>> cp.flush()

Example (ingestion):
    >>> cp = Checkpoint(Path("./data/ingest_checkpoint.json"))
    >>> cp.load()
    >>> cp.mark_completed("PLAW/119/file.xml", {"records_parsed": 45, "records_inserted": 45})
    >>> cp.flush()
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class FailedItem:
    """Record of a failed processing attempt.

    Attributes:
        item_id: Unique identifier for the item that failed
        error: Error message describing the failure
        timestamp: ISO timestamp of when the failure occurred
        context: Optional extra information (url, line number, batch range, etc.)
    """

    item_id: str
    error: str
    timestamp: str
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        result = {
            "item_id": self.item_id,
            "error": self.error,
            "timestamp": self.timestamp,
        }
        if self.context:
            result["context"] = self.context
        return result

    @classmethod
    def from_dict(cls, data: dict) -> "FailedItem":
        """Create FailedItem from dictionary."""
        return cls(
            item_id=data["item_id"],
            error=data.get("error", ""),
            timestamp=data.get("timestamp", ""),
            context=data.get("context", {}),
        )


class Checkpoint:
    """Tracks progress of any multi-item operation with resume support.

    Maintains a checkpoint file that records which items have been
    successfully processed and which have failed. On resume, previously
    completed items are skipped and failed items are retried.

    The checkpoint file is a JSON file with this structure:
        {
            "completed": {
                "item_id_1": {"size": 23000},
                "item_id_2": {"records_parsed": 45}
            },
            "failed": [
                {"item_id": "...", "error": "...", "timestamp": "...", "context": {...}}
            ],
            "last_updated": "2025-01-01T00:00:00+00:00"
        }
    """

    def __init__(self, checkpoint_path: Path):
        """Initialize Checkpoint.

        Args:
            checkpoint_path: Path to the checkpoint JSON file
        """
        self._path = checkpoint_path
        self._completed: dict[str, dict[str, Any]] = {}
        self._failed: list[FailedItem] = []
        self._dirty_count = 0

    @property
    def completed(self) -> dict[str, dict[str, Any]]:
        """Dictionary mapping item IDs to their metadata."""
        return self._completed

    @property
    def failed(self) -> list[FailedItem]:
        """List of failed item records."""
        return self._failed

    @property
    def path(self) -> Path:
        """Path to the checkpoint file."""
        return self._path

    def load(self) -> None:
        """Load checkpoint from disk.

        If the checkpoint file doesn't exist, starts with empty state.
        If the file is corrupted, logs a warning and starts fresh.
        """
        if not self._path.exists():
            logger.debug(f"No checkpoint file found at {self._path}, starting fresh")
            return

        try:
            with open(self._path) as f:
                data = json.load(f)

            self._completed = data.get("completed", {})
            self._failed = [FailedItem.from_dict(item) for item in data.get("failed", [])]

            logger.info(
                f"Loaded checkpoint: {len(self._completed)} completed, "
                f"{len(self._failed)} previously failed"
            )

        except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as e:
            logger.warning(f"Corrupted checkpoint file at {self._path}: {e}. Starting fresh.")
            self._completed = {}
            self._failed = []
        except OSError as e:
            logger.warning(f"Could not read checkpoint file at {self._path}: {e}. Starting fresh.")
            self._completed = {}
            self._failed = []

    def mark_completed(self, item_id: str, metadata: dict[str, Any]) -> None:
        """Mark an item as successfully processed.

        Also removes the item from the failed list if it was there.

        Args:
            item_id: Unique identifier for the item
            metadata: Arbitrary metadata to store (size, record count, etc.)
        """
        self._completed[item_id] = metadata
        # Remove from failed list if previously failed
        self._failed = [f for f in self._failed if f.item_id != item_id]
        self._dirty_count += 1

    def mark_failed(self, item_id: str, error: str, context: dict[str, Any] | None = None) -> None:
        """Record a failed processing attempt.

        Replaces any existing failure record for the same item_id.

        Args:
            item_id: Unique identifier for the item
            error: Error message
            context: Optional extra info (url, source file, line number, etc.)
        """
        # Remove existing record for this item
        self._failed = [f for f in self._failed if f.item_id != item_id]
        self._failed.append(
            FailedItem(
                item_id=item_id,
                error=error,
                timestamp=datetime.now(UTC).isoformat(),
                context=context or {},
            )
        )
        self._dirty_count += 1

    def is_completed(self, item_id: str) -> bool:
        """Check if an item is recorded as completed.

        Args:
            item_id: Unique identifier to check

        Returns:
            True if item is in the completed set
        """
        return item_id in self._completed

    def get_metadata(self, item_id: str) -> dict[str, Any] | None:
        """Get the metadata for a completed item.

        Args:
            item_id: Unique identifier to check

        Returns:
            Metadata dict, or None if not in completed set
        """
        return self._completed.get(item_id)

    def remove_completed(self, item_id: str) -> None:
        """Remove an item from the completed set.

        Args:
            item_id: Unique identifier to remove
        """
        if item_id in self._completed:
            del self._completed[item_id]
            self._dirty_count += 1

    def flush(self, force: bool = False) -> None:
        """Write checkpoint to disk if there are pending changes.

        Uses atomic write (write to temp file, then rename) to prevent
        corruption if interrupted mid-write.

        Args:
            force: If True, write even if no changes since last flush
        """
        if not force and self._dirty_count == 0:
            return

        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)

            data = {
                "completed": self._completed,
                "failed": [f.to_dict() for f in self._failed],
                "last_updated": datetime.now(UTC).isoformat(),
            }

            # Atomic write: write to temp file then rename
            temp_path = self._path.with_suffix(".tmp")
            with open(temp_path, "w") as f:
                json.dump(data, f, indent=2)

            temp_path.replace(self._path)
            self._dirty_count = 0

        except OSError as e:
            logger.error(f"Failed to write checkpoint to {self._path}: {e}")

    def should_flush(self, interval: int = 10) -> bool:
        """Check if checkpoint should be flushed based on dirty count.

        Args:
            interval: Flush after this many changes

        Returns:
            True if dirty count has reached the flush interval
        """
        return self._dirty_count >= interval

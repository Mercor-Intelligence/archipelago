"""Manifest file handling for crawler.

This module provides functionality to read and write manifest files,
which track discovered files between crawl and download phases.
"""

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..exceptions import ConfigurationError


@dataclass
class ManifestItem:
    """A single item in the manifest.

    Attributes:
        name: Filename
        url: URL to download from
        size: File size in bytes (optional)
        last_modified: Last modification timestamp (optional)
    """

    name: str
    url: str
    size: int | None = None
    last_modified: str | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for JSON serialization."""
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: dict) -> "ManifestItem":
        """Create ManifestItem from dictionary."""
        if "url" not in data:
            raise ConfigurationError("Manifest item missing required 'url' field")
        return cls(
            name=data.get("name", ""),
            url=data["url"],
            size=data.get("size"),
            last_modified=data.get("last_modified"),
        )


@dataclass
class Manifest:
    """Manifest tracking discovered files.

    The manifest serves as an intermediate format between crawl and download
    phases. Users can edit the manifest to filter files before downloading.

    Attributes:
        files: List of files in the manifest
        created_at: Timestamp when manifest was created
        root_url: Root URL that was crawled (optional)
    """

    files: list[ManifestItem] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    root_url: str | None = None

    def save(self, path: Path) -> None:
        """Save manifest to JSON file.

        Args:
            path: Path to save manifest

        Raises:
            ConfigurationError: If file cannot be written
        """
        try:
            path.parent.mkdir(parents=True, exist_ok=True)

            data = {
                "created_at": self.created_at,
                "root_url": self.root_url,
                "file_count": len(self.files),
                "files": [f.to_dict() for f in self.files],
            }

            with open(path, "w") as f:
                json.dump(data, f, indent=2)

        except OSError as e:
            raise ConfigurationError(f"Failed to save manifest to {path}: {e}") from e

    @classmethod
    def load(cls, path: Path) -> "Manifest":
        """Load manifest from JSON file.

        Args:
            path: Path to manifest file

        Returns:
            Manifest instance

        Raises:
            ConfigurationError: If file cannot be read or parsed
        """
        try:
            with open(path) as f:
                data = json.load(f)

            files = [ManifestItem.from_dict(item) for item in data.get("files", [])]

            return cls(
                files=files,
                created_at=data.get("created_at", ""),
                root_url=data.get("root_url"),
            )

        except ConfigurationError:
            raise
        except FileNotFoundError:
            raise ConfigurationError(f"Manifest file not found: {path}")
        except json.JSONDecodeError as e:
            raise ConfigurationError(f"Invalid JSON in manifest {path}: {e}")
        except OSError as e:
            raise ConfigurationError(f"Failed to read manifest {path}: {e}")

    def __len__(self) -> int:
        """Return number of files in manifest."""
        return len(self.files)

"""Fixture loading utilities."""

import json
import logging
from pathlib import Path
from typing import Any

from .models import FixtureExpected, FixtureModel, FixtureRequest

logger = logging.getLogger(__name__)


class FixtureLoader:
    """Load test fixtures from JSON files."""

    def __init__(self, fixtures_dir: str | Path):
        """Initialize the fixture loader.

        Args:
            fixtures_dir: Directory containing fixture files
        """
        self.fixtures_dir = Path(fixtures_dir).resolve()
        if not self.fixtures_dir.exists():
            raise FileNotFoundError(f"Fixtures directory not found: {fixtures_dir}")

    def _validate_path(self, fixture_path: str) -> Path:
        """Validate that fixture_path is within fixtures_dir to prevent path traversal.

        Args:
            fixture_path: Relative path to fixture file

        Returns:
            Resolved absolute path

        Raises:
            ValueError: If path is outside fixtures directory
        """
        full_path = (self.fixtures_dir / fixture_path).resolve()

        # Ensure the path is within the fixtures directory
        if not full_path.is_relative_to(self.fixtures_dir):
            raise ValueError(f"Fixture path must be within fixtures directory: {fixture_path}")

        return full_path

    def load(self, fixture_path: str, auto_detect: bool = True) -> FixtureModel:
        """Load a single fixture from a JSON file.

        Supports two formats:
        1. FixtureModel format: {name, request, expected, response}
        2. Raw data format: Any JSON (auto-wrapped as FixtureModel)

        Args:
            fixture_path: Path to fixture file relative to fixtures_dir
            auto_detect: If True, auto-detect format and wrap raw JSON

        Returns:
            Parsed FixtureModel

        Raises:
            FileNotFoundError: If fixture file doesn't exist
            ValueError: If fixture JSON is invalid or path is outside fixtures directory
        """
        full_path = self._validate_path(fixture_path)

        if not full_path.exists():
            raise FileNotFoundError(f"Fixture not found: {full_path}")

        with open(full_path) as f:
            data = json.load(f)

        # Auto-detect format: try parsing as FixtureModel, fall back to wrapping raw data
        if auto_detect:
            try:
                # Attempt to parse as FixtureModel
                return FixtureModel(**data)
            except Exception:
                # Not a valid FixtureModel - wrap raw data
                logger.debug(
                    f"Auto-detecting raw JSON format for {fixture_path}, wrapping as FixtureModel"
                )
                return FixtureModel(
                    name=fixture_path,
                    request=FixtureRequest(
                        method="MOCK",
                        endpoint=str(fixture_path),
                    ),
                    expected=FixtureExpected(
                        status=200,
                        data=data,
                    ),
                    response=data,
                )

        # No auto-detect - strict parsing
        return FixtureModel(**data)

    def load_all(self, pattern: str = "**/*.json", auto_detect: bool = True) -> list[FixtureModel]:
        """Load all fixtures matching a glob pattern.

        Args:
            pattern: Glob pattern to match fixture files
            auto_detect: If True, auto-detect format and wrap raw JSON

        Returns:
            List of loaded FixtureModel objects
        """
        fixtures = []
        failed = []

        for fixture_file in self.fixtures_dir.glob(pattern):
            if fixture_file.is_file():
                try:
                    # Use load() method to get consistent auto-detect behavior
                    relative_path = fixture_file.relative_to(self.fixtures_dir)
                    fixture = self.load(str(relative_path), auto_detect=auto_detect)
                    fixtures.append(fixture)
                except Exception as e:
                    # Collect failures to report
                    failed.append((fixture_file, e))

        # Report all failures at once
        if failed:
            logger.warning(f"Failed to load {len(failed)} fixtures")
            for file, err in failed:
                logger.debug(f"  {file}: {err}")

        return fixtures

    def save(self, fixture: FixtureModel, fixture_path: str) -> None:
        """Save a fixture to a JSON file.

        Args:
            fixture: FixtureModel to save
            fixture_path: Path where to save the fixture

        Raises:
            ValueError: If path is outside fixtures directory
        """
        full_path = self._validate_path(fixture_path)
        full_path.parent.mkdir(parents=True, exist_ok=True)

        with open(full_path, "w") as f:
            json.dump(fixture.model_dump(exclude_none=True), f, indent=2)

    def load_raw(self, fixture_path: str) -> dict[str, Any]:
        """Load fixture as raw dictionary without validation.

        Args:
            fixture_path: Path to fixture file

        Returns:
            Raw dictionary data

        Raises:
            FileNotFoundError: If fixture file doesn't exist
            ValueError: If path is outside fixtures directory
        """
        full_path = self._validate_path(fixture_path)

        if not full_path.exists():
            raise FileNotFoundError(f"Fixture not found: {full_path}")

        with open(full_path) as f:
            return json.load(f)

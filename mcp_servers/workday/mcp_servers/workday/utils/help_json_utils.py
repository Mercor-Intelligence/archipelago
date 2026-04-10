"""JSON helpers for metadata columns."""

import json
from typing import Any


def serialize_metadata(metadata: dict[str, Any] | None) -> str | None:
    """Serialize metadata dict to JSON string."""
    if metadata is None:
        return None
    return json.dumps(metadata, separators=(",", ":"), sort_keys=True)


def parse_metadata(metadata_json: str | None) -> dict[str, Any] | None:
    """Deserialize metadata JSON string to dict."""
    if metadata_json in (None, ""):
        return None
    return json.loads(metadata_json)

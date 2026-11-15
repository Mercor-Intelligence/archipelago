"""Settings for keyword search, read from the ``KEYWORD_SEARCH_CONFIG`` JSON env var.

Every key is optional and falls back to its default. A bad value never stops the
server: broken JSON leaves the feature off, and a bad key falls back alone.
"""

import json
import os
from dataclasses import dataclass, field

from loguru import logger

CONFIG_ENV_VAR = "KEYWORD_SEARCH_CONFIG"
STATE_LOCATION_ENV_VAR = "STATE_LOCATION"
DEFAULT_STATE_LOCATION = "/.apps_data/filesystem"

SUPPORTED_FILE_TYPES = ("txt", "md", "csv", "eml", "docx", "pptx", "xlsx", "pdf")


@dataclass(frozen=True)
class KeywordSearchConfig:
    enabled: bool = False
    folder: str = "/filesystem"
    file_types: tuple[str, ...] = SUPPORTED_FILE_TYPES
    max_seconds_per_file: float = 30.0
    max_total_text_mb: float = 500.0
    # Studio stops a populate hook at 3000 s; finishing well before keeps populate green.
    max_build_minutes: float = 40.0
    # Under the 60 s per-tool-call timeout of loop_final_answer_agent.
    wait_for_build_seconds: float = 40.0
    # Per search; with the search itself it must stay under the same 60 s timeout.
    max_refresh_seconds: float = 15.0
    warnings: list[str] = field(default_factory=list, compare=False)


def _as_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _as_positive_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if value > 0 else None


def _as_folder(value: object) -> str | None:
    return value if isinstance(value, str) and value.startswith("/") else None


def _as_file_types(value: object) -> tuple[str, ...] | None:
    if not isinstance(value, list) or not value:
        return None
    types: list[str] = []
    for item in value:
        if not isinstance(item, str):
            return None
        ext = item.strip().lower().lstrip(".")
        if ext not in SUPPORTED_FILE_TYPES:
            return None
        if ext not in types:
            types.append(ext)
    return tuple(types)


_PARSERS = {
    "enabled": _as_bool,
    "folder": _as_folder,
    "file_types": _as_file_types,
    "max_seconds_per_file": _as_positive_number,
    "max_total_text_mb": _as_positive_number,
    "max_build_minutes": _as_positive_number,
    "wait_for_build_seconds": _as_positive_number,
    "max_refresh_seconds": _as_positive_number,
}


def parse_config(raw: str | None) -> KeywordSearchConfig:
    """Parse the JSON setting; anything missing or invalid takes its default."""
    if raw is None or not raw.strip():
        return KeywordSearchConfig()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return KeywordSearchConfig(
            warnings=[
                f"{CONFIG_ENV_VAR} is not valid JSON ({exc}); keyword search is off"
            ]
        )
    if not isinstance(data, dict):
        return KeywordSearchConfig(
            warnings=[f"{CONFIG_ENV_VAR} must be a JSON object; keyword search is off"]
        )

    values: dict[str, object] = {}
    warnings: list[str] = []
    for key, value in data.items():
        parser = _PARSERS.get(key)
        if parser is None:
            warnings.append(f"{CONFIG_ENV_VAR}: unknown key {key!r} ignored")
            continue
        parsed = parser(value)
        if parsed is None:
            warnings.append(
                f"{CONFIG_ENV_VAR}: invalid {key}={value!r}; using the default"
            )
            continue
        values[key] = parsed
    return KeywordSearchConfig(**values, warnings=warnings)  # pyright: ignore[reportArgumentType]


def load_config() -> KeywordSearchConfig:
    config = parse_config(os.getenv(CONFIG_ENV_VAR))
    for warning in config.warnings:
        logger.warning(warning)
    return config


def state_location() -> str:
    """Folder where keyword indexes are uploaded with a world or task."""
    return os.getenv(STATE_LOCATION_ENV_VAR) or DEFAULT_STATE_LOCATION

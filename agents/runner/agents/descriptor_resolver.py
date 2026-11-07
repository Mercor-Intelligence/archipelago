from math import isfinite
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from runner.agents.descriptors import Conversion
from runner.agents.harness_descriptor_registry import (
    CATALOG,
    HARNESS_PARENT_IDS,
    PARAMETER_ALIASES,
)
from runner.utils.settings import get_settings

Source = Literal["canonical", "legacy", "default"]


class ResolvedValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: JsonValue
    source: Source
    source_key: str | None = None


class ResolvedConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    harness_id: str
    values: dict[str, ResolvedValue] = Field(default_factory=dict)
    unmapped: dict[str, JsonValue] = Field(default_factory=dict)
    issues: dict[str, str] = Field(default_factory=dict)

    def __getitem__(self, descriptor_id: str) -> JsonValue:
        return self.values[descriptor_id].value

    def __contains__(self, descriptor_id: object) -> bool:
        return descriptor_id in self.values

    def get(self, descriptor_id: str, default: JsonValue = None) -> JsonValue:
        resolved = self.values.get(descriptor_id)
        return default if resolved is None else resolved.value


class ConversionError(ValueError):
    pass


def convert_legacy(value: JsonValue, conversion: Conversion) -> JsonValue:
    if value is None or conversion in ("identity", "fraction", "tokens"):
        return value
    if conversion == "milliseconds":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ConversionError("must be numeric to convert milliseconds")
        if not isfinite(value):
            raise ConversionError("must be finite")
        return value / 1000
    if conversion == "prepend":
        if not isinstance(value, bool):
            raise ConversionError("must be a boolean to project prompt mode")
        return "prepend" if value else "off"
    raise ConversionError(f"unknown conversion {conversion}")


LIST_VALUED_TEXT_DESCRIPTORS = frozenset({"allowed_tools", "disabled_tools"})


def schema_issue(
    value: JsonValue, schema: dict[str, JsonValue], *, accepts_list: bool = False
) -> str | None:
    if value is None:
        return None
    field_type = schema.get("field_type")
    if field_type == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"expected number, got {type(value).__name__}"
        low, high = schema.get("min_value"), schema.get("max_value")
        if isinstance(low, (int, float)) and value < low:
            return f"{value} is below min {low}"
        if isinstance(high, (int, float)) and value > high:
            return f"{value} is above max {high}"
    elif field_type == "boolean" and not isinstance(value, bool):
        return f"expected boolean, got {type(value).__name__}"
    elif field_type in ("text", "textarea") and not (
        isinstance(value, str)
        or (
            accepts_list
            and isinstance(value, list)
            and all(isinstance(item, str) for item in value)
        )
    ):
        return f"expected text, got {type(value).__name__}"
    elif field_type == "select":
        options = schema.get("options")
        if isinstance(options, list) and value not in options:
            return f"{value!r} is not one of {options}"
    return None


# Old keys a harness reads ahead of its aliases, where that is not aliases-first.
LEGACY_KEY_PRIORITY: dict[str, tuple[str, ...]] = {
    "user_sim_harness_agent": ("user_sim_max_turns",),
}


def legacy_keys(harness_id: str, descriptor_id: str) -> dict[str, Conversion]:
    """Old keys for one descriptor in the order the harness reads them."""
    parent = HARNESS_PARENT_IDS.get(harness_id, harness_id)
    mapping = CATALOG.harnesses[harness_id].parameters[descriptor_id].legacy_keys
    first = LEGACY_KEY_PRIORITY.get(parent, ())
    aliases = PARAMETER_ALIASES.get(parent, {})
    return dict(
        sorted(
            mapping.items(),
            key=lambda item: (item[0] not in first, item[0] not in aliases),
        )
    )


def resolve_config(
    harness_id: str, config_values: dict[str, JsonValue]
) -> ResolvedConfig:
    """One value per descriptor: saved key, else registry default.

    Old keys win over the canonical key unless DESCRIPTOR_CANONICAL_KEYS_ENABLED is on.
    """
    canonical_first = get_settings().DESCRIPTOR_CANONICAL_KEYS_ENABLED
    harness = CATALOG.harnesses.get(harness_id)
    if harness is None:
        raise KeyError(f"{harness_id} declares no descriptors")
    resolved = ResolvedConfig(harness_id=harness_id)
    consumed: set[str] = set()

    def add_issue(key: str, issue: str) -> None:
        earlier = resolved.issues.get(key)
        resolved.issues[key] = issue if earlier is None else f"{earlier}; {issue}"

    for key, mapping in harness.parameters.items():
        descriptor = CATALOG.descriptors[key]
        legacy: list[tuple[str, JsonValue]] = []
        legacy_mapping = legacy_keys(harness_id, key)
        canonical_priority = key in legacy_mapping
        for source, conversion in legacy_mapping.items():
            if source not in config_values:
                continue
            consumed.add(source)
            if source == key:
                continue
            try:
                legacy.append(
                    (source, convert_legacy(config_values[source], conversion))
                )
            except ConversionError as error:
                add_issue(key, f"{source} {error}")
                legacy.append((source, config_values[source]))
        if len({repr(value) for _, value in legacy}) > 1:
            add_issue(key, f"Multiple legacy keys disagree; using {legacy[0][0]}")
        if key in config_values:
            consumed.add(key)
        if key in config_values and (
            canonical_first or canonical_priority or not legacy
        ):
            value = config_values[key]
            resolved.values[key] = ResolvedValue(
                value=value, source="canonical", source_key=key
            )
            if legacy and legacy[0][1] != value:
                add_issue(
                    key,
                    f"{legacy[0][0]}={legacy[0][1]!r} disagrees with {key}={value!r}; using {key}",
                )
        elif legacy:
            source, value = legacy[0]
            resolved.values[key] = ResolvedValue(
                value=value, source="legacy", source_key=source
            )
            if key in config_values and config_values[key] != value:
                add_issue(
                    key,
                    f"{source}={value!r} disagrees with {key}={config_values[key]!r}; using {source}",
                )
        elif mapping.on_missing == "default":
            resolved.values[key] = ResolvedValue(
                value=mapping.runtime_default, source="default"
            )
        if key in resolved.values:
            issue = schema_issue(
                resolved.values[key].value,
                {**descriptor.field_schema, **mapping.field_schema},
                accepts_list=key in LIST_VALUED_TEXT_DESCRIPTORS,
            )
            if issue is not None:
                add_issue(key, issue)
    resolved.unmapped = {k: v for k, v in config_values.items() if k not in consumed}
    return resolved

"""Utilities for building child agent run inputs."""

from typing import Any, Literal, cast, get_args

from runner.agents.models import AgentRunInput

InheritedField = Literal[
    "mcp_gateway_url",
    "mcp_gateway_auth_token",
    "mcp_gateway_actor_id",
    "orchestrator_extra_args",
    "parent_trajectory_output",
    "custom_args",
    "task_custom_fields",
    "inner_agent_config",
    "rest_services",
]
"""Parent fields a child inherits by default and a caller may drop."""

INHERITED_FIELDS: frozenset[str] = frozenset(get_args(InheritedField))
OVERRIDE_ONLY_FIELDS: frozenset[str] = frozenset({"orchestrator_model"})
CALLER_SET_FIELDS: frozenset[str] = frozenset(
    {"initial_messages", "agent_config_values"}
)
FIXED_FIELDS: frozenset[str] = frozenset({"trajectory_id"})


def child_run_input(
    parent: AgentRunInput,
    *,
    initial_messages: list[Any],
    agent_config_values: dict[str, Any],
    drop: frozenset[InheritedField] = frozenset(),
    **overrides: Any,
) -> AgentRunInput:
    """Child input that inherits every parent field except those dropped or overridden."""
    for field_name in drop:
        if field_name not in INHERITED_FIELDS:
            raise TypeError(f"Cannot drop child AgentRunInput field {field_name}")

    conflicts = drop.intersection(overrides)
    if conflicts:
        field_name = sorted(conflicts)[0]
        raise ValueError(f"Cannot both drop and override {field_name}")

    allowed_overrides = INHERITED_FIELDS | OVERRIDE_ONLY_FIELDS
    for field_name in overrides:
        if field_name not in allowed_overrides:
            raise TypeError(f"Cannot override child AgentRunInput field {field_name}")

    model_fields = cast(dict[str, Any], AgentRunInput.model_fields)
    values = {field_name: getattr(parent, field_name) for field_name in model_fields}
    values["initial_messages"] = initial_messages
    values["agent_config_values"] = agent_config_values
    for field_name in drop:
        values[field_name] = [] if field_name == "rest_services" else None
    values.update(overrides)
    return AgentRunInput(**values)

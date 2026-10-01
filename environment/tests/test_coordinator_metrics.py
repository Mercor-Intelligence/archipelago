from datetime import UTC, datetime

from runner.coordinator.agents.models import (
    AgentConfig,
    VCAHarnessConfigEnriched,
    VirtualCoworkerAgent,
)
from runner.coordinator.runtime import _persona_base_tags


def make_vca() -> VirtualCoworkerAgent:
    now = datetime.now(UTC)
    return VirtualCoworkerAgent(
        actor_id="admin_agent",
        persona="You are Admin Agent.",
        instructions="advance environment",
        vca_harness_config=VCAHarnessConfigEnriched(
            vca_harness_config_id="vca_harness_test",
            vca_id="admin_agent",
            agent_id="agent_test",
            agent_version=1,
            orchestrator_id="orch_test",
            orchestrator_version=1,
            created_by="user_test",
            created_at=now,
            updated_at=now,
            agent_config=AgentConfig(
                agent_config_id="loop_agent",
                agent_name="Loop",
                agent_config_values={},
            ),
            orchestrator_model="openai/gpt-4o-mini",
        ),
    )


def test_persona_base_tags_are_low_cardinality() -> None:
    # status is appended per-emit by the record_* helpers, not baked into the base tags.
    assert _persona_base_tags(make_vca()) == [
        "agent_id:agent_test",
        "orchestrator_model:openai/gpt-4o-mini",
    ]

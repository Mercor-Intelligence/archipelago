"""The actor id must not travel on argv.

``/proc/<pid>/cmdline`` is world-readable, so a sibling actor can read another's
identity straight off it. ``/proc/<pid>/environ`` is 0400, which narrows the
reader to the same uid — and that closes outright once each actor has its own
OS user.
"""

from pathlib import Path

import pytest

from runner.coordinator.state.store import (
    MCP_GATEWAY_ACTOR_ID_ENV,
    CoordinatorStore,
)

from .test_coordinator import make_virtual_coworker_agent

ACTOR = "vca_riley123"


@pytest.fixture
def prepared(tmp_path: Path) -> tuple[list[str], dict[str, str]]:
    store = CoordinatorStore(root=tmp_path / "state")
    return store.agent_configs.prepare_agent_run(
        vca=make_virtual_coworker_agent(actor_id=ACTOR),
        run_id="run_1",
        mcp_gateway_url="http://127.0.0.1:8080/mcp/",
        filesystem_dir=str(tmp_path / "fs"),
    )


def test_actor_id_is_not_on_argv(prepared: tuple[list[str], dict[str, str]]) -> None:
    command, _ = prepared

    assert "--mcp-gateway-actor-id" not in command
    assert ACTOR not in command, "the actor id reached /proc/<pid>/cmdline"


def test_actor_id_is_handed_over_the_environment(
    prepared: tuple[list[str], dict[str, str]],
) -> None:
    _, env = prepared

    assert env[MCP_GATEWAY_ACTOR_ID_ENV] == ACTOR


def test_the_variable_name_is_the_one_the_runner_reads() -> None:
    """One name across two packages.

    The agent runner lives in ``archipelago/agents`` and cannot be imported
    from here, so pin the literal on both sides instead. A rename in either
    package fails this and sends the author to the other one, rather than
    silently handing every run an empty actor id.
    """
    assert MCP_GATEWAY_ACTOR_ID_ENV == "MCP_GATEWAY_ACTOR_ID"

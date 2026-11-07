"""The gateway must not let a caller choose the actor id it forwards to app servers.

`_meta` rides in the caller's own request and is proxied on to the backend, so only
a nonce the gateway has already redeemed can carry identity there.
"""

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastmcp import Client as FastMCPClient
from fastmcp import FastMCP
from fastmcp.server import create_proxy
from fastmcp.server.dependencies import get_context
from fastmcp.tools import ToolResult

from runner.coordinator import middleware as coordinator_middleware
from runner.coordinator.agents.models import (
    COORDINATOR_ACTOR_ID_VALUE,
    COORDINATOR_DISPATCH_ORIGIN,
    TARGET_AGENT_ACTOR_ID_VALUE,
    TOOL_CALL_ACTOR_KEY,
    TOOL_CALL_DISPATCH_NONCE_KEY,
    TOOL_CALL_ORIGIN_KEY,
)
from runner.coordinator.config.models import CoordinatorConfig
from runner.coordinator.dispatch_identity import issue_dispatch_nonce
from runner.coordinator.middleware import CoordinatorToolCallMiddleware
from runner.coordinator.runtime import Coordinator, set_coordinator_for_tests
from tests.test_coordinator import make_virtual_coworker_agent

# The TA's bearer is a Modal sandbox connect token, not an actor id.
TA_CONNECT_TOKEN = "sandbox-connect-token-abc123"
VCA_ACTOR_ID = "admin_agent"


def _install_coordinator(root: Path, *, with_persona: bool = False) -> Coordinator:
    (root / "config").mkdir(parents=True, exist_ok=True)
    agents = {VCA_ACTOR_ID: make_virtual_coworker_agent()} if with_persona else {}
    config = CoordinatorConfig(enabled=True, agents=agents)
    (root / "config/config.json").write_text(
        json.dumps(config.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8"
    )
    coordinator = Coordinator(root=root)
    coordinator._started = True
    set_coordinator_for_tests(coordinator)
    return coordinator


def _wire_request(bearer: str) -> SimpleNamespace:
    return SimpleNamespace(
        scope={"headers": [(b"authorization", f"Bearer {bearer}".encode())]}
    )


def _forwarded_bearer(request: SimpleNamespace) -> str:
    for name, value in request.scope["headers"]:
        if name.lower() == b"authorization":
            return value.decode()
    return "<none>"


async def _call_next(_: object) -> ToolResult:
    return ToolResult(content=[])


async def _drive(
    monkeypatch: pytest.MonkeyPatch,
    request: SimpleNamespace,
    meta: SimpleNamespace | None,
) -> str:
    """Send one tool call through the middleware; return the bearer it forwards."""
    monkeypatch.setattr(coordinator_middleware, "get_http_request", lambda: request)
    context = SimpleNamespace(
        message=SimpleNamespace(name="mail_read_mail", arguments={}, meta=meta),
        fastmcp_context=None,
    )
    await CoordinatorToolCallMiddleware().on_call_tool(
        cast(Any, context), cast(Any, _call_next)
    )
    return _forwarded_bearer(request)


@pytest.mark.parametrize(
    "claimed",
    [COORDINATOR_ACTOR_ID_VALUE, VCA_ACTOR_ID, "tenant_not_in_any_config"],
)
@pytest.mark.asyncio
async def test_wire_caller_cannot_choose_the_forwarded_actor_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, claimed: str
) -> None:
    _install_coordinator(tmp_path / "state")
    meta = SimpleNamespace(**{TOOL_CALL_ACTOR_KEY: claimed})

    forwarded = await _drive(monkeypatch, _wire_request(TA_CONNECT_TOKEN), meta)

    assert forwarded == f"Bearer {TARGET_AGENT_ACTOR_ID_VALUE}"


@pytest.mark.asyncio
async def test_a_dispatch_nonce_is_good_for_one_call_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_coordinator(tmp_path / "state")
    nonce = issue_dispatch_nonce(VCA_ACTOR_ID, COORDINATOR_DISPATCH_ORIGIN)
    meta = SimpleNamespace(**{TOOL_CALL_DISPATCH_NONCE_KEY: nonce})

    first = await _drive(monkeypatch, _wire_request(TA_CONNECT_TOKEN), meta)
    replayed = await _drive(monkeypatch, _wire_request(TA_CONNECT_TOKEN), meta)

    assert first == f"Bearer {VCA_ACTOR_ID}"
    assert replayed == f"Bearer {TARGET_AGENT_ACTOR_ID_VALUE}"


@pytest.mark.asyncio
async def test_bearer_still_identifies_a_configured_persona(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_coordinator(tmp_path / "state", with_persona=True)

    forwarded = await _drive(monkeypatch, _wire_request(VCA_ACTOR_ID), None)

    assert forwarded == f"Bearer {VCA_ACTOR_ID}"


@pytest.mark.asyncio
async def test_unknown_bearer_falls_back_to_the_target_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_coordinator(tmp_path / "state")

    forwarded = await _drive(monkeypatch, _wire_request(TA_CONNECT_TOKEN), None)

    assert forwarded == f"Bearer {TARGET_AGENT_ACTOR_ID_VALUE}"


def _recorded_actors(root: Path) -> list[str]:
    path = root / "checkpoint_observations/mcp_calls.jsonl"
    if not path.exists():
        return []
    return [json.loads(line)["actor_id"] for line in path.read_text().splitlines()]


def _gateway_over_backend() -> tuple[FastMCP[None], list[dict[str, Any]]]:
    """A gateway proxying a backend that records the `_meta` it is handed."""
    seen: list[dict[str, Any]] = []
    backend: FastMCP[None] = FastMCP("backend")

    @backend.tool
    def mail_read_mail() -> str:
        request_context = get_context().request_context
        meta = request_context.meta if request_context is not None else None
        seen.append(meta.model_dump() if meta is not None else {})
        return "ok"

    gateway = create_proxy(backend, name="Gateway")
    gateway.add_middleware(CoordinatorToolCallMiddleware())
    return gateway, seen


@pytest.mark.asyncio
async def test_a_backend_only_ever_sees_a_spent_nonce(tmp_path: Path) -> None:
    """The proxy forwards `_meta`, so the nonce must be worthless by the time it lands."""
    root = tmp_path / "state"
    _install_coordinator(root)
    gateway, seen = _gateway_over_backend()
    nonce = issue_dispatch_nonce(VCA_ACTOR_ID, COORDINATOR_DISPATCH_ORIGIN)
    dispatch_meta = {
        TOOL_CALL_ACTOR_KEY: VCA_ACTOR_ID,
        TOOL_CALL_ORIGIN_KEY: COORDINATOR_DISPATCH_ORIGIN,
        TOOL_CALL_DISPATCH_NONCE_KEY: nonce,
    }
    async with FastMCPClient(gateway) as client:
        await client.call_tool("mail_read_mail", {}, meta=dispatch_meta)
        # Replay everything the backend read off the wire, as a backend would.
        stolen = {k: v for k, v in seen[0].items() if k != "progressToken"}
        await client.call_tool("mail_read_mail", {}, meta=stolen)

    assert stolen[TOOL_CALL_DISPATCH_NONCE_KEY] == nonce
    assert _recorded_actors(root) == [VCA_ACTOR_ID, TARGET_AGENT_ACTOR_ID_VALUE]


@pytest.mark.asyncio
async def test_in_process_dispatch_keeps_its_actor(tmp_path: Path) -> None:
    """The nonce survives FastMCP's in-memory `_meta` round trip."""
    root = tmp_path / "state"
    _install_coordinator(root)
    gateway, _ = _gateway_over_backend()
    async with FastMCPClient(gateway) as client:
        await client.call_tool(
            "mail_read_mail",
            {},
            meta={
                TOOL_CALL_DISPATCH_NONCE_KEY: issue_dispatch_nonce(
                    VCA_ACTOR_ID, COORDINATOR_DISPATCH_ORIGIN
                )
            },
        )

    assert _recorded_actors(root) == [VCA_ACTOR_ID]


@pytest.mark.asyncio
async def test_in_process_call_without_a_nonce_is_the_target_agent(
    tmp_path: Path,
) -> None:
    root = tmp_path / "state"
    _install_coordinator(root)
    gateway, _ = _gateway_over_backend()
    async with FastMCPClient(gateway) as client:
        await client.call_tool(
            "mail_read_mail", {}, meta={TOOL_CALL_ACTOR_KEY: VCA_ACTOR_ID}
        )

    assert _recorded_actors(root) == [TARGET_AGENT_ACTOR_ID_VALUE]

"""Check, once per trajectory, that the harness's first own model request carries the world's system prompt as declared."""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

from loguru import logger

from runner.agents.descriptors import SystemInstructionsPlacement
from runner.agents.models import (
    AgentConfigIds,
    LitellmAnyMessage,
    content_to_str,
    get_msg_attr,
)
from runner.utils.logging.terminal_error import FaultKind
from runner.utils.metrics import increment
from runner.utils.settings import get_settings

_SYSTEM_ROLES = frozenset({"system", "developer"})
MISMATCH_METRIC = "studio.trajectory.system_instructions_placement_mismatch"
CONTRACT_FAULT: dict[str, Any] = {
    "kind": FaultKind.HARNESS_CONTRACT.value,
    "descriptor": "system_instructions_placement",
}


@dataclass
class SystemInstructionsCheck:
    """One latch per trajectory, shared by reference across asyncio child contexts."""

    harness: str
    placement: SystemInstructionsPlacement
    configured: str
    checked: bool = False
    failure: str | None = None


# Never use a mutable default: each trajectory entrypoint owns a fresh instance.
system_instructions_check_ctx: ContextVar[SystemInstructionsCheck | None] = ContextVar(
    "system_instructions_check", default=None
)


def _normalise(text: str) -> str:
    return " ".join(text.split())


def _texts(messages: list[LitellmAnyMessage], roles: frozenset[str]) -> list[str]:
    return [
        content_to_str(get_msg_attr(m, "content"))
        for m in messages
        if get_msg_attr(m, "role") in roles
    ]


def system_text(messages: list[LitellmAnyMessage]) -> str:
    """The system- and developer-role text of ``messages``, whitespace-normalised."""
    return _normalise(" ".join(_texts(messages, _SYSTEM_ROLES)))


def system_instructions_applied(
    placement: SystemInstructionsPlacement,
    configured: str,
    request_messages: list[LitellmAnyMessage],
) -> bool:
    """Whether ``request_messages`` carry the configured system prompt the way ``placement`` declares."""
    expected = _normalise(configured)
    if not expected:
        return True
    if placement == "not_applicable":
        everything = _normalise(
            " ".join(
                content_to_str(get_msg_attr(m, "content")) for m in request_messages
            )
        )
        return expected not in everything
    if placement == "prepend_to_user":
        users = _texts(request_messages, frozenset({"user"}))
        return bool(users) and _normalise(users[0]).startswith(expected)
    actual = system_text(request_messages)
    if placement == "replace":
        return actual == expected
    return expected in actual


@contextmanager
def system_instructions_check(
    harness: str,
    placement: SystemInstructionsPlacement | None,
    initial_messages: list[LitellmAnyMessage],
) -> Iterator[SystemInstructionsCheck | None]:
    """Arm the first-request check for the enclosed run and yield its latch; a None ``placement`` (a knob decides per run) leaves it disarmed."""
    state = (
        None
        if placement is None
        else SystemInstructionsCheck(
            harness=harness,
            placement=placement,
            configured=system_text(initial_messages),
        )
    )
    token = system_instructions_check_ctx.set(state)
    try:
        yield state
    finally:
        system_instructions_check_ctx.reset(token)


def check_first_request(
    request_messages: list[LitellmAnyMessage], harness: AgentConfigIds | None
) -> None:
    """On ``harness``'s first own request, count and warn (and, under enforcement, record a failure) when the prompt is not applied as declared.

    ``harness`` is the harness whose main loop sent the request; None, or a harness other
    than the one the trajectory runs, is a side call (summariser, user sim, inner agent).
    """
    state = system_instructions_check_ctx.get()
    if state is None or state.checked or state.harness != harness:
        return
    state.checked = True
    if system_instructions_applied(state.placement, state.configured, request_messages):
        return
    increment(
        MISMATCH_METRIC,
        tags=[f"harness:{state.harness}", f"declared:{state.placement}"],
    )
    # Never ERROR or a bound fault here: the terminal-error sink would keep it as the run's cause.
    logger.warning(
        f"system_instructions_placement_mismatch harness={state.harness} "
        f"declared={state.placement} configured_len={len(state.configured)} "
        f"request_system_len={len(system_text(request_messages))}"
    )
    if get_settings().HARNESS_DECLARATION_ENFORCEMENT_ENABLED:
        state.failure = (
            f"Harness {state.harness!r} sent a first request that does not carry the "
            f"configured system prompt as its 'system_instructions_placement' "
            f"{state.placement!r} declares."
        )

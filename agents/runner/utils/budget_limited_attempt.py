from enum import StrEnum


class AttemptOutcome(StrEnum):
    WALL_BUDGET = "wall_budget"
    WALL_BUDGET_HARD = "wall_budget_hard"
    MAX_STEPS = "max_steps"
    MAX_TURNS = "max_turns"
    AGENT_TIMEOUT = "agent_timeout"
    TOKEN_BUDGET = "token_budget"


def budget_limited_attempt_marker(outcome: AttemptOutcome) -> dict[str, bool | str]:
    """Output keys for an attempt the harness stopped at a budget; the status stays as reported."""
    return {"budget_limited_attempt": True, "attempt_outcome": outcome.value}

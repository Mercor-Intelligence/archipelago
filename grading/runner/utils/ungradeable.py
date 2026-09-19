"""Shared constructor for a verifier result that reached no verdict.

When to use it, and how to pick a cause: "Reporting a criterion you could not
judge" in ``archipelago/grading/README.md``.
"""

from __future__ import annotations

from typing import Any

from runner.evals.models import EvalImplInput
from runner.models import VerifierResult, VerifierResultStatus
from runner.utils.grading_log import logger

UNGRADEABLE_CAUSES = (
    "config_error",
    "model_unavailable",
    "snapshot_unreadable",
    "disk_exhausted",
    "deliverable_truncated",
    "all_files_over_caps",
    "all_databases_failed_to_load",
    "no_verdict",
    "judge_declined",
)

SOFT_CAUSES_FIELD = "ungradeable_soft_causes"

UNGRADEABLE_KEY = "ungradeable"
UNGRADEABLE_CAUSE_KEY = "ungradeable_cause"
NO_DELIVERABLE_KEY = "no_deliverable"
REASON_KEY = "reason"

_SCORE_KEYS = frozenset({"score", "score_100"})
_UNGRADEABLE_KEYS = frozenset({UNGRADEABLE_KEY, UNGRADEABLE_CAUSE_KEY})


def _reject(values: dict[str, Any], forbidden: frozenset[str], why: str) -> None:
    clash = forbidden & values.keys()
    if clash:
        raise ValueError(
            f"caller values carry {sorted(clash)}, which this constructor owns. {why}"
        )


def soft_causes(input: EvalImplInput) -> frozenset[str]:  # noqa: A002
    """Causes this world scores as 0.0 instead of voiding the run.

    Empty when unset. Unknown strings are dropped, so a stale world config
    cannot fail a grade that would otherwise have completed.
    """
    configured = (input.eval_config.eval_config_values or {}).get(SOFT_CAUSES_FIELD)
    if not isinstance(configured, list):
        return frozenset()
    return frozenset(c for c in configured if c in UNGRADEABLE_CAUSES)


def is_ungradeable(result: VerifierResult) -> bool:
    """Whether this row reached no verdict. True on the soft path too."""
    return bool(result.verifier_result_values.get(UNGRADEABLE_KEY))


def is_no_deliverable(result: VerifierResult) -> bool:
    """Whether this row scored 0.0 because the agent produced nothing."""
    return bool(result.verifier_result_values.get(NO_DELIVERABLE_KEY))


def no_deliverable_result(
    input: EvalImplInput,  # noqa: A002
    reason: str,
    *,
    values: dict[str, Any] | None = None,
) -> VerifierResult:
    """A real scored 0.0 for an agent that produced nothing.

    Distinct from `ungradeable_result`. The grader worked, so the run is not
    void and this counts toward the score. `no_deliverable` marks it so the
    case can be found later, for example to separate a genuinely empty attempt
    from one where the environment gave the agent nothing to work with.

    Lifted from `agentic_verifier._no_deliverable`.

    Raises:
        ValueError: if ``values`` carries a score key or an ungradeable marker.
            This row is scored and is not ungradeable, so both are this
            constructor's to write.
    """
    extra = dict(values or {})
    _reject(
        extra,
        _SCORE_KEYS,
        "A no-deliverable row is scored 0.0 and the score is written here.",
    )
    _reject(
        extra,
        _UNGRADEABLE_KEYS,
        "A no-deliverable row is a verdict, so it is never also ungradeable.",
    )

    logger.bind(
        message_type="verifier_result",
        verifier_id=input.verifier.verifier_id,
        payload={NO_DELIVERABLE_KEY: True, "score": 0.0},
    ).info(f"No deliverable: {reason}")

    return VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=0.0,
        status=VerifierResultStatus.OK,
        message=reason,
        verifier_result_values={
            **extra,
            # Scored, so it carries the score; a sum keyed on it must not skip these.
            "score": 0.0,
            "score_100": 0,
            NO_DELIVERABLE_KEY: True,
            REASON_KEY: reason,
        },
    )


def ungradeable_result(
    input: EvalImplInput,  # noqa: A002
    reason: str,
    *,
    cause: str,
    values: dict[str, Any] | None = None,
) -> VerifierResult:
    """A row saying this criterion could not be judged.

    ``cause`` must be one of ``UNGRADEABLE_CAUSES``. ``reason`` is free text for
    a human reading the grade. ``values`` carries eval-specific diagnostics.

    Status is ERROR unless the world lists ``cause`` in
    ``ungradeable_soft_causes``, where the row is instead a scored 0.0.

    Raises:
        ValueError: if ``cause`` is outside the closed set, or if ``values``
            carries a score key on the ERROR path.
    """
    if cause not in UNGRADEABLE_CAUSES:
        raise ValueError(
            f"unknown ungradeable cause {cause!r}; add it to UNGRADEABLE_CAUSES "
            "and to the eval-config field options in both registries first"
        )

    extra = dict(values or {})
    soft = cause in soft_causes(input)

    _reject(
        extra,
        frozenset({NO_DELIVERABLE_KEY}),
        "An ungradeable row reached no verdict, so it is never also a scored "
        "no-deliverable row.",
    )
    if not soft:
        _reject(
            extra,
            _SCORE_KEYS,
            f"Cause {cause!r} is fatal here, so the row was never scored and a "
            "sum over it would count a voided grade as a real zero.",
        )

    logger.bind(
        message_type="verifier_result",
        verifier_id=input.verifier.verifier_id,
        payload={UNGRADEABLE_KEY: True, UNGRADEABLE_CAUSE_KEY: cause, "soft": soft},
    ).warning(f"Ungradeable: {reason}")

    return VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=0.0,
        status=(VerifierResultStatus.OK if soft else VerifierResultStatus.ERROR),
        message=f"Ungradeable: {reason}",
        verifier_result_values={
            **extra,
            # Last, so caller diagnostics cannot overwrite the reserved keys.
            **({"score": 0.0, "score_100": 0} if soft else {}),
            UNGRADEABLE_KEY: True,
            UNGRADEABLE_CAUSE_KEY: cause,
            REASON_KEY: reason,
        },
    )

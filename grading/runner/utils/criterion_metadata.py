from loguru import logger
from pydantic import BaseModel, ConfigDict, Field, StrictBool, TypeAdapter

from runner.models import Verifier, VerifierResult, VerifierResultStatus

CRITERION_DEFINITIONS_KEY = "criterion_definitions"


class CriterionDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criterion_id: str = Field(min_length=1, pattern=r"^\S+$")
    criterion: str = Field(min_length=1, pattern=r"\S")
    critical: StrictBool


_DEFINITIONS = TypeAdapter(list[CriterionDefinition])


def parse_criterion_definitions(value: object) -> list[CriterionDefinition]:
    definitions = _DEFINITIONS.validate_python([] if value is None else value)
    if len({item.criterion_id for item in definitions}) != len(definitions):
        raise ValueError("Criterion definition IDs must be unique")
    return definitions


def critical_criteria_metadata(
    results: list[VerifierResult],
    verifiers: list[Verifier],
) -> dict[str, int | None]:
    """Criticality counts for a grade, or no keys when criticality does not apply.

    Reported off the authored ``criterion_definitions`` rather than an opt-in
    flag, because criticality is a property of the rubric and not of the
    scoring method that happens to run over it.
    """
    expected_ids = {
        verifier.verifier_id
        for verifier in verifiers
        if verifier.verifier_values.get(CRITERION_DEFINITIONS_KEY) not in (None, [])
    }
    reporting_ids = expected_ids | {
        result.verifier_id
        for result in results
        if CRITERION_DEFINITIONS_KEY in result.verifier_result_values
    }
    if not reporting_ids:
        # Nothing declares definitions, so there is no ratio to report. Paired
        # nulls here would make "undeterminable" the normal state on every
        # world that has not authored a criterion list.
        return {}
    reporting_results = [
        result for result in results if result.verifier_id in reporting_ids
    ]
    try:
        if not expected_ids <= {result.verifier_id for result in reporting_results}:
            raise ValueError("Missing result for a structured verifier")
        return {**critical_criteria_counts(reporting_results)}
    except ValueError as exc:
        # Every raise above lands here as the same two nulls, so log the cause.
        logger.bind(
            message_type="critical_criteria_unavailable",
            verifier_ids=sorted(result.verifier_id for result in reporting_results),
        ).warning("Critical criteria counts unavailable: {}", exc)
        return {"critical_criteria_total": None, "critical_criteria_met": None}


def critical_criteria_counts(results: list[VerifierResult]) -> dict[str, int]:
    if not results or len({result.verifier_id for result in results}) != len(results):
        raise ValueError("Critical counts require distinct verifier results")
    total = met = 0
    for result in results:
        values = result.verifier_result_values
        if (
            result.status != VerifierResultStatus.OK
            or values.get("ungradeable")
            or values.get("verdict_salvaged")
        ):
            raise ValueError("Critical counts require complete, gradeable results")
        definitions = parse_criterion_definitions(values.get(CRITERION_DEFINITIONS_KEY))
        if not definitions:
            raise ValueError("Critical counts require authored criterion definitions")
        raw_ledger = values.get("criteria")
        if not isinstance(raw_ledger, list):
            raise ValueError("Critical counts require a criterion ledger")
        ledger: dict[str, bool] = {}
        for row in raw_ledger:
            if not isinstance(row, dict):
                raise ValueError("Invalid criterion ledger entry")
            if row.get("source") == "beyond_guidance":
                continue
            identifier = row.get("criterion_id")
            passed = row.get("met")
            if (
                row.get("source") != "guidance"
                or not isinstance(identifier, str)
                or type(passed) is not bool
                or identifier in ledger
            ):
                raise ValueError(
                    "Criterion results require unique IDs and boolean outcomes"
                )
            ledger[identifier] = passed
        if set(ledger) != {item.criterion_id for item in definitions}:
            raise ValueError(
                "Criterion results must cover the authored definitions exactly"
            )
        for definition in definitions:
            if definition.critical:
                total += 1
                met += int(ledger[definition.criterion_id])
    # Zero criticals is a real rubric; nulls above mean undeterminable.
    return {"critical_criteria_total": total, "critical_criteria_met": met}

"""Tests for the no-op negative control (``runner.validate.validate_no_op``).

Everything here is OFFLINE and deterministic: snapshots are tiny in-memory
zips and the verifiers use pattern_match_check (programmatic, no LLM), so no
API key or network access is required.
"""

import io
import zipfile

from runner.evals.models import EvalConfig, EvalIds
from runner.models import GradingSettings, Verifier, VerifierResultStatus
from runner.validate import validate, validate_no_op

PREEXISTING_PATH = "filesystem/notes/readme.txt"
PREEXISTING_TEXT = "Project kickoff checklist: book a room, invite the team.\n"
GOLDEN_ONLY_PATH = "filesystem/report/summary.txt"
GOLDEN_ONLY_TEXT = "Survey summary: the gorilla population grew 12% in 2025.\n"

EVAL_CONFIGS = [
    EvalConfig(
        eval_config_id="ec_pattern_match",
        eval_config_name="Pattern Match Check",
        eval_defn_id=EvalIds.PATTERN_MATCH_CHECK,
        eval_config_values={},
    ),
    EvalConfig(
        eval_config_id="ec_tool_call",
        eval_config_name="Tool Call Check",
        eval_defn_id=EvalIds.TOOL_CALL_CHECK,
        eval_config_values={},
    ),
]

# No LLM-judge eval runs in these tests; the model name is never used.
GRADING_SETTINGS = GradingSettings(llm_judge_model="offline/none")


def _snapshot(files: dict[str, str]) -> io.BytesIO:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, content in files.items():
            zf.writestr(path, content)
    buffer.seek(0)
    return buffer


def _initial() -> io.BytesIO:
    return _snapshot({PREEXISTING_PATH: PREEXISTING_TEXT})


def _golden() -> io.BytesIO:
    return _snapshot(
        {PREEXISTING_PATH: PREEXISTING_TEXT, GOLDEN_ONLY_PATH: GOLDEN_ONLY_TEXT}
    )


def _pattern_verifier(verifier_id: str, pattern: str, index: int) -> Verifier:
    """pattern_match_check over *.txt files in the final snapshot (no LLM)."""
    return Verifier(
        verifier_id=verifier_id,
        world_id=None,
        task_id="test_task",
        eval_config_id="ec_pattern_match",
        verifier_values={
            "pattern": pattern,
            "search_target": "files",
            "file_pattern": "*.txt",
            "case_sensitive": False,
        },
        verifier_index=index,
    )


# The three rubric classes the two controls tell apart:
# - good: keyed to content only the golden solution creates
# - vacuous: keyed to content ALREADY in the initial snapshot — passes golden,
#   but also passes doing nothing; only the no-op control catches it
# - broken: keyed to content in neither state — golden validation catches it
GOOD = _pattern_verifier("ver_good", "gorilla population", 0)
VACUOUS = _pattern_verifier("ver_vacuous", "kickoff checklist", 1)
BROKEN = _pattern_verifier("ver_broken", "unicorn dividend", 2)
VERIFIERS = [GOOD, VACUOUS, BROKEN]


async def test_golden_validation_cannot_see_the_vacuous_rubric():
    """On the golden state the vacuous rubric is indistinguishable from the
    good one — both pass; only the broken one fails."""
    results = await validate(
        _initial(),
        _golden(),
        VERIFIERS,
        EVAL_CONFIGS,
        GRADING_SETTINGS,
        skip_trajectory_verifiers=False,
    )
    scores = {r.verifier_id: r.score for r in results}
    assert scores == {"ver_good": 1.0, "ver_vacuous": 1.0, "ver_broken": 0.0}


async def test_no_op_control_flags_the_vacuous_rubric():
    """On the no-op state the good rubric fails as expected, while the vacuous
    rubric passes — the false positive this control exists to catch."""
    results = await validate_no_op(
        _initial(), VERIFIERS, EVAL_CONFIGS, GRADING_SETTINGS
    )
    by_id = {r.verifier_id: r for r in results}
    # All three RAN (pattern_match_check depends on FINAL_ANSWER, which is
    # no-op tolerant): graded results, not neutral skips.
    assert not any(r.verifier_result_values.get("skipped") for r in results)
    assert all(r.status == VerifierResultStatus.OK for r in results)
    assert by_id["ver_good"].score == 0.0
    assert by_id["ver_vacuous"].score == 1.0


async def test_no_op_results_keep_original_verifier_order():
    results = await validate_no_op(
        _initial(), VERIFIERS, EVAL_CONFIGS, GRADING_SETTINGS
    )
    assert [r.verifier_id for r in results] == [v.verifier_id for v in VERIFIERS]


async def test_transcript_only_verifiers_are_skipped_not_failed():
    """A transcript-only eval (tool_call_check) gets a NEUTRAL skip on the
    no-op run — its transcript is empty by construction, so a fail verdict
    would be meaningless (and some transcript-only evals raise outright)."""
    tool_call = Verifier(
        verifier_id="ver_tool_call",
        world_id=None,
        task_id="test_task",
        eval_config_id="ec_tool_call",
        verifier_values={},
        verifier_index=0,
    )
    results = await validate_no_op(
        _initial(), [tool_call], EVAL_CONFIGS, GRADING_SETTINGS
    )
    assert results[0].verifier_result_values.get("skipped") is True
    assert results[0].status == VerifierResultStatus.OK


async def test_dependents_of_skipped_verifiers_are_transitively_skipped():
    """A verifier depending on a no-op-skipped verifier is itself skipped
    (neutral), rather than crashing the run on a missing dependency result."""
    tool_call = Verifier(
        verifier_id="ver_tool_call",
        world_id=None,
        task_id="test_task",
        eval_config_id="ec_tool_call",
        verifier_values={},
        verifier_index=0,
    )
    dependent = Verifier(
        verifier_id="ver_dependent",
        world_id=None,
        task_id="test_task",
        eval_config_id="ec_pattern_match",
        verifier_values={
            "pattern": "gorilla population",
            "search_target": "files",
            "file_pattern": "*.txt",
            "case_sensitive": False,
        },
        verifier_index=1,
        verifier_dependencies=["ver_tool_call"],
    )
    results = await validate_no_op(
        _initial(), [tool_call, dependent], EVAL_CONFIGS, GRADING_SETTINGS
    )
    assert [r.verifier_id for r in results] == ["ver_tool_call", "ver_dependent"]
    assert all(r.verifier_result_values.get("skipped") for r in results)
    assert all(r.status == VerifierResultStatus.OK for r in results)


async def test_no_op_rewinds_an_already_read_snapshot_stream():
    """The initial stream is read once and duplicated internally; a stream
    left at EOF by a prior consumer must not break the run."""
    snapshot = _initial()
    snapshot.read()  # exhaust the stream
    results = await validate_no_op(snapshot, VERIFIERS, EVAL_CONFIGS, GRADING_SETTINGS)
    assert len(results) == len(VERIFIERS)
    assert all(r.status == VerifierResultStatus.OK for r in results)

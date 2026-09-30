"""Batch system-steer judge helper implementing AdvancedIF SystemSteerIFRubricsJudge.

Makes a single LLM call with ALL criteria for a task batched as rubrics,
matching the OSS evaluation approach. Returns a per-criterion result dict
keyed by criteria text.
"""

import inspect
import json
from typing import IO, Literal

from litellm import Choices
from loguru import logger
from pydantic import BaseModel, Field, ValidationError

from runner.evals.output_llm.utils.shared import LLM_JUDGE_TIMEOUT, MAX_JSON_RETRIES
from runner.evals.output_llm_system_steer.main import STEER_FEW_SHOT_EXAMPLES
from runner.models import AgentTrajectoryOutput, GradingSettings, Verifier
from runner.utils.llm import call_llm

EVAL_DEFN_ID = "output_llm_system_steer"

# Mirrors SYSTEM_STEER_JUDGE_PROMPT from AdvancedIF/judge.py — extended with rubrics_passed
SYSTEM_STEER_JUDGE_PROMPT: str = inspect.cleandoc(
    """Your job is to assess if the AI's response to the user's most recent prompt correctly follows the system instructions.
You are provided with a set of rubrics. Rubrics represents the constraints AI response needs to follow. They are summarized from the system instruction and
the conversation history (which is partial hidden from you for simplicity.)
Below are few examples on how you should judge.

<START OF EXAMPLES>
{few_shot_examples}
<END OF EXAMPLES>

The system prompt:
--------------------------------------------------------------
{system_prompt}

--------------------------------------------------------------
User's most recent prompt:
{user_prompt_last_turn}
--------------------------------------------------------------
Here's the AI's response to the user's most recent prompt:
{response_text}
--------------------------------------------------------------

Here are the rubrics:
--------------------------------------------------------------
{rubrics_text}
--------------------------------------------------------------
Your response should be a JSON object with the following schema:
{{
    "grades": [
        {{"question_index": 1, "answer": "answer to question 1 in the rubrics", "passed": "YES" or "NO"}},
        {{"question_index": 2, "answer": "answer to question 2 in the rubrics", "passed": "YES" or "NO"}}
    ],
    "SATISFIED_ALL_REQUIREMENTS": "YES" if the AI's response passes ALL rubrics. "NO" otherwise.
}}
Return exactly one grade object for every rubric question, with "question_index" set to the question's 1-based position and "passed" set to "YES" if the AI's response passes that question and "NO" otherwise."""
)


class SystemSteerCriterionGrade(BaseModel):
    question_index: int = Field(
        description="1-based index of the rubric question this grade is for, matching its position in the rubrics list."
    )
    answer: str = Field(
        description="Your assessment of how the AI's response addresses this rubric question."
    )
    passed: Literal["YES", "NO"] = Field(
        description="YES if the AI's response passes this rubric question, NO otherwise."
    )


class SystemSteerJudgeBatchResponse(BaseModel):
    # A list of per-question verdicts (not a dict) so it can be passed to the
    # provider as a structured-output `response_format`: the shape is enforced
    # server-side, and `passed` is constrained to YES/NO. This replaces the old
    # loose json_object mode, under which the judge intermittently returned
    # syntactically-valid-but-mis-shaped verdicts that failed the completeness
    # check and surfaced as re-gradeable errors.
    grades: list[SystemSteerCriterionGrade]
    SATISFIED_ALL_REQUIREMENTS: Literal["YES", "NO"]


class SystemSteerCriterionResult(BaseModel):
    judge_grade: str
    grade_rationale: str
    satisfied_all_requirements: str
    rubrics_check: dict[str, str]
    rubrics_passed: dict[str, str]


def _extract_system_steer_context(trajectory: AgentTrajectoryOutput) -> tuple[str, str]:
    """Extract (system_prompt, user_prompt_last_turn). Mirrors SystemSteerIFRubricsJudge."""
    if not trajectory or not trajectory.messages:
        return "", ""

    messages = list(trajectory.messages)
    while messages and messages[-1].get("role") == "assistant":
        messages.pop()

    system_prompt = ""
    for msg in messages:
        if msg.get("role") == "system":
            system_prompt = str(msg.get("content", ""))
            break

    user_prompt_last_turn = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            user_prompt_last_turn = str(msg.get("content", ""))
            break

    return system_prompt, user_prompt_last_turn


async def if_system_steer_judge_result_helper(
    initial_snapshot_bytes: IO[bytes],
    final_snapshot_bytes: IO[bytes],
    trajectory: AgentTrajectoryOutput,
    verifiers: list[Verifier],
    eval_defn_id_by_config_id: dict[str, str],
    grading_settings: GradingSettings,
) -> dict[str, SystemSteerCriterionResult]:
    """Batch-grade all output_llm_system_steer criteria in one LLM call.

    Returns a dict mapping criteria text → SystemSteerCriterionResult.
    """
    steer_config_ids = {
        ec_id
        for ec_id, defn_id in eval_defn_id_by_config_id.items()
        if defn_id == EVAL_DEFN_ID
    }
    steer_verifiers = [v for v in verifiers if v.eval_config_id in steer_config_ids]

    if not steer_verifiers:
        return {}

    ordered = sorted(steer_verifiers, key=lambda v: v.verifier_index)
    criteria_list = [v.verifier_values.get("criteria", "") for v in ordered]

    final_answer = ""
    if trajectory.messages:
        last = trajectory.messages[-1]
        final_answer = str(last.get("content", ""))

    system_prompt, user_prompt_last_turn = _extract_system_steer_context(trajectory)
    rubrics_text = json.dumps(criteria_list, indent=4)

    prompt = SYSTEM_STEER_JUDGE_PROMPT.format(
        few_shot_examples=STEER_FEW_SHOT_EXAMPLES,
        system_prompt=system_prompt,
        user_prompt_last_turn=user_prompt_last_turn,
        response_text=final_answer,
        rubrics_text=rubrics_text,
    )
    messages = [{"role": "user", "content": prompt}]

    logger.info(
        f"[HELPER][SYSTEM_STEER_JUDGE] criteria={len(criteria_list)} | "
        f"system_prompt_len={len(system_prompt)}"
    )

    parsed: SystemSteerJudgeBatchResponse | None = None
    for attempt in range(MAX_JSON_RETRIES):
        response = await call_llm(
            model=grading_settings.llm_judge_model,
            messages=messages,
            timeout=LLM_JUDGE_TIMEOUT,
            extra_args=grading_settings.llm_judge_extra_args,
            # Pass the Pydantic model as response_format — litellm translates it
            # into the provider's structured-output protocol so the shape is
            # enforced server-side instead of relying on prompt-only instructions.
            response_format=SystemSteerJudgeBatchResponse,
        )
        choices = response.choices
        if not choices or not isinstance(choices[0], Choices):
            logger.warning(
                f"[HELPER][SYSTEM_STEER_JUDGE] retry {attempt + 1}/{MAX_JSON_RETRIES}: empty response"
            )
            continue
        raw = choices[0].message.content
        if not raw:
            logger.warning(
                f"[HELPER][SYSTEM_STEER_JUDGE] retry {attempt + 1}/{MAX_JSON_RETRIES}: empty content"
            )
            continue
        try:
            candidate = SystemSteerJudgeBatchResponse.model_validate_json(raw)
        except ValidationError as e:
            logger.warning(
                f"[HELPER][SYSTEM_STEER_JUDGE] retry {attempt + 1}/{MAX_JSON_RETRIES}: {e}"
            )
            continue
        # Structured output enforces the per-grade shape (and YES/NO), but the
        # judge can still return a grade for only a subset of questions. Require
        # a verdict for every criterion; otherwise retry, and ultimately score
        # every criterion fail (the `parsed is None` branch below) rather than
        # grading against a partial verdict set.
        graded_indices = {g.question_index for g in candidate.grades}
        missing = [
            i + 1 for i in range(len(criteria_list)) if (i + 1) not in graded_indices
        ]
        if missing:
            logger.warning(
                f"[HELPER][SYSTEM_STEER_JUDGE] retry {attempt + 1}/{MAX_JSON_RETRIES}: "
                f"incomplete verdicts, {len(missing)}/{len(criteria_list)} "
                "criteria missing a grade"
            )
            continue
        parsed = candidate
        break

    if parsed is None:
        # Structured output failed to produce a complete verdict set even after
        # MAX_JSON_RETRIES. Rather than raise a hard grading error (a red dot on
        # the coverage tab that keeps the leaderboard from being fully graded),
        # score every criterion 0/fail. With provider-enforced structured output
        # this path is rare; when it hits, a conservative fail beats leaving the
        # trajectory un-gradeable. The rationale marks these so they stay
        # auditable rather than looking like a genuine judge verdict.
        logger.error(
            f"[HELPER][SYSTEM_STEER_JUDGE] no complete verdict set after "
            f"{MAX_JSON_RETRIES} attempts; scoring all {len(criteria_list)} "
            "criteria as fail"
        )
        return {
            criteria: SystemSteerCriterionResult(
                judge_grade="fail",
                grade_rationale=(
                    "Judge did not return a complete structured verdict after "
                    f"{MAX_JSON_RETRIES} attempts; scored as fail."
                ),
                satisfied_all_requirements="NO",
                rubrics_check={f"question_{i + 1}": ""},
                rubrics_passed={f"question_{i + 1}": "NO"},
            )
            for i, criteria in enumerate(criteria_list)
        }

    by_index = {g.question_index: g for g in parsed.grades}
    results: dict[str, SystemSteerCriterionResult] = {}
    for i, criteria in enumerate(criteria_list):
        key = f"question_{i + 1}"
        grade = by_index[i + 1]
        rationale = grade.answer
        passed = grade.passed == "YES"
        results[criteria] = SystemSteerCriterionResult(
            judge_grade="pass" if passed else "fail",
            grade_rationale=rationale,
            satisfied_all_requirements="YES" if passed else "NO",
            rubrics_check={key: rationale},
            rubrics_passed={key: grade.passed},
        )

    logger.info(
        f"[HELPER][SYSTEM_STEER_JUDGE] done | "
        f"pass={sum(1 for r in results.values() if r.judge_grade == 'pass')}/{len(results)}"
    )
    return results

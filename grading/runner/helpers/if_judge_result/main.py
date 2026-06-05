"""Batch IF judge helper implementing AdvancedIF IFRubricsJudge.

Makes a single LLM call with ALL criteria for a task batched as rubrics,
matching the OSS evaluation approach. Returns a per-criterion result dict
keyed by criteria text.
"""

import inspect
import io
import json

from litellm import Choices
from loguru import logger
from pydantic import BaseModel, ValidationError

from runner.evals.output_llm.utils.shared import LLM_JUDGE_TIMEOUT, MAX_JSON_RETRIES
from runner.models import AgentTrajectoryOutput, GradingSettings, Verifier
from runner.utils.llm import call_llm

EVAL_DEFN_ID = "output_llm_with_system"

# Mirrors JUDGE_PROMPT from AdvancedIF/judge.py — extended with rubrics_passed for per-criterion scoring
JUDGE_PROMPT: str = inspect.cleandoc(
    """
Your job is to assess if the AI's response to the user's most recent prompt correctly follows the user's instructions

The conversation history:
--------------------------------------------------------------
{full_conversation}
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
Your response should be a JSON blob with the following schema:
{{
    "rubrics_check":{{
        "question_1": "answer to question 1 in the rubrics",
        "question_2": "answer to question 2 in the rubrics",
        ...
    }},
    "rubrics_passed":{{
        "question_1": "YES if the AI's response passes question 1. NO otherwise.",
        "question_2": "YES if the AI's response passes question 2. NO otherwise.",
        ...
    }},
    "SATISFIED_ALL_REQUIREMENTS": "YES" if the AI's response passes ALL rubrics. "NO" otherwise.
}}
    """
)


class IFJudgeBatchResponse(BaseModel):
    rubrics_check: dict[str, str]
    rubrics_passed: dict[str, str]
    SATISFIED_ALL_REQUIREMENTS: str


class IFCriterionResult(BaseModel):
    judge_grade: str
    grade_rationale: str
    satisfied_all_requirements: str
    rubrics_check: dict[str, str]
    rubrics_passed: dict[str, str]


def _extract_if_context(trajectory: AgentTrajectoryOutput) -> tuple[str, str]:
    """Extract (full_conversation, user_prompt_last_turn). Mirrors IFRubricsJudge._format_conversation_history."""
    if not trajectory or not trajectory.messages:
        return "", ""

    messages = list(trajectory.messages)
    while messages and messages[-1].get("role") == "assistant":
        messages.pop()

    if not messages:
        return "", ""

    user_prompt_last_turn = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            user_prompt_last_turn = str(msg.get("content", ""))
            break

    context_messages = messages[:-1]
    formatted: list[str] = []
    turn = 1
    for msg in context_messages:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role and content:
            formatted.append(f"{role} [{turn}]: {content}")
            if role == "assistant":
                turn += 1

    return "\n".join(formatted), user_prompt_last_turn


async def if_judge_result_helper(
    initial_snapshot_bytes: io.BytesIO,
    final_snapshot_bytes: io.BytesIO,
    trajectory: AgentTrajectoryOutput,
    verifiers: list[Verifier],
    eval_defn_id_by_config_id: dict[str, str],
    grading_settings: GradingSettings,
) -> dict[str, IFCriterionResult]:
    """Batch-grade all output_llm_with_system criteria in one LLM call.

    Returns a dict mapping criteria text → IFCriterionResult.
    """
    if_config_ids = {
        ec_id
        for ec_id, defn_id in eval_defn_id_by_config_id.items()
        if defn_id == EVAL_DEFN_ID
    }
    if_verifiers = [v for v in verifiers if v.eval_config_id in if_config_ids]

    if not if_verifiers:
        return {}

    # Collect criteria in verifier_index order for stable question_N mapping
    ordered = sorted(if_verifiers, key=lambda v: v.verifier_index)
    criteria_list = [v.verifier_values.get("criteria", "") for v in ordered]

    # Extract final answer and conversation context from trajectory
    final_answer = ""
    if trajectory.messages:
        last = trajectory.messages[-1]
        final_answer = str(last.get("content", ""))

    full_conversation, user_prompt_last_turn = _extract_if_context(trajectory)
    rubrics_text = json.dumps(criteria_list, indent=4)

    prompt = JUDGE_PROMPT.format(
        full_conversation=full_conversation,
        user_prompt_last_turn=user_prompt_last_turn,
        response_text=final_answer,
        rubrics_text=rubrics_text,
    )
    messages = [{"role": "user", "content": prompt}]

    logger.info(
        f"[HELPER][IF_JUDGE] criteria={len(criteria_list)} | "
        f"history_turns={full_conversation.count('[') // 2}"
    )

    parsed: IFJudgeBatchResponse | None = None
    for attempt in range(MAX_JSON_RETRIES):
        response = await call_llm(
            model=grading_settings.llm_judge_model,
            messages=messages,
            timeout=LLM_JUDGE_TIMEOUT,
            extra_args=grading_settings.llm_judge_extra_args,
            response_format={"type": "json_object"},
        )
        choices = response.choices
        if not choices or not isinstance(choices[0], Choices):
            logger.warning(
                f"[HELPER][IF_JUDGE] retry {attempt + 1}/{MAX_JSON_RETRIES}: empty response"
            )
            continue
        raw = choices[0].message.content
        if not raw:
            logger.warning(
                f"[HELPER][IF_JUDGE] retry {attempt + 1}/{MAX_JSON_RETRIES}: empty content"
            )
            continue
        try:
            parsed = IFJudgeBatchResponse.model_validate_json(raw)
            break
        except ValidationError as e:
            logger.warning(
                f"[HELPER][IF_JUDGE] retry {attempt + 1}/{MAX_JSON_RETRIES}: {e}"
            )
            continue

    if parsed is None:
        raise ValueError(
            f"IF judge batch: invalid JSON after {MAX_JSON_RETRIES} attempts"
        )

    results: dict[str, IFCriterionResult] = {}
    for i, criteria in enumerate(criteria_list):
        key = f"question_{i + 1}"
        rationale = parsed.rubrics_check.get(key, "")
        passed_str = parsed.rubrics_passed.get(key, "NO").strip().upper()
        passed = passed_str == "YES"
        results[criteria] = IFCriterionResult(
            judge_grade="pass" if passed else "fail",
            grade_rationale=rationale,
            satisfied_all_requirements="YES" if passed else "NO",
            rubrics_check={key: rationale},
            rubrics_passed={key: passed_str},
        )

    logger.info(
        f"[HELPER][IF_JUDGE] done | "
        f"pass={sum(1 for r in results.values() if r.judge_grade == 'pass')}/{len(results)}"
    )
    return results

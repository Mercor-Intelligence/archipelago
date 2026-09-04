"""Batch user-sim judge helper.

Grades the simulated user's behavior against rubrics summarized from the
user-sim system prompt. Mirrors the shape of if_system_steer_judge_result but
the prompt framing is sim-POV: the system prompt is the sim's steer, the
last non-sim message is what the agent said to the sim, and the response
being graded is the sim's output.

Supports a `grade_target` key in verifier_values for multi-turn grading:
    "last_turn"        (default) grade the final sim reply in the trajectory
    "turn_N"           grade the Nth (agent, sim) pair, 1-indexed
    "full_conversation" render the entire transcript for the judge

Verifiers with different grade_target values trigger one LLM call per group;
verifiers sharing a grade_target are batched into a single call.
"""

import inspect
import json
import re
from typing import IO, Any, cast

from litellm import Choices
from loguru import logger
from pydantic import BaseModel, ValidationError

from runner.evals.output_llm.utils.shared import LLM_JUDGE_TIMEOUT, MAX_JSON_RETRIES
from runner.evals.user_sim_judge.main import USER_SIM_FEW_SHOT_EXAMPLES
from runner.models import AgentTrajectoryOutput, GradingSettings, Verifier
from runner.utils.llm import call_llm

EVAL_DEFN_ID = "user_sim_judge"

# Single-turn template: used for last_turn and turn_N. Renders the agent's
# message to the sim and the sim's response as a Q-A pair.
USER_SIM_JUDGE_PROMPT_SINGLE_TURN: str = inspect.cleandoc(
    """Your job is to assess whether the simulated user's response correctly follows the user-sim steering instructions.

In this evaluation, a model is playing the role of a simulated user. You are judging that model's behavior as the simulated user — not an assistant. The system prompt below defines the simulated user's role, goals, and constraints. The rubrics describe what the simulated user's response should or should not contain.

Below are examples on how to judge.

<START OF EXAMPLES>
{few_shot_examples}
<END OF EXAMPLES>

The user-sim steering system prompt:
--------------------------------------------------------------
{system_prompt}

--------------------------------------------------------------
Agent's most recent message to the simulated user:
{agent_prompt}
--------------------------------------------------------------
The simulated user's response to grade:
{response_text}
--------------------------------------------------------------

Here are the rubrics:
--------------------------------------------------------------
{rubrics_text}
--------------------------------------------------------------
Your response should be a JSON blob with the following schema:
{{
    "rubrics_check": {{
        "question_1": "answer to question 1 in the rubrics",
        "question_2": "answer to question 2 in the rubrics",
        ...
    }},
    "rubrics_passed": {{
        "question_1": "YES if the simulated user's response passes question 1. NO otherwise.",
        "question_2": "YES if the simulated user's response passes question 2. NO otherwise.",
        ...
    }},
    "SATISFIED_ALL_REQUIREMENTS": "YES" if the simulated user's response passes ALL rubrics. "NO" otherwise.
}}"""
)

# Multi-turn template: used for full_conversation. Frames the input as a
# transcript so the judge evaluates behavior across all turns instead of
# treating it as a single Q-A pair.
USER_SIM_JUDGE_PROMPT_FULL_CONVERSATION: str = inspect.cleandoc(
    """Your job is to assess whether the simulated user's behavior across an entire multi-turn conversation correctly follows the user-sim steering instructions.

In this evaluation, a model is playing the role of a simulated user. You are judging that model's behavior as the simulated user — not an assistant. The system prompt below defines the simulated user's role, goals, and constraints. The rubrics describe what the simulated user's behavior should or should not exhibit. Evaluate the sim's behavior holistically across every turn shown in the transcript.

Below are examples on how to judge.

<START OF EXAMPLES>
{few_shot_examples}
<END OF EXAMPLES>

The user-sim steering system prompt:
--------------------------------------------------------------
{system_prompt}

--------------------------------------------------------------
--- MULTI-TURN TRANSCRIPT (evaluate the simulated user's behavior across all turns) ---
{transcript}
--------------------------------------------------------------

Here are the rubrics:
--------------------------------------------------------------
{rubrics_text}
--------------------------------------------------------------
Your response should be a JSON blob with the following schema:
{{
    "rubrics_check": {{
        "question_1": "answer to question 1 in the rubrics",
        "question_2": "answer to question 2 in the rubrics",
        ...
    }},
    "rubrics_passed": {{
        "question_1": "YES if the simulated user's behavior across the transcript passes question 1. NO otherwise.",
        "question_2": "YES if the simulated user's behavior across the transcript passes question 2. NO otherwise.",
        ...
    }},
    "SATISFIED_ALL_REQUIREMENTS": "YES" if the simulated user's behavior passes ALL rubrics. "NO" otherwise.
}}"""
)


class UserSimJudgeBatchResponse(BaseModel):
    rubrics_check: dict[str, str]
    rubrics_passed: dict[str, str]
    SATISFIED_ALL_REQUIREMENTS: str


class UserSimCriterionResult(BaseModel):
    judge_grade: str
    grade_rationale: str
    satisfied_all_requirements: str
    rubrics_check: dict[str, str]
    rubrics_passed: dict[str, str]


GRADE_TARGET_LAST_TURN = "last_turn"
GRADE_TARGET_FULL_CONVERSATION = "full_conversation"
_TURN_N_RE = re.compile(r"^turn_(\d+)$")


def _normalize_grade_target(raw: str) -> str:
    """Normalize a verifier's grade_target value to a canonical form.

    Accepts: "" (→ last_turn), "last_turn", "full_conversation", "turn_N".
    Unknown strings (e.g. "turn_foo", "third") fall back to last_turn with a
    warning. This is parser-level fallback only — out-of-range turn_N (e.g.
    turn_5 on a 3-turn trajectory) is treated as a misconfiguration and
    surfaced as a verifier failure downstream, not silently regraded.
    """
    cleaned = (raw or "").strip()
    if not cleaned:
        return GRADE_TARGET_LAST_TURN
    if cleaned in {GRADE_TARGET_LAST_TURN, GRADE_TARGET_FULL_CONVERSATION}:
        return cleaned
    if _TURN_N_RE.match(cleaned):
        return cleaned
    logger.warning(
        f"[HELPER][USER_SIM_JUDGE] unknown grade_target={raw!r}, "
        f"falling back to {GRADE_TARGET_LAST_TURN}"
    )
    return GRADE_TARGET_LAST_TURN


def _system_prompt(messages: list[dict[str, Any]]) -> str:
    for msg in messages:
        if msg.get("role") == "system":
            return str(msg.get("content", ""))
    return ""


def _chat_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return only user/assistant messages.

    Filtering tool messages is required before any index math: harness sims
    interleave tool calls/results between user/assistant turns, so positional
    pairing (even=agent, odd=sim) only holds after they're stripped.
    """
    return [m for m in messages if m.get("role") in {"user", "assistant"}]


def _render_transcript(chat: list[dict[str, Any]]) -> str:
    """Render chat messages as a labeled multi-turn transcript.

    Convention (per configurable_user_sim_agent — the SOT user-sim harness;
    simple_user_sim_agent uses the same convention): the trajectory begins
    with the simulated user's task brief at chat[0] (role=user). After the
    brief, the conversation alternates inner-agent response (role=assistant)
    and simulated-user reply (role=user). The brief is rendered separately;
    pairs from chat[1:] onward are 1-indexed as turns.
    """
    if not chat:
        return ""
    lines: list[str] = [f"[Initial brief] Sim: {str(chat[0].get('content', ''))}"]
    turn = 0
    i = 1
    while i < len(chat):
        turn += 1
        agent = chat[i]
        lines.append(f"[Turn {turn}] Agent: {str(agent.get('content', ''))}")
        if i + 1 < len(chat):
            sim = chat[i + 1]
            lines.append(f"[Turn {turn}] Sim:   {str(sim.get('content', ''))}")
        i += 2
    return "\n".join(lines)


class _OutOfRangeTurn(Exception):
    """turn_N requested but trajectory has fewer turns than N."""

    requested: int
    available: int

    def __init__(self, requested: int, available: int) -> None:
        super().__init__(
            f"turn {requested} requested but trajectory has {available} turn(s)"
        )
        self.requested = requested
        self.available = available


def _extract_single_turn_pair(
    chat: list[dict[str, Any]], turn_index: int
) -> tuple[str, str]:
    """Return (agent_prompt, sim_reply) for the given 1-indexed turn.

    Configurable convention: chat[0] is the simulated user's initial brief;
    turn 1 is the first (agent, sim) pair after the brief. So turn N maps
    to (chat[2N - 1], chat[2N]).

    Raises _OutOfRangeTurn if the requested pair is not present.
    """
    # Pairs available after the initial brief at chat[0]
    available = max(0, (len(chat) - 1) // 2)
    if turn_index < 1 or turn_index > available:
        raise _OutOfRangeTurn(turn_index, available)
    agent_idx = 1 + (turn_index - 1) * 2
    sim_idx = agent_idx + 1
    return (
        str(chat[agent_idx].get("content", "")),
        str(chat[sim_idx].get("content", "")),
    )


def _extract_last_turn_pair(chat: list[dict[str, Any]]) -> tuple[str, str]:
    if not chat:
        return "", ""
    response_text = str(chat[-1].get("content", ""))
    agent_prompt = str(chat[-2].get("content", "")) if len(chat) >= 2 else ""
    return agent_prompt, response_text


def _build_prompt_for_target(
    grade_target: str,
    chat: list[dict[str, Any]],
    system_prompt: str,
    rubrics_text: str,
) -> str:
    """Build the full judge prompt for one grade_target group.

    Raises _OutOfRangeTurn if turn_N is out of range so the caller can
    surface a verifier-level failure instead of silently regrading.
    """
    if grade_target == GRADE_TARGET_FULL_CONVERSATION:
        transcript = _render_transcript(chat)
        return USER_SIM_JUDGE_PROMPT_FULL_CONVERSATION.format(
            few_shot_examples=USER_SIM_FEW_SHOT_EXAMPLES,
            system_prompt=system_prompt,
            transcript=transcript,
            rubrics_text=rubrics_text,
        )

    if grade_target == GRADE_TARGET_LAST_TURN:
        agent_prompt, response_text = _extract_last_turn_pair(chat)
    else:
        match = _TURN_N_RE.match(grade_target)
        assert match, (
            f"non-canonical grade_target reached prompt builder: {grade_target!r}"
        )
        agent_prompt, response_text = _extract_single_turn_pair(
            chat, int(match.group(1))
        )

    return USER_SIM_JUDGE_PROMPT_SINGLE_TURN.format(
        few_shot_examples=USER_SIM_FEW_SHOT_EXAMPLES,
        system_prompt=system_prompt,
        agent_prompt=agent_prompt,
        response_text=response_text,
        rubrics_text=rubrics_text,
    )


async def _judge_one_group(
    prompt: str,
    grading_settings: GradingSettings,
) -> UserSimJudgeBatchResponse:
    """Run the LLM judge for one grade_target group with JSON-retry."""
    messages = [{"role": "user", "content": prompt}]
    parsed: UserSimJudgeBatchResponse | None = None
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
                f"[HELPER][USER_SIM_JUDGE] retry {attempt + 1}/{MAX_JSON_RETRIES}: empty response"
            )
            continue
        raw = choices[0].message.content
        if not raw:
            logger.warning(
                f"[HELPER][USER_SIM_JUDGE] retry {attempt + 1}/{MAX_JSON_RETRIES}: empty content"
            )
            continue
        try:
            parsed = UserSimJudgeBatchResponse.model_validate_json(raw)
            break
        except ValidationError as e:
            logger.warning(
                f"[HELPER][USER_SIM_JUDGE] retry {attempt + 1}/{MAX_JSON_RETRIES}: {e}"
            )
            continue

    if parsed is None:
        raise ValueError(
            f"User sim judge batch: invalid JSON after {MAX_JSON_RETRIES} attempts"
        )
    return parsed


def _result_for_misconfig(rationale: str) -> UserSimCriterionResult:
    """Build a fail result that surfaces a misconfiguration to the user."""
    return UserSimCriterionResult(
        judge_grade="fail",
        grade_rationale=rationale,
        satisfied_all_requirements="NO",
        rubrics_check={"misconfiguration": rationale},
        rubrics_passed={"misconfiguration": "NO"},
    )


async def user_sim_judge_result_helper(
    initial_snapshot_bytes: IO[bytes],
    final_snapshot_bytes: IO[bytes],
    trajectory: AgentTrajectoryOutput,
    verifiers: list[Verifier],
    eval_defn_id_by_config_id: dict[str, str],
    grading_settings: GradingSettings,
) -> dict[str, UserSimCriterionResult]:
    """Batch-grade user_sim_judge criteria.

    Verifiers are grouped by their `grade_target`. Each group becomes one LLM
    call (criteria within a group are batched). When all verifiers share a
    target — the common case today — a single call covers everything.
    """
    sim_config_ids = {
        ec_id
        for ec_id, defn_id in eval_defn_id_by_config_id.items()
        if defn_id == EVAL_DEFN_ID
    }
    sim_verifiers = [v for v in verifiers if v.eval_config_id in sim_config_ids]

    if not sim_verifiers:
        return {}

    ordered = sorted(sim_verifiers, key=lambda v: v.verifier_index)

    raw_messages: list[dict[str, Any]] = (
        cast(list[dict[str, Any]], list(trajectory.messages))
        if trajectory and trajectory.messages
        else []
    )
    system_prompt = _system_prompt(raw_messages)
    chat = _chat_messages(raw_messages)

    groups: dict[str, list[Verifier]] = {}
    group_order: list[str] = []
    for v in ordered:
        target = _normalize_grade_target(str(v.verifier_values.get("grade_target", "")))
        if target not in groups:
            groups[target] = []
            group_order.append(target)
        groups[target].append(v)

    logger.info(
        f"[HELPER][USER_SIM_JUDGE] verifiers={len(ordered)} | groups={len(groups)} | "
        f"targets={group_order} | chat_messages={len(chat)}"
    )

    results: dict[str, UserSimCriterionResult] = {}

    for target in group_order:
        group_verifiers = groups[target]
        criteria_list = [v.verifier_values.get("criteria", "") for v in group_verifiers]
        rubrics_text = json.dumps(criteria_list, indent=4)

        try:
            prompt = _build_prompt_for_target(target, chat, system_prompt, rubrics_text)
        except _OutOfRangeTurn as e:
            rationale = (
                f"grade_target={target!r} requested but trajectory has only "
                f"{e.available} (agent, sim) turn(s); not regrading."
            )
            logger.warning(f"[HELPER][USER_SIM_JUDGE] {rationale}")
            for criteria in criteria_list:
                results[criteria] = _result_for_misconfig(rationale)
            continue

        logger.info(
            f"[HELPER][USER_SIM_JUDGE] group target={target} | criteria={len(criteria_list)} | "
            f"system_prompt_len={len(system_prompt)}"
        )

        parsed = await _judge_one_group(prompt, grading_settings)

        for i, criteria in enumerate(criteria_list):
            key = f"question_{i + 1}"
            rationale = parsed.rubrics_check.get(key, "")
            passed_str = parsed.rubrics_passed.get(key, "NO").strip().upper()
            passed = passed_str == "YES"
            results[criteria] = UserSimCriterionResult(
                judge_grade="pass" if passed else "fail",
                grade_rationale=rationale,
                satisfied_all_requirements="YES" if passed else "NO",
                rubrics_check={key: rationale},
                rubrics_passed={key: passed_str},
            )

    logger.info(
        f"[HELPER][USER_SIM_JUDGE] done | "
        f"pass={sum(1 for r in results.values() if r.judge_grade == 'pass')}/{len(results)}"
    )
    return results

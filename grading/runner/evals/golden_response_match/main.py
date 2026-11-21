"""Golden response match eval - compares agent answer to a golden response."""

from __future__ import annotations

from litellm import Choices
from pydantic import BaseModel, Field

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult, VerifierResultStatus
from runner.utils.llm import build_messages, call_llm

# Default timeout for LLM judge calls (3 minutes)
LLM_JUDGE_TIMEOUT = 180

SYSTEM_PROMPT = """You are an expert grader comparing answers.

CRITICAL: Focus on content equivalence, not formatting differences.

IGNORE these formatting differences:
- \\boxed{} wrappers around answers
- Extra whitespace, newlines, punctuation
- Different but equivalent representations (mathematical, textual, etc.)
- Capitalization differences

EXAMPLES of MATCHING answers:
- "42" vs "\\boxed{42}" -> SAME (ignore wrapper)
- "\\frac{1}{2}" vs "0.5" -> SAME (equivalent values)

When comparing numerical answers:
- Treat numbers as equivalent if they represent the same quantity within normal rounding uncertainty (difference < ~1% or within one significant figure: 2.67 ~= 2.7, 0.500 ~= 0.5).
- However, do not treat them as equivalent if the difference suggests reduced precision or rounding to a different order of magnitude (e.g., 2.67 vs 2.6 or 0.965 vs 0.96 when precision appears deliberate in the golden answer).
- If the golden answer appears rounded to a fixed precision (e.g., 2 significant figures), the grading answer should match that same level of precision or be numerically identical when rounded to that level.

If the difference indicates a fundamentally different value or scale (e.g., 2.7 vs 27, 1.2 vs 12), consider them not equivalent.

Return JSON with fields: is_match (boolean) and rationale (string)."""

USER_PROMPT_TEMPLATE = """Golden answer:
{golden_response}

Candidate answer:
{candidate_answer}

Context (optional):
{problem_context}

Domain instructions (optional):
{domain_instructions}
"""


class GoldenResponseMatchSchema(BaseModel):
    rationale: str = Field(description="Concise explanation for the judgment")
    is_match: bool = Field(description="Whether the answers are equivalent")


def _format_optional_text(value: str | None) -> str:
    text = (value or "").strip()
    return text if text else "None"


async def golden_response_match_eval(input: EvalImplInput) -> VerifierResult:
    """Grade candidate answer by comparing to golden response."""
    verifier_values = input.verifier.verifier_values or {}

    golden_response_raw = verifier_values.get("golden_response")
    if golden_response_raw is None:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="Missing required field: golden_response",
        )
    golden_response = str(golden_response_raw).strip()
    if not golden_response:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="Missing required field: golden_response",
        )

    if not input.helper_results or HelperIds.FINAL_ANSWER not in input.helper_results:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="Missing helper results for final answer",
        )

    candidate_answer = str(input.helper_results[HelperIds.FINAL_ANSWER] or "").strip()
    if not candidate_answer:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            verifier_result_values={
                "result": 0,
                "reason": "Missing candidate answer",
            },
        )

    problem_context = _format_optional_text(verifier_values.get("problem_context"))
    domain_instructions = _format_optional_text(
        verifier_values.get("domain_instructions")
    )

    user_prompt = USER_PROMPT_TEMPLATE.format(
        golden_response=golden_response,
        candidate_answer=candidate_answer,
        problem_context=problem_context,
        domain_instructions=domain_instructions,
    )

    model = input.grading_settings.llm_judge_model
    extra_args = input.grading_settings.llm_judge_extra_args or {}

    try:
        messages = build_messages(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )
        response = await call_llm(
            model=model,
            messages=messages,
            timeout=LLM_JUDGE_TIMEOUT,
            extra_args=extra_args,
            response_format=GoldenResponseMatchSchema,
        )

        choices = response.choices
        if not choices or not isinstance(choices[0], Choices):
            raise ValueError("LLM returned empty response")

        raw_content = choices[0].message.content
        if not raw_content:
            raise ValueError("LLM returned empty content")

        parsed = GoldenResponseMatchSchema.model_validate_json(raw_content)
        result = 1 if parsed.is_match else 0

        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=float(result),
            verifier_result_values={
                "result": result,
                "reason": parsed.rationale,
            },
        )

    except Exception as e:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={"error": str(e)},
            message=f"Golden response grading failed: {str(e)}",
        )

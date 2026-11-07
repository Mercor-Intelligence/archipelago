"""HLE Judge eval — grades model responses using the exact OSS judge prompt.

The judge extracts the model's final answer and determines correctness,
handling equivalent numerical/algebraic formats. Works identically for both
MCQ (answer is a letter) and short-answer (answer is free text) questions.

Source (verbatim prompt):
  https://github.com/centerforaisafety/hle/blob/main/hle_eval/run_judge_results.py
"""

from __future__ import annotations

from typing import Literal

from litellm import Choices
from loguru import logger
from pydantic import BaseModel

from runner.evals.models import EvalImplInput
from runner.models import VerifierResult, VerifierResultStatus
from runner.utils.llm import call_llm
from runner.utils.trajectory import extract_final_assistant_response

# Default timeout for the judge LLM call (3 minutes)
LLM_JUDGE_TIMEOUT = 180

# Verbatim judge prompt from the OSS harness.
# Source: https://github.com/centerforaisafety/hle/blob/main/hle_eval/run_judge_results.py
# The OSS harness sends a single user message with no system prompt.
JUDGE_PROMPT_TEMPLATE = """\
Judge whether the following [response] to [question] is correct or not based on the precise and unambiguous [correct_answer] below.

[question]: {question}

[response]: {response}

Your judgement must be in the format and criteria specified below:

extracted_final_answer: The final exact answer extracted from the [response]. Put the extracted answer as 'None' if there is no exact, final answer to extract from the response.

[correct_answer]: {correct_answer}

reasoning: Explain why the extracted_final_answer is correct or incorrect based on [correct_answer], focusing only on if there are meaningful differences between [correct_answer] and the extracted_final_answer. Do not comment on any background to the problem, do not attempt to solve the problem, do not argue for any answer different than [correct_answer], focus only on whether the answers match.

correct: Answer 'yes' if extracted_final_answer matches the [correct_answer] given above, or is within a small margin of error for numerical problems. Answer 'no' otherwise, i.e. if there if there is any inconsistency, ambiguity, non-equivalency, or if the extracted answer is incorrect.

confidence: The extracted confidence score between 0% and 100% from [response]. Put 100 if there is no confidence score available.\
"""


class HLEJudgeResponse(BaseModel):
    extracted_final_answer: str
    reasoning: str
    correct: Literal["yes", "no"]
    confidence: int


async def hle_judge_eval(input: EvalImplInput) -> VerifierResult:
    """Grade an HLE question using the OSS-faithful LLM judge."""
    verifier_values = input.verifier.verifier_values or {}
    task_id = input.verifier.task_id or "unknown"

    correct_answer = str(verifier_values.get("hle_correct_answer", "")).strip()
    if not correct_answer:
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="Missing required field: hle_correct_answer",
        )

    question = str(verifier_values.get("hle_question", "")).strip()
    final_response = extract_final_assistant_response(input)

    if not final_response:
        logger.info(f"[HLE_JUDGE] task={task_id} | no assistant response → score=0.0")
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            verifier_result_values={
                "extracted_final_answer": "None",
                "reasoning": "No assistant response found in trajectory.",
                "correct": "no",
                "confidence": 100,
            },
        )

    judge_prompt = JUDGE_PROMPT_TEMPLATE.format(
        question=question,
        response=final_response,
        correct_answer=correct_answer,
    )

    model = input.grading_settings.llm_judge_model
    extra_args = input.grading_settings.llm_judge_extra_args or {}

    try:
        # Single user message — no system prompt, matching the OSS harness.
        messages = [{"role": "user", "content": judge_prompt}]
        response = await call_llm(
            model=model,
            messages=messages,
            timeout=LLM_JUDGE_TIMEOUT,
            extra_args=extra_args,
            response_format=HLEJudgeResponse,
        )

        choices = response.choices
        if not choices or not isinstance(choices[0], Choices):
            raise ValueError("LLM returned empty response")

        raw_content = choices[0].message.content
        if not raw_content:
            raise ValueError("LLM returned empty content")

        parsed = HLEJudgeResponse.model_validate_json(raw_content)
        score = 1.0 if parsed.correct == "yes" else 0.0

        logger.info(
            f"[HLE_JUDGE] task={task_id} | correct={parsed.correct} score={score} "
            f"| extracted={parsed.extracted_final_answer!r}"
        )

        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=score,
            verifier_result_values={
                "extracted_final_answer": parsed.extracted_final_answer,
                "reasoning": parsed.reasoning,
                "correct": parsed.correct,
                "confidence": parsed.confidence,
            },
        )

    except Exception as e:
        logger.error(f"[HLE_JUDGE] task={task_id} | judge call failed: {e}")
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={"error": str(e)},
            message=f"HLE judge failed: {str(e)}",
        )

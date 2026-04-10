"""DB Diff LLM Tools Judge - evaluates database changes against criteria using tool-augmented LLM.

Instead of dumping the entire DB diff into a single prompt (which fails on large diffs),
this verifier gives the LLM tools to lazily explore the diff data:
- inspect_table: get column names and row counts for a table
- get_rows: paginated access to specific changed rows
- run_python: execute arbitrary Python against the diff data
- submit_verdict: terminate with pass/fail

The LLM starts with a compact summary and drills into relevant tables on demand.
"""

import copy
import json
from typing import Any

from litellm import Choices
from loguru import logger
from pydantic import BaseModel, ValidationError

from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.models import VerifierResult
from runner.utils.llm import build_messages, call_llm

from .tools import TOOL_DEFINITIONS, build_summary, execute_tool

# Default timeout for LLM calls (1 hour)
LLM_JUDGE_TIMEOUT = 3600

# Max tool-use iterations before we force a verdict
MAX_ITERATIONS = 20

LOG_PREFIX = "DB_DIFF_LLM_TOOLS"

SYSTEM_PROMPT = """You are evaluating database changes against specific criteria.

You have tools to explore the database diff. The diff may be very large,
so do NOT try to load all data at once. Instead:

1. Review the summary of changes provided (table names and row counts)
2. Use inspect_table to check column names for relevant tables
3. Use get_rows to examine specific changes that matter for the criteria
4. Use run_python if you need to filter, aggregate, or search across the data
5. Call submit_verdict once you have enough evidence

Be efficient — only inspect tables and rows relevant to the evaluation criteria.
Focus on what the criteria asks for and gather just enough evidence to decide."""


class VerdictResponse(BaseModel):
    """Parsed verdict from submit_verdict tool call."""

    result: int
    reason: str


async def db_diff_llm_tools_eval(input: EvalImplInput) -> VerifierResult:
    """
    DB Diff LLM Tools Judge - Evaluate database changes against criteria using tool-augmented LLM.

    This verifier:
    1. Receives DB diff results from the DB_DIFF helper
    2. Gives the LLM a compact summary + tools to explore the diff
    3. The LLM iteratively inspects relevant tables/rows
    4. The LLM calls submit_verdict when it has enough evidence

    Verifier config fields:
    - criteria: The criteria describing expected database changes (required)

    Returns:
    - judge_grade: "pass" or "fail"
    - grade_rationale: Explanation from the LLM
    - db_diff_summary: Compact summary of database changes
    """
    verifier_values = input.verifier.verifier_values or {}
    task_id = input.verifier.task_id or "unknown"

    # 1. Get criteria (required)
    criteria = verifier_values.get("criteria", "")
    if not criteria:
        raise ValueError("Missing required field: criteria")

    logger.info(
        f"[{LOG_PREFIX}] task={task_id} | evaluating criteria: {criteria[:100]}..."
    )

    try:
        # 2. Get DB diff from helper results
        if not input.helper_results:
            raise ValueError("Missing helper results")

        db_diff_result = input.helper_results.get(HelperIds.DB_DIFF)
        if not db_diff_result:
            logger.warning(
                f"[{LOG_PREFIX}] task={task_id} | no DB diff found, failing criterion"
            )
            return VerifierResult(
                verifier_id=input.verifier.verifier_id,
                verifier_version=input.verifier.verifier_version,
                score=0.0,
                verifier_result_values={
                    "judge_grade": "fail",
                    "grade_rationale": "No database diff was available to evaluate.",
                    "db_diff_summary": "No diff available",
                },
            )

        # 3. Deep copy diff data once per verifier to isolate from concurrent verifiers
        #    and protect against mutations from run_python
        db_diff_result = copy.deepcopy(db_diff_result)

        # 4. Build compact summary
        db_diff_summary = build_summary(db_diff_result)

        # 4. Get model settings
        model = input.grading_settings.llm_judge_model
        extra_args = dict(input.grading_settings.llm_judge_extra_args or {})

        # 5. Build initial messages
        user_prompt = (
            f"Criteria to evaluate: {criteria}\n\n"
            f"Database Changes:\n{db_diff_summary}\n\n"
            f"Use the tools to explore the diff and call submit_verdict when ready."
        )
        messages = build_messages(
            system_prompt=SYSTEM_PROMPT,
            user_prompt=user_prompt,
        )

        logger.debug(
            f"[{LOG_PREFIX}] task={task_id} | initial summary:\n{db_diff_summary}"
        )

        # 6. Tool-use loop
        tool_call_log: list[dict[str, Any]] = []

        for iteration in range(MAX_ITERATIONS):
            logger.info(
                f"[{LOG_PREFIX}] task={task_id} | iteration {iteration + 1}/{MAX_ITERATIONS}"
            )

            response = await call_llm(
                model=model,
                messages=messages,
                timeout=LLM_JUDGE_TIMEOUT,
                extra_args={**extra_args, "tools": TOOL_DEFINITIONS},
            )

            choices = response.choices
            if not choices or not isinstance(choices[0], Choices):
                logger.warning(
                    f"[{LOG_PREFIX}] task={task_id} | empty or unexpected choices: {response.choices}"
                )
                continue

            choice = choices[0]
            assistant_message = choice.message

            # Check for tool calls
            if not assistant_message.tool_calls:
                # No tool calls — LLM responded with text directly
                # Try to extract a verdict from the text content
                content = assistant_message.content or ""
                logger.info(
                    f"[{LOG_PREFIX}] task={task_id} | LLM responded without tools: {content[:200]}"
                )

                # Try to parse as JSON verdict
                verdict = _try_parse_verdict(content)
                if verdict:
                    return _build_result(input, verdict, db_diff_summary, tool_call_log)

                # Append and prompt to use submit_verdict
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {
                        "role": "user",
                        "content": "Please call submit_verdict with your result (0 or 1) and reason.",
                    }
                )
                continue

            # Append assistant message with tool calls
            messages.append(assistant_message.model_dump())

            # Process each tool call
            for tool_call in assistant_message.tool_calls:
                fn = tool_call.function
                tool_name = fn.name or ""
                try:
                    tool_args = json.loads(fn.arguments)
                except json.JSONDecodeError:
                    tool_args = {}

                logger.info(
                    f"[{LOG_PREFIX}] task={task_id} | tool={tool_name} args={json.dumps(tool_args)[:200]}"
                )

                # Handle submit_verdict specially — it terminates the loop
                if tool_name == "submit_verdict":
                    verdict = VerdictResponse(
                        result=tool_args.get("result", 0),
                        reason=tool_args.get("reason", "No reason provided"),
                    )
                    tool_call_log.append(
                        {
                            "tool": tool_name,
                            "args": tool_args,
                        }
                    )
                    return _build_result(input, verdict, db_diff_summary, tool_call_log)

                # Execute tool and append result
                tool_result = await execute_tool(tool_name, tool_args, db_diff_result)

                tool_call_log.append(
                    {
                        "tool": tool_name,
                        "args": tool_args,
                        "result_length": len(tool_result),
                    }
                )

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_result,
                    }
                )

        # Max iterations reached without verdict
        error_msg = f"Max iterations ({MAX_ITERATIONS}) reached without verdict"
        logger.error(f"[{LOG_PREFIX}] task={task_id} | {error_msg}")
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            verifier_result_values={
                "judge_grade": "fail",
                "grade_rationale": error_msg,
                "db_diff_summary": db_diff_summary,
                "tool_call_log": tool_call_log,
            },
        )

    except Exception as e:
        error_msg = f"DB diff LLM tools evaluation failed: {str(e)}"
        logger.error(f"[{LOG_PREFIX}] task={task_id} | error: {error_msg}")
        raise ValueError(error_msg) from e


def _try_parse_verdict(content: str) -> VerdictResponse | None:
    """Try to parse a text response as a JSON verdict."""
    try:
        data = json.loads(content)
        if isinstance(data, list) and len(data) == 1:
            data = data[0]
        if isinstance(data, dict) and "result" in data and "reason" in data:
            if isinstance(data["reason"], dict):
                data["reason"] = json.dumps(data["reason"])
            return VerdictResponse.model_validate(data)
    except (json.JSONDecodeError, ValidationError):
        pass
    return None


def _build_result(
    input: EvalImplInput,
    verdict: VerdictResponse,
    db_diff_summary: str,
    tool_call_log: list[dict[str, Any]],
) -> VerifierResult:
    """Build a VerifierResult from a verdict."""
    task_id = input.verifier.task_id or "unknown"
    passed = verdict.result == 1

    logger.info(
        f"[{LOG_PREFIX}] task={task_id} | "
        f"result: {'PASS' if passed else 'FAIL'} | "
        f"iterations: {len(tool_call_log)} tool calls | "
        f"reason: {verdict.reason[:100]}"
    )

    return VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=1.0 if passed else 0.0,
        verifier_result_values={
            "judge_grade": "pass" if passed else "fail",
            "grade_rationale": verdict.reason,
            "db_diff_summary": db_diff_summary,
            "tool_call_log": tool_call_log,
        },
    )

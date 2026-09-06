"""DB Diff LLM Tools Judge - evaluates database changes against criteria using tool-augmented LLM.

Instead of dumping the entire DB diff into a single prompt (which fails on large diffs),
this verifier gives the LLM tools to lazily explore the diff data:
- inspect_table: get column names and row counts for a table
- get_rows: paginated access to specific changed rows
- search_rows: substring search over the COMPLETE changed-row set of a table
  (including rows beyond the materialized row-body cap)
- run_python: execute arbitrary Python against the diff data
- submit_verdict: terminate with pass/fail

The LLM starts with a compact summary and drills into relevant tables on demand.
"""

import copy
import json

from litellm import Choices
from pydantic import BaseModel, ValidationError

from runner.evals.models import EvalImplInput
from runner.helpers.db_diff.main import scope_result_to_databases
from runner.helpers.models import HelperIds
from runner.models import VerifierResult
from runner.utils.grading_log import logger
from runner.utils.llm import build_messages, call_llm

from .tools import TOOL_DEFINITIONS, build_summary, execute_tool

# Default timeout for LLM calls (1 hour)
LLM_JUDGE_TIMEOUT = 3600

# Max tool-use iterations before we force a verdict
MAX_ITERATIONS = 250

LOG_PREFIX = "DB_DIFF_LLM_TOOLS"

SYSTEM_PROMPT = """You are evaluating database changes against specific criteria.

You have tools to explore the database diff. The diff may be very large,
so do NOT try to load all data at once. Instead:

1. Review the summary of changes provided (table names and row counts)
2. Use inspect_table to check column names for relevant tables
3. Use get_rows to examine specific changes that matter for the criteria
4. Use search_rows to check whether a value exists ANYWHERE in a table's
   changes — it searches the complete diff data, including rows beyond the
   materialized cap in truncated tables
5. Use run_python if you need to filter, aggregate, or search across the data
6. Call submit_verdict once you have enough evidence

Be efficient — only inspect tables and rows relevant to the evaluation criteria.
Focus on what the criteria asks for and gather just enough evidence to decide.

IMPORTANT: get_rows and run_python only see the materialized row bodies (up to
100 per table). For tables marked as truncated (rows_truncated from
inspect_table, or "row bodies capped" in the summary), absence from
get_rows/run_python output is NOT evidence of absence — you MUST confirm with
search_rows before concluding a value is missing from such a table. In the
rare case that search_rows itself reports it cannot search the complete data
(searched_complete_data=false), that table is genuinely inconclusive for the
value — do not retry; note the limitation in your rationale and decide from
the exact counts and the materialized rows you do have.

**Stop searching and submit fail immediately if any of the following are true:**
- A table that must contain the expected change shows 0 rows of the relevant change type (added, deleted, or modified) in the summary
- A search_rows call on the relevant table returns no matches for the key field or value
- On a non-truncated table only: you have made 3+ queries with the same approach and found nothing matching the criteria, or a run_python search returned no results for the key field or value

Do not try alternative phrasings, different offsets, or related tables after a clear
negative result. Absence of the expected data IS the evidence — submit fail immediately."""

# Reason attached when the loop exhausts MAX_ITERATIONS without a terminal
# submit_verdict. It must NOT assert anything about the diff's contents: a judge
# model that fails to tool-call reliably never examined it, and reading the
# fail-closed 0 as "the changes are missing" fabricates a grading finding.
INCONCLUSIVE_REASON_TEMPLATE = (
    "Inconclusive: the judge model did not call submit_verdict within "
    "{max_iterations} tool-use iterations, so no verdict was reached. Scored 0 "
    "by fail-closed default; this is not a finding about the database diff's "
    "contents. Criteria: {criteria}"
)


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
    verifier_id = input.verifier.verifier_id

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

        # 3b. Scope to the verifier's expected output database(s), when named.
        # ``expected_output_files`` is resolved server-side from the verifier's
        # EXPECTED_OUTPUT_FILES custom field. Only .db entries activate scoping:
        # the field also carries non-database output artifacts (e.g. .docx
        # dependencies on GDM worlds), which must not empty the diff. No .db
        # entries keeps the full diff.
        raw_expected = verifier_values.get("expected_output_files")
        expected_db_names = [
            name.strip()
            for name in (raw_expected if isinstance(raw_expected, list) else [])
            if isinstance(name, str) and name.strip().lower().endswith(".db")
        ]
        scope_note = None
        if expected_db_names:
            db_diff_result, scope_note = scope_result_to_databases(
                db_diff_result, expected_db_names
            )
            logger.info(f"[{LOG_PREFIX}] task={task_id} | {scope_note.splitlines()[0]}")

        # 4. Build compact summary
        db_diff_summary = build_summary(db_diff_result)
        if scope_note:
            db_diff_summary = f"{scope_note}\n\n{db_diff_summary}"

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
                    return _build_result(input, verdict, db_diff_summary)

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

                # Handle submit_verdict specially — it terminates the loop
                if tool_name == "submit_verdict":
                    verdict = VerdictResponse(
                        result=tool_args.get("result", 0),
                        reason=tool_args.get("reason", "No reason provided"),
                    )
                    logger.bind(
                        message_type="tool_call",
                        verifier_id=verifier_id,
                        payload={"tool": tool_name, "args": tool_args},
                    ).info(
                        f"submit_verdict: {'pass' if verdict.result == 1 else 'fail'}"
                    )
                    return _build_result(input, verdict, db_diff_summary)

                # Execute tool and append result
                tool_result = await execute_tool(tool_name, tool_args, db_diff_result)

                logger.bind(
                    message_type="tool_call",
                    verifier_id=verifier_id,
                    payload={
                        "tool": tool_name,
                        "args": tool_args,
                        "result_length": len(tool_result),
                    },
                ).info(f"{tool_name}: {tool_args.get('table_name', '')}".strip(": "))

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": tool_result,
                    }
                )

        rationale = INCONCLUSIVE_REASON_TEMPLATE.format(
            max_iterations=MAX_ITERATIONS, criteria=criteria
        )
        logger.warning(
            f"[{LOG_PREFIX}] task={task_id} | max iterations reached without a "
            f"terminal submit_verdict, scoring 0 (fail-closed): {rationale[:200]}"
        )
        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=0.0,
            verifier_result_values={
                "judge_grade": "fail",
                "grade_rationale": rationale,
                "db_diff_summary": db_diff_summary,
                "inconclusive": True,
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
) -> VerifierResult:
    """Build a VerifierResult from a verdict."""
    task_id = input.verifier.task_id or "unknown"
    passed = verdict.result == 1

    logger.info(
        f"[{LOG_PREFIX}] task={task_id} | "
        f"result: {'PASS' if passed else 'FAIL'} | "
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
        },
    )

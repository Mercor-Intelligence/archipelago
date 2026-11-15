"""DB State LLM Tools Judge - evaluates the FINAL database state against criteria.

The drop-in twin of ``db_diff_llm_tools``: same criteria text, same output
fields, same tool-augmented loop — but the evidence is the final database state
rather than a baseline-vs-final diff, so no baseline is needed and the
``DB_DIFF`` helper never runs.

Why final state is enough: every DB criterion we author names an artifact the
agent produced ("the email sent to Aileen…", "the post to the Ops-BD channel…").
The diff was a retrieval mechanism for those, not a semantic requirement. The one
thing it bought that state does not is causality — a post-state assertion can be
satisfied by a row that was already in the seed — which is handled at authoring
time (the criterion must be false on the pristine seed) rather than by shipping a
gigabyte-scale baseline into every grading container.

The LLM starts with a compact summary of the databases and their tables, then
drills in on demand:
- list_tables / inspect_table: what exists, and what its columns are
- get_rows: paginated reads
- search_rows: literal substring search across a table's COMPLETE contents
- run_sql: one read-only statement for joins/filters/aggregates
- submit_verdict: terminate with pass/fail
"""

import asyncio
import json
import sqlite3
from collections.abc import Iterable
from typing import Any

from litellm import Choices
from pydantic import BaseModel, ValidationError

from runner.evals.models import EvalImplInput
from runner.evals.utils import agent_actions
from runner.helpers.models import HelperIds
from runner.models import VerifierResult
from runner.utils.grading_log import logger
from runner.utils.llm import build_messages, call_llm

from .tools import (
    TOOL_DEFINITIONS,
    build_summary,
    execute_tool,
    read_table_index,
    truncate_tool_response,
)

# Default timeout for LLM calls (1 hour)
LLM_JUDGE_TIMEOUT = 3600

# Max tool-use iterations before we force a verdict
MAX_ITERATIONS = 250

LOG_PREFIX = "DB_STATE_LLM_TOOLS"

SYSTEM_PROMPT = """You are evaluating the final state of one or more databases against specific criteria.

You have tools to explore the databases. They may be very large, so do NOT try
to read whole tables. Instead:

1. Review the summary provided (databases, tables and row counts)
2. Use inspect_table to check column names for relevant tables
3. Use search_rows to locate the record the criteria is about — it searches the
   COMPLETE contents of a table, every column, case-insensitively
4. Use get_rows to page through a table when you already know it is small
5. Use run_sql for joins, filters and aggregates the other tools cannot express
   (one read-only statement per call)
6. Call submit_verdict once you have enough evidence

You are looking at the state AFTER the agent finished, not a list of changes.
Judge whether the state satisfies the criteria — find the record the criteria
names and evaluate its content. Do not speculate about what the agent changed;
you cannot see that, and the criteria do not ask.

Be efficient — only inspect tables and rows relevant to the evaluation criteria.
Focus on what the criteria asks for and gather just enough evidence to decide.

A row_count of "unknown" means the count could not be computed (a scan that
outran its deadline, say) — it does NOT mean the table is empty. Query the table
before concluding anything about it.

**Stop searching and submit fail immediately if any of the following are true:**
- The table that must contain the expected record has 0 rows
- A search_rows call on the relevant table returns no matches for the key field or value
- You have made 3+ queries with the same approach and found nothing matching the criteria

Do not try alternative phrasings, different offsets, or related tables after a clear
negative result. Absence of the expected data IS the evidence — submit fail immediately."""

# Reason attached when the loop exhausts MAX_ITERATIONS without a terminal
# submit_verdict. It must NOT assert anything about the databases' contents: a
# judge model that fails to tool-call reliably never examined them, and reading
# the fail-closed 0 as "the record is missing" fabricates a grading finding.
INCONCLUSIVE_REASON_TEMPLATE = (
    "Inconclusive: the judge model did not call submit_verdict within "
    "{max_iterations} tool-use iterations, so no verdict was reached. Scored 0 "
    "by fail-closed default; this is not a finding about the database state's "
    "contents. Criteria: {criteria}"
)


# Reason attached when the judge declines to grade because the action log
# records the operation but the scoped databases do not hold the resulting
# record. Like the iteration-exhaustion case above this scores 0 fail-closed,
# and for the same reason must not be read as a finding about the data: the
# judge is reporting that it could not corroborate, not that the criteria fail.
NON_CORROBORATED_REASON_TEMPLATE = (
    "Inconclusive: the agent's action log records the operation the criteria "
    "concern, but the databases under evaluation do not contain the resulting "
    "record, so the criteria could be neither confirmed nor refuted from the "
    "data. Scored 0 by fail-closed default; this is not a finding about the "
    "database state's contents. Judge's account: {reason} Criteria: {criteria}"
)

# The shipped prompt tells the judge it cannot see what the agent changed. With
# the action log attached that is no longer true, so the sentence is swapped
# rather than left to contradict the tools. Held as a constant so the swap
# fails loudly in tests if the prompt is reworded.
_NO_PROVENANCE_PARAGRAPH = (
    "You are looking at the state AFTER the agent finished, not a list of changes.\n"
    "Judge whether the state satisfies the criteria — find the record the criteria\n"
    "names and evaluate its content. Do not speculate about what the agent changed;\n"
    "you cannot see that, and the criteria do not ask."
)

_WITH_PROVENANCE_PARAGRAPH = (
    "You are looking at the state AFTER the agent finished. Judge whether the\n"
    "state satisfies the criteria — find the record the criteria names and\n"
    "evaluate its content."
)


def build_system_prompt(has_actions: bool) -> str:
    """The prompt as shipped, unless an action log is attached."""
    if not has_actions:
        return SYSTEM_PROMPT
    return (
        SYSTEM_PROMPT.replace(_NO_PROVENANCE_PARAGRAPH, _WITH_PROVENANCE_PARAGRAPH)
        + agent_actions.PROMPT_SECTION
    )


class VerdictResponse(BaseModel):
    """Parsed verdict from submit_verdict tool call."""

    result: int
    reason: str


def _db_path_matches(db_path: str, name: str) -> bool:
    """Scoping match, identical to the diff judge's (``helpers/db_diff/main.py``).

    A name may be a bare filename ("mail.db"), a connector-qualified suffix
    ("foundry_mail/mail.db"), or the full snapshot path. Case-insensitive, to
    stay consistent with the ``.db``-suffix detection that activates scoping.
    """
    db_path, name = db_path.lower(), name.lower()
    return db_path == name or db_path.endswith("/" + name)


def _copy_dump_backed(selected: dict[str, Any]) -> dict[str, sqlite3.Connection]:
    """Private read-only copies of the dump-backed databases in scope.

    A ``.sql`` dump has no file behind it — the SNAPSHOT_DBS helper only ever
    materializes it in memory — so the only way to a private handle is
    ``backup()`` from the helper's connection. That connection is opened inside
    the helper coroutine with SQLite's default same-thread check, so this MUST
    run on the event loop's thread; calling it from the executor raises
    ``ProgrammingError`` and aborts the grade. The copy is cheap relative to the
    parse-and-load the helper already did inline on that same thread.

    The copy is pinned ``query_only`` because this judge executes model-authored
    SQL and must never reach the helper's shared writable handle.
    """
    copies: dict[str, sqlite3.Connection] = {}
    try:
        for path, entry in selected.items():
            if entry.get("temp_path"):
                continue
            private = sqlite3.connect(":memory:", check_same_thread=False)
            copies[path] = private
            entry["connection"].backup(private)
            private.execute("PRAGMA query_only = ON")
    except BaseException:
        _close_all(copies.values())
        raise
    return copies


def _open_read_only(
    entry: dict[str, Any], dump_copy: sqlite3.Connection | None
) -> sqlite3.Connection:
    """A private, read-only connection to one final-state database.

    The SNAPSHOT_DBS helper hands out writable connections shared by every
    verifier in the run, and this judge executes model-authored SQL. So each
    verifier gets its own handle: the ``.db`` case (the whole point of the
    "Database Files (.db)" target) reopens the helper's temp copy through a
    read-only ``file:`` URI, and the ``.sql``-dump case takes the private copy
    ``_copy_dump_backed`` already made on the owning thread.

    ``check_same_thread=False`` because the tool handlers run in the event
    loop's executor (a SQLite query must not stall the loop shared with every
    other verifier), so the querying thread is not the opening one. That is safe
    here and only here: the handle is private to this verifier, and the judge
    loop awaits each tool call in turn, so no two threads ever use it at once.
    """
    temp_path = entry.get("temp_path")
    if temp_path:
        return sqlite3.connect(
            f"file:{temp_path}?mode=ro", uri=True, check_same_thread=False
        )
    if dump_copy is None:
        raise ValueError(f"dump-backed database has no private copy: {entry}")
    return dump_copy


def _select_databases(
    snapshot_dbs: dict[str, Any], expected_db_names: list[str]
) -> tuple[dict[str, Any], str | None]:
    """Choose the databases to judge, plus a human-readable scope note.

    The note is None when the verifier named no database and every database is
    in scope. Names that match nothing are reported in the note rather than
    silently dropped, mirroring ``scope_result_to_databases``: an empty scope
    with an explanatory note grades deterministically, while falling back to
    every database would silently reintroduce connectors the criterion never
    named. Cheap and side-effect free — no connection is opened here.
    """
    by_path = {
        str(entry.get("path") or alias): entry for alias, entry in snapshot_dbs.items()
    }

    note: str | None = None
    if expected_db_names:
        names = list(dict.fromkeys(n.strip() for n in expected_db_names if n.strip()))
        selected = {
            path: entry
            for path, entry in by_path.items()
            if any(_db_path_matches(path, name) for name in names)
        }
        unmatched = [
            name
            for name in names
            if not any(_db_path_matches(path, name) for path in by_path)
        ]
        parts = [f"Database scope: {names} -> {sorted(selected) or 'no match'}."]
        if unmatched:
            parts.append(
                f"No database matched {unmatched} (present: {sorted(by_path)})."
            )
        ambiguous = {
            name: sorted(p for p in by_path if _db_path_matches(p, name))
            for name in names
            if len([p for p in by_path if _db_path_matches(p, name)]) > 1
        }
        if ambiguous:
            parts.append(
                f"Ambiguous name(s) matched several databases: {ambiguous}; "
                "qualify by connector to narrow."
            )
        note = " ".join(parts)
    else:
        selected = by_path

    return selected, note


def _open_and_index(
    selected: dict[str, Any], dump_copies: dict[str, sqlite3.Connection]
) -> dict[str, Any]:
    """Open the scoped databases read-only and read their table index.

    BLOCKING: the index costs one ``COUNT(*)`` per table — a full scan each, on
    databases that reach into the gigabytes. Callers on the event loop must go
    through ``build_state_async``.
    """
    databases: dict[str, Any] = {}
    try:
        for path, entry in selected.items():
            databases[path] = {
                "connection": _open_read_only(entry, dump_copies.get(path))
            }
        state: dict[str, Any] = {"databases": databases}
        state["tables"] = read_table_index(state)
    except BaseException:
        _close_all(
            [db["connection"] for db in databases.values()], dump_copies.values()
        )
        raise
    return state


def _build_state(
    snapshot_dbs: dict[str, Any], expected_db_names: list[str]
) -> tuple[dict[str, Any], str | None]:
    """Select the databases to judge and open read-only connections to them.

    BLOCKING, and it copies dump-backed databases on the calling thread. Callers
    on the event loop must go through ``build_state_async``.
    """
    selected, note = _select_databases(snapshot_dbs, expected_db_names)
    return _open_and_index(selected, _copy_dump_backed(selected)), note


async def build_state_async(
    snapshot_dbs: dict[str, Any], expected_db_names: list[str]
) -> tuple[dict[str, Any], str | None]:
    """``_build_state``, with the expensive half off the event loop.

    Opening the ``.db`` handles and reading the table index are the costly part
    and go to the executor: the tool handlers already run there so a query
    cannot stall the loop shared with every concurrent verifier, and doing that
    for the queries but not for the setup that issues the same kind of work just
    moves the stall to the start of the grade, where parallel grades all pay it
    at once.

    Copying the dump-backed databases stays inline — see ``_copy_dump_backed``:
    the helper's dump connection is bound to this thread, so ``backup()`` off it
    raises. Scoping runs first so only the databases actually in scope are
    copied.
    """
    selected, note = _select_databases(snapshot_dbs, expected_db_names)
    dump_copies = _copy_dump_backed(selected)
    loop = asyncio.get_running_loop()
    try:
        state = await loop.run_in_executor(None, _open_and_index, selected, dump_copies)
    except BaseException:
        _close_all(dump_copies.values())
        raise
    return state, note


def _close_all(*groups: Iterable[sqlite3.Connection]) -> None:
    for group in groups:
        for connection in group:
            try:
                connection.close()
            except sqlite3.Error:  # pragma: no cover - best-effort cleanup
                pass


def _close(state: dict[str, Any]) -> None:
    _close_all(db["connection"] for db in state.get("databases", {}).values())


async def db_state_llm_tools_eval(input: EvalImplInput) -> VerifierResult:
    """
    DB State LLM Tools Judge - evaluate the final database state against criteria
    using a tool-augmented LLM.

    This verifier:
    1. Receives the final-snapshot databases from the SNAPSHOT_DBS helper
    2. Gives the LLM a compact summary + tools to explore them
    3. The LLM iteratively inspects relevant tables/rows
    4. The LLM calls submit_verdict when it has enough evidence

    Verifier config fields:
    - criteria: The criteria describing the expected database state (required)

    Returns:
    - judge_grade: "pass" or "fail"
    - grade_rationale: Explanation from the LLM
    - db_diff_summary: Compact summary of the database state evaluated (field id
      kept from the diff judge — it is the contract for trajectory views,
      exports and scoring configs)
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

    state: dict[str, Any] = {}
    try:
        # 2. Get the final-state databases from helper results
        if not input.helper_results:
            raise ValueError("Missing helper results")

        snapshot_dbs = input.helper_results.get(HelperIds.SNAPSHOT_DBS)
        if not snapshot_dbs:
            logger.warning(
                f"[{LOG_PREFIX}] task={task_id} | no databases in the final "
                "snapshot, failing criterion"
            )
            return VerifierResult(
                verifier_id=input.verifier.verifier_id,
                verifier_version=input.verifier.verifier_version,
                score=0.0,
                verifier_result_values={
                    "judge_grade": "fail",
                    "grade_rationale": "No database state was available to evaluate.",
                    "db_diff_summary": "No databases available",
                },
            )

        # 3. Scope to the verifier's expected output database(s), when named.
        # ``expected_output_files`` is resolved server-side from the verifier's
        # EXPECTED_OUTPUT_FILES custom field. Only .db entries activate scoping:
        # the field also carries non-database output artifacts (e.g. .docx
        # dependencies on GDM worlds), which must not empty the scope. No .db
        # entries keeps every database.
        raw_expected = verifier_values.get("expected_output_files")
        expected_db_names = [
            name.strip()
            for name in (raw_expected if isinstance(raw_expected, list) else [])
            if isinstance(name, str) and name.strip().lower().endswith(".db")
        ]

        state, scope_note = await build_state_async(snapshot_dbs, expected_db_names)
        if scope_note:
            logger.info(f"[{LOG_PREFIX}] task={task_id} | {scope_note}")

        # 4. Build compact summary
        db_state_summary = build_summary(state)
        if scope_note:
            db_state_summary = f"{scope_note}\n\n{db_state_summary}"

        # 5. Get model settings
        model = input.grading_settings.llm_judge_model
        extra_args = dict(input.grading_settings.llm_judge_extra_args or {})

        # 6. Build initial messages. The action log is attached only when the
        # trajectory actually recorded tool calls — with nothing to show, the
        # judge runs exactly as it did before this was added.
        actions = agent_actions.build_action_index(input.trajectory.messages)
        if actions:
            logger.info(
                f"[{LOG_PREFIX}] task={task_id} | attaching agent action log "
                f"({len(actions)} actions)"
            )
        tool_definitions = TOOL_DEFINITIONS + (
            agent_actions.TOOL_DEFINITIONS if actions else []
        )

        user_prompt = (
            f"Criteria to evaluate: {criteria}\n\n"
            f"Final Database State:\n{db_state_summary}\n\n"
            f"Use the tools to explore the databases and call submit_verdict when ready."
        )
        messages = build_messages(
            system_prompt=build_system_prompt(bool(actions)),
            user_prompt=user_prompt,
        )

        logger.debug(
            f"[{LOG_PREFIX}] task={task_id} | initial summary:\n{db_state_summary}"
        )

        # 7. Tool-use loop
        for iteration in range(MAX_ITERATIONS):
            logger.info(
                f"[{LOG_PREFIX}] task={task_id} | iteration {iteration + 1}/{MAX_ITERATIONS}"
            )

            response = await call_llm(
                model=model,
                messages=messages,
                timeout=LLM_JUDGE_TIMEOUT,
                extra_args={**extra_args, "tools": tool_definitions},
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
                # No tool calls — LLM responded with text directly.
                # Try to extract a verdict from the text content
                content = assistant_message.content or ""
                logger.info(
                    f"[{LOG_PREFIX}] task={task_id} | LLM responded without tools: {content[:200]}"
                )

                verdict = _try_parse_verdict(content)
                if verdict:
                    return _build_result(input, verdict, db_state_summary)

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
                    return _build_result(input, verdict, db_state_summary)

                # The judge declining to grade because the action log and the
                # databases disagree also terminates the loop. Fail-closed, and
                # flagged so it is not mistaken for a finding about the data.
                if tool_name == agent_actions.INCONCLUSIVE_TOOL_NAME:
                    rationale = NON_CORROBORATED_REASON_TEMPLATE.format(
                        reason=str(tool_args.get("reason", "")).strip(),
                        criteria=criteria,
                    )
                    logger.warning(
                        f"[{LOG_PREFIX}] task={task_id} | non-corroborated action "
                        f"log, scoring 0 (fail-closed): {rationale[:200]}"
                    )
                    return _build_result(
                        input,
                        VerdictResponse(result=0, reason=rationale),
                        db_state_summary,
                        inconclusive=True,
                    )

                # Action-log tools are in-memory; the database tools are not, so
                # only the latter go through the executor in execute_tool. Both
                # paths are capped alike — an untruncated action-log reply
                # would blow the same context budget execute_tool enforces on
                # every other tool.
                tool_result = agent_actions.execute(tool_name, tool_args, actions)
                if tool_result is not None:
                    tool_result = truncate_tool_response(tool_result)
                else:
                    tool_result = await execute_tool(tool_name, tool_args, state)

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
                "db_diff_summary": db_state_summary,
                "inconclusive": True,
            },
        )

    except Exception as e:
        error_msg = f"DB state LLM tools evaluation failed: {str(e)}"
        logger.error(f"[{LOG_PREFIX}] task={task_id} | error: {error_msg}")
        raise ValueError(error_msg) from e
    finally:
        # Close only the private handles opened above — the helper's own shared
        # connections stay open for the other verifiers in the run.
        _close(state)


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
    db_state_summary: str,
    inconclusive: bool = False,
) -> VerifierResult:
    """Build a VerifierResult from a verdict."""
    task_id = input.verifier.task_id or "unknown"
    passed = verdict.result == 1

    logger.info(
        f"[{LOG_PREFIX}] task={task_id} | "
        f"result: {'PASS' if passed else 'FAIL'} | "
        f"reason: {verdict.reason[:100]}"
    )

    values: dict[str, Any] = {
        "judge_grade": "pass" if passed else "fail",
        "grade_rationale": verdict.reason,
        "db_diff_summary": db_state_summary,
    }
    if inconclusive:
        values["inconclusive"] = True

    return VerifierResult(
        verifier_id=input.verifier.verifier_id,
        verifier_version=input.verifier.verifier_version,
        score=1.0 if passed else 0.0,
        verifier_result_values=values,
    )

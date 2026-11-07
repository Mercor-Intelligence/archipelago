"""Sources of judge input — one extractor per named source.

Trajectory-side extractors (CodeDiff, TrajectoryMessages, FinalAnswer) read
from the agent's run. File extractors (PlanningStatementFile, ProblemStatementFile,
etc.) read named files from the task's initial snapshot zip.

Use `compose_artifacts(input, [id, id, ...])` to fetch multiple sources and
concatenate them under their `label` headers.
"""

from __future__ import annotations

import zipfile
from typing import IO, Any

from runner.evals.models import EvalImplInput

from .models import BaseArtifactExtractor

_MISSING = "[no artifact found in trajectory]"


def _content_to_str(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # litellm message content can be a list of {"type", "text"} parts
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if text:
                    parts.append(str(text))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    return str(content)


def _read_file_from_snapshot(
    snapshot_bytes: IO[bytes] | None,
    *candidate_paths: str,
) -> str | None:
    """Try each path in the zip; return contents of the first match, else None.

    Tries exact match first, then any entry whose path ends with /{candidate}
    (handles zips that wrap files inside a top-level directory like
    tasks/{task_id}/...). Decodes as UTF-8 with replacement on invalid bytes.

    Never raises. On any failure (no snapshot, corrupt zip, missing file,
    decode error) returns None so callers can substitute _MISSING.
    """
    if snapshot_bytes is None:
        return None
    try:
        snapshot_bytes.seek(0)
        with zipfile.ZipFile(snapshot_bytes, "r") as zf:
            names = zf.namelist()
            name_set = set(names)
            for path in candidate_paths:
                if path in name_set:
                    return zf.read(path).decode("utf-8", errors="replace")
                for n in names:
                    if n == path or n.endswith("/" + path):
                        return zf.read(n).decode("utf-8", errors="replace")
        return None
    except (zipfile.BadZipFile, KeyError, OSError, UnicodeDecodeError):
        return None
    finally:
        try:
            if snapshot_bytes is not None:
                snapshot_bytes.seek(0)
        except Exception:
            pass


class CodeDiffExtractor(BaseArtifactExtractor):
    """Reads the git diff (the agent's code change) captured during the run.

    Stored at trajectory.output.solution by the lighthouse parser. Populated
    by the harbor adapter's VERIFICATION_START hook from the sandbox's
    `git diff --cached HEAD`.
    """

    id = "code_diff"
    label = "Code diff"

    async def fetch(self, input: EvalImplInput) -> str:
        output = input.trajectory.output or {}
        solution = output.get("solution")
        return _content_to_str(solution) or _MISSING


class TrajectoryMessagesExtractor(BaseArtifactExtractor):
    """Formats the full multi-turn trajectory as readable text.

    Includes every role — system, user, assistant, tool — plus tool calls and
    their arguments. Used for trajectory-behavior criteria where the judge
    needs to see what the agent did, in what order, and with what tools.
    """

    id = "trajectory_messages"
    label = "Trajectory"

    async def fetch(self, input: EvalImplInput) -> str:
        messages = input.trajectory.messages or []
        if not messages:
            return _MISSING

        lines: list[str] = []
        for i, msg in enumerate(messages):
            role = msg.get("role", "unknown")
            content = _content_to_str(msg.get("content"))
            tool_calls = msg.get("tool_calls") or []

            header = f"--- Message {i + 1} ({role}) ---"
            lines.append(header)
            if content:
                lines.append(content.strip())
            for tc in tool_calls:
                fn = tc.get("function") if isinstance(tc, dict) else None
                name = (
                    fn.get("name", "?")
                    if isinstance(fn, dict)
                    else tc.get("name", "?")
                    if isinstance(tc, dict)
                    else "?"
                )
                args = fn.get("arguments", "") if isinstance(fn, dict) else ""
                lines.append(f"[Tool call: {name}({args})]")
            lines.append("")
        return "\n".join(lines).strip()


class FinalAnswerExtractor(BaseArtifactExtractor):
    """The agent's final response text — for code-QA and plan-style rubrics.

    Returns the last assistant message's content. If no assistant message
    exists, returns the missing-artifact sentinel.
    """

    id = "final_answer"
    label = "Final answer"

    async def fetch(self, input: EvalImplInput) -> str:
        messages = input.trajectory.messages or []
        if not messages:
            return _MISSING

        for msg in reversed(messages):
            if msg.get("role") == "assistant":
                content = _content_to_str(msg.get("content"))
                if content:
                    return content

        return _MISSING


class PlanningStatementFileExtractor(BaseArtifactExtractor):
    """Reads `planning_statement.md` from the task's initial snapshot.

    The rubric-aligned meta-prompt for planning rubrics. Used by
    planning_communication tasks.
    """

    id = "planning_statement_file"
    label = "Planning Statement"

    async def fetch(self, input: EvalImplInput) -> str:
        content = _read_file_from_snapshot(
            input.initial_snapshot_bytes, "planning_statement.md"
        )
        return content or _MISSING


class ProblemStatementFileExtractor(BaseArtifactExtractor):
    """Reads `problem_statement.md` from the task's initial snapshot.

    The SWE problem description shown to the judge for execution rubrics.
    """

    id = "problem_statement_file"
    label = "Problem Statement"

    async def fetch(self, input: EvalImplInput) -> str:
        content = _read_file_from_snapshot(
            input.initial_snapshot_bytes, "problem_statement.md"
        )
        return content or _MISSING


class PromptStatementFileExtractor(BaseArtifactExtractor):
    """Reads `prompt_statement.md` — the code-QA question text.

    Used by swe_bench_ext / codeqa-ext.
    """

    id = "prompt_statement_file"
    label = "Question"

    async def fetch(self, input: EvalImplInput) -> str:
        content = _read_file_from_snapshot(
            input.initial_snapshot_bytes, "prompt_statement.md"
        )
        return content or _MISSING


class InstructionFileExtractor(BaseArtifactExtractor):
    """Reads `instruction.md` — the terminal-bench task instruction."""

    id = "instruction_file"
    label = "Task Instruction"

    async def fetch(self, input: EvalImplInput) -> str:
        content = _read_file_from_snapshot(
            input.initial_snapshot_bytes, "instruction.md"
        )
        return content or _MISSING


class VerifierFieldSnapshotExtractor(BaseArtifactExtractor):
    """Reads a writer-authored field snapshotted into `verifier_values` at
    rubric→verifier sync time. Resolved from `artifact_id` of the form
    `custom_field:<field_name>`.
    """

    id = "custom_field"  # base id; the per-instance id encodes the field name
    label = "Task field"

    def __init__(self, field_name: str):
        self._field_name = field_name
        self.label = f"Task field: {field_name}"

    async def fetch(self, input: EvalImplInput) -> str:
        snapshot = (input.verifier.verifier_values or {}).get(
            "task_field_snapshot"
        ) or {}
        if not isinstance(snapshot, dict):
            return _MISSING
        value = snapshot.get(self._field_name)
        return _content_to_str(value) or _MISSING


# Split into two tuples so the FE dropdowns offer the right options per slot
# (agent_artifact vs task_context). The single ARTIFACT_REGISTRY below merges
# them for fetch-time lookup — admins can still cross over via per-criterion
# override if they want.
_AGENT_ARTIFACT_EXTRACTORS: tuple[BaseArtifactExtractor, ...] = (
    CodeDiffExtractor(),
    TrajectoryMessagesExtractor(),
    FinalAnswerExtractor(),
)
_TASK_CONTEXT_EXTRACTORS: tuple[BaseArtifactExtractor, ...] = (
    PlanningStatementFileExtractor(),
    ProblemStatementFileExtractor(),
    PromptStatementFileExtractor(),
    InstructionFileExtractor(),
)

ARTIFACT_REGISTRY: dict[str, BaseArtifactExtractor] = {
    e.id: e for e in (*_AGENT_ARTIFACT_EXTRACTORS, *_TASK_CONTEXT_EXTRACTORS)
}

AGENT_ARTIFACT_OPTIONS: list[str] = [e.id for e in _AGENT_ARTIFACT_EXTRACTORS]
TASK_CONTEXT_OPTIONS: list[str] = [e.id for e in _TASK_CONTEXT_EXTRACTORS]

MISSING_ARTIFACT: str = _MISSING

_CUSTOM_FIELD_PREFIX = "custom_field:"


def _resolve_extractor(aid: str) -> BaseArtifactExtractor | None:
    """Look up a registered extractor, or build a parameterised one for the
    `custom_field:<name>` form. Returns None if the id is unknown.
    """
    if aid.startswith(_CUSTOM_FIELD_PREFIX):
        field_name = aid[len(_CUSTOM_FIELD_PREFIX) :]
        if not field_name:
            return None
        return VerifierFieldSnapshotExtractor(field_name)
    return ARTIFACT_REGISTRY.get(aid)


async def compose_artifacts(
    input: EvalImplInput,
    artifact_ids: list[str],
) -> tuple[str, list[str], list[str]]:
    """Fetch each named extractor and concatenate under labeled headers.

    Returns (composed_text, present_ids, missing_ids).
      - composed_text: each present extractor's output preceded by `## {label}`,
        sections separated by blank lines. _MISSING if every extractor missing.
      - present_ids: extractor ids whose fetch returned real content.
      - missing_ids: ids whose fetch returned _MISSING or that weren't in the registry.

    A single present extractor: rendered without a header (clean prompt when
    admin selected one source). Multiple: each gets its labeled section.
    """
    present_ids: list[str] = []
    missing_ids: list[str] = []
    sections: list[tuple[str, str]] = []  # (label, content)

    for aid in artifact_ids:
        ext = _resolve_extractor(aid)
        if ext is None:
            missing_ids.append(aid)
            continue
        content = await ext.fetch(input)
        if content == _MISSING:
            missing_ids.append(aid)
            continue
        sections.append((ext.label or aid, content))
        present_ids.append(aid)

    if not sections:
        return _MISSING, present_ids, missing_ids

    if len(sections) == 1:
        # Single source — no outer header so the prompt template can add its
        # own framing if it wants.
        return sections[0][1], present_ids, missing_ids

    return (
        "\n\n".join(f"## {label}\n{content}" for label, content in sections),
        present_ids,
        missing_ids,
    )

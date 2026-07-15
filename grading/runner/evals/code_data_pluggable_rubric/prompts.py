"""Judge prompt templates.

One class per prompt id. Each owns its system prompt and renders the user
prompt by combining the criterion (with inlined rationale), task_context,
and agent_artifact into the final body. All templates use the same
CriterionVerdict response schema so parsing is uniform.

Adding a new prompt = one new BaseJudgePrompt subclass; the id is derived
from the class name (see models._derive_id_from_class_name).
"""

from __future__ import annotations

from .models import BaseJudgePrompt

_OUTPUT_INSTRUCTION = """
Respond with a single JSON object and nothing else:
{
  "passed": <true | false>,
  "reason": "<one to three sentences citing specific evidence>"
}
""".strip()


def _format_criterion(criterion: str, rationale: str | None) -> str:
    """Render the criterion line. Inlines rationale as bracketed suffix.

    Matches the lighthouse benchmark grader convention (see
    swe_bench_ext.rubric_utils.convert_codeqa_rubric_to_framework) so prompts
    are byte-comparable to benchmark-internal grading.
    """
    line = criterion.strip()
    if rationale and rationale.strip():
        line = f"{line} [Rationale: {rationale.strip()}]"
    return line


def _format_task_context(task_context: str) -> str:
    """Render the composed task_context block (or empty)."""
    tc = (task_context or "").strip()
    if not tc:
        return ""
    return f"{tc}\n\n"


class CodeDiffPromptV1(BaseJudgePrompt):
    system_prompt = (
        "You are an expert code reviewer evaluating a code change against a "
        "single criterion. Grade strictly on the evidence in the change. Do not "
        "give credit for hedging, partial implementations, or behavior that "
        "is described but not implemented."
    )

    def build_judge_prompt(
        self,
        *,
        criterion: str,
        rationale: str | None,
        category: list[str],
        agent_artifact: str,
        task_context: str = "",
        weight_label: str | None = None,
    ) -> str:
        del weight_label  # not surfaced in this prompt
        return (
            f"{_format_task_context(task_context)}"
            f"## Criterion\n{_format_criterion(criterion, rationale)}\n\n"
            "## Agent's output\n"
            f"{agent_artifact}\n\n"
            "## Your task\nEvaluate whether the agent's output satisfies the criterion.\n\n"
            f"{_OUTPUT_INSTRUCTION}"
        )


class PlanPromptV1(BaseJudgePrompt):
    """Plan grading. Pair agent_artifact with `final_answer` (or `final_answer`
    + `code_diff` if the agent also produced code) and set task_context to
    `planning_statement_file`.
    """

    system_prompt = (
        "You are an expert code reviewer evaluating a software engineering plan. "
        "For each criterion, determine whether the plan adequately addresses it.\n\n"
        "A criterion is MET if the plan:\n"
        "  - Explicitly describes implementing/handling the requirement, OR\n"
        "  - Clearly implies the requirement will be addressed through the described approach, OR\n"
        "  - Implicitly covers the requirement via the overall approach.\n\n"
        "A criterion is NOT MET if:\n"
        "  - The plan does not mention or address the requirement, OR\n"
        "  - The plan explicitly excludes or deprioritizes the requirement, OR\n"
        "  - The described approach would likely not satisfy the requirement.\n\n"
        "Be strict but fair. Do not give credit for execution outcomes — only plan content."
    )

    def build_judge_prompt(
        self,
        *,
        criterion: str,
        rationale: str | None,
        category: list[str],
        agent_artifact: str,
        task_context: str = "",
        weight_label: str | None = None,
    ) -> str:
        del weight_label  # not surfaced in this prompt
        return (
            f"{_format_task_context(task_context)}"
            f"## Criterion\n{_format_criterion(criterion, rationale)}\n\n"
            "## Plan\n"
            f"{agent_artifact}\n\n"
            "## Your task\n"
            "Evaluate whether the plan satisfies the criterion using the rules in the "
            "system prompt.\n\n"
            f"{_OUTPUT_INSTRUCTION}"
        )


class TrajectoryPromptV1(BaseJudgePrompt):
    system_prompt = (
        "You are evaluating an AI agent's trajectory — the sequence of "
        "actions, tool calls, and reasoning the agent took. Grade based on "
        "what the agent did, not what it produced. Cite specific tool calls "
        "or messages as evidence."
    )

    def build_judge_prompt(
        self,
        *,
        criterion: str,
        rationale: str | None,
        category: list[str],
        agent_artifact: str,
        task_context: str = "",
        weight_label: str | None = None,
    ) -> str:
        del weight_label  # not surfaced in this prompt
        return (
            f"{_format_task_context(task_context)}"
            f"## Criterion\n{_format_criterion(criterion, rationale)}\n\n"
            "## Agent's trajectory\n"
            f"{agent_artifact}\n\n"
            "## Your task\n"
            "Evaluate whether the agent's behavior satisfies the criterion. "
            "Consider what the agent investigated, in what order, and with what tools.\n\n"
            f"{_OUTPUT_INSTRUCTION}"
        )


class CodeQAWithPenaltyPromptV1(BaseJudgePrompt):
    """Ported from swe_bench_ext.rubric_grader.CODEQA_GRADING_GUIDELINES."""

    system_prompt = (
        "You are evaluating a model's plain-text answer to a question about "
        "a codebase. Apply the following grading rules:\n\n"
        "- 'critical' criteria: core knowledge the answer MUST demonstrate. "
        "  Strict grading. Mark passed=true only if the answer clearly demonstrates this.\n"
        "- 'bonus' criteria: extra credit for depth/precision. Lenient grading. "
        "  Mark passed=true if the answer addresses this at all.\n"
        "- 'penalty' criteria: check if the answer contains the described ERROR or "
        "  MISCONCEPTION. For penalty criteria, passed=true means the answer DOES "
        "  contain the error (which will cause a deduction). Only mark passed=true "
        "  if the error is clearly present.\n\n"
        "Grade based on the written answer, not the agent's exploration process."
    )

    def build_judge_prompt(
        self,
        *,
        criterion: str,
        rationale: str | None,
        category: list[str],
        agent_artifact: str,
        task_context: str = "",
        weight_label: str | None = None,
    ) -> str:
        label = (weight_label or "critical").strip().lower()
        return (
            f"{_format_task_context(task_context)}"
            f"## Criterion (weight label: {label})\n"
            f"{_format_criterion(criterion, rationale)}\n\n"
            "## Agent's answer\n"
            f"{agent_artifact}\n\n"
            "## Your task\n"
            f"This criterion has weight label '{label}'. Apply the grading rules "
            "in the system prompt for that label (critical / bonus / penalty).\n\n"
            f"{_OUTPUT_INSTRUCTION}"
        )


PROMPT_REGISTRY: dict[str, BaseJudgePrompt] = {
    p.id: p
    for p in (
        CodeDiffPromptV1(),
        PlanPromptV1(),
        TrajectoryPromptV1(),
        CodeQAWithPenaltyPromptV1(),
    )
}

PROMPT_OPTIONS: list[str] = list(PROMPT_REGISTRY.keys())

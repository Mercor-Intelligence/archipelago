"""
Agent registry mapping agent IDs to their implementations and config schemas.
"""

from typing import Any

from runner.agents.loop_agent.main import run as loop_agent_run
from runner.agents.loop_truncated_tools_agent.main import (
    run as loop_truncated_tools_agent_run,
)
from runner.agents.models import (
    AgentConfigIds,
    AgentDefn,
    AgentImpl,
    AgentRunInput,
    AgentTrajectoryOutput,
)
from runner.agents.react_toolbelt_agent.main import run as react_toolbelt_agent_run
from runner.agents.single_shot_multimodal.main import run as single_shot_multimodal_run
from runner.agents.singleshot_agent.main import run as single_shot_agent_run
from runner.agents.stirrup_agent.main import run as stirrup_agent_run
from runner.models import TaskFieldSchema, TaskFieldType

def _eq_filter(field_id: str, value: str) -> dict[str, Any]:
    return {"predicate_custom_field_id": field_id, "comparison": "eq", "value": value}

# Stirrup's Studio-facing knobs, hoisted so `indago_stirrup_agent` can share them
# rather than carry a copy that drifts. Defined here, INSIDE the agent-registry
# sync markers, so `sync_agent_definitions.sh` carries it to the server registry
# along with the entries that use it. (The marker tokens are deliberately not
# spelled out above: the OSS codegen asserts no marker text survives into
# generated output, and prose quoting one trips it.)
_STIRRUP_CONFIG_FIELDS = [
    TaskFieldSchema(
        field_id="timeout",
        field_type=TaskFieldType.NUMBER,
        label="Timeout (seconds)",
        description="Maximum time for agent execution. Sized for a full 250-turn GDPval-AA v2 run; a 100-turn run simply finishes early.",
        default_value=27000,  # 7.5 hours
        min_value=300,  # 5 minutes
        max_value=28800,  # 8 hours
    ),
    TaskFieldSchema(
        field_id="max_turns",
        field_type=TaskFieldType.NUMBER,
        label="Max Turns",
        description="Maximum number of agent turns. GDPval-AA v2 uses 250; the default stays 100 so existing worlds are unchanged.",
        default_value=100,
        min_value=1,
        max_value=250,
    ),
    TaskFieldSchema(
        field_id="turns_remaining_warning_threshold",
        field_type=TaskFieldType.NUMBER,
        label="Turn Warning Threshold",
        description="Inject turn warnings when remaining turns <= this value (AA GDPval default: 80)",
        default_value=80,
        min_value=1,
        max_value=100,
    ),
    TaskFieldSchema(
        field_id="tool_call_timeout",
        field_type=TaskFieldType.NUMBER,
        label="Tool Call Timeout (seconds)",
        description="Timeout for individual tool calls",
        default_value=300,
        min_value=60,
        max_value=600,
    ),
    TaskFieldSchema(
        field_id="llm_response_timeout",
        field_type=TaskFieldType.NUMBER,
        label="LLM Response Timeout (seconds)",
        description="Timeout for LLM API calls. With streaming on this is a per-chunk idle timeout, not a total-generation cap.",
        default_value=600,
        min_value=30,
        max_value=1800,
    ),
    TaskFieldSchema(
        field_id="stream",
        field_type=TaskFieldType.BOOLEAN,
        label="Stream Responses",
        description=(
            "Stream model responses so the HTTP connection stays active. "
            "Required for long generations: a non-streaming call holds an "
            "idle connection that the load balancer drops at its 30-minute "
            "idle timeout, causing 502/timeout errors and 3-hour run "
            "failures. Leave on unless a provider misbehaves on streaming."
        ),
        default_value=True,
    ),
    TaskFieldSchema(
        field_id="custom_system_prompt",
        field_type=TaskFieldType.TEXTAREA,
        label="Custom System Prompt",
        description="Additional instructions appended to Stirrup's base system prompt (e.g., GDPval-AA instructions)",
    ),
    TaskFieldSchema(
        field_id="base_system_prompt",
        field_type=TaskFieldType.TEXTAREA,
        label="Base System Prompt",
        description=(
            "REPLACES Stirrup's base system prompt. Leave empty for the "
            'built-in default: "You are an AI agent that will be given '
            "a specific task. You are to complete that task using the "
            "tools provided in {max_turns} steps. You will need to call "
            "the finish tool as your last step. IMPORTANT: The 'reason' "
            "parameter you pass to the finish tool IS your final answer "
            "- put your complete response, summary, or explanation "
            "directly in the 'reason' parameter. Any text you write "
            'outside the finish tool call will be ignored." '
            "'{max_turns}' is replaced with the configured Max Turns; "
            "every other brace is literal, so a JSON example is safe to "
            "include. Omit the placeholder and the step budget is "
            "appended at the end. Unlike Custom System Prompt (which is "
            "appended), this swaps out the base clause only - the input "
            "files section and Custom System Prompt still follow it."
        ),
    ),
    TaskFieldSchema(
        field_id="enable_abandon_task",
        field_type=TaskFieldType.BOOLEAN,
        label="Enable Abandon Task Tool",
        description=(
            "Offer GDPval-AA v2's abandon_task tool alongside finish, "
            "letting the model end a run early with a brief reason when "
            "it judges the task impossible instead of submitting files. "
            "An abandoned run still completes and is still graded, so "
            "give-ups stay in the model's average rather than vanishing "
            "from it, and output['abandoned'] tells the two terminal "
            "paths apart. NOTE: grading reads the run's filesystem, not "
            "the tool's declared paths, so any files written before "
            "abandoning are still scored - abandoning is not the same as "
            "submitting nothing. Off by default: enabling it changes the "
            "toolbelt, so opt in per world."
        ),
        default_value=False,
    ),
    TaskFieldSchema(
        field_id="disabled_tools",
        field_type=TaskFieldType.TEXT,
        label="Disabled Tools",
        description=(
            "Comma-separated MCP tool names to hide from the model "
            "(e.g. stirrup_code_execution_read_image_file). Leave empty "
            "to expose every platform tool, which is the default and "
            "paper-faithful Stirrup. A hidden tool the model calls anyway "
            "is answered with an error instead of being executed; finish "
            "can never be disabled. A value set earlier through the API "
            "as a JSON list still applies, but a text box cannot show a "
            "list, so it appears blank here — retype it before saving if "
            "you need to change it."
        ),
    ),
    TaskFieldSchema(
        field_id="accounting_mode",
        field_type=TaskFieldType.SELECT,
        label="Accounting Mode",
        description=(
            "Which per-turn $/token budget-warning mechanism is active "
            "for this run: Default (none), 90/10 Token Accounting (Token "
            "Budget below), or Full Cost Accounting (real $ cost "
            "tracking further below). Mutually exclusive. Turn Warning "
            "Threshold (above) is independent of this selector and "
            "applies in any mode."
        ),
        options=["default", "token_accounting", "cost_accounting"],
        default_value="default",
    ),
    TaskFieldSchema(
        field_id="token_budget",
        field_type=TaskFieldType.NUMBER,
        label="Token Budget",
        description=(
            "Total provider-reported prompt+completion tokens the run "
            "may spend. The agent is told the remaining budget each "
            "turn and gets one final turn to call the finish tool once "
            "it is exhausted. 0 disables budgeting. Only used in 90/10 "
            "Token Accounting."
        ),
        default_value=0,
        min_value=0,
        conditional_render_filter=[_eq_filter("accounting_mode", "token_accounting")],
    ),
    TaskFieldSchema(
        field_id="input_cost_per_token",
        field_type=TaskFieldType.NUMBER,
        label="Input Cost Override ($/token)",
        description=(
            "Overrides the litellm pricing table's input rate for this "
            "run. Only used when explicitly set; otherwise the table "
            "default applies. Only used in Full Cost Accounting."
        ),
        min_value=0,
        conditional_render_filter=[_eq_filter("accounting_mode", "cost_accounting")],
    ),
    TaskFieldSchema(
        field_id="output_cost_per_token",
        field_type=TaskFieldType.NUMBER,
        label="Output Cost Override ($/token)",
        description=(
            "Overrides the litellm pricing table's output rate for "
            "this run. Only used when explicitly set; otherwise the "
            "table default applies. Only used in Full Cost Accounting."
        ),
        min_value=0,
        conditional_render_filter=[_eq_filter("accounting_mode", "cost_accounting")],
    ),
    TaskFieldSchema(
        field_id="cached_input_cost_per_token",
        field_type=TaskFieldType.NUMBER,
        label="Cached Input Cost Override ($/token)",
        description=(
            "Overrides the litellm pricing table's cache-read rate for "
            "this run. Only used when explicitly set; otherwise the "
            "table default (or a 10% of input rate heuristic) applies. "
            "Only used in Full Cost Accounting."
        ),
        min_value=0,
        conditional_render_filter=[_eq_filter("accounting_mode", "cost_accounting")],
    ),
    TaskFieldSchema(
        field_id="cache_creation_cost_per_token",
        field_type=TaskFieldType.NUMBER,
        label="Cache Creation Cost Override ($/token)",
        description=(
            "Overrides the litellm pricing table's cache-write rate "
            "for this run. Only used when explicitly set; otherwise "
            "the table default (or a 125% of input rate heuristic) "
            "applies. Only used in Full Cost Accounting."
        ),
        min_value=0,
        conditional_render_filter=[_eq_filter("accounting_mode", "cost_accounting")],
    ),
    TaskFieldSchema(
        field_id="cost_budget_usd",
        field_type=TaskFieldType.NUMBER,
        label="Cost Budget (USD)",
        description=(
            "Total USD the run may spend. The agent is told the "
            "remaining budget each turn and gets one final turn to "
            "call the finish tool once it is exhausted. 0 disables the "
            "cap, but cost is still logged. Only used in Full Cost "
            "Accounting."
        ),
        default_value=0,
        min_value=0,
        conditional_render_filter=[_eq_filter("accounting_mode", "cost_accounting")],
    ),
]

# Stirrup's fields, with ONE widened bound. A scanning tool is a single tool call
# that runs for as long as the scan does -- observed up to 1h55m on the SECPRJ-1900
# set -- while stirrup's `tool_call_timeout` is authored for ordinary tools and caps
# the form at 600s. The cap is UI metadata (nothing server-side or in the runner
# enforces it, which is why those scans completed), so this changes no behaviour;
# it makes the agent's config able to EXPRESS the timeout its own docstring says it
# needs, instead of an operator having to go around the form.
#
# Derived from the stirrup list rather than copied, so a field added to stirrup
# still appears here. Only the named field is replaced.
_INDAGO_STIRRUP_TOOL_TIMEOUT_MAX = 7200  # 2h, over the 1h55m observed worst case
_INDAGO_STIRRUP_CONFIG_FIELDS = [
    field.model_copy(update={"max_value": _INDAGO_STIRRUP_TOOL_TIMEOUT_MAX})
    if field.field_id == "tool_call_timeout"
    else field
    for field in _STIRRUP_CONFIG_FIELDS
]

AGENT_REGISTRY: dict[AgentConfigIds, AgentDefn] = {
    AgentConfigIds.LOOP_AGENT: AgentDefn(
        agent_config_id=AgentConfigIds.LOOP_AGENT,
        agent_impl=loop_agent_run,
        agent_config_fields=[
            TaskFieldSchema(
                field_id="timeout",
                field_type=TaskFieldType.NUMBER,
                label="Timeout (seconds)",
                description="Maximum time for agent execution",
                default_value=10800,  # 3 hours
                min_value=300,  # 5 minutes
                max_value=28800,  # 8 hours
            ),
            TaskFieldSchema(
                field_id="max_steps",
                field_type=TaskFieldType.NUMBER,
                label="Max Steps",
                description="Maximum number of LLM calls before stopping",
                default_value=100,
                min_value=1,
                max_value=1000,
            ),
            TaskFieldSchema(
                field_id="tool_call_timeout",
                field_type=TaskFieldType.NUMBER,
                label="Tool Call Timeout (seconds)",
                description="Timeout for individual tool calls",
                default_value=60,
                min_value=10,
                max_value=600,
            ),
            TaskFieldSchema(
                field_id="llm_response_timeout",
                field_type=TaskFieldType.NUMBER,
                label="LLM Response Timeout (seconds)",
                description="Timeout for LLM API calls",
                default_value=600,
                min_value=30,
                max_value=1200,
            ),
            TaskFieldSchema(
                field_id="accounting_mode",
                field_type=TaskFieldType.SELECT,
                label="Accounting Mode",
                description=(
                    "Which per-step $/token budget-warning mechanism is active "
                    "for this run: Default (none), 90/10 Token Accounting (Token "
                    "Budget below), or Full Cost Accounting (real $ cost "
                    "tracking further below). Mutually exclusive. Agents "
                    "configured before this field existed that already have a "
                    "Token Budget set keep working under 90/10 Token Accounting "
                    "automatically. Turn Warnings (below) are independent of "
                    "this selector and apply in any mode."
                ),
                options=["default", "token_accounting", "cost_accounting"],
                default_value="default",
            ),
            TaskFieldSchema(
                field_id="token_budget",
                field_type=TaskFieldType.NUMBER,
                label="Token Budget",
                description=(
                    "Total provider-reported prompt+completion tokens the run "
                    "may spend. The agent is told the remaining budget each "
                    "step and gets one final step once it is exhausted. "
                    "0 disables budgeting. Only used in 90/10 Token Accounting."
                ),
                default_value=0,
                min_value=0,
                conditional_render_filter=[
                    _eq_filter("accounting_mode", "token_accounting")
                ],
            ),
            TaskFieldSchema(
                field_id="turn_warnings_enabled",
                field_type=TaskFieldType.BOOLEAN,
                label="Enable Turn Warnings",
                description=(
                    "Inject a per-step 'N step(s) remaining' warning so the "
                    "agent wraps up before hitting Max Steps. Off by default. "
                    "Independent of Accounting Mode/Token Budget — applies in "
                    "any mode, including Full Cost Accounting."
                ),
                default_value=False,
            ),
            TaskFieldSchema(
                field_id="input_cost_per_token",
                field_type=TaskFieldType.NUMBER,
                label="Input Cost Override ($/token)",
                description=(
                    "Overrides the litellm pricing table's input rate for this "
                    "run. Only used when explicitly set; otherwise the table "
                    "default applies. Only used in Full Cost Accounting."
                ),
                min_value=0,
                conditional_render_filter=[
                    _eq_filter("accounting_mode", "cost_accounting")
                ],
            ),
            TaskFieldSchema(
                field_id="output_cost_per_token",
                field_type=TaskFieldType.NUMBER,
                label="Output Cost Override ($/token)",
                description=(
                    "Overrides the litellm pricing table's output rate for "
                    "this run. Only used when explicitly set; otherwise the "
                    "table default applies. Only used in Full Cost Accounting."
                ),
                min_value=0,
                conditional_render_filter=[
                    _eq_filter("accounting_mode", "cost_accounting")
                ],
            ),
            TaskFieldSchema(
                field_id="cached_input_cost_per_token",
                field_type=TaskFieldType.NUMBER,
                label="Cached Input Cost Override ($/token)",
                description=(
                    "Overrides the litellm pricing table's cache-read rate for "
                    "this run. Only used when explicitly set; otherwise the "
                    "table default (or a 10% of input rate heuristic) applies. "
                    "Only used in Full Cost Accounting."
                ),
                min_value=0,
                conditional_render_filter=[
                    _eq_filter("accounting_mode", "cost_accounting")
                ],
            ),
            TaskFieldSchema(
                field_id="cache_creation_cost_per_token",
                field_type=TaskFieldType.NUMBER,
                label="Cache Creation Cost Override ($/token)",
                description=(
                    "Overrides the litellm pricing table's cache-write rate "
                    "for this run. Only used when explicitly set; otherwise "
                    "the table default (or a 125% of input rate heuristic) "
                    "applies. Only used in Full Cost Accounting."
                ),
                min_value=0,
                conditional_render_filter=[
                    _eq_filter("accounting_mode", "cost_accounting")
                ],
            ),
            TaskFieldSchema(
                field_id="cost_budget_usd",
                field_type=TaskFieldType.NUMBER,
                label="Cost Budget (USD)",
                description=(
                    "Total USD the run may spend. The agent is told the "
                    "remaining budget each step and gets one final step once "
                    "it is exhausted. 0 disables the cap, but cost is still "
                    "logged. Only used in Full Cost Accounting."
                ),
                default_value=0,
                min_value=0,
                conditional_render_filter=[
                    _eq_filter("accounting_mode", "cost_accounting")
                ],
            ),
        ],
    ),
    AgentConfigIds.LOOP_TRUNCATED_TOOLS_AGENT: AgentDefn(
        agent_config_id=AgentConfigIds.LOOP_TRUNCATED_TOOLS_AGENT,
        agent_impl=loop_truncated_tools_agent_run,
        agent_config_fields=[
            TaskFieldSchema(
                field_id="timeout",
                field_type=TaskFieldType.NUMBER,
                label="Timeout (seconds)",
                description="Maximum time for agent execution",
                default_value=10800,  # 3 hours
                min_value=300,  # 5 minutes
                max_value=28800,  # 8 hours
            ),
            TaskFieldSchema(
                field_id="max_steps",
                field_type=TaskFieldType.NUMBER,
                label="Max Steps",
                description="Maximum number of LLM calls before stopping",
                default_value=100,
                min_value=1,
                max_value=1000,
            ),
            TaskFieldSchema(
                field_id="tool_call_timeout",
                field_type=TaskFieldType.NUMBER,
                label="Tool Call Timeout (seconds)",
                description="Timeout for individual tool calls",
                default_value=60,
                min_value=10,
                max_value=600,
            ),
            TaskFieldSchema(
                field_id="llm_response_timeout",
                field_type=TaskFieldType.NUMBER,
                label="LLM Response Timeout (seconds)",
                description="Timeout for LLM API calls",
                default_value=600,
                min_value=30,
                max_value=1200,
            ),
            # max_output_lines is deliberately NOT exposed here. The line check
            # in _truncate_output runs first and tests `total_lines > max_lines`,
            # but every MCP tool result is single-line JSON, so total_lines == 1
            # and the branch is structurally unreachable — 0 line-truncations
            # across 677 production results. The code still reads
            # config.get("max_output_lines", 200), so agent instances with a
            # stored value keep loading and behave bit-identically; only the
            # control is retired, so operators stop sizing a knob that does
            # nothing. web_research_agent keeps its copy of the field: web
            # pages really are multi-line and there it fires.
            TaskFieldSchema(
                field_id="max_output_chars",
                field_type=TaskFieldType.NUMBER,
                label="Max Output Characters",
                description=(
                    "Maximum characters per tool output before truncation. "
                    "This is the only cap on tool output for this agent. Note "
                    "the unit: dense JSON tool results measured ~2.5 "
                    "characters per billed token, so 100,000 characters is "
                    "roughly 40,000 tokens of context per result — a 48-call "
                    "parallel batch at that setting can cost ~1.9M tokens."
                ),
                default_value=32768,
                min_value=1000,
                max_value=100000,
            ),
            TaskFieldSchema(
                field_id="context_trim_utilization",
                field_type=TaskFieldType.NUMBER,
                label="Context Trim Utilization",
                description=(
                    "Fraction of the model's input window above which the "
                    "oldest exchanges (an assistant message and its tool "
                    "results, always dropped together) are removed before each "
                    "request is sent. Also enables recovery from a "
                    "context-window rejection: the run trims and retries "
                    "instead of ending — unless there is nothing left to trim, "
                    "in which case it still ends immediately rather than "
                    "resending the same oversize prompt. 0 disables both "
                    "(default). 0.85 is a "
                    "reasonable starting point; the budget is measured against "
                    "the provider's own reported prompt tokens, not an "
                    "estimate."
                ),
                default_value=0,
                min_value=0,
                max_value=0.95,
            ),
            TaskFieldSchema(
                field_id="context_trim_floor",
                field_type=TaskFieldType.NUMBER,
                label="Context Trim Floor",
                description=(
                    "Once trimming fires, how far down to trim — as a fraction "
                    "of the input window. Trimming back to exactly the "
                    "utilization limit re-fires on nearly every following step "
                    "and re-pays the prompt cache each time, so this is "
                    "deliberately lower. Ignored when Context Trim Utilization "
                    "is 0."
                ),
                default_value=0.6,
                min_value=0.2,
                max_value=0.9,
            ),
            TaskFieldSchema(
                field_id="max_step_tool_result_tokens",
                field_type=TaskFieldType.NUMBER,
                label="Max Tool-Result Tokens per Step",
                description=(
                    "Total tool-result tokens a single step's tool calls may "
                    "return. Once spent, the remaining results in that step "
                    "are replaced with an error telling the model to narrow "
                    "its query — refused rather than quietly shortened, so it "
                    "cannot mistake a partial result for a complete one. This "
                    "covers what Context Trim Utilization cannot: a step whose "
                    "own parallel batch overflows the window before the next "
                    "trim can run. 0 disables (default). When Context Trim "
                    "Utilization is also set, the effective budget is the "
                    "smaller of this and the room actually left in the window."
                ),
                default_value=0,
                min_value=0,
            ),
            TaskFieldSchema(
                field_id="spill_oversized_results",
                field_type=TaskFieldType.BOOLEAN,
                label="Spill Oversized Tool Results to Filesystem",
                description=(
                    "When a tool result exceeds Max Tool-Result Tokens per "
                    "Step, write it to the environment filesystem and hand the "
                    "model the path, instead of refusing it. The model reads "
                    "the complete result with the code execution tool, which "
                    "shares that filesystem, so the payload never enters the "
                    "context window. Off by default, because refusing is the "
                    "right answer for a narrowable call — the model just asks "
                    "again with a tighter query. Turn it on for worlds whose "
                    "overflow comes from atomic fetches (id in, whole object "
                    "out, no range parameter), where there is no narrower "
                    "query to make and a refusal ends the task. Needs a tool "
                    "that can read the filesystem, e.g. code execution. Falls "
                    "back to the refusal if the write fails."
                ),
                default_value=False,
            ),
            TaskFieldSchema(
                field_id="accounting_mode",
                field_type=TaskFieldType.SELECT,
                label="Accounting Mode",
                description=(
                    "Which per-step $/token budget-warning mechanism is active "
                    "for this run: Default (none), 90/10 Token Accounting (Token "
                    "Budget below), or Full Cost Accounting (real $ cost "
                    "tracking further below). Mutually exclusive. Agents "
                    "configured before this field existed that already have a "
                    "Token Budget set keep working under 90/10 Token Accounting "
                    "automatically. Turn Warnings (below) are independent of "
                    "this selector and apply in any mode."
                ),
                options=["default", "token_accounting", "cost_accounting"],
                default_value="default",
            ),
            TaskFieldSchema(
                field_id="token_budget",
                field_type=TaskFieldType.NUMBER,
                label="Token Budget",
                description=(
                    "Total provider-reported prompt+completion tokens the run "
                    "may spend. The agent is told the remaining budget each "
                    "step and gets one final step once it is exhausted. "
                    "0 disables budgeting. Only used in 90/10 Token Accounting."
                ),
                default_value=0,
                min_value=0,
                conditional_render_filter=[
                    _eq_filter("accounting_mode", "token_accounting")
                ],
            ),
            TaskFieldSchema(
                field_id="turn_warnings_enabled",
                field_type=TaskFieldType.BOOLEAN,
                label="Enable Turn Warnings",
                description=(
                    "Inject a per-step 'N step(s) remaining' warning so the "
                    "agent wraps up before hitting Max Steps. Off by default. "
                    "Independent of Accounting Mode/Token Budget — applies in "
                    "any mode, including Full Cost Accounting."
                ),
                default_value=False,
            ),
            TaskFieldSchema(
                field_id="input_cost_per_token",
                field_type=TaskFieldType.NUMBER,
                label="Input Cost Override ($/token)",
                description=(
                    "Overrides the litellm pricing table's input rate for this "
                    "run. Only used when explicitly set; otherwise the table "
                    "default applies. Only used in Full Cost Accounting."
                ),
                min_value=0,
                conditional_render_filter=[
                    _eq_filter("accounting_mode", "cost_accounting")
                ],
            ),
            TaskFieldSchema(
                field_id="output_cost_per_token",
                field_type=TaskFieldType.NUMBER,
                label="Output Cost Override ($/token)",
                description=(
                    "Overrides the litellm pricing table's output rate for "
                    "this run. Only used when explicitly set; otherwise the "
                    "table default applies. Only used in Full Cost Accounting."
                ),
                min_value=0,
                conditional_render_filter=[
                    _eq_filter("accounting_mode", "cost_accounting")
                ],
            ),
            TaskFieldSchema(
                field_id="cached_input_cost_per_token",
                field_type=TaskFieldType.NUMBER,
                label="Cached Input Cost Override ($/token)",
                description=(
                    "Overrides the litellm pricing table's cache-read rate for "
                    "this run. Only used when explicitly set; otherwise the "
                    "table default (or a 10% of input rate heuristic) applies. "
                    "Only used in Full Cost Accounting."
                ),
                min_value=0,
                conditional_render_filter=[
                    _eq_filter("accounting_mode", "cost_accounting")
                ],
            ),
            TaskFieldSchema(
                field_id="cache_creation_cost_per_token",
                field_type=TaskFieldType.NUMBER,
                label="Cache Creation Cost Override ($/token)",
                description=(
                    "Overrides the litellm pricing table's cache-write rate "
                    "for this run. Only used when explicitly set; otherwise "
                    "the table default (or a 125% of input rate heuristic) "
                    "applies. Only used in Full Cost Accounting."
                ),
                min_value=0,
                conditional_render_filter=[
                    _eq_filter("accounting_mode", "cost_accounting")
                ],
            ),
            TaskFieldSchema(
                field_id="cost_budget_usd",
                field_type=TaskFieldType.NUMBER,
                label="Cost Budget (USD)",
                description=(
                    "Total USD the run may spend. The agent is told the "
                    "remaining budget each step and gets one final step once "
                    "it is exhausted. 0 disables the cap, but cost is still "
                    "logged. Only used in Full Cost Accounting."
                ),
                default_value=0,
                min_value=0,
                conditional_render_filter=[
                    _eq_filter("accounting_mode", "cost_accounting")
                ],
            ),
        ],
    ),
    AgentConfigIds.REACT_TOOLBELT_AGENT: AgentDefn(
        agent_config_id=AgentConfigIds.REACT_TOOLBELT_AGENT,
        agent_impl=react_toolbelt_agent_run,
        agent_config_fields=[
            TaskFieldSchema(
                field_id="timeout",
                field_type=TaskFieldType.NUMBER,
                label="Timeout (seconds)",
                description="Maximum time for agent execution",
                default_value=10800,  # 3 hours
                min_value=300,  # 5 minutes
                max_value=28800,  # 8 hours
            ),
            TaskFieldSchema(
                field_id="max_steps",
                field_type=TaskFieldType.NUMBER,
                label="Max Steps",
                description="Maximum number of LLM calls before stopping",
                default_value=250,
                min_value=1,
                max_value=1000,
            ),
            TaskFieldSchema(
                field_id="tool_call_timeout",
                field_type=TaskFieldType.NUMBER,
                label="Tool Call Timeout (seconds)",
                description="Timeout for individual tool calls",
                default_value=60,
                min_value=10,
                max_value=600,
            ),
            TaskFieldSchema(
                field_id="llm_response_timeout",
                field_type=TaskFieldType.NUMBER,
                label="LLM Response Timeout (seconds)",
                description="Timeout for a single LLM API call",
                default_value=600,
                min_value=30,
                max_value=10800,
            ),
        ],
    ),
    AgentConfigIds.SINGLESHOT_AGENT: AgentDefn(
        agent_config_id=AgentConfigIds.SINGLESHOT_AGENT,
        agent_impl=single_shot_agent_run,
        agent_config_fields=[
            TaskFieldSchema(
                field_id="llm_response_timeout",
                field_type=TaskFieldType.NUMBER,
                label="LLM Response Timeout (seconds)",
                description="Timeout for LLM API calls",
                default_value=600,
                min_value=30,
                max_value=10800,
            ),
        ],
    ),
    AgentConfigIds.STIRRUP_AGENT: AgentDefn(
        agent_config_id=AgentConfigIds.STIRRUP_AGENT,
        agent_impl=stirrup_agent_run,
        agent_config_fields=_STIRRUP_CONFIG_FIELDS,
    ),
    AgentConfigIds.SINGLE_SHOT_MULTIMODAL: AgentDefn(
        agent_config_id=AgentConfigIds.SINGLE_SHOT_MULTIMODAL,
        agent_impl=single_shot_multimodal_run,
        agent_config_fields=[
            TaskFieldSchema(
                field_id="llm_response_timeout",
                field_type=TaskFieldType.NUMBER,
                label="LLM Response Timeout (seconds)",
                description="Timeout for LLM API calls",
                default_value=600,
                min_value=30,
                max_value=10800,
            ),
        ],
    ),
}

def get_agent_impl(agent_config_id: str) -> AgentImpl:
    """
    Get the agent implementation function for the given agent config ID.

    Args:
        agent_config_id: The agent config ID to look up (e.g., "loop_agent")

    Returns:
        The agent implementation function

    Raises:
        ValueError: If the agent config ID is not found in the registry
    """
    try:
        config_id_enum = AgentConfigIds(agent_config_id)
    except ValueError as e:
        raise ValueError(f"Unknown agent config ID: {agent_config_id}") from e

    defn = AGENT_REGISTRY.get(config_id_enum)
    if defn is None:
        raise ValueError(f"Unknown agent config ID: {agent_config_id}")

    if defn.agent_impl is None:
        raise ValueError(
            f"Agent '{agent_config_id}' is registered but has no implementation"
        )

    return defn.agent_impl

def get_agent_defn(agent_config_id: str) -> AgentDefn:
    """
    Get the full agent definition for the given agent config ID.

    Args:
        agent_config_id: The agent config ID to look up (e.g., "loop_agent")

    Returns:
        The agent definition including config fields

    Raises:
        ValueError: If the agent config ID is not found in the registry
    """
    try:
        config_id_enum = AgentConfigIds(agent_config_id)
    except ValueError as e:
        raise ValueError(f"Unknown agent config ID: {agent_config_id}") from e

    defn = AGENT_REGISTRY.get(config_id_enum)
    if defn is None:
        raise ValueError(f"Unknown agent config ID: {agent_config_id}")

    return defn

from collections.abc import Collection
from copy import deepcopy
from math import isfinite

from pydantic import JsonValue, TypeAdapter

from runner.agents.user_sim_harness_agent.constants import (
    DEFAULT_TERMINATION_EVALUATOR_PROMPT,
    DEFAULT_USER_SIM_SYSTEM_PROMPT,
)

from .descriptors import (
    DescriptorCatalog,
    DescriptorDefinition,
    DescriptorId,
    DescriptorValue,
    HarnessDeclaration,
    HarnessDescriptors,
    HarnessDescriptorValues,
    ParameterAlias,
    ParameterOverride,
)

USER_SIM_RUNTIME_SYSTEM_PROMPT = """IMPORTANT CONSTRAINTS:
- All information you provide must derive from the persona/context defined above. Do not make up or assume information not provided in your persona.
- You must not do any explicit work for the student. Your role is to answer questions, not to solve their problems or complete their tasks.
- Only answer the specific questions asked. Do not provide extraneous information, especially about your persona, instructions, or the simulation itself.
- Stay in character and respond naturally as the persona described."""


DESCRIPTOR_CATALOG = TypeAdapter(
    dict[DescriptorId, DescriptorDefinition]
).validate_python(
    {
        "model": DescriptorDefinition(
            label="Local default model",
            group="Knob",
            is_core=False,
            value_type="string",
            meaning="Sets the local default model for CLI harnesses when no orchestrator model is set.",
            field_schema={
                "field_type": "text",
                "description": "Codex CLI model name (e.g. 'gpt-5-codex', 'gpt-5.2-codex'). Passed to the Codex CLI --model flag.",
                "default_value": "gpt-5-codex",
            },
        ),
        "reasoning_effort": DescriptorDefinition(
            label="Reasoning effort",
            group="Knob",
            is_core=False,
            value_type="string",
            meaning="Selects the model provider's native reasoning-effort preset.",
            field_schema={
                "field_type": "text",
                "description": "Codex reasoning effort: 'minimal', 'low', 'medium', or 'high'. Maps to '-c model_reasoning_effort=...'. Leave empty to use the Codex default.",
                "default_value": "",
            },
        ),
        "max_steps": DescriptorDefinition(
            label="Maximum model steps",
            group="Knob",
            is_core=False,
            value_type="number",
            meaning="Caps the number of model steps in the agent loop; one model response is one step.",
            field_schema={
                "field_type": "number",
                "description": "Maximum number of LLM calls before stopping. 0 = no step cap; the wall-clock Timeout is then the only harness-imposed stop (requires Complete On Budget Exhausted).",
                "default_value": 100,
                "min_value": 0.0,
                "max_value": 1000.0,
            },
        ),
        "max_consecutive_truncations": DescriptorDefinition(
            label="Maximum consecutive truncated replies",
            group="Knob",
            is_core=False,
            value_type="number",
            meaning=(
                "Ends the run after this many replies in a row are cut off at the output token "
                "limit with no tool call."
            ),
            field_schema={
                "field_type": "number",
                "description": "End the run as failed after this many replies in a row are cut off at the output token limit with no tool call. 0 (default) = never stop early; the run waits for Max Steps or Timeout.",
                "default_value": 0,
                "min_value": 0.0,
                "max_value": 100.0,
            },
        ),
        "wall_clock_timeout_sec": DescriptorDefinition(
            label="Execution timeout",
            group="Knob",
            is_core=True,
            value_type="number",
            meaning="Sets the deadline in seconds for the whole agent run.",
            field_schema={
                "field_type": "number",
                "description": "Maximum time for agent execution",
                "default_value": 10800,
                "min_value": 300.0,
                "max_value": 28800.0,
            },
        ),
        "tool_call_timeout_sec": DescriptorDefinition(
            label="Tool call timeout",
            group="Knob",
            is_core=False,
            value_type="number",
            meaning="Sets the deadline in seconds for one tool invocation.",
            field_schema={
                "field_type": "number",
                "description": "Timeout for individual tool calls",
                "default_value": 60,
                "min_value": 10.0,
                "max_value": 600.0,
            },
        ),
        "llm_call_timeout_sec": DescriptorDefinition(
            label="Model response timeout",
            group="Knob",
            is_core=False,
            value_type="number",
            meaning="Sets the deadline in seconds for one model request attempt.",
            field_schema={
                "field_type": "number",
                "description": "Timeout for LLM API calls",
                "default_value": 600,
                "min_value": 30.0,
                "max_value": 1200.0,
            },
        ),
        "llm_stream_idle_timeout_sec": DescriptorDefinition(
            label="Stall timeout (seconds)",
            group="Knob",
            is_core=False,
            value_type="number",
            meaning="Sets the maximum silence in seconds without a streamed event before the run is stopped.",
            field_schema={
                "field_type": "number",
                "description": "Seconds without an opencode event before the run is killed "
                "as stalled. A long reasoning step emits nothing until it "
                "ends, so slow models need more than the app default "
                "(1200). Only takes effect on platforms whose opencode app "
                "accepts the stall_timeout field.",
                "default_value": None,
                "min_value": 300,
                "max_value": 28800,
            },
        ),
        "llm_streaming": DescriptorDefinition(
            label="Stream responses",
            group="Knob",
            is_core=False,
            value_type="boolean",
            meaning="Controls whether model calls stream.",
            field_schema={
                "field_type": "boolean",
                "description": "Stream model responses. Off by default (the Loop agent's historical behaviour); required when LLM Idle Credit is set, since time-to-first-token is unmeasurable non-streaming.",
                "default_value": False,
            },
        ),
        "max_output_tokens": DescriptorDefinition(
            label="Maximum output tokens",
            group="Knob",
            is_core=False,
            value_type="number",
            meaning="Limits output tokens in an individual model response.",
            field_schema={
                "field_type": "number",
                "description": "Per-response output cap (CLAUDE_CODE_MAX_OUTPUT_TOKENS). Unset keeps the CLI default of 64000.",
                "min_value": 1000.0,
                "max_value": 128000.0,
            },
        ),
        "budget_exhaustion_finalizes": DescriptorDefinition(
            label="Complete on execution budget exhaustion",
            group="Knob",
            is_core=False,
            value_type="boolean",
            meaning=(
                "Reports an execution-time or step-budget stop as completed so its output can be "
                "graded."
            ),
            field_schema={
                "field_type": "boolean",
                "description": "When on, a harness-imposed stop — the wall-clock Timeout, or the step cap when Max Steps > 0 — ends the trajectory COMPLETED (so the deliverable on disk is graded) instead of the default FAILED (step cap) / ERROR (wall clock). The output carries budget_limited_attempt and attempt_outcome. Required when Max Steps = 0. Token/cost accounting exhaustion is unaffected (stays FAILED). Off by default.",
                "default_value": False,
            },
        ),
        "skip_tools_on_final_step": DescriptorDefinition(
            label="Skip final-step tools",
            group="Knob",
            is_core=False,
            value_type="boolean",
            meaning="Records but skips tool execution on the last model step.",
            field_schema={
                "field_type": "boolean",
                "description": "When on, tool calls in the response to the last allowed LLM call (step Max Steps, or the final turn after a token/cost budget runs out) are recorded but not executed, since the model could never see their results. At the step cap the run then ends COMPLETED if Complete On Budget Exhausted is on, FAILED otherwise; after a token/cost budget runs out it ends FAILED either way. Matches AgentDojo's tool loop: Max Steps = max_iters + 1. Off by default.",
                "default_value": False,
            },
        ),
        "disable_nudges": DescriptorDefinition(
            label="Disable continuation nudges",
            group="Knob",
            is_core=False,
            value_type="boolean",
            meaning="Stops injecting continuation reminders after responses without tool calls.",
            field_schema={
                "field_type": "boolean",
                "description": "When on, the harness never injects a 'continue' or reasoning-only reminder message: any response without tool calls (including an empty or reasoning-only one) ends the run COMPLETED with that turn as final. A response with no valid choices is a provider fault: it ends the run ERROR, so it is auto-retried, not graded. Opt-in turn/token/cost warnings are unaffected. Off by default.",
                "default_value": False,
            },
        ),
        "enable_abandon_task": DescriptorDefinition(
            label="Enable abandon-task tool",
            group="Knob",
            is_core=False,
            value_type="boolean",
            meaning="Offers a terminal tool for the agent to give up explicitly.",
            field_schema={
                "field_type": "boolean",
                "description": "Offer GDPval-AA v2's abandon_task tool alongside finish, letting the model end a run early with a brief reason when it judges the task impossible instead of submitting files. An abandoned run still completes and is still graded, so give-ups stay in the model's average rather than vanishing from it, and output['abandoned'] tells the two terminal paths apart. NOTE: grading reads the run's filesystem, not the tool's declared paths, so any files written before abandoning are still scored - abandoning is not the same as submitting nothing. Off by default: enabling it changes the toolbelt, so opt in per world.",
                "default_value": False,
            },
        ),
        "llm_idle_credit_sec": DescriptorDefinition(
            label="Model idle-time credit",
            group="Knob",
            is_core=False,
            value_type="number",
            meaning="Caps cumulative time-to-first-token seconds credited back to the execution budget.",
            field_schema={
                "field_type": "number",
                "description": "Cumulative cap on provider time-to-first-token idle (queue + prefill) credited back against the wall-clock Timeout. A credit, not an exemption; generation-bound models gain nothing. 0 = off. Requires Stream on and Complete On Budget Exhausted on.",
                "default_value": 0,
                "min_value": 0.0,
                "max_value": 28800.0,
            },
        ),
        "accounting_mode": DescriptorDefinition(
            label="Budget accounting mode",
            group="Knob",
            is_core=False,
            value_type="select",
            meaning="Selects no per-call budget accounting, token accounting, or cost accounting.",
            field_schema={
                "field_type": "select",
                "description": "Which per-step $/token budget-warning mechanism is active for this run: Default (none), 90/10 Token Accounting (Token Budget below), or Full Cost Accounting (real $ cost tracking further below). Mutually exclusive. Agents configured before this field existed that already have a Token Budget set keep working under 90/10 Token Accounting automatically. Turn Warnings (below) are independent of this selector and apply in any mode.",
                "default_value": "default",
                "options": ["default", "token_accounting", "cost_accounting"],
            },
        ),
        "token_budget": DescriptorDefinition(
            label="Run token budget",
            group="Knob",
            is_core=False,
            value_type="number",
            meaning="Sets the total provider-reported prompt plus completion token budget.",
            field_schema={
                "field_type": "number",
                "description": "Total provider-reported prompt+completion tokens the run may spend. The agent is told the remaining budget each step and gets one final step once it is exhausted. 0 disables budgeting. Only used in 90/10 Token Accounting.",
                "default_value": 0,
                "min_value": 0.0,
                "conditional_render_filter": [
                    {
                        "predicate_custom_field_id": "accounting_mode",
                        "comparison": "eq",
                        "value": "token_accounting",
                    }
                ],
            },
        ),
        "cost_budget_usd": DescriptorDefinition(
            label="Run cost budget",
            group="Knob",
            is_core=False,
            value_type="number",
            meaning="Sets the run's model-cost budget in USD.",
            field_schema={
                "field_type": "number",
                "description": "Total USD the run may spend. The agent is told the remaining budget each step and gets one final step once it is exhausted. 0 disables the cap, but cost is still logged. Only used in Full Cost Accounting.",
                "default_value": 0,
                "min_value": 0.0,
                "conditional_render_filter": [
                    {
                        "predicate_custom_field_id": "accounting_mode",
                        "comparison": "eq",
                        "value": "cost_accounting",
                    }
                ],
            },
        ),
        "input_cost_per_token": DescriptorDefinition(
            label="Input-token price",
            group="Knob",
            is_core=False,
            value_type="number",
            meaning="Overrides the USD price of one uncached input token.",
            field_schema={
                "field_type": "number",
                "description": "Overrides the litellm pricing table's input rate for this run. Only used when explicitly set; otherwise the table default applies. Only used in Full Cost Accounting.",
                "min_value": 0.0,
                "conditional_render_filter": [
                    {
                        "predicate_custom_field_id": "accounting_mode",
                        "comparison": "eq",
                        "value": "cost_accounting",
                    }
                ],
            },
        ),
        "output_cost_per_token": DescriptorDefinition(
            label="Output-token price",
            group="Knob",
            is_core=False,
            value_type="number",
            meaning="Overrides the USD price of one output token.",
            field_schema={
                "field_type": "number",
                "description": "Overrides the litellm pricing table's output rate for this run. Only used when explicitly set; otherwise the table default applies. Only used in Full Cost Accounting.",
                "min_value": 0.0,
                "conditional_render_filter": [
                    {
                        "predicate_custom_field_id": "accounting_mode",
                        "comparison": "eq",
                        "value": "cost_accounting",
                    }
                ],
            },
        ),
        "cached_input_cost_per_token": DescriptorDefinition(
            label="Cached-input-token price",
            group="Knob",
            is_core=False,
            value_type="number",
            meaning="Overrides the USD price of one cache-read input token.",
            field_schema={
                "field_type": "number",
                "description": "Overrides the litellm pricing table's cache-read rate for this run. Only used when explicitly set; otherwise the table default (or a 10% of input rate heuristic) applies. Only used in Full Cost Accounting.",
                "min_value": 0.0,
                "conditional_render_filter": [
                    {
                        "predicate_custom_field_id": "accounting_mode",
                        "comparison": "eq",
                        "value": "cost_accounting",
                    }
                ],
            },
        ),
        "cache_creation_cost_per_token": DescriptorDefinition(
            label="Cache-creation-token price",
            group="Knob",
            is_core=False,
            value_type="number",
            meaning="Overrides the USD price of one cache-write token.",
            field_schema={
                "field_type": "number",
                "description": "Overrides the litellm pricing table's cache-write rate for this run. Only used when explicitly set; otherwise the table default (or a 125% of input rate heuristic) applies. Only used in Full Cost Accounting.",
                "min_value": 0.0,
                "conditional_render_filter": [
                    {
                        "predicate_custom_field_id": "accounting_mode",
                        "comparison": "eq",
                        "value": "cost_accounting",
                    }
                ],
            },
        ),
        "budget_warnings_to_model": DescriptorDefinition(
            label="Enable remaining-step warnings",
            group="Knob",
            is_core=False,
            value_type="boolean",
            meaning="Tells the model how much step budget remains.",
            field_schema={
                "field_type": "boolean",
                "description": "Inject a per-step 'N step(s) remaining' warning so the agent wraps up before hitting Max Steps. Off by default. Independent of Accounting Mode/Token Budget — applies in any mode, including Full Cost Accounting.",
                "default_value": False,
            },
        ),
        "budget_warning_threshold_steps": DescriptorDefinition(
            label="Remaining-turn warning threshold",
            group="Knob",
            is_core=False,
            value_type="number",
            meaning="Sets the remaining steps at which the first budget warning is injected.",
            field_schema={
                "field_type": "number",
                "description": "Inject turn warnings when remaining turns <= this value (AA GDPval default: 80)",
                "default_value": 80,
                "min_value": 1.0,
                "max_value": 100.0,
            },
        ),
        "context_window_tokens": DescriptorDefinition(
            label="Context-window reference size",
            group="Knob",
            is_core=False,
            value_type="number",
            meaning="Sets the token-window ceiling used to calculate a fractional compaction trigger.",
            field_schema={
                "field_type": "number",
                "description": "Ceiling the compaction cutoff is measured against. 0 = resolve per model (server rate card, then litellm). Set to pin one ceiling for every model in a batch.",
                "default_value": 0,
                "min_value": 0.0,
                "max_value": 2000000.0,
            },
        ),
        "context_compaction_threshold": DescriptorDefinition(
            label="Context-compaction threshold",
            group="Knob",
            is_core=False,
            value_type="number",
            meaning="Sets when accumulated context triggers compaction.",
            field_schema={
                "field_type": "number",
                "description": "Compaction trigger as a fraction of the context window.",
                "default_value": 0,
                "min_value": 0,
                "max_value": 0.9,
            },
        ),
        "context_compaction_strategy": DescriptorDefinition(
            label="Context compaction mode",
            group="Knob",
            is_core=False,
            value_type="select",
            meaning="Selects how compaction summarizes the conversation when triggered.",
            field_schema={
                "field_type": "select",
                "description": "Summarize flattened text in a separate call, or request a summary inside the conversation with tools declared.",
                "options": ["flattened", "in_conversation"],
                "default_value": "flattened",
            },
        ),
        "context_compaction_call_timeout_sec": DescriptorDefinition(
            label="Context-summary timeout",
            group="Knob",
            is_core=False,
            value_type="number",
            meaning="Sets the deadline in seconds for each summarizer call, separate from the model call timeout.",
            field_schema={
                "field_type": "number",
                "description": "Timeout for each LLM call that summarizes the conversation. 0 = harness default (300, or the orchestrator's timeout if it sets one). A set value takes precedence over the orchestrator's timeout. The LLM gateway caps priority by timeout: above 600 seconds these calls run at P4 at best, above 1200 at P5, so they can wait longer in the queue.",
                "default_value": 0,
                "min_value": 0.0,
                "max_value": 3600.0,
            },
        ),
        "attachments_staged_to_sandbox": DescriptorDefinition(
            label="Stage prompt files in the workspace",
            group="Knob",
            is_core=False,
            value_type="boolean",
            meaning=(
                "Writes non-image task attachments into the sandbox filesystem and references "
                "them from the prompt."
            ),
            field_schema={
                "field_type": "boolean",
                "description": "Upload files attached to user messages into the sandbox filesystem (/filesystem/turn_inputs/turn_<n>/) and replace them in the prompt with their paths. Turn on for models whose API rejects file content; the model then reads the files with its tools. Images stay inline.",
                "default_value": False,
            },
        ),
        "prompt_attachment_kinds": DescriptorDefinition(
            label="Forward prompt images",
            group="Knob",
            is_core=False,
            value_type="boolean",
            meaning="Selects which task attachments are forwarded into the model prompt.",
            field_schema={
                "field_type": "boolean",
                "description": "Forward images on the task prompt to the agent as file attachments. opencode has no image channel, so an image reaches it as one more file in the workspace, which its Read tool renders (image/* and PDF are both handled). Without this an image block on the task is dropped entirely — only `type: file` attachments travel. Off by default because turning it on changes what an existing world's trajectories measure: the agent starts seeing an image the task always carried.",
                "default_value": False,
            },
        ),
        "input_files": DescriptorDefinition(
            label="Prompt input files",
            group="Knob",
            is_core=False,
            value_type="json",
            meaning="Existing file paths included in the prompt context.",
            field_schema={
                "field_type": "json",
                "description": "Existing file paths included in the prompt context.",
            },
        ),
        "user_persona_file": DescriptorDefinition(
            label="Task fields as a persona file",
            group="Knob",
            is_core=False,
            value_type="json",
            meaning="Renders selected task custom fields into a named Markdown file in the workspace.",
            field_schema={
                "field_type": "json",
                "description": 'When set, extracts task custom fields into a markdown file written to the workspace root before the run. Shape: {"filename": "USER_PERSONA.md", "fields": [{"header": "User Name", "field_id": "field_..."}, ...]}. Each populated field becomes a `## header` section; the file is omitted entirely when none of the listed fields has a value.',
            },
        ),
        "inject_date": DescriptorDefinition(
            label="Task-derived date instruction",
            group="Knob",
            is_core=False,
            value_type="json",
            meaning="Appends a formatted date instruction taken from a selected task custom field.",
            field_schema={
                "field_type": "json",
                "description": 'When set, appends a date line to the system prompt at run time. Shape: {"field_id": "field_...", "template": "Today\'s date is {date}."}. Reads the named date custom field; appends nothing when that field is absent or empty.',
            },
        ),
        "base_system_prompt": DescriptorDefinition(
            label="Base system-prompt replacement",
            group="Knob",
            is_core=False,
            value_type="string",
            meaning="Replaces the harness's built-in base instruction text.",
            field_schema={
                "field_type": "textarea",
                "description": "REPLACES Stirrup's base system prompt. Leave empty for the built-in default: \"You are an AI agent that will be given a specific task. You are to complete that task using the tools provided in {max_turns} steps. You will need to call the finish tool as your last step. IMPORTANT: The 'reason' parameter you pass to the finish tool IS your final answer - put your complete response, summary, or explanation directly in the 'reason' parameter. Any text you write outside the finish tool call will be ignored.\" '{max_turns}' is replaced with the configured Max Turns; every other brace is literal, so a JSON example is safe to include. Omit the placeholder and the step budget is appended at the end. Unlike Custom System Prompt (which is appended), this swaps out the base clause only - the input files section and Custom System Prompt still follow it.",
            },
        ),
        "custom_system_prompt": DescriptorDefinition(
            label="Additional system instructions",
            group="Knob",
            is_core=False,
            value_type="string",
            meaning="Appends instruction text to the base system prompt.",
            field_schema={
                "field_type": "textarea",
                "description": "Additional instructions appended to Stirrup's base system prompt (e.g., GDPval-AA instructions)",
            },
        ),
        "harness_prompt_additions": DescriptorDefinition(
            label="Harness prompt additions",
            group="Knob",
            is_core=False,
            value_type="boolean",
            meaning="Appends the harness's user-interaction and abandonment instructions.",
            field_schema={
                "field_type": "boolean",
                "description": "Append built-in instructions about user interaction and, when enabled, when to abandon a task.",
                "default_value": True,
            },
        ),
        "task_prompt_template": DescriptorDefinition(
            label="Task prompt template",
            group="Knob",
            is_core=False,
            value_type="string",
            meaning="Wraps the first user message in a template containing task and reference-file placeholders.",
            field_schema={
                "field_type": "textarea",
                "description": "Replaces {task} and {reference_files}; other braces stay literal. Without {task}, append the task. Leave empty to send it unchanged.",
            },
        ),
        "harness_wording": DescriptorDefinition(
            label="Harness wording",
            group="Knob",
            is_core=False,
            value_type="select",
            meaning="Selects the text used by the finish tool, turn warnings and continuation nudges.",
            field_schema={
                "field_type": "select",
                "description": "Use Mercor's finish-answer instructions or upstream Stirrup's wording.",
                "options": ["mercor", "aa_stirrup"],
                "default_value": "mercor",
            },
        ),
        "validate_finish_paths": DescriptorDefinition(
            label="Validate finish paths",
            group="Knob",
            is_core=False,
            value_type="boolean",
            meaning="Rejects a finish call when its submitted paths do not exist as files.",
            field_schema={
                "field_type": "boolean",
                "description": "Check submitted file paths using the platform shell and let the model correct missing files before finishing.",
                "default_value": False,
            },
        ),
        "system_instructions_mode": DescriptorDefinition(
            label="World system-prompt placement",
            group="Knob",
            is_core=True,
            value_type="select",
            meaning="Chooses how the world's system instructions reach the model.",
            field_schema={
                "field_type": "select",
                "description": "Codex takes a single prompt string, so the system prompt is folded into the front of the task. Turn off to send the task alone, which is how codex behaved before this was added.",
                "default_value": "prepend",
                "options": ["prepend", "off"],
            },
        ),
        "disabled_tools": DescriptorDefinition(
            label="Tool denial rules",
            group="Knob",
            is_core=False,
            value_type="string",
            meaning="Restricts selected tools or tool calls using the harness's native deny rules.",
            field_schema={
                "field_type": "text",
                "description": "Comma-separated MCP tool names to hide from the model (e.g. stirrup_code_execution_read_image_file). Leave empty to expose every platform tool, which is the default and paper-faithful Stirrup. A hidden tool the model calls anyway is answered with an error instead of being executed; finish can never be disabled. A value set earlier through the API as a JSON list still applies, but a text box cannot show a list, so it appears blank here — retype it before saving if you need to change it.",
            },
        ),
        "enabled_tools": DescriptorDefinition(
            label="Enabled tools",
            group="Knob",
            is_core=False,
            value_type="json",
            meaning="Exact list of tool names the harness may use; unset keeps the built-in list.",
            field_schema={
                "field_type": "json",
                "description": "Exact list of tool names the harness may use; unset keeps the built-in list.",
            },
        ),
        "allowed_tools": DescriptorDefinition(
            label="Preapproved tools",
            group="Knob",
            is_core=False,
            value_type="string",
            meaning="Selects tools preapproved by the native permission system.",
            field_schema={
                "field_type": "text",
                "description": "Comma-separated Claude Code tool names to PREAPPROVE, replacing the harness default (Read, Glob, Edit, Write, Grep, Bash, WebFetch, WebSearch, mcp__gateway__*). Leave empty to keep that default. This is not a filter: a tool left out of the list is still callable, because runs use bypassPermissions and an unlisted tool falls through to the permission mode. To actually take a tool away, name it in Disallowed Tools.",
            },
        ),
        "max_output_lines": DescriptorDefinition(
            label="Tool output line cap",
            group="Knob",
            is_core=False,
            value_type="integer",
            meaning="Truncates each tool result after this many lines before the model sees it.",
            field_schema={
                "field_type": "number",
                "description": "Truncate tool output after this many lines",
                "default_value": 200,
                "min_value": 50.0,
                "max_value": 1000.0,
            },
        ),
        "max_output_chars": DescriptorDefinition(
            label="Tool output character cap",
            group="Knob",
            is_core=False,
            value_type="integer",
            meaning="Truncates each tool result after this many characters before the model sees it.",
            field_schema={
                "field_type": "number",
                "description": "Truncate tool output after this many characters",
                "default_value": 32768,
                "min_value": 4096.0,
                "max_value": 131072.0,
            },
        ),
        "expose_execution_limits": DescriptorDefinition(
            label="Expose execution limits",
            group="Knob",
            is_core=False,
            value_type="boolean",
            meaning="Tells the model its step, wall-clock and token limits, with a progress update before each model call.",
            field_schema={
                "field_type": "boolean",
                "description": "Tell the model its configured step, wall-clock, per-response output-token, and cumulative completion-token limits, with a transient step/time update before each model call.",
                "default_value": False,
            },
        ),
        "reserve_final_answer_step": DescriptorDefinition(
            label="Reserve final answer step",
            group="Knob",
            is_core=False,
            value_type="boolean",
            meaning="Keeps the last model step for the final answer and hides research tools on it.",
            field_schema={
                "field_type": "boolean",
                "description": "Reserve the last configured model step for final_answer and hide research tools on that step. Also exposes the execution limits so the model knows about the reservation.",
                "default_value": False,
            },
        ),
        "final_step_user_message": DescriptorDefinition(
            label="Final step user message",
            group="Knob",
            is_core=False,
            value_type="string",
            meaning="User message sent on the reserved last step, which then offers no tools.",
            field_schema={
                "field_type": "textarea",
                "description": "With Reserve Final Answer Step on, send this as a user message on the reserved last turn and offer no tools, so the model answers in plain text and may decline. Execution limits are then exposed only when Expose Execution Limits is on. Empty = force final_answer.",
            },
        ),
        "fail_on_tool_system_error": DescriptorDefinition(
            label="Fail on tool system error",
            group="Knob",
            is_core=False,
            value_type="boolean",
            meaning="Ends the run as an infrastructure error when a tool reports a provider, quota or connection failure.",
            field_schema={
                "field_type": "boolean",
                "description": "End the trajectory as an infrastructure error when an MCP tool reports a provider, quota, or connection failure.",
                "default_value": False,
            },
        ),
        "completion_token_budget": DescriptorDefinition(
            label="Completion token budget",
            group="Knob",
            is_core=False,
            value_type="integer",
            meaning="Caps cumulative completion tokens, including reasoning tokens, across the whole run.",
            field_schema={
                "field_type": "number",
                "description": "Optional cumulative completion-token budget for the full trajectory, including provider-reported reasoning tokens.",
                "min_value": 1.0,
            },
        ),
        "max_context_tokens": DescriptorDefinition(
            label="Max context tokens",
            group="Knob",
            is_core=False,
            value_type="integer",
            meaning="Caps a single call's context below the model's native window; a call over it is handled as a context overflow.",
            field_schema={
                "field_type": "number",
                "description": "Optional context-window cap, for running a model as if its window were smaller. A call whose provider-reported prompt plus completion tokens exceed it is handled as a context overflow.",
                "min_value": 1.0,
            },
        ),
        "expose_run_context_tools": DescriptorDefinition(
            label="Run context tools",
            group="Knob",
            is_core=False,
            value_type="boolean",
            meaning="Adds tools that report the task's pinned location and the current time.",
            field_schema={
                "field_type": "boolean",
                "description": 'Add get_current_location and get_current_time, so a question can say "near me" or "tonight" instead of naming the place and date. Both are appended after Allowed/Excluded Tools filtering, so those fields cannot hide them. Off by default.',
                "default_value": False,
            },
        ),
        "enable_resum": DescriptorDefinition(
            label="ReSum summarization",
            group="Knob",
            is_core=False,
            value_type="boolean",
            meaning="Summarizes older messages once context use crosses the ReSum trigger fraction.",
            field_schema={
                "field_type": "boolean",
                "description": "Summarize old messages once context crosses the threshold",
                "default_value": False,
            },
        ),
        "resum_trigger_fraction": DescriptorDefinition(
            label="ReSum trigger fraction",
            group="Knob",
            is_core=False,
            value_type="number",
            meaning="Fraction of the model context window that triggers ReSum summarization.",
            field_schema={
                "field_type": "number",
                "description": "Fraction of the model context window that triggers ReSum",
                "default_value": 0.7,
                "min_value": 0.1,
                "max_value": 0.95,
            },
        ),
        "resum_keep_recent_messages": DescriptorDefinition(
            label="ReSum recent messages",
            group="Knob",
            is_core=False,
            value_type="integer",
            meaning="Number of newest non-system messages ReSum keeps verbatim.",
            field_schema={
                "field_type": "number",
                "description": "Number of newest non-system messages kept verbatim",
                "default_value": 10,
                "min_value": 1.0,
                "max_value": 100.0,
            },
        ),
        "permission_mode": DescriptorDefinition(
            label="Native permission policy",
            group="Knob",
            is_core=False,
            value_type="string",
            meaning="Selects the native tool-approval or sandbox-access posture for vendor-run tools.",
            field_schema={
                "field_type": "text",
                "description": "Claude Code permission mode: one of 'default', 'acceptEdits', 'plan', 'bypassPermissions'. Use 'bypassPermissions' for headless eval so MCP gateway tools aren't blocked on approval.",
                "default_value": "bypassPermissions",
            },
        ),
        "workspace_roots": DescriptorDefinition(
            label="Permitted workspace roots",
            group="Knob",
            is_core=False,
            value_type="json",
            meaning="Specifies the directories the agent's native file tools may read and write; unset keeps the sandbox cwd.",
            field_schema={
                "field_type": "json",
                "description": 'Absolute directories the SDK\'s file tools (view_file / edit_file / create_file) may touch, as a JSON list. Leave unset and the SDK scopes them to the MCP container\'s cwd, which in an eval sandbox is also where the graded answer lives. Point this at the work tree instead, e.g. ["/filesystem/environment"] or ["/workspace"]. Roots named solution or tests are dropped automatically; [] means unrestricted.',
            },
        ),
        "use_toolbelt": DescriptorDefinition(
            label="Use toolbelt",
            group="Knob",
            is_core=False,
            value_type="boolean",
            meaning="Starts the model with toolbelt meta-tools that add MCP tools on demand.",
            field_schema={
                "field_type": "boolean",
                "description": "On (default): the model starts with toolbelt meta-tools and adds MCP tools as needed. Off: every MCP tool is offered from the first step, and todo_write is the only meta-tool kept.",
                "default_value": True,
            },
        ),
        "use_mcp_cli": DescriptorDefinition(
            label="Use MCP command-line transport",
            group="Knob",
            is_core=False,
            value_type="boolean",
            meaning=(
                "Routes gateway tool access through an MCP CLI subprocess instead of native tool "
                "declarations."
            ),
            field_schema={
                "field_type": "boolean",
                "description": "When on, native MCP servers are disabled for the run and the model reaches every gateway tool through the `mcp_cli` subprocess (list/schema/execute) via the bash tool. Keeps the run at zero native function declarations — required for Vertex models on tool-heavy platforms (Vertex rejects >512 declarations) and keeps tool schemas out of context. Default off = native MCP tools (current behavior). Only takes effect on platforms whose opencode app supports use_mcp_cli.",
                "default_value": False,
            },
        ),
        "use_rest_cli": DescriptorDefinition(
            label="Use REST command-line transport",
            group="Knob",
            is_core=False,
            value_type="boolean",
            meaning="Routes REST services through lazy CLI schema discovery and execution.",
            field_schema={
                "field_type": "boolean",
                "description": "Requires Use mcp_cli mode. When on, REST API services are navigated via the `rest_cli` subprocess (search/schema/curl) instead of seeding each full OpenAPI spec into the workspace — the spec is fetched lazily from the gateway and never enters the model's context. Default off = specs are dumped inline (current behavior). Opt-in per world for staged rollout; only takes effect on platforms whose antigravity app supports rest_cli.",
                "default_value": False,
            },
        ),
        "record_tool_declarations": DescriptorDefinition(
            label="Record offered tool definitions",
            group="Knob",
            is_core=False,
            value_type="boolean",
            meaning="Includes the exact offered tool schemas in trajectory output.",
            field_schema={
                "field_type": "boolean",
                "description": "Write the exact tool definitions offered to the model (name, description and parameter schema, after Disabled Tools and the text-only image filter) into the trajectory output as output['tool_declarations'], so an export can ship them with the transcript. Off by default: the list can run to tens of KB and every consumer that renders the whole trajectory output (AutoQC, judges) would carry it.",
                "default_value": False,
            },
        ),
        "llm_api_protocol": DescriptorDefinition(
            label="Anthropic passthrough route",
            group="Knob",
            is_core=False,
            value_type="boolean",
            meaning="Selects the LLM wire protocol the harness speaks through the gateway.",
            field_schema={
                "field_type": "boolean",
                "description": "Route Claude Code through the LiteLLM proxy's Anthropic passthrough (/anthropic) and enable tool search, so large MCP tool catalogs are deferred instead of inlined into the prompt (avoids 'prompt is too long'). Only for worlds whose model is a real Anthropic id; passthrough bypasses LiteLLM alias resolution and spend logging. Default off.",
                "default_value": False,
            },
        ),
        "user_sim_model": DescriptorDefinition(
            label="Simulated-user model",
            group="Knob",
            is_core=False,
            value_type="string",
            meaning="Selects the model that generates simulated-user replies.",
            field_schema={
                "field_type": "text",
                "description": "Model to use for generating simulated user responses",
                "default_value": "openai/gpt-5",
            },
        ),
        "termination_evaluator_model": DescriptorDefinition(
            label="Termination-evaluator model",
            group="Knob",
            is_core=False,
            value_type="string",
            meaning="Selects the model that judges whether the conversation should stop.",
            field_schema={
                "field_type": "text",
                "description": "Model for the stop/continue termination judge",
                "default_value": "openai/gpt-5",
            },
        ),
        "max_user_turns": DescriptorDefinition(
            label="Simulated-user turn limit",
            group="Knob",
            is_core=False,
            value_type="number",
            meaning="Caps user-side turns in a simulated conversation; distinct from max_steps.",
            field_schema={
                "field_type": "number",
                "description": "Hard cap on user simulation turns (namespaced to avoid collision with inner agent's max_turns)",
                "default_value": 10,
                "min_value": 1.0,
                "max_value": 100.0,
            },
        ),
        "user_sim_prompt": DescriptorDefinition(
            label="Simulated-user persona prompt",
            group="Knob",
            is_core=False,
            value_type="string",
            meaning="Defines the persona/instructions used to generate user replies.",
            field_schema={
                "field_type": "textarea",
                "required": True,
                "description": "Persona system prompt for the simulated user",
            },
        ),
        "user_sim_system_prompt": DescriptorDefinition(
            label="Simulated-user constraints",
            group="Knob",
            is_core=False,
            value_type="string",
            meaning="Adds behavioral constraints to the simulated-user persona prompt.",
            field_schema={
                "field_type": "textarea",
                "description": "Constraints appended to the user sim persona prompt. Override to customize behavior for different persona types.",
                "default_value": DEFAULT_USER_SIM_SYSTEM_PROMPT,
            },
        ),
        "termination_evaluator_prompt": DescriptorDefinition(
            label="Termination-evaluator instructions",
            group="Knob",
            is_core=False,
            value_type="string",
            meaning="Defines the stop/continue evaluation instructions.",
            field_schema={
                "field_type": "textarea",
                "description": "System prompt for the termination evaluator",
                "default_value": DEFAULT_TERMINATION_EVALUATOR_PROMPT,
            },
        ),
        "done_marker": DescriptorDefinition(
            label="Simulated-user stop marker",
            group="Knob",
            is_core=False,
            value_type="string",
            meaning="Sets the response marker that signals simulated-user completion.",
            field_schema={
                "field_type": "text",
                "description": "Stop signal string in user sim response",
                "default_value": "[DONE]",
            },
        ),
        "require_full_history": DescriptorDefinition(
            label="Require complete conversation history",
            group="Knob",
            is_core=False,
            value_type="boolean",
            meaning="Fails when the prompt does not fit instead of trimming old conversation history.",
            field_schema={"field_type": "boolean"},
        ),
        "preserve_messages": DescriptorDefinition(
            label="Preserve complete prompt",
            group="Knob",
            is_core=False,
            value_type="boolean",
            meaning="Sends the complete prompt without context trimming.",
            field_schema={
                "field_type": "boolean",
                "description": "Send every input message without context trimming. Oversized inputs fail at the provider.",
                "default_value": False,
            },
        ),
        "recorded_tool_history_field": DescriptorDefinition(
            label="Recorded tool history field",
            group="Knob",
            is_core=False,
            value_type="string",
            meaning="Replays recorded tool definitions and completed assistant/tool messages from a task custom field.",
            field_schema={
                "field_type": "text",
                "description": "Task custom field containing native tools and completed assistant/tool messages to replay before generation.",
                "default_value": "",
                "required": False,
            },
        ),
        "thinking_display": DescriptorDefinition(
            label="Thinking display mode",
            group="Knob",
            is_core=False,
            value_type="string",
            meaning="Selects how the native CLI displays thinking content.",
            field_schema={"field_type": "text"},
        ),
        "exclude_answer_dirs": DescriptorDefinition(
            label="Exclude answer directories",
            group="Knob",
            is_core=False,
            value_type="boolean",
            meaning="Filters protected solution/test roots from the configured workspace allowlist.",
            field_schema={"field_type": "boolean"},
        ),
        "retry_mode": DescriptorDefinition(
            label="Native API retry preset",
            group="Knob",
            is_core=False,
            value_type="string",
            meaning="Selects the native SDK's model-API retry policy preset.",
            field_schema={"field_type": "text"},
        ),
        "harness_version": DescriptorDefinition(
            label="Harness version",
            group="Declaration",
            is_core=True,
            value_type="integer",
            meaning="Version of the harness implementation's contract; bump when its behaviour or declared descriptors change.",
            field_schema={
                "field_type": "number",
                "description": "Positive integer harness contract version.",
                "min_value": 1,
                "step": 1,
            },
        ),
        "usage_shape": DescriptorDefinition(
            label="Usage shape",
            group="Emitter",
            is_core=True,
            value_type="select",
            meaning="Shape in which the harness emits token usage and cost.",
            field_schema={
                "field_type": "select",
                "options": ["usage_tracker", "openai", "anthropic", "vendor"],
            },
        ),
        "requires_sandbox": DescriptorDefinition(
            label="Requires sandbox",
            group="Declaration",
            is_core=True,
            value_type="boolean",
            meaning="Whether the harness needs an environment sandbox to run.",
            field_schema={"field_type": "boolean"},
        ),
        "requires_mcp_gateway": DescriptorDefinition(
            label="Requires MCP gateway",
            group="Declaration",
            is_core=True,
            value_type="boolean",
            meaning="Whether the harness reaches tools through the MCP gateway.",
            field_schema={"field_type": "boolean"},
        ),
        "log_message_types": DescriptorDefinition(
            label="Logged message types",
            group="Emitter",
            is_core=True,
            value_type="json",
            meaning="Message types the harness writes to the trajectory log; always includes final_answer.",
            field_schema={
                "field_type": "json",
                "description": "List of string message types written to the trajectory log; always includes final_answer.",
            },
        ),
        "required_credentials": DescriptorDefinition(
            label="Required credentials",
            group="Declaration",
            is_core=True,
            value_type="json",
            meaning="Credentials the harness needs at run time.",
            field_schema={
                "field_type": "json",
                "description": "List of credential names required at run time.",
                "default_value": [],
            },
        ),
    }
)

HARNESS_PARAMETERS: dict[str, dict[str, ParameterOverride]] = {
    "loop_agent": {
        "wall_clock_timeout_sec": ParameterOverride(
            runtime_default=10800, on_missing="default"
        ),
        "max_steps": ParameterOverride(runtime_default=100, on_missing="default"),
        "tool_call_timeout_sec": ParameterOverride(
            runtime_default=60, on_missing="default"
        ),
        "llm_call_timeout_sec": ParameterOverride(
            runtime_default=600, on_missing="default"
        ),
        "llm_streaming": ParameterOverride(),
        "attachments_staged_to_sandbox": ParameterOverride(),
        "budget_exhaustion_finalizes": ParameterOverride(),
        "skip_tools_on_final_step": ParameterOverride(),
        "disable_nudges": ParameterOverride(),
        "llm_idle_credit_sec": ParameterOverride(),
        "accounting_mode": ParameterOverride(),
        "token_budget": ParameterOverride(),
        "budget_warnings_to_model": ParameterOverride(),
        "input_cost_per_token": ParameterOverride(),
        "output_cost_per_token": ParameterOverride(),
        "cached_input_cost_per_token": ParameterOverride(),
        "cache_creation_cost_per_token": ParameterOverride(),
        "cost_budget_usd": ParameterOverride(),
    },
    "react_toolbelt_agent": {
        "wall_clock_timeout_sec": ParameterOverride(
            runtime_default=10800, on_missing="default"
        ),
        "max_steps": ParameterOverride(
            field_schema={
                "description": "Maximum number of LLM calls before stopping",
                "default_value": 250,
                "min_value": 1.0,
            },
            runtime_default=250,
            on_missing="default",
        ),
        "tool_call_timeout_sec": ParameterOverride(
            runtime_default=180, on_missing="default"
        ),
        "llm_call_timeout_sec": ParameterOverride(
            field_schema={
                "description": "Timeout for a single LLM API call",
                "max_value": 10800.0,
            },
            runtime_default=600,
            on_missing="default",
        ),
        "attachments_staged_to_sandbox": ParameterOverride(),
        "use_toolbelt": ParameterOverride(runtime_default=True, on_missing="default"),
        "max_consecutive_truncations": ParameterOverride(
            runtime_default=0, on_missing="default"
        ),
        "context_window_tokens": ParameterOverride(),
        "context_compaction_threshold": ParameterOverride(value_unit="fraction"),
        "context_compaction_call_timeout_sec": ParameterOverride(),
    },
    "stirrup_agent": {
        "wall_clock_timeout_sec": ParameterOverride(
            field_schema={
                "description": "Maximum time for agent execution. Sized for a full 250-turn GDPval-AA "
                "v2 run; a 100-turn run simply finishes early.",
                "default_value": 27000,
            },
            runtime_default=27000,
            on_missing="default",
        ),
        "max_steps": ParameterOverride(
            field_schema={
                "description": "Maximum number of agent turns. GDPval-AA v2 uses 250; the default "
                "stays 100 so existing worlds are unchanged. 0 = no turn cap; the "
                "wall-clock Timeout is then the only harness-imposed stop (requires "
                "Complete On Budget Exhausted).",
                "max_value": 250.0,
            },
            runtime_default=100,
            on_missing="default",
        ),
        "budget_warning_threshold_steps": ParameterOverride(
            runtime_default=80, on_missing="default"
        ),
        "tool_call_timeout_sec": ParameterOverride(
            field_schema={"default_value": 300, "min_value": 60.0},
            runtime_default=300,
            on_missing="default",
        ),
        "llm_call_timeout_sec": ParameterOverride(
            field_schema={
                "description": "Timeout for LLM API calls. With streaming on this is a per-chunk idle "
                "timeout, not a total-generation cap.",
                "max_value": 1800.0,
            },
            runtime_default=600,
            on_missing="default",
        ),
        "llm_streaming": ParameterOverride(
            field_schema={
                "description": "Stream model responses so the HTTP connection stays active. Required "
                "for long generations: a non-streaming call holds an idle connection "
                "that the load balancer drops at its 30-minute idle timeout, causing "
                "502/timeout errors and 3-hour run failures. Leave on unless a "
                "provider misbehaves on streaming.",
                "default_value": True,
            },
            runtime_default=True,
            on_missing="default",
        ),
        "budget_exhaustion_finalizes": ParameterOverride(
            field_schema={
                "description": "When on, a harness-imposed stop — the wall-clock Timeout, or the turn "
                "cap when Max Turns > 0 — ends the trajectory COMPLETED (so the "
                "deliverable on disk is graded) instead of the default FAILED (turn "
                "cap) / ERROR (wall clock). The output carries budget_limited_attempt "
                "and attempt_outcome so a budget-limited grade is distinguishable from "
                "a clean finish. Required when Max Turns = 0. Token/cost accounting "
                "exhaustion is unaffected (stays FAILED). Off by default."
            }
        ),
        "llm_idle_credit_sec": ParameterOverride(
            field_schema={
                "description": "Cumulative cap on provider time-to-first-token idle (queue + prefill) "
                "credited back against the wall-clock Timeout, so Studio-side provider "
                "queueing does not eat a run's budget. A credit, not an exemption: "
                "measured per call as the wait before the first streamed output token "
                "and capped in total at this value. Includes reasoning for providers "
                "that stream it; generation-bound models gain nothing. 0 = off. "
                "Requires Stream on and Complete On Budget Exhausted on."
            }
        ),
        "context_window_tokens": ParameterOverride(),
        "context_compaction_threshold": ParameterOverride(value_unit="fraction"),
        "custom_system_prompt": ParameterOverride(),
        "base_system_prompt": ParameterOverride(),
        "harness_prompt_additions": ParameterOverride(
            runtime_default=True, on_missing="default"
        ),
        "task_prompt_template": ParameterOverride(),
        "harness_wording": ParameterOverride(
            runtime_default="mercor", on_missing="default"
        ),
        "validate_finish_paths": ParameterOverride(
            runtime_default=False, on_missing="default"
        ),
        "context_compaction_strategy": ParameterOverride(
            runtime_default="flattened", on_missing="default"
        ),
        "enable_abandon_task": ParameterOverride(),
        "record_tool_declarations": ParameterOverride(),
        "disabled_tools": ParameterOverride(),
        "accounting_mode": ParameterOverride(
            field_schema={
                "description": "Which per-turn $/token budget-warning mechanism is active for this "
                "run: Default (none), 90/10 Token Accounting (Token Budget below), or "
                "Full Cost Accounting (real $ cost tracking further below). Mutually "
                "exclusive. Turn Warning Threshold (above) is independent of this "
                "selector and applies in any mode."
            }
        ),
        "token_budget": ParameterOverride(
            field_schema={
                "description": "Total provider-reported prompt+completion tokens the run may spend. "
                "The agent is told the remaining budget each turn and gets one final "
                "turn to call the finish tool once it is exhausted. 0 disables "
                "budgeting. Only used in 90/10 Token Accounting."
            }
        ),
        "input_cost_per_token": ParameterOverride(),
        "output_cost_per_token": ParameterOverride(),
        "cached_input_cost_per_token": ParameterOverride(),
        "cache_creation_cost_per_token": ParameterOverride(),
        "cost_budget_usd": ParameterOverride(
            field_schema={
                "description": "Total USD the run may spend. The agent is told the remaining budget "
                "each turn and gets one final turn to call the finish tool once it is "
                "exhausted. 0 disables the cap, but cost is still logged. Only used in "
                "Full Cost Accounting."
            }
        ),
        "input_files": ParameterOverride(
            field_schema={
                "field_type": "json",
                "description": "Existing file paths to include in the prompt context, as a JSON list.",
                "default_value": None,
            },
            runtime_default=[],
            on_missing="default",
        ),
    },
    "singleshot_agent": {
        "llm_call_timeout_sec": ParameterOverride(
            field_schema={"max_value": 10800.0},
            runtime_default=600,
            on_missing="default",
        ),
        "preserve_messages": ParameterOverride(
            runtime_default=False, on_missing="default"
        ),
        "recorded_tool_history_field": ParameterOverride(
            runtime_default="", on_missing="default"
        ),
        "require_full_history": ParameterOverride(
            runtime_default=False, on_missing="default"
        ),
    },
    "responses_agent": {
        "llm_call_timeout_sec": ParameterOverride(
            field_schema={
                "description": "Timeout for LLM API calls. With streaming on this is a per-chunk idle "
                "timeout, not a total-generation cap.",
                "max_value": 1800.0,
            },
            runtime_default=600,
            on_missing="default",
        ),
        "llm_streaming": ParameterOverride(
            field_schema={
                "description": "Stream model responses so the HTTP connection stays active. Required "
                "for long generations: a non-streaming call holds an idle connection "
                "that the load balancer drops at its 30-minute idle timeout, causing "
                "502/timeout errors and 3-hour run failures. Leave on unless a "
                "provider misbehaves on streaming.",
                "default_value": True,
            },
            runtime_default=True,
            on_missing="default",
        ),
    },
    "codex_agent": {
        "model": ParameterOverride(runtime_default="gpt-5-codex", on_missing="default"),
        "permission_mode": ParameterOverride(
            field_schema={
                "description": "Codex sandbox: 'read-only', 'workspace-write', or 'danger-full-access'. Maps to --sandbox.",
                "default_value": "workspace-write",
            }
        ),
        "reasoning_effort": ParameterOverride(),
        "wall_clock_timeout_sec": ParameterOverride(
            field_schema={
                "description": "Wallclock cap for a single Codex run, enforced by the MCP server.",
                "default_value": 3600,
            }
        ),
        "system_instructions_mode": ParameterOverride(
            runtime_default="prepend", on_missing="default"
        ),
    },
    "claude_code_agent": {
        "allowed_tools": ParameterOverride(),
        "disabled_tools": ParameterOverride(
            field_schema={
                "description": "Comma-separated Claude Code tool names to REMOVE from the agent's "
                "tool surface (e.g. WebSearch,WebFetch). Unlike Allowed Tools this is "
                "a real gate: a denied tool is dropped from the request before the "
                "permission mode is consulted, so bypassPermissions cannot bring it "
                "back. A world that means to measure recall rather than research "
                "denies WebSearch and WebFetch here — and both arms of an A/B must use "
                "the same list, or the comparison measures tool access, not the skill. "
                "Built-in names are not the whole tool surface: a platform's MCP tools "
                "arrive as mcp__gateway__<gateway tool name> and the harness default "
                "preapproves mcp__gateway__*, so a platform carrying the web-search "
                "app needs those denied too. Prefer the glob mcp__gateway__* (or "
                "mcp__* for every MCP server) over hand-listing tools: deny rules "
                "support tool-name globs, and a hand-listed name silently stops "
                "matching when the gateway re-prefixes per service — web_search's "
                "find_in_page is mcp__gateway__find_in_page on a single-server "
                "platform and mcp__gateway__web_search_find_in_page once another MCP "
                "server joins it, so adding an unrelated app to a platform can quietly "
                "reopen a gate you thought was shut. A scoped entry like Bash(rm *) "
                "filters matching calls instead of removing the tool. Leave empty to "
                "deny nothing."
            }
        ),
        "attachments_staged_to_sandbox": ParameterOverride(
            field_schema={
                "description": "Forward non-image attachments on the task prompt (PDF, .docx, .xlsx — "
                "Studio's 'Upload to context') to the agent. The app writes each to "
                "the run workspace and names its absolute path and MIME type in the "
                "prompt. Images are unaffected: they always travel, on their own "
                "channel. Off by default because turning it on changes what an "
                "existing world's trajectories measure — the agent starts seeing a "
                "file the task always carried. Requires a platform whose claude-agent "
                "app declares the `files` field: on an older pinned build a task "
                "carrying an attachment FAILS, naming the stale service commit, rather "
                "than completing without the file and grading the model on a task it "
                "never saw."
            }
        ),
        "permission_mode": ParameterOverride(),
        "max_steps": ParameterOverride(
            field_schema={
                "description": "Maximum agent loop iterations inside Claude Code.",
                "min_value": 1.0,
            }
        ),
        "wall_clock_timeout_sec": ParameterOverride(
            field_schema={
                "description": "Maximum time for a single Claude Code run.",
                "default_value": 3600,
            }
        ),
        "max_output_tokens": ParameterOverride(),
        "system_instructions_mode": ParameterOverride(
            field_schema={
                "description": "How the world's system prompt reaches Claude Code. 'append' sends it "
                "as --append-system-prompt, keeping CC's own built-in prompt and "
                "adding the world's on top. 'replace' (default) sends --system-prompt, "
                "which overrides CC's built-in prompt entirely — a materially "
                "different agent. Default is 'replace' because changing it would "
                "change what every existing world's trajectories measure.",
                "default_value": "replace",
                "options": ["replace", "append"],
            }
        ),
        "llm_api_protocol": ParameterOverride(),
        "user_persona_file": ParameterOverride(),
        "llm_call_timeout_sec": ParameterOverride(
            field_schema={
                "default_value": None,
                "min_value": None,
                "max_value": None,
                "description": "Model API request timeout in seconds. Missing or zero falls back to the orchestrator's api_timeout_ms.",
            }
        ),
        "thinking_display": ParameterOverride(),
    },
    "opencode_agent": {
        "prompt_attachment_kinds": ParameterOverride(),
        "llm_stream_idle_timeout_sec": ParameterOverride(),
        "model": ParameterOverride(
            field_schema={
                "description": "LiteLLM-style model id (e.g. anthropic/claude-sonnet-4-5, "
                "fireworks_ai/accounts/fireworks/models/qwen3-coder-480b-a35b-instruct). "
                "The OpenCode sandbox routes all calls through Studio's LiteLLM proxy, "
                "so any model the proxy serves is valid.",
                "default_value": "anthropic/claude-sonnet-4-5",
            },
            runtime_default="auto",
            on_missing="default",
        ),
        "use_mcp_cli": ParameterOverride(),
        "wall_clock_timeout_sec": ParameterOverride(
            field_schema={
                "description": "Maximum wall-clock time for a single OpenCode run, forwarded to the "
                "opencode app as a per-run budget. Unset (default) defers to the "
                "platform's OPENCODE_DEFAULT_TIMEOUT. Opt-in per world for staged "
                "rollout; only takes effect on platforms whose opencode app accepts "
                "the timeout field.",
                "default_value": None,
            }
        ),
        "context_compaction_threshold": ParameterOverride(
            field_schema={
                "description": "Compaction trigger in tokens.",
                "default_value": None,
                "min_value": 1,
                "max_value": None,
            },
            value_unit="tokens",
        ),
        "reasoning_effort": ParameterOverride(
            field_schema={
                "field_type": "select",
                "description": "Pin the reasoning effort for this run, forwarded through the LiteLLM "
                "proxy to whichever upstream model serves the request. Unset (default) "
                "omits the setting, so the model's backend default governs (current "
                "behavior). Opt-in per world for staged rollout; only takes effect on "
                "platforms whose opencode app supports reasoning_effort.",
                "default_value": None,
                "options": ["minimal", "low", "medium", "high"],
            }
        ),
        "user_persona_file": ParameterOverride(
            field_schema={
                "description": "When set, extracts task custom fields into a markdown file written to "
                'the workspace root before the run. Shape: {"filename": '
                '"USER_PERSONA.md", "fields": [{"header": "User Name", "field_id": '
                '"field_..."}, ...]}. Each populated field becomes a `## header` '
                "section; the file is omitted entirely when none of the listed fields "
                "has a value. Same shape and same rendering as the Claude Code and "
                "Antigravity agents. Nothing tells the agent the file exists, so pair "
                "it with a system prompt that names the file (and, on this agent, with "
                "System prompt mode = append). Only takes effect on platforms whose "
                "opencode app accepts the seed_files field."
            }
        ),
        "system_instructions_mode": ParameterOverride(
            field_schema={
                "description": "How the world's system prompt reaches OpenCode, whose CLI has no "
                "system-prompt flag. 'append' sends it as `.opencoderules` content, "
                "which the opencode app appends to its own default rules. 'off' "
                "(default) forwards no system prompt — the historical behavior of this "
                "agent, kept as the default because turning it on changes what an "
                "existing world's trajectories measure.",
                "default_value": "off",
                "options": ["off", "append"],
            }
        ),
    },
    "antigravity_agent": {
        "permission_mode": ParameterOverride(
            field_schema={
                "description": "Antigravity policy preset: 'bypass' (headless default, maps to the "
                "SDK's policy.allow_all()), 'read_only' (inspection tools only), or "
                "'ask' (interactive — requires an ask_user handler; falls back to "
                "bypass for headless eval). The wrapping MCP server translates these "
                "into concrete deny/allow/ask_user policy lists. Note 'bypass' is "
                "still scoped by workspaces: the SDK auto-prepends workspace_only "
                "file-tool denies, so file ops outside the configured workspaces are "
                "denied even under bypass. Set workspaces=[] for fully unrestricted "
                "access.",
                "default_value": "bypass",
            }
        ),
        "workspace_roots": ParameterOverride(),
        "max_steps": ParameterOverride(
            field_schema={
                "description": "Maximum agent loop iterations. Advisory: the Antigravity SDK does not "
                "expose a hard turn limit; the wrapping MCP server logs this value and "
                "may enforce it externally.",
                "min_value": 1.0,
            }
        ),
        "wall_clock_timeout_sec": ParameterOverride(
            field_schema={
                "description": "Maximum wall-clock time for a single Antigravity run.",
                "default_value": 3600,
            }
        ),
        "context_compaction_threshold": ParameterOverride(
            field_schema={
                "description": "Compaction trigger in tokens.",
                "default_value": None,
                "min_value": 1,
                "max_value": None,
            },
            value_unit="tokens",
        ),
        "use_mcp_cli": ParameterOverride(
            field_schema={
                "description": "Route MCP through the `mcp_cli` subprocess shim on the "
                "antigravity_server side instead of the SDK's native MCP wiring. "
                "Default off = current behavior (LocalAgentConfig.mcp_servers points "
                "at the Studio gateway, model sees N native MCP tools). On = "
                "antigravity_server skips native wiring, sets MCP_CLI_SERVERS, loads "
                "the mcp-cli skill, and the model invokes connectors via `run_command "
                "mcp_cli --connector=gateway --method=...`."
            }
        ),
        "use_rest_cli": ParameterOverride(),
        "reasoning_effort": ParameterOverride(
            field_schema={
                "field_type": "select",
                "description": "Pin the Gemini thinking level for this run. Unset (default) omits the "
                "setting, so the model's backend default governs (current behavior). "
                "Opt-in per world for staged rollout; only takes effect on platforms "
                "whose antigravity app supports thinking_level.",
                "default_value": None,
                "options": ["minimal", "low", "medium", "high", "extra_high"],
            }
        ),
        "user_persona_file": ParameterOverride(),
        "inject_date": ParameterOverride(),
        "exclude_answer_dirs": ParameterOverride(),
        "enabled_tools": ParameterOverride(
            field_schema={
                "field_type": "json",
                "description": "Tool names passed to the Antigravity SDK, as a JSON list. Unset or empty keeps the built-in list.",
            }
        ),
        "retry_mode": ParameterOverride(),
    },
    "cursor_agent": {
        "model": ParameterOverride(
            field_schema={
                "description": "Model identifier. Supports Cursor identifiers (opus-4.6, sonnet-4.6, "
                "gpt-5.2, gemini-3-pro) or standard format (anthropic/claude-sonnet-4, "
                "openai/gpt-5). Use 'auto' for Cursor's default. Will auto-map to "
                "closest Cursor model.",
                "default_value": "auto",
            },
            runtime_default="auto",
            on_missing="default",
        ),
    },
    "gemini_cli_agent": {
        "model": ParameterOverride(
            field_schema={
                "description": "Gemini CLI model name or alias (e.g. 'gemini-2.5-pro', 'pro', "
                "'flash'). Passed directly to the Gemini CLI --model flag.",
                "default_value": "gemini-2.5-pro",
            },
            runtime_default="gemini-2.5-pro",
            on_missing="default",
        ),
    },
    "user_sim_harness_agent": {
        "wall_clock_timeout_sec": ParameterOverride(
            runtime_default=10800, on_missing="default"
        ),
        "max_steps": ParameterOverride(
            field_schema={
                "description": "Maximum number of inner agent LLM calls (shared across turns)",
                "min_value": 1.0,
            },
            runtime_default=100,
            on_missing="default",
        ),
        "tool_call_timeout_sec": ParameterOverride(
            runtime_default=60, on_missing="default"
        ),
        "llm_call_timeout_sec": ParameterOverride(
            runtime_default=600, on_missing="default"
        ),
        "user_sim_model": ParameterOverride(
            runtime_default="openai/gpt-5", on_missing="default"
        ),
        "user_sim_prompt": ParameterOverride(runtime_default="", on_missing="default"),
        "user_sim_system_prompt": ParameterOverride(
            runtime_default=USER_SIM_RUNTIME_SYSTEM_PROMPT,
            on_missing="default",
        ),
        "termination_evaluator_model": ParameterOverride(
            runtime_default="openai/gpt-5", on_missing="default"
        ),
        "termination_evaluator_prompt": ParameterOverride(
            runtime_default=DEFAULT_TERMINATION_EVALUATOR_PROMPT,
            on_missing="default",
        ),
        "done_marker": ParameterOverride(
            runtime_default="[DONE]", on_missing="default"
        ),
        "max_user_turns": ParameterOverride(runtime_default=10, on_missing="default"),
    },
    "web_research_agent": {
        "max_steps": ParameterOverride(
            field_schema={
                "description": "Maximum number of LLM calls (search-read iterations)",
                "default_value": 50,
                "min_value": 1.0,
                "max_value": 200.0,
            }
        ),
        "wall_clock_timeout_sec": ParameterOverride(
            field_schema={
                "description": "Maximum time for the full research session",
                "default_value": 10800,
            }
        ),
        "tool_call_timeout_sec": ParameterOverride(
            field_schema={
                "description": "Timeout per tool call (web_search, fetch_webpage)",
                "default_value": 120,
            }
        ),
        "llm_call_timeout_sec": ParameterOverride(
            field_schema={
                "description": "Timeout for each LLM call (includes thinking time)",
                "default_value": 600,
                "min_value": 60.0,
                "max_value": 1800.0,
            }
        ),
        "base_system_prompt": ParameterOverride(
            field_schema={
                "description": "System prompt for research guidance. Leave empty for the built-in default."
            }
        ),
        "disabled_tools": ParameterOverride(
            field_schema={
                "description": "Comma-separated MCP tool names to hide from the model. Leave empty to expose every platform tool. Applied after Enabled tools, so an exclusion wins over an allow."
            }
        ),
        "enabled_tools": ParameterOverride(
            field_schema={
                "description": "MCP tool names to expose, hiding everything else, as a list or a comma-separated string. Leave empty to keep every platform tool. An unmatched name fails the run rather than being ignored."
            }
        ),
        "max_output_lines": ParameterOverride(),
        "max_output_chars": ParameterOverride(),
        "expose_execution_limits": ParameterOverride(),
        "reserve_final_answer_step": ParameterOverride(),
        "final_step_user_message": ParameterOverride(),
        "fail_on_tool_system_error": ParameterOverride(),
        "completion_token_budget": ParameterOverride(),
        "max_context_tokens": ParameterOverride(),
        "expose_run_context_tools": ParameterOverride(),
        "enable_resum": ParameterOverride(),
        "resum_trigger_fraction": ParameterOverride(),
        "resum_keep_recent_messages": ParameterOverride(),
    },
}

PARAMETER_ALIASES: dict[str, dict[str, ParameterAlias]] = {
    "loop_agent": {
        "max_steps": ParameterAlias(descriptor_id="max_steps", conversion="identity"),
    },
    "react_toolbelt_agent": {
        "max_steps": ParameterAlias(descriptor_id="max_steps", conversion="identity"),
        "context_compaction_cutoff": ParameterAlias(
            descriptor_id="context_compaction_threshold", conversion="fraction"
        ),
    },
    "stirrup_agent": {
        "max_turns": ParameterAlias(descriptor_id="max_steps", conversion="identity"),
        "stirrup_wording": ParameterAlias(
            descriptor_id="harness_wording", conversion="identity"
        ),
        "summarization_mode": ParameterAlias(
            descriptor_id="context_compaction_strategy", conversion="identity"
        ),
        "context_compaction_cutoff": ParameterAlias(
            descriptor_id="context_compaction_threshold", conversion="fraction"
        ),
    },
    "singleshot_agent": {},
    "responses_agent": {},
    "web_research_agent": {
        "max_steps": ParameterAlias(descriptor_id="max_steps", conversion="identity"),
        "system_prompt": ParameterAlias(
            descriptor_id="base_system_prompt", conversion="identity"
        ),
        "excluded_tools": ParameterAlias(
            descriptor_id="disabled_tools", conversion="identity"
        ),
        "allowed_tools": ParameterAlias(
            descriptor_id="enabled_tools", conversion="identity"
        ),
        "resum_trigger_fraction": ParameterAlias(
            descriptor_id="resum_trigger_fraction", conversion="identity"
        ),
        "resum_utilization_threshold": ParameterAlias(
            descriptor_id="resum_trigger_fraction", conversion="identity"
        ),
        "resum_keep_recent_messages": ParameterAlias(
            descriptor_id="resum_keep_recent_messages", conversion="identity"
        ),
        "resum_keep_recent": ParameterAlias(
            descriptor_id="resum_keep_recent_messages", conversion="identity"
        ),
    },
    "codex_agent": {
        "sandbox_mode": ParameterAlias(
            descriptor_id="permission_mode", conversion="identity"
        ),
        "prepend_system_prompt": ParameterAlias(
            descriptor_id="system_instructions_mode", conversion="prepend"
        ),
    },
    "claude_code_agent": {
        "max_turns": ParameterAlias(descriptor_id="max_steps", conversion="identity"),
        "attach_prompt_files": ParameterAlias(
            descriptor_id="attachments_staged_to_sandbox", conversion="identity"
        ),
        "disallowed_tools": ParameterAlias(
            descriptor_id="disabled_tools", conversion="identity"
        ),
        "api_timeout_ms": ParameterAlias(
            descriptor_id="llm_call_timeout_sec", conversion="milliseconds"
        ),
    },
    "opencode_agent": {
        "attach_prompt_images": ParameterAlias(
            descriptor_id="prompt_attachment_kinds", conversion="identity"
        ),
        "compaction_threshold": ParameterAlias(
            descriptor_id="context_compaction_threshold", conversion="tokens"
        ),
    },
    "antigravity_agent": {
        "max_turns": ParameterAlias(descriptor_id="max_steps", conversion="identity"),
        "thinking_level": ParameterAlias(
            descriptor_id="reasoning_effort", conversion="identity"
        ),
        "allowed_tools": ParameterAlias(
            descriptor_id="enabled_tools", conversion="identity"
        ),
        "tool_allowlist": ParameterAlias(
            descriptor_id="enabled_tools", conversion="identity"
        ),
        "compaction_threshold": ParameterAlias(
            descriptor_id="context_compaction_threshold", conversion="tokens"
        ),
    },
    "cursor_agent": {},
    "gemini_cli_agent": {},
    "user_sim_harness_agent": {
        "max_steps": ParameterAlias(descriptor_id="max_steps", conversion="identity"),
        "max_turns": ParameterAlias(
            descriptor_id="max_user_turns", conversion="identity"
        ),
    },
}


RENAMED_DESCRIPTOR_IDS: dict[str, DescriptorId] = {
    "anthropic_passthrough": "llm_api_protocol",
    "complete_on_budget_exhausted": "budget_exhaustion_finalizes",
    "context_compaction_mode": "context_compaction_strategy",
    "context_compaction_timeout": "context_compaction_call_timeout_sec",
    "llm_response_timeout": "llm_call_timeout_sec",
    "model_steps": "max_steps",
    "stage_files_to_env": "attachments_staged_to_sandbox",
    "stall_timeout": "llm_stream_idle_timeout_sec",
    "stream": "llm_streaming",
    "system_prompt_mode": "system_instructions_mode",
    "timeout": "wall_clock_timeout_sec",
    "tool_call_timeout": "tool_call_timeout_sec",
    "turn_warnings_enabled": "budget_warnings_to_model",
    "turns_remaining_warning_threshold": "budget_warning_threshold_steps",
    "user_sim_max_turns": "max_user_turns",
    "workspaces": "workspace_roots",
}

HARNESS_PARENT_IDS = {
    "stirrup_agent_jev": "stirrup_agent",
    "indago_stirrup_agent": "stirrup_agent",
}


def _parameters_for_harness(harness_id: str) -> dict[str, ParameterOverride]:
    overrides = HARNESS_PARAMETERS[harness_id]
    aliases = PARAMETER_ALIASES[harness_id]
    parameters: dict[str, ParameterOverride] = {}
    for key in DESCRIPTOR_CATALOG:
        override = overrides.get(key)
        if override is None:
            continue
        parameters[key] = override.model_copy(
            update={
                "legacy_keys": {
                    **{
                        old: "identity"
                        for old, new in RENAMED_DESCRIPTOR_IDS.items()
                        if new == key
                    },
                    **{
                        source: alias.conversion
                        for source, alias in aliases.items()
                        if alias.descriptor_id == key
                    },
                }
            },
            deep=True,
        )
    return parameters


_HARNESS_DESCRIPTORS = {
    harness: HarnessDescriptors(parameters=_parameters_for_harness(harness))
    for harness in HARNESS_PARAMETERS
}
for _derived, _parent in HARNESS_PARENT_IDS.items():
    _HARNESS_DESCRIPTORS[_derived] = HarnessDescriptors(
        parameters={
            key: override.model_copy(deep=True)
            for key, override in _HARNESS_DESCRIPTORS[_parent].parameters.items()
        }
    )

CATALOG = DescriptorCatalog(
    descriptors=DESCRIPTOR_CATALOG,
    harnesses=_HARNESS_DESCRIPTORS,
)


def project_descriptor_values(
    harness_id: str,
    config_values: dict[str, JsonValue],
    preferred_keys: Collection[str] = (),
) -> HarnessDescriptorValues:
    projection = HarnessDescriptorValues(harness_id=harness_id)
    consumed: set[str] = set()
    harness_parameters = CATALOG.harnesses.get(harness_id)
    for key in CATALOG.descriptors:
        mapping = harness_parameters.parameters.get(key) if harness_parameters else None
        if mapping is None:
            continue
        sources = [
            source
            for source in mapping.legacy_keys
            if source != key and source in config_values
        ]
        if key in config_values:
            sources.append(key)
        if not sources:
            continue
        consumed.update(sources)
        source = next(
            (source for source in sources if source in preferred_keys), sources[0]
        )
        conversion = mapping.legacy_keys.get(source, "identity")
        value, issue = _convert_descriptor_value(
            source, config_values[source], conversion
        )
        if issue is not None:
            projection.issues[key] = issue
            continue
        for other_source in sources:
            if other_source == source:
                continue
            other_value, other_issue = _convert_descriptor_value(
                other_source,
                config_values[other_source],
                mapping.legacy_keys.get(other_source, "identity"),
            )
            if other_issue is not None:
                projection.issues.setdefault(key, other_issue)
                break
            if other_value != value or isinstance(other_value, bool) != isinstance(
                value, bool
            ):
                projection.issues.setdefault(
                    key, f"Multiple saved keys; using {source}"
                )
                break
        projection.values[key] = DescriptorValue(
            value=value, source_key=source, value_unit=mapping.value_unit
        )
    projection.unmapped_keys = sorted(config_values.keys() - consumed)
    return projection


def _convert_descriptor_value(
    source: str, value: JsonValue, conversion: str
) -> tuple[JsonValue, str | None]:
    value = deepcopy(value)
    if conversion == "milliseconds" and value is not None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return value, f"{source} must be numeric to convert milliseconds"
        if not isfinite(value):
            return value, f"{source} must be finite"
        value /= 1000
    elif conversion == "prepend" and value is not None:
        if not isinstance(value, bool):
            return value, f"{source} must be a boolean to project prompt mode"
        value = "prepend" if value else "off"
    return value, None


def add_canonical_keys(
    harness_id: str,
    config_values: dict[str, JsonValue],
    preferred_keys: Collection[str] = (),
) -> dict[str, JsonValue]:
    projection = project_descriptor_values(
        harness_id, config_values, preferred_keys=preferred_keys
    )
    return {
        **config_values,
        **{
            key: value.value
            for key, value in projection.values.items()
            if value.source_key != key
        },
    }


HARNESS_DECLARATIONS: dict[str, HarnessDeclaration] = {
    "ace_grounded_agent": HarnessDeclaration(
        usage_shape="openai",
        requires_sandbox=True,
        requires_mcp_gateway=False,
        system_instructions_placement="append",
        log_message_types=["final_answer", "trajectory_output"],
    ),
    "afm_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=False,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "grounding",
            "reasoning",
            "response",
            "step",
            "thinking",
            "tool_call",
            "tool_result",
            "web_search",
        ],
    ),
    "aide_code_agent": HarnessDeclaration(
        usage_shape="vendor",
        requires_sandbox=False,
        requires_mcp_gateway=False,
        system_instructions_placement="append",
        log_message_types=[
            "aide_bundle",
            "aide_log_tail",
            "aide_modal_call",
            "aide_modal_death",
            "aide_modal_result",
            "aide_result",
            "aide_snapshot",
            "configure",
            "final_answer",
            "response",
        ],
        required_credentials=["MODAL_IDENTITY_TOKEN", "RL_STUDIO_API_KEY"],
    ),
    "antigravity_agent": HarnessDeclaration(
        usage_shape="vendor",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="replace",
        log_message_types=[
            "configure",
            "final_answer",
            "progress",
            "response",
            "result",
            "step",
            "thinking",
            "tool_call",
            "tool_result",
            "trajectory_output",
        ],
    ),
    "arc_agi3_agent": HarnessDeclaration(
        usage_shape="openai",
        requires_sandbox=False,
        requires_mcp_gateway=False,
        system_instructions_placement="not_applicable",
        log_message_types=["final_answer"],
    ),
    "argus_agent_v2": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "main_loop_input",
            "main_loop_step",
            "prefill",
            "reasoning",
            "response",
            "step",
            "thinking",
            "tool_call",
            "tool_result",
        ],
    ),
    "asb_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=False,
        requires_mcp_gateway=False,
        system_instructions_placement="append",
        log_message_types=["configure", "final_answer", "reasoning", "thinking"],
    ),
    "browsecomp_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=False,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "input",
            "model_response",
            "trajectory_output",
        ],
    ),
    "browser_use_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "pruning",
            "reasoning",
            "response",
            "resum",
            "step",
            "structured_context",
            "summarization_input",
            "summarization_output",
            "thinking",
            "todo",
            "tool",
            "tool_call",
            "tool_result",
        ],
    ),
    "claude_code_agent": HarnessDeclaration(
        usage_shape="anthropic",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement=None,
        log_message_types=[
            "configure",
            "final_answer",
            "response",
            "result",
            "step",
            "thinking",
            "tool_call",
            "tool_result",
            "trajectory_output",
        ],
    ),
    "codex_agent": HarnessDeclaration(
        usage_shape="openai",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement=None,
        log_message_types=[
            "configure",
            "final_answer",
            "result",
            "step",
            "thinking",
            "tool_call",
            "tool_result",
            "trajectory_output",
        ],
    ),
    "computer_use_agent": HarnessDeclaration(
        usage_shape="openai",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "pruning",
            "reasoning",
            "response",
            "resum",
            "step",
            "structured_context",
            "summarization_input",
            "summarization_output",
            "thinking",
            "todo",
            "tool",
            "tool_call",
            "tool_result",
        ],
    ),
    "configurable_user_sim_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "ask_user",
            "configure",
            "final_answer",
            "judge_score",
            "reasoning",
            "response",
            "step",
            "thinking",
            "tool_call",
            "tool_result",
            "turn",
            "user_sim_response",
        ],
    ),
    "cursor_agent": HarnessDeclaration(
        usage_shape="vendor",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "error",
            "final_answer",
            "result",
            "step",
            "thinking",
            "tool_call",
            "tool_result",
            "trajectory_output",
        ],
    ),
    "deferred_tools_agent": HarnessDeclaration(
        usage_shape="openai",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "compaction",
            "configure",
            "final_answer",
            "reasoning",
            "response",
            "step",
            "step_cost",
            "thinking",
            "tool_call",
            "tool_result",
            "tool_search",
            "turn_warning",
        ],
    ),
    "gemini_cli_agent": HarnessDeclaration(
        usage_shape="vendor",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "result",
            "step",
            "thinking",
            "tool_call",
            "tool_result",
            "trajectory_output",
        ],
    ),
    "gemini_native_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=False,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "post_loop_execution",
            "reasoning",
            "response",
            "step",
            "thinking",
            "tool_call",
            "tool_result",
        ],
    ),
    "golden_trim_agent": HarnessDeclaration(
        usage_shape="openai",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "reasoning",
            "resum",
            "step",
            "tool_call",
            "tool_result",
        ],
    ),
    "harbor_apex_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "reasoning",
            "response",
            "resum",
            "resum_fallback",
            "step",
            "text_tool_calls",
            "thinking",
            "tool_alias",
            "tool_arguments_alias",
            "tool_call",
            "tool_result",
            "turn_warning",
        ],
    ),
    "harbor_native_agent": HarnessDeclaration(
        usage_shape="vendor",
        requires_sandbox=True,
        requires_mcp_gateway=False,
        system_instructions_placement="prepend_to_user",
        log_message_types=[
            "final_answer",
            "response",
            "step",
            "tool_call",
            "tool_result",
        ],
    ),
    "harmbench_search_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=False,
        requires_mcp_gateway=False,
        system_instructions_placement="replace",
        log_message_types=["final_answer", "pair_step", "tap_depth"],
    ),
    "harvey_lab_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="replace",
        log_message_types=[
            "configure",
            "final_answer",
            "reasoning",
            "response",
            "step",
            "tool_call",
            "tool_result",
        ],
    ),
    "indago_stirrup_agent": HarnessDeclaration(
        usage_shape="openai",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "context",
            "final_answer",
            "finish",
            "reasoning",
            "response",
            "step",
            "step_cost",
            "thinking",
            "tool_call",
            "tool_result",
            "turn_warning",
        ],
    ),
    "kidbench_child_actor_agent": HarnessDeclaration(
        usage_shape="openai",
        requires_sandbox=False,
        requires_mcp_gateway=False,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "reasoning",
            "response",
            "step",
            "thinking",
            "tool_call",
            "tool_result",
            "turn",
            "user_sim_response",
        ],
    ),
    "language_guard_harness_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "language_guard",
            "reasoning",
            "response",
            "step",
            "step_cost",
            "thinking",
            "tool_call",
            "tool_result",
            "turn_warning",
        ],
    ),
    "legal_research_agent": HarnessDeclaration(
        usage_shape="openai",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "compaction",
            "configure",
            "final_answer",
            "reasoning",
            "response",
            "step",
            "thinking",
            "tool_call",
            "tool_result",
        ],
    ),
    "lighthouse_code_agent": HarnessDeclaration(
        usage_shape="vendor",
        requires_sandbox=False,
        requires_mcp_gateway=False,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "input",
            "lighthouse_bundle",
            "lighthouse_content_filter",
            "lighthouse_heartbeat",
            "lighthouse_log",
            "lighthouse_modal_call",
            "lighthouse_modal_result",
            "lighthouse_patch_snapshot",
            "lighthouse_result",
            "lighthouse_worker_started",
            "reasoning",
            "response",
            "tool_call",
            "tool_result",
            "turn",
        ],
        required_credentials=["MODAL_IDENTITY_TOKEN", "RL_STUDIO_API_KEY"],
    ),
    "long_context_agent": HarnessDeclaration(
        usage_shape="openai",
        requires_sandbox=True,
        requires_mcp_gateway=False,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "input",
            "reasoning",
            "response_json",
            "thinking",
            "tool_artifacts",
            "trajectory_output",
        ],
    ),
    "loop_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "reasoning",
            "response",
            "step",
            "step_cost",
            "thinking",
            "tool_call",
            "tool_result",
            "turn_warning",
        ],
    ),
    "loop_anthropic_workaround": HarnessDeclaration(
        usage_shape="openai",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "reasoning",
            "response",
            "step",
            "step_cost",
            "thinking",
            "tool_call",
            "tool_result",
            "turn_warning",
        ],
    ),
    "loop_final_answer_agent": HarnessDeclaration(
        usage_shape="openai",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "reasoning",
            "response",
            "step",
            "step_cost",
            "thinking",
            "tool_call",
            "tool_result",
            "turn_warning",
        ],
    ),
    "loop_truncated_tools_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "compaction",
            "configure",
            "final_answer",
            "reasoning",
            "response",
            "step",
            "step_cost",
            "thinking",
            "tool_call",
            "tool_result",
            "turn_warning",
        ],
    ),
    "monitorbench_retry_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=False,
        requires_mcp_gateway=False,
        system_instructions_placement="append",
        log_message_types=["final_answer"],
    ),
    "monitorbench_shade_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=False,
        requires_mcp_gateway=False,
        system_instructions_placement="append",
        log_message_types=["final_answer"],
    ),
    "multiturn_multimodel_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=False,
        system_instructions_placement="append",
        log_message_types=["configure", "final_answer", "reasoning", "thinking"],
    ),
    "multiturn_multimodel_multimodal_agent": HarnessDeclaration(
        usage_shape="openai",
        requires_sandbox=True,
        requires_mcp_gateway=False,
        system_instructions_placement="append",
        log_message_types=["configure", "final_answer", "reasoning", "thinking"],
    ),
    "neon_synthetic_seed_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=False,
        system_instructions_placement="prepend_to_user",
        log_message_types=[
            "final_answer",
            "stop_reason",
            "system_prompt_rewrite",
            "token_usage",
            "warn_system_prompt_null_from_planner",
            "warn_system_prompt_rewrite_failed",
            "warn_system_prompt_rewrite_incomplete",
            "warn_unexpected_system_prompt",
        ],
    ),
    "opencode_agent": HarnessDeclaration(
        usage_shape="vendor",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement=None,
        log_message_types=[
            "assistant",
            "configure",
            "error",
            "final_answer",
            "opencode_stream",
            "progress",
            "reasoning",
            "step",
            "step_cost",
            "tool_call",
            "tool_result",
            "trajectory_output",
        ],
    ),
    "paired_prompt_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=False,
        requires_mcp_gateway=False,
        system_instructions_placement="append",
        log_message_types=["configure", "final_answer", "trajectory_output"],
    ),
    "profound_react_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "budget",
            "configure",
            "final_answer",
            "reasoning",
            "response",
            "resum",
            "step",
            "step_cost",
            "thinking",
            "tool",
            "tool_call",
            "tool_result",
            "turn_warning",
        ],
    ),
    "prometheus_agent": HarnessDeclaration(
        usage_shape="openai",
        requires_sandbox=True,
        requires_mcp_gateway=False,
        system_instructions_placement="replace",
        log_message_types=["configure", "final_answer"],
        required_credentials=[
            "MODAL_IDENTITY_TOKEN",
            "RL_STUDIO_API_KEY",
            "SPARTA_PROXY_TOKEN",
        ],
    ),
    "prometheus_stream_agent": HarnessDeclaration(
        usage_shape="openai",
        requires_sandbox=True,
        requires_mcp_gateway=False,
        system_instructions_placement="not_applicable",
        log_message_types=["final_answer", "grading_transcript_event"],
    ),
    "prompt_chain_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=False,
        requires_mcp_gateway=False,
        system_instructions_placement="append",
        log_message_types=["final_answer"],
    ),
    "ramp_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "reasoning",
            "response",
            "resum",
            "step",
            "subagent_delivery",
            "subagent_result",
            "thinking",
            "tool",
            "tool_call",
            "tool_result",
            "turn_warning",
            "write_notification",
        ],
    ),
    "ramp_responses_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "reasoning",
            "response",
            "resum",
            "step",
            "subagent_delivery",
            "subagent_result",
            "thinking",
            "tool",
            "tool_call",
            "tool_result",
            "turn_warning",
            "write_notification",
        ],
    ),
    "react_toolbelt_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "reasoning",
            "response",
            "resum",
            "step",
            "thinking",
            "tool",
            "tool_call",
            "tool_result",
        ],
    ),
    "react_toolbelt_anthropic_workaround": HarnessDeclaration(
        usage_shape="openai",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "reasoning",
            "response",
            "resum",
            "step",
            "thinking",
            "tool",
            "tool_call",
            "tool_result",
        ],
    ),
    "react_toolbelt_claude_opus_47_workaround": HarnessDeclaration(
        usage_shape="openai",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "reasoning",
            "response",
            "resum",
            "step",
            "thinking",
            "tool",
            "tool_call",
            "tool_result",
        ],
    ),
    "react_toolbelt_claude_queso_eap_workaround": HarnessDeclaration(
        usage_shape="openai",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "reasoning",
            "response",
            "resum",
            "step",
            "thinking",
            "tool",
            "tool_call",
            "tool_result",
        ],
    ),
    "react_toolbelt_jev": HarnessDeclaration(
        usage_shape="openai",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "reasoning",
            "response",
            "resum",
            "step",
            "thinking",
            "tool",
            "tool_call",
            "tool_result",
        ],
    ),
    "react_toolbelt_responses_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "reasoning",
            "response",
            "resum",
            "step",
            "tool",
            "tool_call",
            "tool_result",
        ],
    ),
    "react_toolbelt_simple_user_sim_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "reasoning",
            "response",
            "resum",
            "step",
            "thinking",
            "tool",
            "tool_call",
            "tool_result",
            "turn",
            "user_sim_response",
        ],
    ),
    "react_toolbelt_swt_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "reasoning",
            "response",
            "resum",
            "step",
            "thinking",
            "tool",
            "tool_call",
            "tool_result",
        ],
    ),
    "react_toolbelt_user_sim_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "reasoning",
            "response",
            "resum",
            "step",
            "thinking",
            "tool",
            "tool_call",
            "tool_result",
            "turn",
            "user_sim_response",
        ],
    ),
    "read_last_message_agent": HarnessDeclaration(
        usage_shape="openai",
        requires_sandbox=True,
        requires_mcp_gateway=False,
        system_instructions_placement="not_applicable",
        log_message_types=["configure", "final_answer"],
    ),
    "research_apex_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "reasoning",
            "response",
            "resum",
            "step",
            "text_tool_calls",
            "thinking",
            "tool_alias",
            "tool_arguments_alias",
            "tool_call",
            "tool_result",
            "turn_warning",
        ],
    ),
    "responses_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=False,
        system_instructions_placement="append",
        log_message_types=[
            "annotations",
            "configure",
            "final_answer",
            "reasoning",
            "response_json",
            "tool_call",
            "tool_calls",
            "trajectory_output",
            "web_search",
        ],
    ),
    "responses_agent_v2": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=False,
        system_instructions_placement="append",
        log_message_types=[
            "annotations",
            "configure",
            "final_answer",
            "reasoning",
            "response_json",
            "tool_call",
            "tool_calls",
            "trajectory_output",
            "web_search",
        ],
    ),
    "risk_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=False,
        system_instructions_placement="not_applicable",
        log_message_types=["configure", "final_answer"],
    ),
    "risk_agent_profile": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=False,
        system_instructions_placement="not_applicable",
        log_message_types=["configure", "final_answer"],
    ),
    "risk_agent_v0": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=False,
        system_instructions_placement="not_applicable",
        log_message_types=["configure", "final_answer"],
    ),
    "risk_agent_v1": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=False,
        system_instructions_placement="not_applicable",
        log_message_types=["configure", "final_answer"],
    ),
    "scicode_chain_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=False,
        requires_mcp_gateway=False,
        system_instructions_placement="not_applicable",
        log_message_types=["final_answer"],
    ),
    "scripted_turns_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "reasoning",
            "response",
            "step",
            "thinking",
            "tool_call",
            "tool_result",
            "turn",
            "turn_complete",
            "turn_snapshot",
            "user_sim_response",
        ],
    ),
    "scripted_user_sim_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=False,
        system_instructions_placement="replace",
        log_message_types=[
            "agent_message",
            "configure",
            "final_answer",
            "response",
            "turn",
            "user_sim_response",
        ],
    ),
    "simple_user_sim_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "reasoning",
            "response",
            "step",
            "thinking",
            "tool_call",
            "tool_result",
            "turn",
            "user_sim_response",
        ],
    ),
    "single_shot_multimodal": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=False,
        system_instructions_placement="append",
        log_message_types=[
            "annotations",
            "configure",
            "final_answer",
            "input",
            "reasoning",
            "response_json",
            "thinking",
            "tool_artifacts",
            "tool_calls",
            "trajectory_output",
            "web_search",
        ],
    ),
    "single_shot_zoom_multimodal": HarnessDeclaration(
        usage_shape="openai",
        requires_sandbox=True,
        requires_mcp_gateway=False,
        system_instructions_placement="append",
        log_message_types=["configure", "final_answer", "reasoning", "thinking"],
    ),
    "singleshot_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=False,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "reasoning",
            "response_json",
            "thinking",
            "tool_artifacts",
            "trajectory_output",
        ],
    ),
    "sourcing_mcp_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "reasoning",
            "response",
            "step",
            "tool_call",
            "tool_result",
        ],
    ),
    "sparta_external_agent": HarnessDeclaration(
        usage_shape="vendor",
        requires_sandbox=True,
        requires_mcp_gateway=False,
        system_instructions_placement="not_applicable",
        log_message_types=[
            "configure",
            "final_answer",
            "grading_transcript_event",
            "response",
            "step",
            "thinking",
            "tool_call",
            "tool_result",
            "transcript_event",
            "user_message",
        ],
        required_credentials=["MODAL_IDENTITY_TOKEN"],
    ),
    "stirrup_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "context",
            "final_answer",
            "finish",
            "reasoning",
            "response",
            "step",
            "step_cost",
            "thinking",
            "tool_call",
            "tool_result",
            "turn_warning",
        ],
    ),
    "stirrup_agent_jev": HarnessDeclaration(
        usage_shape="openai",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "context",
            "final_answer",
            "finish",
            "reasoning",
            "response",
            "step",
            "step_cost",
            "thinking",
            "tool_call",
            "tool_result",
            "turn_warning",
        ],
    ),
    "summarization_harness": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "reasoning",
            "response",
            "resum",
            "step",
            "thinking",
            "tool_call",
            "tool_result",
        ],
    ),
    "tau3_banking_agent": HarnessDeclaration(
        usage_shape="openai",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "reasoning",
            "response",
            "step",
            "tool_call",
            "tool_result",
            "turn",
            "user_sim_response",
        ],
    ),
    "tau_half_duplex_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement=None,
        log_message_types=[
            "configure",
            "error",
            "final_answer",
            "reasoning",
            "response",
            "step",
            "termination",
            "tool_call",
            "tool_result",
            "turn",
            "user_sim_response",
        ],
    ),
    "toolbelt_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "context_management",
            "final_answer",
            "reasoning",
            "response",
            "step",
            "thinking",
            "tool_call",
            "tool_result",
            "toolbelt",
        ],
    ),
    "user_sim_harness_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "configure",
            "final_answer",
            "reasoning",
            "response",
            "step",
            "thinking",
            "tool_call",
            "tool_result",
            "turn",
            "user_sim_response",
        ],
    ),
    "web_research_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=True,
        system_instructions_placement="append",
        log_message_types=[
            "budget_submit_command",
            "budget_warning",
            "configure",
            "final_answer",
            "reasoning",
            "response",
            "resum",
            "step",
            "thinking",
            "tool_call",
            "tool_result",
        ],
    ),
    "web_search_responses_agent": HarnessDeclaration(
        usage_shape="usage_tracker",
        requires_sandbox=True,
        requires_mcp_gateway=False,
        system_instructions_placement="append",
        log_message_types=[
            "final_answer",
            "reasoning",
            "response",
            "resum",
            "step",
            "tool",
            "web_search",
        ],
    ),
}


def declared(harness_id: str) -> HarnessDeclaration:
    """The facts ``harness_id`` declares; raises KeyError for an unregistered harness."""
    return HARNESS_DECLARATIONS[harness_id]

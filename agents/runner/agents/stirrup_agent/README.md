# Stirrup Agent

GDPval-AA compatible agent implementation that replicates the [Stirrup framework](https://github.com/ArtificialAnalysis/Stirrup) behavior.

## Overview

The Stirrup Agent is designed for benchmarking against the GDPval-AA evaluation framework. It implements Stirrup's exact agent behaviors:

- **Turn-based execution** with configurable `max_turns` (default: 100; GDPval-AA v2 uses 250, which the field now allows)
- **Turn warnings** injected when remaining turns ≤ threshold (default: 80)
- **Context summarization** at 70% of context window
- **`finish` tool** with Stirrup's exact schema: `{reason: str, paths: list[str]}`
- **`abandon_task` tool** (GDPval-AA v2, opt-in) with schema `{reason: str}`
- **Configurable base system prompt**, defaulting to Stirrup's wording
- **Aliased MCP tools** with Stirrup-compatible XML output formatting

## Tools

| Stirrup Tool | MCP Tool | Output Format |
|--------------|----------|---------------|
| `run_shell` | `run_shell` (mercor-aa-code-execution) | XML with exit_code, stdout, stderr |
| `web_search` | `web_search_google_custom_search` | XML with results (title, url, description) |
| `fetch_web_page` | Built-in (trafilatura) | XML with url, body |
| `view_image` | Built-in (MCP filesystem) | Image data for model |
| `finish` | Built-in | Task completion |
| `abandon_task` | Built-in (opt-in) | Task declared impossible |

## Configuration

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_turns` | int | 100 | Maximum agent turns before forced termination (max 250) |
| `turns_remaining_warning_threshold` | int | 80 | Inject warnings when remaining ≤ this value |
| `custom_system_prompt` | str | None | Extra instructions **appended** to the base prompt (e.g., GDPval-AA prompt) |
| `base_system_prompt` | str | None | **Replaces** the base prompt clause. Blank = the built-in default. Supports `{max_turns}`; all other braces are literal |
| `enable_abandon_task` | bool | False | Offer the `abandon_task` tool alongside `finish` |
| `record_tool_declarations` | bool | False | Write the tool definitions offered to the model into `output["tool_declarations"]` |
| `harness_prompt_additions` | bool | True | Append the harness's no-user-interaction and `abandon_task` lines after the base prompt. Off when `base_system_prompt` already says both |
| `task_prompt_template` | str | None | Wraps the task prompt (first user message) of a new run at `{task}`. `{reference_files}` lists the sandbox's files at run start in AA's format; all other braces are literal |
| `stirrup_wording` | str | `mercor` | `aa_stirrup` uses upstream Stirrup's `finish` description, turn warnings and "Please continue the task" nudge |
| `validate_finish_paths` | bool | False | Refuse a `finish` whose paths aren't existing files (upstream's check), via the platform's shell tool |
| `summarization_mode` | str | `flattened` | `in_conversation` requests the summary inside the real conversation, as upstream does |
| `timeout` | int | 27000 | Overall timeout in seconds (sized for a 250-turn run) |
| `tool_call_timeout` | int | 300 | Timeout for individual tool calls |
| `llm_response_timeout` | int | 600 | Timeout for LLM API calls |

## System Prompt Format

The agent constructs its system prompt following Stirrup's exact format:

```
You are an AI agent that will be given a specific task. You are to complete that task using the tools provided in {max_turns} steps. You will need to call the finish tool as your last step, where you will pass your finish reason and paths to any files that you wish to return to the user. You are not able to interact with the user during the task.

[Optional: Input files section]

Follow these instructions from the User:
[Custom system prompt if provided]
```

## Differences from ReAct Toolbelt Agent

| Feature | ReAct Toolbelt | Stirrup Agent |
|---------|----------------|---------------|
| Tool access | Dynamic toolbelt | Aliased tools with XML formatting |
| Termination | `final_answer(answer, status)` | `finish(reason, paths)` |
| Turn warnings | None | Injected when remaining ≤ threshold |
| System prompt | Static | Dynamic with max_turns + custom |
| Meta-tools | toolbelt_*, todo_write | None |
| Default max steps | 250 | 100 |

## Usage

Configure a world with:
- Agent: `stirrup_agent`
- `custom_system_prompt`: GDPval-AA evaluation prompt (if benchmarking)
- Adjust `max_turns` as needed for your task complexity

## Mirroring AA's harness (GDPval-AA v2.1)

All opt-in. Set `max_turns=250`, `enable_abandon_task=true`, `tool_call_timeout=600`,
`stirrup_wording=aa_stirrup`, `validate_finish_paths=true`, `summarization_mode=in_conversation`,
AA's published system prompt in `base_system_prompt` with `harness_prompt_additions=false`, and
AA's task-submission prompt in `task_prompt_template`, adapted to the platform's tool names. Still
different: the tool set and sandbox come from the platform, and AA's abandon tool is named
`abandon_task_finish`.

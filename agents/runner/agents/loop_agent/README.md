# Loop Agent

This is the standard agent implementation. It receives the task prompt and runs in a loop calling tools, stopping when no tool calls are made. It doesn't have any context summarization or other features. https://www.braintrust.dev/blog/agent-while-loop

## Steps

One step is one LLM call plus the execution of the tool calls in its response. `max_steps` caps the number of LLM calls. A step still counts when the call times out, when the response has no valid choices, or when the harness answers it with a nudge.

## Upstream-parity flags

Both flags are off by default. Together they make the loop behave like AgentDojo's `ToolsExecutionLoop`.

- `skip_tools_on_final_step`: tool calls in the response to the last allowed LLM call are kept in the recorded assistant message but are not executed, and no tool results are added for them. The last allowed call is step `max_steps`, or the one final turn after a token or cost budget runs out. At the step cap the run then ends `COMPLETED` with `attempt_outcome: "max_steps"` when `complete_on_budget_exhausted` is on, and `FAILED` otherwise. After a token or cost budget runs out it ends `FAILED`, even with `complete_on_budget_exhausted` on. For AgentDojo's `max_iters=N`, set `max_steps=N+1`. The default `max_iters=15` gives 16 LLM calls and 15 tool rounds.
- `disable_nudges`: the loop never injects the `"continue"` message (sent after a response with no valid choices) or the reasoning-only reminder. A response with no tool calls ends the run `COMPLETED`, and that turn is final even if it has no content. A response with no valid choices is a provider fault, not an answer: it ends the run `ERROR` with an `empty_response` fault and adds no message, so Studio retries it automatically instead of grading the partial state or scoring it in pass@1.

`disable_nudges` does not change the opt-in turn, token or cost warnings (`turn_warnings_enabled`, `accounting_mode`), and it does not change the request shaping in `generate_response`.

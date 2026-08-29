# Loop Truncated Tools Agent

This is a variant of the standard loop agent that truncates tool outputs to prevent context window overflow.

## Behavior

Identical to `loop_agent` except all tool responses are truncated to:
- **max_output_lines**: Maximum 200 lines (configurable)
- **max_output_chars**: Maximum 32,768 characters (configurable)

When truncation occurs, the output is appended with `... (truncated N lines) ...` or `... (truncated N chars) ...` (whichever limit was hit).

## Use Cases

- Tasks where tools may return very large outputs (e.g., file reads, command outputs)
- Preventing context window overflow errors with smaller context models
- Reducing token costs when full tool output isn't necessary

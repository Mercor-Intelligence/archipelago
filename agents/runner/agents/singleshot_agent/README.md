# Single Shot Agent

A minimal agent that makes exactly **one LLM call** with the provided prompt and returns the response.
## Configuration

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `llm_response_timeout` | number | 600 | Timeout in seconds for the LLM API call |

## Extra Args (via `orchestrator_extra_args`)

| Field | Type | Description |
|-------|------|-------------|
| `max_input_tokens` | number | Maximum tokens for input. If not provided, uses model's default context window |
| `temperature` | float | LLM temperature (0.0-2.0) |
| `max_tokens` | number | Maximum tokens to generate |
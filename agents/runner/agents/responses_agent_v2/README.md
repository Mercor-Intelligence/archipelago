# Responses Agent V2

A single-shot agent that uses the **OpenAI Responses API** (`litellm.aresponses`) instead of the traditional Chat Completions API.

V2 builds on the baseline `responses_agent` with file handling improvements and rate-limit resilience.

## V2 Improvements

- **Message normalization**: Translates Chat Completions content blocks (`text`, `image_url`, `file`) to the Responses API format (`input_text`, `input_image`, `input_file`)
- **File uploads with retry**: Inline base64 file data is uploaded to the provider's Files API via `litellm.acreate_file`, with exponential backoff on transient errors (rate limits, timeouts, server errors)
- **Inter-upload delay**: A small delay between consecutive file uploads prevents bursting the provider's Files API rate limit
- **Gemini format conversion**: Automatically converts unsupported Gemini MIME types (docx, xlsx, xls, doc) to text/CSV before upload

## Features

- **Text Output**: Extracts and normalizes text content from responses
- **Tool/Function Calls**: Parses function calls in the new Responses API format
- **Web Search**: Native support for the `web_search` tool (enable via config)
- **Annotations**: Captures citations, URL references, and other annotations
- **Reasoning Content**: Extracts thinking/reasoning content when available

## Configuration

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `llm_response_timeout` | number | 600 | Timeout for LLM API calls in seconds |

Tools (including `{"type": "web_search"}`) are passed via `orchestrator_extra_args["tools"]`.

## Output Structure

The agent produces a structured output with:

```python
{
    "raw_response": {...},           # Full API response
    "parsed": {...},                 # Parsed output object
    "text": "...",                   # Extracted text content
    "tool_calls": [...],             # All tool calls made
    "web_search_calls": [...],       # Web search queries
    "annotations": [...]             # Citations and references
}
```

## When to Use

Choose this agent when you need:
- Web search capabilities (built into the API)
- File input support (PDFs, images, documents)
- Structured annotations and citations
- The latest OpenAI API features
- Single-shot generation (no looping/tool execution)

For multi-turn conversations with actual tool execution, use `loop_agent` or `toolbelt_agent` instead.
For the baseline agent without file handling, use `responses_agent`.

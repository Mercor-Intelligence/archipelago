# Responses Agent

A single-shot agent that uses the **OpenAI Responses API** (`litellm.aresponses`) instead of the traditional Chat Completions API.

## Overview

The Responses API is OpenAI's newer API designed for structured outputs, web search, and improved tool calling. This agent makes a single call to the API and properly parses all output types.

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

## Responses API Output Types

The Responses API returns an `output` array with items of different types:

| Type | Description |
|------|-------------|
| `message` | Contains text content with `output_text` items |
| `function_call` | A function/tool call made by the model |
| `web_search_call` | A web search action with query |
| `reasoning` | Model's reasoning/thinking content |
| `tool_call` | Generic tool call format |

## When to Use

Choose this agent when you need:
- Web search capabilities (built into the API)
- Structured annotations and citations
- The latest OpenAI API features
- Single-shot generation (no looping/tool execution)

For multi-turn conversations with actual tool execution, use `loop_agent` or `toolbelt_agent` instead.

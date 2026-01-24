# mcp-schema

Schema utilities for MCP servers, providing Gemini-compatible JSON schema generation.

## Problem

The Gemini API's Function Calling feature requires a specific subset of JSON Schema. It does **not** support:

- `$defs` / `$ref` (Pydantic nested model references)
- `anyOf` (Pydantic `Optional[X]` / `X | None` patterns)
- `default` values
- `title` fields

Pydantic v2 generates all of these by default, causing errors like:

```
ValueError: Unknown field for Schema: $defs
ValidationError: Extra inputs are not permitted [input_value='#/$defs/MyModel']
```

## Solution

This package provides utilities to transform Pydantic schemas into flat, Gemini-compatible formats.

## Installation

```bash
uv pip install -e packages/mcp_schema
```

## Usage

### Option 1: Use GeminiBaseModel (Recommended)

The simplest approach - inherit from `GeminiBaseModel` instead of `BaseModel`:

```python
from mcp_schema import GeminiBaseModel

class DataInput(GeminiBaseModel):
    action: str
    file_path: str | None = None
    confirm: bool = False

class ToolRequest(GeminiBaseModel):
    session_id: str
    request: DataInput

# Schema is automatically Gemini-compatible
schema = ToolRequest.model_json_schema()
# No $defs, $ref, or anyOf!
```

### Option 2: Use the Schema Generator

For existing models you don't want to change:

```python
from pydantic import BaseModel
from mcp_schema import GeminiSchemaGenerator

class MyInput(BaseModel):
    name: str
    value: int | None = None

# Use the custom generator
schema = MyInput.model_json_schema(schema_generator=GeminiSchemaGenerator)
```

### Option 3: Post-process Existing Schemas

For dynamic schemas or third-party models:

```python
from mcp_schema import flatten_schema

original_schema = SomeModel.model_json_schema()
gemini_schema = flatten_schema(original_schema)
```

## Before / After

**Before (Pydantic default):**

```json
{
  "$defs": {
    "DataInput": {
      "properties": {
        "action": { "type": "string" },
        "file_path": {
          "anyOf": [{ "type": "string" }, { "type": "null" }]
        }
      }
    }
  },
  "properties": {
    "request": { "$ref": "#/$defs/DataInput" }
  }
}
```

**After (Gemini-compatible):**

```json
{
  "properties": {
    "request": {
      "properties": {
        "action": { "type": "string" },
        "file_path": { "type": "string" }
      }
    }
  }
}
```

## API Reference

### `GeminiBaseModel`

Base model class that automatically generates Gemini-compatible schemas.

### `GeminiSchemaGenerator`

Custom Pydantic schema generator for use with `model_json_schema()`.

### `flatten_schema(schema: dict) -> dict`

Post-process any JSON schema to remove Gemini-incompatible constructs.

### `get_gemini_schema(model: type[BaseModel]) -> dict`

Convenience function to get a Gemini-compatible schema from any Pydantic model.

## License

MIT

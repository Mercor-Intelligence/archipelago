from litellm.types.utils import Message

from runner.utils.llm import (
    _strip_invalid_required_schema_fields,
    _strip_unsupported_openai_message_fields,
)


def test_strip_unsupported_openai_message_fields() -> None:
    message = Message(
        role="assistant",
        content="answer",
        provider_specific_fields={"internal": "value"},
        reasoning_content="reasoning",
        refusal="refusal",
    )

    assert _strip_unsupported_openai_message_fields([message]) == [
        {"role": "assistant", "content": "answer"}
    ]


def test_strip_invalid_required_schema_fields_recursively() -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "example",
                "parameters": {
                    "type": "object",
                    "properties": {"valid": {"type": "string"}},
                    "required": ["valid", "missing"],
                },
            },
        }
    ]

    sanitized = _strip_invalid_required_schema_fields(tools)

    assert sanitized[0]["function"]["parameters"]["required"] == ["valid"]

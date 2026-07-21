from runner.utils.llm import _strip_unsupported_openai_message_fields


def test_strip_unsupported_openai_message_fields_recursively() -> None:
    messages = [
        {
            "role": "assistant",
            "content": "answer",
            "provider_specific_fields": {"internal": "value"},
            "reasoning_content": "reasoning",
            "refusal": "refusal",
            "optional": None,
        }
    ]

    assert _strip_unsupported_openai_message_fields(messages) == [
        {"role": "assistant", "content": "answer"}
    ]

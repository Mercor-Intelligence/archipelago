from pydantic import BaseModel, ConfigDict, JsonValue, model_validator


class RecordedToolHistory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tools: list[dict[str, JsonValue]]
    messages: list[dict[str, JsonValue]]

    @model_validator(mode="after")
    def validate_history(self) -> "RecordedToolHistory":
        pending: set[str] = set()
        seen: set[str] = set()
        for message in self.messages:
            if message.get("role") == "assistant":
                calls = message.get("tool_calls")
                if pending or not isinstance(calls, list) or not calls:
                    raise ValueError(
                        "Expected assistant tool calls after resolved results"
                    )
                for call in calls:
                    if not isinstance(call, dict):
                        raise ValueError("Recorded tool calls must be objects")
                    call_id = call.get("id")
                    if not isinstance(call_id, str) or not call_id:
                        raise ValueError("Recorded tool calls require string IDs")
                    if call_id in seen:
                        raise ValueError("Duplicate recorded tool call ID")
                    seen.add(call_id)
                    pending.add(call_id)
            elif message.get("role") == "tool":
                call_id = message.get("tool_call_id")
                if not isinstance(call_id, str) or call_id not in pending:
                    raise ValueError("Recorded tool result has no matching call")
                pending.remove(call_id)
            else:
                raise ValueError(
                    "Recorded history accepts only assistant calls and tool results"
                )
        if pending:
            raise ValueError("Recorded history has unresolved tool calls")
        return self

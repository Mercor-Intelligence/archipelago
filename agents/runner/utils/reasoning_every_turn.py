from collections.abc import Mapping, Sequence
from typing import Any

from litellm.files.main import ModelResponse

from runner.utils.llm import is_gemini_model

# Models that reject an assistant message without reasoning ("" counts as missing).
# Matched as lowercase substrings of the model name.
REASONING_EVERY_TURN_MODELS: tuple[str, ...] = ("beam-501b-a23b",)
# " " passes where "" is rejected.
REASONING_PLACEHOLDER = " "


def needs_reasoning_every_turn(model: str) -> bool:
    lowered = model.lower()
    return any(name in lowered for name in REASONING_EVERY_TURN_MODELS)


def add_reasoning_placeholders(
    messages: Sequence[Mapping[str, Any]],
) -> list[Mapping[str, Any]]:
    """Mode 1: give each assistant message without reasoning the placeholder."""
    return [
        {**message, "reasoning_content": REASONING_PLACEHOLDER}
        if message["role"] == "assistant" and not message.get("reasoning_content")
        else message
        for message in messages
    ]


def replay_reasoning(response: ModelResponse, model: str) -> dict[str, Any] | None:
    """Mode 2: the reasoning fields to send back on the next turns.

    Attach them to the stored assistant message. For a model that
    `needs_reasoning_every_turn`, also run `add_reasoning_placeholders` before
    each call, so a turn with no real reasoning (a seeded greeting) gets " ".
    """
    message = response.model_dump()["choices"][0]["message"]
    if is_gemini_model(model):
        # Gemini wants its thought signatures back, not the thoughts.
        signatures = (message.get("provider_specific_fields") or {}).get(
            "thought_signatures"
        )
        return (
            {"provider_specific_fields": {"thought_signatures": signatures}}
            if signatures
            else None
        )
    replay = {
        key: message[key]
        for key in ("reasoning_content", "thinking_blocks", "reasoning_items")
        if message.get(key)
    }
    # The proxy lifts opaque ones to the top level for Vercel.
    if details := (message.get("provider_specific_fields") or {}).get(
        "reasoning_details"
    ):
        replay["provider_specific_fields"] = {"reasoning_details": details}
    return replay or None

"""Monkey-patches for litellm bugs. Remove when upgrading past the fix."""

from importlib.metadata import version as _pkg_version
from typing import Any

from litellm.litellm_core_utils.streaming_handler import CustomStreamWrapper
from litellm.llms.custom_httpx import http_handler
from litellm.llms.openai.chat.gpt_5_transformation import OpenAIGPT5Config
from packaging.version import Version

# litellm <= 1.82.1 incorrectly rejects xhigh reasoning_effort for gpt-5.4.
# The hardcoded check only allows gpt-5.1-codex-max and gpt-5.2, but OpenAI's
# model card confirms gpt-5.4 supports xhigh. Patch until litellm ships the fix.
_LITELLM_MAX_BUGGY_VERSION = Version("1.82.1")

if Version(_pkg_version("litellm")) <= _LITELLM_MAX_BUGGY_VERSION:
    _original_map = OpenAIGPT5Config.map_openai_params

    def _patched_map(
        self: OpenAIGPT5Config,
        non_default_params: dict[str, Any],
        optional_params: dict[str, Any],
        model: str,
        drop_params: bool,
    ) -> dict[str, Any]:
        # Temporarily make gpt-5.4 look like gpt-5.2 so the xhigh check passes.
        fake_model = False
        reasoning_effort = non_default_params.get(
            "reasoning_effort"
        ) or optional_params.get("reasoning_effort")
        if "gpt-5.4" in model and reasoning_effort == "xhigh":
            fake_model = True
            model = model.replace("gpt-5.4", "gpt-5.2")

        result = _original_map(
            self, non_default_params, optional_params, model, drop_params
        )

        # Restore the real model — callers may inspect optional_params["model"] downstream.
        if fake_model:
            result["model"] = result.get("model", "").replace("gpt-5.2", "gpt-5.4")

        return result

    OpenAIGPT5Config.map_openai_params = _patched_map  # type: ignore[assignment]


# litellm drops a streamed delta whose only payload is reasoning_details (Vercel's encrypted reasoning).
_KEEPS_REASONING_DETAILS = "_keeps_reasoning_details"


def _keep_reasoning_details_chunks(original: Any) -> Any:
    def is_chunk_non_empty(
        self: CustomStreamWrapper, *args: Any, **kwargs: Any
    ) -> bool:
        if original(self, *args, **kwargs):
            return True
        response = kwargs.get("model_response", args[1] if len(args) > 1 else None)
        choices = getattr(response, "choices", None) or []
        delta = getattr(choices[0], "delta", None) if choices else None
        return bool(getattr(delta, "reasoning_details", None))

    setattr(is_chunk_non_empty, _KEEPS_REASONING_DETAILS, True)
    return is_chunk_non_empty


_is_chunk_non_empty = getattr(CustomStreamWrapper, "is_chunk_non_empty", None)
if _is_chunk_non_empty is not None and not getattr(
    _is_chunk_non_empty, _KEEPS_REASONING_DETAILS, False
):
    CustomStreamWrapper.is_chunk_non_empty = _keep_reasoning_details_chunks(  # type: ignore[method-assign]
        _is_chunk_non_empty
    )


# Azure-hosted Modal containers silently drop TCP flows idle ~270s. Set on the module:
# the AIOHTTP_SO_KEEPALIVE env var is read at litellm import, possibly before this runs.
http_handler.AIOHTTP_SO_KEEPALIVE = True

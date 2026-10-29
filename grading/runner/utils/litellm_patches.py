"""Monkey-patches for litellm bugs. Remove when upgrading past the fix."""

from importlib.metadata import version as _pkg_version
from typing import Any

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

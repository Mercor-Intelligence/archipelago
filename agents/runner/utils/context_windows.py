"""Checked-in context windows for the models Studio runs.
Why a table: ``resolve_context_window`` used to trust two litellm maps, the
server's and the container's. litellm builds ``model_cost`` at import from an
HTTP GET of upstream ``model_prices_and_context_window.json`` (5s timeout, no
retry) and otherwise from the copy bundled with the installed version. The
bundled 1.92 map on the server has no ``claude-opus-5`` or ``gpt-5.6``; the
container's 1.84 also lacks ``claude-opus-4-8``. So the ceiling a run got
depended on whether a boot-time fetch succeeded, and a staler server map could
hand the runner a *smaller* window than the container knew. A ceiling that
decides when a benchmark run is summarized has to be a fact checked into the
repo, not a network outcome.

This module is stdlib-only and is copied verbatim to
``rl-studio/server/agent_definitions/context_windows.py`` by
``scripts/sync_agent_definitions.sh`` (CI fails on drift), so the server rate
card and the runner cannot disagree.

Values were taken from litellm's upstream JSON on 2026-09-21. When a value here
is wrong the fix is a one-line edit and a test, not a litellm bump. Keys are
the bare model id; a ``provider/model`` key overrides it where a provider
serves a smaller window than the model's home API.
"""

import re

CONTEXT_WINDOW_TOKENS: dict[str, int] = {
    # Anthropic
    "claude-opus-4": 200_000,
    "claude-opus-4-1": 200_000,
    "claude-opus-4-5": 200_000,
    "claude-opus-4-6": 1_000_000,
    "claude-opus-4-7": 1_000_000,
    "claude-opus-4-8": 1_000_000,
    "claude-opus-5": 1_000_000,
    "claude-opus-5-5": 1_000_000,
    "claude-fable-5": 1_000_000,
    "claude-fable-5-1": 1_000_000,
    "claude-mythos-5": 1_000_000,
    "claude-mythos-5-1": 1_000_000,
    # litellm reports 1M for sonnet-4 and sonnet-4-5 (it still carries the
    # beta-era long-context entry, with above-200k pricing tiers). Anthropic's
    # own context-window docs list both as plain 200k models: the 1M models are
    # opus-4-6 and later, sonnet-4-6 and later, and the Fable/Mythos 5 line,
    # and for every one of those "1M is the default: you don't need a beta
    # header". So no entry here depends on a header this runner does not send.
    "claude-sonnet-4": 200_000,
    "claude-sonnet-4-5": 200_000,
    "claude-sonnet-4-6": 1_000_000,
    "claude-sonnet-5": 1_000_000,
    "claude-haiku-4-5": 200_000,
    # OpenAI
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4.1": 1_047_576,
    "gpt-4.1-mini": 1_047_576,
    "gpt-4.1-nano": 1_047_576,
    "o1": 200_000,
    "o3": 200_000,
    "o3-mini": 200_000,
    "o3-pro": 200_000,
    "o4-mini": 200_000,
    "gpt-5": 272_000,
    "gpt-5-mini": 272_000,
    "gpt-5-nano": 272_000,
    "gpt-5-pro": 400_000,
    "gpt-5-codex": 272_000,
    "gpt-5.1": 272_000,
    "gpt-5.1-codex": 272_000,
    "gpt-5.2": 272_000,
    "gpt-5.2-codex": 272_000,
    "gpt-5.2-pro": 272_000,
    "gpt-5.3-codex": 272_000,
    "gpt-5.4": 1_050_000,
    "gpt-5.4-mini": 272_000,
    "gpt-5.4-nano": 272_000,
    "gpt-5.4-pro": 1_050_000,
    "gpt-5.5": 1_050_000,
    "gpt-5.5-pro": 1_050_000,
    "gpt-5.6": 922_000,
    "gpt-5.6-sol": 922_000,
    "gpt-5.6-luna": 922_000,
    "gpt-5.6-terra": 922_000,
    "gpt-5.6-cyber": 400_000,
    "gpt-6-astra": 922_000,
    # Google
    "gemini-2.0-flash": 1_048_576,
    "gemini-2.5-pro": 1_048_576,
    "gemini-2.5-flash": 1_048_576,
    "gemini-2.5-flash-lite": 1_048_576,
    "gemini-2.5-computer-use-preview-10-2025": 128_000,
    "gemini-3-pro-preview": 1_048_576,
    "gemini-3-flash-preview": 1_048_576,
    "gemini-3.1-pro-preview": 1_048_576,
    "gemini-3.1-flash-lite": 1_048_576,
    "gemini-3.1-flash-lite-preview": 1_048_576,
    "gemini-3.5-flash": 1_048_576,
    "gemini-3.5-flash-lite": 1_048_576,
    "gemini-3.6-flash": 1_048_576,
    "gemini-3.7-flash": 1_048_576,
    "gemini-3.8-flash": 1_048_576,
    # Panther vendor checkpoint, served by the gateway as gemini/gemini-mb.
    "gemini-mb": 1_048_576,
    "gemini-mb-vendor-1": 1_048_576,
    "gemini-mb-vendor-1-zdr": 1_048_576,
    # xAI
    "grok-3": 131_072,
    "grok-3-mini": 131_072,
    "grok-4": 256_000,
    "grok-4-fast-reasoning": 2_000_000,
    "grok-4-fast-non-reasoning": 2_000_000,
    "grok-4-1-fast-reasoning": 2_000_000,
    "grok-4-1-fast-non-reasoning": 2_000_000,
    "grok-4.20": 1_000_000,
    "grok-4.3": 1_000_000,
    "grok-4.5": 500_000,
    "grok-4.6": 500_000,
    "grok-4.7": 500_000,
    # Provider-specific caps that differ from the model's home API go here as
    # "provider/model" keys; they win over the bare id.
    "vllm/nemotron-3.5-super": 262_144,  # max_model_len; vllm/ is per route, not a lane
}

# Studio routes OpenAI Responses-API models as ``openai/responses/<model>``; the
# segment is a routing convention, not part of any model id.
_ROUTING_SEGMENTS = frozenset({"responses"})
# Leading ``lane/`` segments are dropped only for lanes Studio is known to
# route through and whose windows this table has been checked against. An
# unknown lane (``azure/gpt-5-pro`` serves 272k where OpenAI direct serves
# 400k; ``github_copilot/gpt-5`` 128k vs 272k) must NOT inherit the home-API
# window: returning None sends it to the rate card and litellm, which do know
# per-lane caps. Onboarding a new lane means adding it here, and adding a
# ``lane/model`` cap below wherever it serves less than the home API.
_KNOWN_LANES = frozenset(
    {
        "anthropic",
        "openai",
        "gemini",
        "vertex_ai",
        "xai",
        "bedrock",
        "gateway",
        "gdm_zdr",
        # Billing-project swap onto gemini/vertex_ai (infra/litellm/config.yaml).
        "code_data_benchmark",
    }
)
# ``claude-opus-4-1-20250805``, ``claude-opus-4-5@20251101``, ``gpt-5.6-2026-01-01``,
# ``claude-sonnet-4-5-20250929-v1:0`` — dated or pinned variants of a base id.
_VERSION_SUFFIX = re.compile(r"(@.*|-\d{8}(-v\d+:\d+)?|-\d{4}-\d{2}-\d{2}|-v\d+:\d+)$")
# Bedrock ids: ``us.anthropic.claude-sonnet-4-5-20250929-v1:0`` — an optional
# region, the vendor, then the model id joined with dots instead of slashes.
_BEDROCK_PREFIX = re.compile(r"^(?:(?:us|eu|apac|global|jp|au)\.)?anthropic\.")


def _normalize_segment(segment: str) -> str:
    return _BEDROCK_PREFIX.sub("", segment)


def lookup_context_window(model: str) -> int | None:
    """Context window for ``model`` from the checked-in table, or None.

    Tries the most specific spelling first so a ``lane/model`` cap wins over
    the bare id: the full string, then the string with each leading known-lane
    segment dropped (``openai/anthropic/claude-opus-4-8`` — a gateway alias —
    reaches ``claude-opus-4-8``), each also with a trailing date, ``@version``
    or Bedrock ``-v1:0`` pin removed. A leading segment that is not a known
    lane ends the search with None.
    """
    if not model:
        return None
    segments = [
        _normalize_segment(s)
        for s in model.strip().lower().split("/")
        if s and s not in _ROUTING_SEGMENTS
    ]
    if not segments:
        return None
    for i in range(len(segments)):
        candidate = "/".join(segments[i:])
        for key in (candidate, _VERSION_SUFFIX.sub("", candidate)):
            window = CONTEXT_WINDOW_TOKENS.get(key)
            if window is not None:
                return window
        if segments[i] not in _KNOWN_LANES:
            return None
    return None

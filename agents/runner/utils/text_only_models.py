import re

# Models CONFIRMED text-only by observed "does not support image/multimodal" 400s.
# For these we proactively withhold the image tool and strip any image from their
# context — matching AA, whose View Image tool "is only exposed to models with
# vision support" — so they finish best-effort on the text instead of dying on a
# 400. Maintained by hand: when a new endpoint is seen erroring this way, add it.
# Safe to list because these are proven no-vision (cannot mis-blind a vision
# model); matched as lowercase substrings, precise enough to exclude vision
# siblings (kimi-k3, minimax-m3).
KNOWN_TEXT_ONLY_MODELS: frozenset[str] = frozenset(
    {
        "deepseek-v3.2",
        "deepseek-v4-pro",
        "glm-5.2",
        "qwen3p8-max",
        "kimi-k2-thinking",
        "minimax-m2.7",
        "nemotron-3-ultra",
        "beam-501b-a23b",
    }
)


def is_known_text_only(model: str) -> bool:
    """True if ``model`` is a confirmed text-only endpoint (proactive image gate).

    A token matches only when bounded by a non-alphanumeric char (or string edge)
    on each side, so it still matches inside a longer name via a separator
    (`nemotron-3-ultra` in `…/nemotron-3-ultra-nvfp4`) but NOT a run-on suffix — a
    `v`-suffixed vision sibling like `glm-5.2v` is not caught. Prevents silently
    blinding a vision model, which (with no reactive fallback) would be unrecoverable.
    """
    lowered = model.lower()
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", lowered)
        for token in KNOWN_TEXT_ONLY_MODELS
    )

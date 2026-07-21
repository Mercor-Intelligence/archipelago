from runner.utils.token_utils import _get_token_multiplier


def test_gemini_token_multiplier_leaves_conservative_headroom() -> None:
    assert _get_token_multiplier("vertex_ai/gemini-3-flash") == 5.0
    assert _get_token_multiplier("openai/gpt-5") == 1.0

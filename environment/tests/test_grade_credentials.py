"""Tests for the in-container grade credential allowlist (runner.grade).

The allowlist is a security boundary: /grade is unauthenticated in hosted-envs and reachable by the
confined model over localhost, so grading_credentials_json must never be able to inject loader/exec
controls into the ROOT grade subprocess.
"""

from runner.grade import _ALLOWED_GRADING_CRED_KEYS, _filter_grading_credentials


def test_allowlisted_credentials_pass_through():
    raw = '{"LITELLM_PROXY_API_KEY": "k1", "OPENAI_API_KEY": "k2", "REDUCTO_API_KEY": "k3"}'
    assert _filter_grading_credentials(raw) == {
        "LITELLM_PROXY_API_KEY": "k1",
        "OPENAI_API_KEY": "k2",
        "REDUCTO_API_KEY": "k3",
    }


def test_loader_and_exec_controls_are_dropped():
    # The core escalation this guards against: a crafted /grade must not be able to set these on the
    # root subprocess (they would run attacker code as root, bypassing the uid split).
    raw = (
        '{"LD_PRELOAD": "/filesystem/evil.so", "PYTHONPATH": "/filesystem",'
        ' "PATH": "/filesystem/bin", "LD_LIBRARY_PATH": "/filesystem",'
        ' "LITELLM_PROXY_API_KEY": "legit"}'
    )
    out = _filter_grading_credentials(raw)
    assert out == {"LITELLM_PROXY_API_KEY": "legit"}
    for danger in ("LD_PRELOAD", "PYTHONPATH", "PATH", "LD_LIBRARY_PATH"):
        assert danger not in out


def test_arbitrary_unknown_keys_are_dropped():
    assert _filter_grading_credentials('{"SOME_RANDOM_KEY": "x"}') == {}


def test_malformed_json_yields_empty():
    assert _filter_grading_credentials("not json") == {}
    assert _filter_grading_credentials("") == {}
    assert _filter_grading_credentials("{") == {}


def test_non_object_json_yields_empty():
    # A JSON array/string/number must not crash or leak.
    assert _filter_grading_credentials('["LD_PRELOAD"]') == {}
    assert _filter_grading_credentials('"LITELLM_PROXY_API_KEY"') == {}
    assert _filter_grading_credentials("42") == {}


def test_values_are_coerced_to_str():
    # A non-string value (e.g. an int) is stringified, never passed through raw.
    assert _filter_grading_credentials('{"OPENAI_API_KEY": 123}') == {
        "OPENAI_API_KEY": "123"
    }


def test_allowlist_excludes_loader_controls():
    for danger in ("LD_PRELOAD", "PYTHONPATH", "PATH", "LD_LIBRARY_PATH", "HOME"):
        assert danger not in _ALLOWED_GRADING_CRED_KEYS

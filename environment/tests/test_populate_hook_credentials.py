"""Populate request credentials reach the app subprocess without leaking into its parent."""

import json
import os
import shlex
import sys
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from runner.data.populate import main
from runner.data.populate.models import LifecycleHook, PopulateRequest, PopulateResult
from runner.utils.s3 import S3Credentials


@pytest.mark.parametrize("tolerate", [False, True])
@pytest.mark.parametrize("session_token", [None, "request-token"])
async def test_request_credentials_reach_hook_subprocess(
    monkeypatch: pytest.MonkeyPatch, tolerate: bool, session_token: str | None
) -> None:
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "parent-key")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "parent-token")
    monkeypatch.setenv("MODAL_IDENTITY_TOKEN", "parent-oidc")
    credentials = S3Credentials(
        access_key_id="request-key",
        secret_access_key=SecretStr("request-secret"),
        session_token=SecretStr(session_token) if session_token else None,
        region="us-west-2",
    )
    expected = {
        "AWS_ACCESS_KEY_ID": "request-key",
        "AWS_SECRET_ACCESS_KEY": "request-secret",
        "AWS_SESSION_TOKEN": session_token,
        "AWS_REGION": "us-west-2",
        "AWS_DEFAULT_REGION": "us-west-2",
        "MODAL_IDENTITY_TOKEN": None,
        "APP_SETTING": "preserved",
    }
    script = (
        "import json, os; "
        f"expected = json.loads({json.dumps(expected)!r}); "
        "assert {k: os.environ.get(k) for k in expected} == expected"
    )
    hook = LifecycleHook(
        name="foundry_deepworld",
        command=f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}",
        env={"APP_SETTING": "preserved", "AWS_SESSION_TOKEN": "hook-token"},
    )
    download = AsyncMock(return_value=PopulateResult(objects_added=0))
    monkeypatch.setattr(main, "populate_data", download)
    result = await main.handle_populate(
        PopulateRequest(
            sources=[],
            post_populate_hooks=[hook],
            s3_credentials=credentials,
            tolerate_identity_rejection=tolerate,
        )
    )
    assert len(result.hook_timings) == 1
    assert download.call_args.kwargs["s3_credentials"] is credentials
    assert os.environ["AWS_ACCESS_KEY_ID"] == "parent-key"
    assert os.environ["AWS_SESSION_TOKEN"] == "parent-token"
    assert hook.env == {"APP_SETTING": "preserved", "AWS_SESSION_TOKEN": "hook-token"}


async def test_no_request_credentials_preserves_hook_environment() -> None:
    hook = LifecycleHook(
        name="populate",
        command='test "$AWS_ACCESS_KEY_ID" = hook-key',
        env={"AWS_ACCESS_KEY_ID": "hook-key"},
    )
    await main.run_lifecycle_hook(hook)


@pytest.mark.parametrize("tolerate", [False, True])
async def test_request_credentials_only_reach_deepworld(
    monkeypatch: pytest.MonkeyPatch, tolerate: bool
) -> None:
    keys = (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
    )
    for key in keys:
        monkeypatch.delenv(key, raising=False)
    credentials = S3Credentials(
        access_key_id="request-key",
        secret_access_key=SecretStr("request-secret"),
        session_token=SecretStr("request-token"),
        region="us-west-2",
    )
    hooks = []
    for name in ("foundry_deepworld", "fineract", "foundry_deepworld_other"):
        script = "import os; assert os.environ.get('APP_SETTING') == 'preserved'; " + (
            "assert os.environ['AWS_ACCESS_KEY_ID'] == 'request-key'"
            if name == "foundry_deepworld"
            else f"assert all(key not in os.environ for key in {keys!r})"
        )
        hooks.append(
            LifecycleHook(
                name=name,
                command=f"{shlex.quote(sys.executable)} -c {shlex.quote(script)}",
                env={"APP_SETTING": "preserved"},
            )
        )
    monkeypatch.setattr(
        main, "populate_data", AsyncMock(return_value=PopulateResult(objects_added=0))
    )
    result = await main.handle_populate(
        PopulateRequest(
            sources=[],
            post_populate_hooks=hooks,
            s3_credentials=credentials,
            tolerate_identity_rejection=tolerate,
        )
    )
    assert len(result.hook_timings) == 3
    assert all(key not in os.environ for key in keys)

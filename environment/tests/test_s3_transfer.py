"""Unit tests for the environment-runner pluggable S3 download backend.

Covers backend selection + safe-fallback and the security guardrails Corridor
flagged (namespace allowlist, path-traversal/metachar rejection, dest confinement
to the sandbox subsystem roots). No network/subprocess — the live A/B exercises
s5cmd itself.
"""

import pytest

from runner.utils.s3_transfer import (
    DEFAULT_BACKEND,
    S5cmdDownloader,
    _confine_to_subsystem,
    _validate_bucket,
    get_s5cmd_downloader,
    key_is_eligible,
    resolve_backend,
)

# --- backend selection ------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, "boto3"),
        ("boto3", "boto3"),
        ("s5cmd", "s5cmd"),
        ("rclone", "boto3"),  # unknown value must not break populate
        ("", "boto3"),
    ],
)
def test_resolve_backend(value, expected):
    assert resolve_backend(value) == expected


def test_default_backend_is_boto3():
    assert DEFAULT_BACKEND == "boto3"


def test_resolve_backend_env_override(monkeypatch):
    monkeypatch.setenv("S3_TRANSFER_BACKEND", "s5cmd")
    assert resolve_backend(None) == "s5cmd"


# --- factory + fallback -----------------------------------------------------


def test_get_downloader_none_for_boto3():
    assert get_s5cmd_downloader("boto3") is None
    assert get_s5cmd_downloader(None) is None


def test_get_downloader_falls_back_when_binary_missing(monkeypatch):
    monkeypatch.setattr("runner.utils.s3_transfer.shutil.which", lambda _: None)
    assert get_s5cmd_downloader("s5cmd") is None


def test_get_downloader_returns_s5cmd_when_selected_and_available(monkeypatch):
    monkeypatch.setattr(
        "runner.utils.s3_transfer.shutil.which", lambda _: "/usr/local/bin/s5cmd"
    )
    dl = get_s5cmd_downloader("s5cmd")
    assert isinstance(dl, S5cmdDownloader)
    assert dl.name == "s5cmd"


# --- key allowlist (Corridor) -----------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "worlds/abc123/filesystem",
        "tasks/abc123",
        "pipelines/snap_abc123/filesystem",  # pipeline-output populate
        "playgrounds/p_1",
    ],
)
def test_key_is_eligible_accepts_snapshot_prefixes(key):
    assert key_is_eligible(key)


@pytest.mark.parametrize(
    "key",
    [
        "",
        "../etc/passwd",
        "worlds/../../etc",
        "/worlds/abc",  # absolute
        "etc/passwd",  # outside the snapshot namespace
        "worlds/abc; rm -rf /",  # shell metacharacters
        "worlds/abc\n",  # trailing newline (fullmatch guards this)
        # grading-only namespaces — populate never reads these, so they must NOT
        # be s5cmd-eligible on the env runner (regression guard for the allowlist).
        "trajectories/t_1/filesystem/data",
        "golden-responses/g_1",
        "snapshot_zips/z_1",
    ],
)
def test_key_is_eligible_rejects_unsafe(key):
    assert not key_is_eligible(key)


def test_validate_bucket():
    assert _validate_bucket("rl-studio-snapshots-prod")
    for bad in ["", "Bad_Bucket", "a", "x" * 100]:
        with pytest.raises(ValueError):
            _validate_bucket(bad)


# --- destination confinement (Corridor) -------------------------------------


@pytest.mark.parametrize("dest", ["/filesystem", "/filesystem/data", "/.apps_data"])
def test_confine_accepts_subsystem_roots(dest):
    assert _confine_to_subsystem(dest) == dest


@pytest.mark.parametrize(
    "dest",
    [
        "/etc",
        "/etc/passwd",
        "/filesystemX",  # not a true child of /filesystem
        "/tmp/escape",
    ],
)
def test_confine_rejects_escape(dest):
    with pytest.raises(ValueError):
        _confine_to_subsystem(dest)


# --- download_prefix validates before ever spawning a subprocess ------------


async def test_download_prefix_rejects_bad_key():
    dl = S5cmdDownloader()
    with pytest.raises(ValueError):
        await dl.download_prefix("rl-studio-snapshots-prod", "../evil", "/filesystem")


async def test_download_prefix_rejects_unconfined_dest():
    dl = S5cmdDownloader()
    with pytest.raises(ValueError):
        await dl.download_prefix(
            "rl-studio-snapshots-prod", "worlds/abc/filesystem", "/etc"
        )

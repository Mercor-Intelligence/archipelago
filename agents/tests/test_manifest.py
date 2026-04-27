"""Tests for RunManifest sidecar.

Tight scope: test serialization, hashing, and that two builds with
identical inputs produce identical manifest hashes for the
deterministic fields. Does NOT exercise runner.main end-to-end.
"""

import json
from pathlib import Path

from runner.manifest import RunManifest


def _base_kwargs():
    return dict(
        trajectory_id="t1",
        agent_config_id="echo_agent",
        agent_config_values={},
        orchestrator_model="anthropic/claude-opus-4-5",
        orchestrator_extra_args={"temperature": 0, "max_tokens": 1024},
        seed=42,
        deterministic=True,
        mcp_server_configs={"a": {"x": 1}, "b": {"y": 2}},
    )


def test_manifest_serializes_to_json(tmp_path: Path):
    m = RunManifest.from_run_inputs(**_base_kwargs())
    out = tmp_path / "m.json"
    m.write(out)
    data = json.loads(out.read_text())
    assert data["trajectory_id"] == "t1"
    assert data["seed"] == 42
    assert data["deterministic"] is True
    assert data["orchestrator_model"] == "anthropic/claude-opus-4-5"
    assert "schema_version" in data
    assert "created_at" in data


def test_mcp_config_hash_is_deterministic():
    m1 = RunManifest.from_run_inputs(**_base_kwargs())
    m2 = RunManifest.from_run_inputs(**_base_kwargs())
    assert m1.mcp_server_configs_hash == m2.mcp_server_configs_hash
    assert m1.mcp_server_configs_hash is not None


def test_mcp_config_hash_changes_when_config_changes():
    kwargs1 = _base_kwargs()
    kwargs2 = _base_kwargs()
    kwargs2["mcp_server_configs"] = {"a": {"x": 999}, "b": {"y": 2}}
    m1 = RunManifest.from_run_inputs(**kwargs1)
    m2 = RunManifest.from_run_inputs(**kwargs2)
    assert m1.mcp_server_configs_hash != m2.mcp_server_configs_hash


def test_attach_snapshots_computes_sha256(tmp_path: Path):
    f = tmp_path / "snap.bin"
    f.write_bytes(b"hello world")
    m = RunManifest.from_run_inputs(**_base_kwargs())
    m.attach_snapshots(initial_path=f)
    # sha256 of "hello world"
    assert m.initial_snapshot_sha256 == (
        "b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9"
    )


def test_missing_snapshot_path_is_silent(tmp_path: Path):
    m = RunManifest.from_run_inputs(**_base_kwargs())
    m.attach_snapshots(initial_path=tmp_path / "does_not_exist.bin")
    assert m.initial_snapshot_sha256 is None


def test_no_git_does_not_crash(monkeypatch, tmp_path: Path):
    from runner import manifest as manifest_mod

    monkeypatch.setattr(manifest_mod, "_git_sha", lambda: None)
    monkeypatch.setattr(manifest_mod, "_git_dirty", lambda: False)
    m = RunManifest.from_run_inputs(**_base_kwargs())
    out = tmp_path / "m.json"
    m.write(out)
    data = json.loads(out.read_text())
    assert data["git_sha"] is None

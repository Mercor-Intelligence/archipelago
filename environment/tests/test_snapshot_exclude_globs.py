"""Snapshot capture honors exclude_globs (e.g. FTS sidecar DBs left in place).

Connectors that protect files from the harvest sweep (mcp-shared
``harvest_protect_globs``) leave them inside ``.apps_data``, where the
capture walk would otherwise upload them into the trajectory snapshot.
``exclude_globs`` is the capture-side half of that contract: patterns are
matched against each file's archive name and matching files are skipped
by every snapshot flavor (tar.gz stream, per-file S3 upload, and the
prebuilt ZIP, which builds from the same file list).
"""

import inspect
from pathlib import Path

import pytest

from runner.data.snapshot import jobs as snapshot_jobs
from runner.data.snapshot import main as snapshot_main
from runner.data.snapshot.models import SnapshotRequest, SnapshotStreamRequest
from runner.data.snapshot.utils import iter_paths


@pytest.fixture
def apps_tree(tmp_path: Path) -> Path:
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "studio.db").write_bytes(b"main")
    (tmp_path / "svc" / "studio.fts.db").write_bytes(b"fts")
    (tmp_path / "top.fts.db").write_bytes(b"fts-top")
    (tmp_path / "notes.txt").write_bytes(b"notes")
    return tmp_path


def _arcnames(root: Path, exclude_globs: list[str] | None) -> set[str]:
    return {
        arcname for _, arcname in iter_paths(str(root), ".apps_data", exclude_globs)
    }


def test_iter_paths_no_globs_yields_everything(apps_tree: Path) -> None:
    assert _arcnames(apps_tree, None) == {
        ".apps_data/svc/studio.db",
        ".apps_data/svc/studio.fts.db",
        ".apps_data/top.fts.db",
        ".apps_data/notes.txt",
    }
    assert _arcnames(apps_tree, []) == _arcnames(apps_tree, None)


def test_iter_paths_excludes_sidecars_at_any_depth(apps_tree: Path) -> None:
    assert _arcnames(apps_tree, [".apps_data/*.fts.db"]) == {
        ".apps_data/svc/studio.db",
        ".apps_data/notes.txt",
    }


def test_trajectory_pattern_excludes_sqlite_journal_companions(
    tmp_path: Path,
) -> None:
    # A live sidecar in WAL mode leaves -wal/-shm next to the DB; capturing
    # those without the DB would restore an orphaned WAL. The trajectory
    # pattern pair covers the DB and its journal companions.
    (tmp_path / "svc").mkdir()
    (tmp_path / "svc" / "studio.db").write_bytes(b"main")
    for name in (
        "studio.fts.db",
        "studio.fts.db-wal",
        "studio.fts.db-shm",
        "studio.fts.db-journal",
    ):
        (tmp_path / "svc" / name).write_bytes(b"x")
    assert _arcnames(tmp_path, [".apps_data/*.fts.db", ".apps_data/*.fts.db-*"]) == {
        ".apps_data/svc/studio.db",
    }


def test_iter_paths_glob_is_scoped_to_arc_prefix(apps_tree: Path) -> None:
    # A pattern anchored to a different subsystem excludes nothing here.
    assert _arcnames(apps_tree, ["filesystem/*.fts.db"]) == _arcnames(apps_tree, None)


def test_iter_paths_multiple_globs(apps_tree: Path) -> None:
    assert _arcnames(apps_tree, ["*.txt", "*.fts.db"]) == {
        ".apps_data/svc/studio.db",
    }


def test_request_models_default_to_no_exclusions() -> None:
    assert SnapshotRequest().exclude_globs == []
    assert SnapshotStreamRequest().exclude_globs == []


def test_older_caller_payload_still_validates() -> None:
    # Callers built before the field existed omit it entirely.
    req = SnapshotRequest.model_validate({"format": "files"})
    assert req.exclude_globs == []


@pytest.mark.parametrize(
    "func",
    [
        snapshot_main.handle_snapshot,
        snapshot_main.handle_snapshot_s3,
        snapshot_main.handle_snapshot_s3_files,
    ],
)
def test_handlers_accept_exclude_globs(func) -> None:
    assert "exclude_globs" in inspect.signature(func).parameters


def test_async_jobs_forward_exclude_globs() -> None:
    src = inspect.getsource(snapshot_jobs.start_snapshot_job)
    assert src.count("exclude_globs=request.exclude_globs") == 2

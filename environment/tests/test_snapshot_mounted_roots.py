"""The capture split: a subsystem root read over a live 9p mount is slow.

A world half can reach the sandbox as a mounted Modal image instead of an S3
download. The mount is read-write and stays live for the whole run, so the
end-of-run capture reads those bytes over 9p — measured ~4.6x slower than local
disk. The per-file upload budget was calibrated against local disk with only ~2x
of margin, and `_upload_with_timeout` ABANDONS past it, so an unscaled budget
loses the snapshot rather than merely slowing it.

These tests pin the three halves of the fix: the budget scales for a mounted
root, it does NOT scale for anything else in the same snapshot, and the prebuilt
archive (a second full-tree read) is declined while a root is mounted.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any

import pytest

from runner.data.snapshot import jobs as snapshot_jobs
from runner.data.snapshot import main as snapshot_main
from runner.data.snapshot.models import SnapshotRequest

# ── the budget ───────────────────────────────────────────────────────


class TestUploadTimeoutSeconds:
    def test_a_local_read_is_unchanged(self) -> None:
        # The whole change has to be inert for every run that mounts nothing,
        # which is still the overwhelming majority of them.
        assert snapshot_main._upload_timeout_seconds(1024) == (
            snapshot_main._UPLOAD_TIMEOUT_FLOOR_SECONDS
        )
        assert snapshot_main._upload_timeout_seconds(
            1024, slow_read=False
        ) == snapshot_main._upload_timeout_seconds(1024)

    def test_a_mounted_read_scales_the_floor(self) -> None:
        # A small file runs on the floor alone, and the floor is what most files
        # get: at 5 MiB/s the throughput term only binds above ~1.5 GiB.
        assert snapshot_main._upload_timeout_seconds(1024, slow_read=True) == (
            snapshot_main._UPLOAD_TIMEOUT_FLOOR_SECONDS
            * snapshot_main._UPLOAD_MOUNT_SLOWDOWN_FACTOR
        )

    def test_a_mounted_read_scales_the_throughput_term_too(self) -> None:
        # Scaling only the floor would leave a multi-GB database on a budget that
        # assumes 5 MiB/s while its bytes arrive at ~1.1 — the exact file the lost
        # snapshot was measured on.
        four_gib = 4 * 1024 * 1024 * 1024
        local = snapshot_main._upload_timeout_seconds(four_gib)
        assert local > snapshot_main._UPLOAD_TIMEOUT_FLOOR_SECONDS  # not on the floor
        assert snapshot_main._upload_timeout_seconds(four_gib, slow_read=True) == (
            local * snapshot_main._UPLOAD_MOUNT_SLOWDOWN_FACTOR
        )

    def test_the_ceiling_keeps_the_retry_sequence_inside_the_deadline(self) -> None:
        # `_upload_single_file` spends up to _UPLOAD_MAX_RETRIES attempts of one
        # budget each, and the caller abandons the job at its polling deadline.
        # Unbounded, 3 x 1380s = 4140s against the 3300s default would be cut off
        # mid-sequence, losing the snapshot after burning the whole window.
        deadline = 3300.0
        ceiling = snapshot_main.slow_read_ceiling(deadline)
        assert ceiling is not None
        budget = snapshot_main._upload_timeout_seconds(
            1024, slow_read=True, ceiling=ceiling
        )
        assert budget * snapshot_main._UPLOAD_MAX_RETRIES <= deadline

    def test_the_ceiling_binds_only_when_it_is_lower(self) -> None:
        # A generous deadline must not shrink the scaled budget below what the
        # factor asks for.
        scaled = snapshot_main._upload_timeout_seconds(1024, slow_read=True)
        assert (
            snapshot_main._upload_timeout_seconds(1024, slow_read=True, ceiling=1e9)
            == scaled
        )

    def test_the_ceiling_never_drops_below_the_local_disk_budget(self) -> None:
        # A deadline short enough to push the ceiling under the unscaled budget
        # would make mounting a root TIGHTER than not mounting it, which inverts
        # the whole point of the scaling.
        unscaled = snapshot_main._upload_timeout_seconds(1024)
        assert (
            snapshot_main._upload_timeout_seconds(1024, slow_read=True, ceiling=1.0)
            == unscaled
        )

    def test_the_ceiling_leaves_a_local_disk_read_alone(self) -> None:
        # It bounds the inflation this adds, not the pre-existing baseline: a
        # large file's unscaled budget already exceeds a third of the deadline
        # and clamping it here would shrink budgets unrelated to mounting.
        four_gib = 4 * 1024 * 1024 * 1024
        assert snapshot_main._upload_timeout_seconds(
            four_gib, ceiling=1.0
        ) == snapshot_main._upload_timeout_seconds(four_gib)

    def test_no_deadline_means_no_ceiling(self) -> None:
        # What an agent older than the field sends. It also sends no
        # mounted_subsystems, so it never reaches the scaled path anyway.
        assert snapshot_main.slow_read_ceiling(None) is None
        assert snapshot_main.slow_read_ceiling(0) is None
        assert snapshot_main.slow_read_ceiling(-1) is None

    def test_the_factor_preserves_the_measured_margin(self) -> None:
        # 300s was chosen as ~2x the slowest successful single-file upload (143s).
        # The scaled floor has to keep that margin against the same worst case read
        # over 9p, or the fix does not cover the case it exists for.
        worst_case_over_9p = 143.0 * snapshot_main._UPLOAD_MOUNT_SLOWDOWN_FACTOR
        assert (
            snapshot_main._upload_timeout_seconds(0, slow_read=True)
            >= 2 * worst_case_over_9p
        )


# ── which files count as mounted ─────────────────────────────────────


class TestIsUnderRoots:
    def test_no_roots_matches_nothing(self) -> None:
        assert not snapshot_main._is_under_roots("/.apps_data/db", frozenset())

    def test_a_file_under_a_named_root_matches(self) -> None:
        assert snapshot_main._is_under_roots(
            "/.apps_data/svc/studio.db", frozenset({".apps_data"})
        )

    def test_the_other_half_of_the_same_snapshot_does_not(self) -> None:
        # The point of deciding per file rather than per snapshot: a run can mount
        # one half and download the other, and the downloaded half must keep the
        # tight budget that detects a genuinely wedged upload.
        assert not snapshot_main._is_under_roots(
            "/filesystem/report.docx", frozenset({".apps_data"})
        )

    def test_a_prefix_lookalike_does_not_match(self) -> None:
        assert not snapshot_main._is_under_roots(
            "/filesystem_backup/old.db", frozenset({"filesystem"})
        )

    def test_the_bare_root_itself_does_not_match(self) -> None:
        # `iter_paths` only ever yields files, so this cannot arise from the walk;
        # it is pinned so the `/<root>/` form is not "simplified" to a prefix test.
        assert not snapshot_main._is_under_roots(
            "/filesystem", frozenset({"filesystem"})
        )


# ── end to end through the handler ───────────────────────────────────


class _FakeObject:
    async def put(self, Body: bytes) -> None:  # noqa: N803 — boto3's own kwarg
        return None


class _FakeBucket:
    async def Object(self, key: str) -> _FakeObject:  # noqa: N802 — boto3 resource
        return _FakeObject()


class _FakeS3:
    async def __aenter__(self) -> _FakeS3:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    async def Bucket(self, name: str) -> _FakeBucket:  # noqa: N802 — boto3 resource
        return _FakeBucket()


class _FakeCoordinator:
    async def finish_actions(self) -> None:
        return None


class _Capture:
    """What the handler decided, per file and for the archive."""

    def __init__(self) -> None:
        self.slow_by_key: dict[str, bool] = {}
        self.archived: list[str] = []


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> _Capture:
    """Drive the real handler over a synthetic two-root tree.

    The walk is faked rather than written to `tmp_path` because the slow-read
    decision keys off the LOCAL path: it has to see `/filesystem/...` and
    `/.apps_data/...`, which no temp directory can produce. Nothing reads the
    files, since the uploader and the archive builder are both faked.
    """
    taken = _Capture()

    def _iter_paths(root_dir: str, arc_prefix: str, exclude_globs: Any = None) -> Any:
        yield Path(f"/{arc_prefix}/a.bin"), f"{arc_prefix}/a.bin"

    async def _fake_upload(
        _bucket: Any,
        local_path: str,
        s3_key: str,
        progress: Any = None,
        *,
        slow_read: bool = False,
        ceiling: float | None = None,
    ) -> int:
        taken.slow_by_key[s3_key] = slow_read
        return 1

    async def _fake_zip(_bucket: Any, _files: Any, prefix: str) -> None:
        taken.archived.append(prefix)

    monkeypatch.setattr(snapshot_main, "iter_paths", _iter_paths)
    monkeypatch.setattr(snapshot_main, "_upload_single_file", _fake_upload)
    monkeypatch.setattr(snapshot_main, "_upload_snapshot_zip", _fake_zip)
    monkeypatch.setattr(snapshot_main, "get_s3_client", lambda _creds: _FakeS3())
    monkeypatch.setattr(snapshot_main, "get_coordinator", lambda: _FakeCoordinator())
    return taken


def _slow(capture: _Capture, subsystem: str) -> bool:
    (value,) = [v for k, v in capture.slow_by_key.items() if subsystem in k]
    return value


async def test_only_the_mounted_root_gets_the_wider_budget(
    capture: _Capture,
) -> None:
    await snapshot_main.handle_snapshot_s3_files(
        snapshot_id="snap_test",
        snapshot_zip_enabled=False,
        mounted_subsystems=[".apps_data"],
    )
    assert _slow(capture, ".apps_data/a.bin") is True
    assert _slow(capture, "filesystem/a.bin") is False


async def test_nothing_is_slow_when_nothing_is_mounted(capture: _Capture) -> None:
    await snapshot_main.handle_snapshot_s3_files(
        snapshot_id="snap_test", snapshot_zip_enabled=False
    )
    assert capture.slow_by_key
    assert not any(capture.slow_by_key.values())


async def test_both_roots_scale_when_both_are_mounted(capture: _Capture) -> None:
    await snapshot_main.handle_snapshot_s3_files(
        snapshot_id="snap_test",
        snapshot_zip_enabled=False,
        mounted_subsystems=["filesystem", ".apps_data"],
    )
    assert all(capture.slow_by_key.values())


async def test_an_unrecognised_root_is_ignored(capture: _Capture) -> None:
    # The staging path a source mount uses lives outside both subsystems, and a
    # caller naming it must not widen the budget for the tree we do capture.
    await snapshot_main.handle_snapshot_s3_files(
        snapshot_id="snap_test",
        snapshot_zip_enabled=False,
        mounted_subsystems=["/.world_src/.apps_data"],
    )
    assert capture.slow_by_key
    assert not any(capture.slow_by_key.values())


async def test_the_archive_is_still_built_under_a_mount(capture: _Capture) -> None:
    # A mounted root makes the archive a second 9p read, which costs time
    # bounded by the caller's polling deadline. Omitting it would instead charge
    # every future grade of this trajectory a prefix LIST plus one GET per
    # object, which is worse exactly where grading reads dominate. A mount must
    # therefore not suppress it — only `snapshot_zip_enabled` may.
    await snapshot_main.handle_snapshot_s3_files(
        snapshot_id="snap_test",
        snapshot_zip_enabled=True,
        mounted_subsystems=[".apps_data"],
    )
    assert len(capture.archived) == 1


async def test_the_archive_still_runs_for_an_unmounted_snapshot(
    capture: _Capture,
) -> None:
    await snapshot_main.handle_snapshot_s3_files(
        snapshot_id="snap_test", snapshot_zip_enabled=True
    )
    assert len(capture.archived) == 1


async def test_the_world_toggle_still_suppresses_the_archive(
    capture: _Capture,
) -> None:
    # `snapshot_zip_enabled` remains the only thing that turns the archive off,
    # so a mount cannot silently change what a world asked for.
    await snapshot_main.handle_snapshot_s3_files(
        snapshot_id="snap_test",
        snapshot_zip_enabled=False,
        mounted_subsystems=[".apps_data"],
    )
    assert capture.archived == []


async def test_the_archive_upload_is_not_marked_slow_read(
    capture: _Capture,
) -> None:
    # The archive is read from a local tempfile whatever tree it was built from,
    # so it must not inherit the widened budget meant for 9p reads.
    import inspect

    src = inspect.getsource(snapshot_main._upload_snapshot_zip)
    assert "slow_read" not in src


# ── the wire ─────────────────────────────────────────────────────────


def test_the_request_model_defaults_to_no_mounts() -> None:
    assert SnapshotRequest().mounted_subsystems == []


def test_an_older_caller_payload_still_validates() -> None:
    # An agent built before this field omits it, and must capture as it does today.
    assert SnapshotRequest.model_validate({"format": "files"}).mounted_subsystems == []


def test_the_handler_accepts_mounted_subsystems() -> None:
    params = inspect.signature(snapshot_main.handle_snapshot_s3_files).parameters
    assert "mounted_subsystems" in params


def test_the_async_job_forwards_mounted_subsystems() -> None:
    src = inspect.getsource(snapshot_jobs.start_snapshot_job)
    assert "mounted_subsystems=request.mounted_subsystems" in src

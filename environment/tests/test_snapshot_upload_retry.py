"""Tests for snapshot upload retry logic.

Covers ``_is_retryable_upload_error``, ``_upload_single_file``, and
``_retry_failed_uploads`` in ``runner.data.snapshot.main``.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from loguru import logger

from runner.data.snapshot import main as snapshot_main

# ── Error factories ──────────────────────────────────────────────────


def _client_error(code: str) -> ClientError:
    return ClientError(
        error_response=cast(
            Any,
            {
                "Error": {"Code": code, "Message": f"simulated {code}"},
                "ResponseMetadata": {
                    "HTTPStatusCode": 500,
                    "RequestId": "req-test",
                    "HostId": "host-test",
                    "HTTPHeaders": {},
                    "RetryAttempts": 0,
                },
            },
        ),
        operation_name="PutObject",
    )


def _make_connection_reset() -> BaseException:
    return ConnectionResetError(54, "Connection reset by peer")


def _make_read_timeout() -> BaseException:
    return ReadTimeoutError(endpoint_url="https://s3.example")


def _make_connect_timeout() -> BaseException:
    return ConnectTimeoutError(endpoint_url="https://s3.example")


def _make_endpoint_unreachable() -> BaseException:
    return EndpointConnectionError(endpoint_url="https://s3.example")


def _make_connection_closed() -> BaseException:
    return ConnectionClosedError(endpoint_url="https://s3.example")


def _make_asyncio_timeout() -> BaseException:
    return TimeoutError()


# ── _is_retryable_upload_error ───────────────────────────────────────


class TestIsRetryableUploadError:
    """Pin the retryable set so accidental changes show up here."""

    @pytest.mark.parametrize(
        "make_exc",
        [
            pytest.param(_make_connection_reset, id="connection_reset"),
            pytest.param(_make_read_timeout, id="read_timeout"),
            pytest.param(_make_connect_timeout, id="connect_timeout"),
            pytest.param(_make_endpoint_unreachable, id="endpoint_unreachable"),
            pytest.param(_make_connection_closed, id="connection_closed"),
            pytest.param(_make_asyncio_timeout, id="asyncio_timeout"),
        ],
    )
    def test_transient_io_exceptions_are_retryable(self, make_exc: Any) -> None:
        assert snapshot_main._is_retryable_upload_error(make_exc())

    @pytest.mark.parametrize(
        "code",
        [
            "IncompleteBody",
            "RequestTimeout",
            "ServiceUnavailable",
            "SlowDown",
            "InternalError",
            "ThrottlingException",
        ],
    )
    def test_transient_s3_error_codes_are_retryable(self, code: str) -> None:
        assert snapshot_main._is_retryable_upload_error(_client_error(code))

    @pytest.mark.parametrize(
        "code",
        ["ExpiredToken", "ExpiredTokenException", "RequestExpired"],
    )
    def test_credential_expiry_is_retryable(self, code: str) -> None:
        """Expiry is transient because the lease is refreshable mid-harvest.

        The caller pushes a new lease to the running job while the upload is in
        flight, so a part that raced the swap succeeds on the retry. When these
        were permanent, one large file outliving its lease failed the entire
        bundle — and each snapshot-level retry re-uploaded every file against
        the same dead lease.
        """
        assert snapshot_main._is_retryable_upload_error(_client_error(code))

    @pytest.mark.parametrize(
        "code",
        ["AccessDenied", "NoSuchKey", "NoSuchBucket"],
    )
    def test_permanent_s3_error_codes_are_not_retryable(self, code: str) -> None:
        assert not snapshot_main._is_retryable_upload_error(_client_error(code))

    def test_lapsed_lease_is_retryable(self) -> None:
        """Not a ClientError: once credentials are refreshable, botocore fails
        the refresh locally and S3 is never asked, so classifying only the
        ExpiredToken error code would miss every occurrence."""
        from runner.utils.s3 import SnapshotCredentialsExpired

        assert snapshot_main._is_retryable_upload_error(
            SnapshotCredentialsExpired("lease gone")
        )

    def test_oserror_subclasses_are_not_retryable(self) -> None:
        """FileNotFoundError, PermissionError etc. must NOT be retried."""
        assert not snapshot_main._is_retryable_upload_error(FileNotFoundError("gone"))
        assert not snapshot_main._is_retryable_upload_error(PermissionError("denied"))

    def test_unrelated_exception_not_retryable(self) -> None:
        assert not snapshot_main._is_retryable_upload_error(TypeError("bug"))

    def test_broad_oserror_not_retryable(self) -> None:
        assert not snapshot_main._is_retryable_upload_error(OSError("nope"))

    def test_client_error_not_in_retryable_tuple(self) -> None:
        """ClientError is handled by the early-return branch, not the tuple."""
        assert ClientError not in snapshot_main._UPLOAD_RETRYABLE_EXCEPTIONS

    def test_oserror_not_in_retryable_tuple(self) -> None:
        assert OSError not in snapshot_main._UPLOAD_RETRYABLE_EXCEPTIONS


# ── _retry_failed_uploads ────────────────────────────────────────────


class _FakeObject:
    def __init__(self, size: int) -> None:
        self._size = size

    async def put(self, Body: bytes) -> None:
        pass


class _FakeBucket:
    async def Object(self, key: str) -> _FakeObject:
        return _FakeObject(0)


@pytest.fixture
def _patch_upload(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace _upload_single_file with a fake that always succeeds
    and records which keys were retried."""
    retried: list[str] = []

    async def _fake_upload(
        _bucket: Any,
        local_path: str,
        s3_key: str,
        *,
        slow_read: bool = False,
        ceiling: float | None = None,
    ) -> int:
        retried.append(s3_key)
        return 42

    monkeypatch.setattr(snapshot_main, "_upload_single_file", _fake_upload)
    return retried


@pytest.fixture
def _patch_upload_fail(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace _upload_single_file with a fake that always fails."""
    retried: list[str] = []

    async def _fake_upload(
        _bucket: Any,
        local_path: str,
        s3_key: str,
        *,
        slow_read: bool = False,
        ceiling: float | None = None,
    ) -> int:
        retried.append(s3_key)
        raise ConnectionResetError("still broken")

    monkeypatch.setattr(snapshot_main, "_upload_single_file", _fake_upload)
    return retried


class TestRetryFailedUploads:
    files_to_upload: list[tuple[str, str]] = [
        ("/tmp/a.txt", "prefix/a.txt"),
        ("/tmp/b.txt", "prefix/b.txt"),
        ("/tmp/c.txt", "prefix/c.txt"),
    ]

    @pytest.mark.asyncio
    async def test_only_transient_errors_are_retried(
        self, _patch_upload: list[str]
    ) -> None:
        """Transient errors get retried; permanent ones do not."""
        failed: list[tuple[int, BaseException]] = [
            (0, _make_connection_reset()),  # transient → retry
            (1, _client_error("AccessDenied")),  # permanent → skip
        ]
        sizes: list[int] = [100]

        with pytest.raises(RuntimeError, match="1 file.*failed after"):
            await snapshot_main._retry_failed_uploads(
                _FakeBucket(), self.files_to_upload, failed, sizes
            )

        # Only file 0 was retried (permanent AccessDenied never retried)
        assert _patch_upload == ["prefix/a.txt"]
        # File 0 succeeded on the first retry pass → its size appended
        assert 42 in sizes

    @pytest.mark.asyncio
    async def test_all_transient_succeed_on_retry(
        self, _patch_upload: list[str]
    ) -> None:
        failed: list[tuple[int, BaseException]] = [
            (0, _make_connection_reset()),
            (2, _make_read_timeout()),
        ]
        sizes: list[int] = [100]

        # Should not raise
        await snapshot_main._retry_failed_uploads(
            _FakeBucket(), self.files_to_upload, failed, sizes
        )

        assert set(_patch_upload) == {"prefix/a.txt", "prefix/c.txt"}
        assert sizes == [100, 42, 42]

    @pytest.mark.asyncio
    async def test_permanent_only_failures_raise_without_retry(
        self, _patch_upload: list[str]
    ) -> None:
        """When all failures are permanent, no retries are attempted."""
        failed: list[tuple[int, BaseException]] = [
            (0, _client_error("AccessDenied")),
            (1, FileNotFoundError("gone")),
        ]
        sizes: list[int] = []

        with pytest.raises(RuntimeError, match="2 file.*failed after"):
            await snapshot_main._retry_failed_uploads(
                _FakeBucket(), self.files_to_upload, failed, sizes
            )

        assert _patch_upload == []

    @pytest.mark.asyncio
    async def test_transient_retry_still_fails(
        self, _patch_upload_fail: list[str], fake_sleep: _SleepRecorder
    ) -> None:
        """A file that keeps failing transiently is retried every pass, then raised."""
        failed: list[tuple[int, BaseException]] = [
            (0, _make_connection_reset()),
        ]
        sizes: list[int] = []

        with pytest.raises(RuntimeError, match="1 file.*failed after"):
            await snapshot_main._retry_failed_uploads(
                _FakeBucket(), self.files_to_upload, failed, sizes
            )

        # Retried once per pass across all passes, then reported.
        assert (
            _patch_upload_fail == ["prefix/a.txt"] * snapshot_main._UPLOAD_RETRY_PASSES
        )
        assert sizes == []

    @pytest.mark.asyncio
    async def test_straggler_recovered_on_later_pass(
        self, monkeypatch: pytest.MonkeyPatch, fake_sleep: _SleepRecorder
    ) -> None:
        """A file that fails the first retry pass but succeeds on a later pass
        is recovered — the whole snapshot must NOT fail. This is the point of
        the multi-pass retry: transient stragglers among thousands of tiny
        files don't sink the bundle."""
        attempts: dict[str, int] = {}

        async def _flaky(
            _bucket: Any,
            _local_path: str,
            s3_key: str,
            *,
            slow_read: bool = False,
            ceiling: float | None = None,
        ) -> int:
            attempts[s3_key] = attempts.get(s3_key, 0) + 1
            if attempts[s3_key] < 2:  # fail first retry pass, succeed on the next
                raise ConnectionResetError("transient straggler")
            return 7

        monkeypatch.setattr(snapshot_main, "_upload_single_file", _flaky)

        failed: list[tuple[int, BaseException]] = [(0, _make_connection_reset())]
        sizes: list[int] = []

        # Must not raise — recovered on the second pass.
        await snapshot_main._retry_failed_uploads(
            _FakeBucket(), self.files_to_upload, failed, sizes
        )
        assert attempts["prefix/a.txt"] == 2
        assert sizes == [7]

    def test_retry_passes_is_multi_pass(self) -> None:
        """Contract: the orchestration retries transient failures more than once
        so a single bad pass can't fail the whole snapshot."""
        assert snapshot_main._UPLOAD_RETRY_PASSES >= 2

    @pytest.mark.asyncio
    async def test_time_budget_caps_further_passes(
        self,
        _patch_upload_fail: list[str],
        fake_sleep: _SleepRecorder,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A spent wall-clock budget stops further passes even when pass count
        remains — bounds worst-case duration for a genuinely wedged file
        (retryable TimeoutError) instead of multiplying it by the pass count."""
        monkeypatch.setattr(snapshot_main, "_UPLOAD_RETRY_TOTAL_BUDGET_S", 0.0)
        failed: list[tuple[int, BaseException]] = [(0, _make_connection_reset())]
        sizes: list[int] = []

        with pytest.raises(RuntimeError, match="1 file.*failed after"):
            await snapshot_main._retry_failed_uploads(
                _FakeBucket(), self.files_to_upload, failed, sizes
            )

        # Budget already spent → only the first retry pass runs, not all passes.
        assert _patch_upload_fail == ["prefix/a.txt"]

    @pytest.mark.asyncio
    async def test_empty_failed_list_is_noop(self, _patch_upload: list[str]) -> None:
        sizes: list[int] = [100]
        await snapshot_main._retry_failed_uploads(
            _FakeBucket(), self.files_to_upload, [], sizes
        )
        assert _patch_upload == []
        assert sizes == [100]


# ── _upload_single_file ──────────────────────────────────────────────


class _SleepRecorder:
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.calls.append(delay)


class _UploadScript:
    """Scripts the sequence of results for each upload attempt.

    ``None`` means success, a ``BaseException`` is raised.
    """

    def __init__(self, script: list[object], file_size: int = 100) -> None:
        self.script: list[object] = list(script)
        self.calls: int = 0
        self.file_size = file_size

    async def Object(self, key: str) -> _ScriptedObject:
        return _ScriptedObject(self)


class _ScriptedObject:
    def __init__(self, script: _UploadScript) -> None:
        self._script = script

    async def put(self, Body: bytes) -> None:
        idx = self._script.calls
        self._script.calls += 1
        if idx >= len(self._script.script):
            raise AssertionError(f"_UploadScript exhausted at attempt {idx + 1}")
        item = self._script.script[idx]
        if isinstance(item, BaseException):
            raise item


@pytest.fixture
def fake_sleep(monkeypatch: pytest.MonkeyPatch) -> _SleepRecorder:
    recorder = _SleepRecorder()
    monkeypatch.setattr(snapshot_main.asyncio, "sleep", recorder)
    return recorder


@pytest.fixture
def _patch_file_size(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make os.path.getsize return a small size so we use the PUT path."""
    monkeypatch.setattr(snapshot_main.os.path, "getsize", lambda _: 100)


@pytest.fixture
def _patch_aiofiles_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub aiofiles.open to return bytes without touching disk."""

    class _FakeFile:
        async def read(self) -> bytes:
            return b"x" * 100

        async def __aenter__(self) -> _FakeFile:
            return self

        async def __aexit__(self, *_: object) -> None:
            pass

    monkeypatch.setattr(snapshot_main.aiofiles, "open", lambda *a, **kw: _FakeFile())


class TestUploadSingleFile:
    @pytest.mark.asyncio
    async def test_success_no_retry(
        self,
        fake_sleep: _SleepRecorder,
        _patch_file_size: None,
        _patch_aiofiles_read: None,
    ) -> None:
        script = _UploadScript([None])
        result = await snapshot_main._upload_single_file(
            script, "/tmp/test.txt", "prefix/test.txt"
        )
        assert result == 100
        assert script.calls == 1
        assert fake_sleep.calls == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "make_exc",
        [
            pytest.param(_make_connection_reset, id="connection_reset"),
            pytest.param(_make_read_timeout, id="read_timeout"),
            pytest.param(_make_connect_timeout, id="connect_timeout"),
            pytest.param(_make_endpoint_unreachable, id="endpoint_unreachable"),
            pytest.param(_make_connection_closed, id="connection_closed"),
            pytest.param(_make_asyncio_timeout, id="asyncio_timeout"),
        ],
    )
    async def test_transient_error_retried_then_succeeds(
        self,
        make_exc: Any,
        fake_sleep: _SleepRecorder,
        _patch_file_size: None,
        _patch_aiofiles_read: None,
    ) -> None:
        script = _UploadScript([make_exc(), None])
        result = await snapshot_main._upload_single_file(
            script, "/tmp/test.txt", "prefix/test.txt"
        )
        assert result == 100
        assert script.calls == 2
        assert len(fake_sleep.calls) == 1

    @pytest.mark.asyncio
    async def test_retryable_s3_code_retried(
        self,
        fake_sleep: _SleepRecorder,
        _patch_file_size: None,
        _patch_aiofiles_read: None,
    ) -> None:
        script = _UploadScript([_client_error("SlowDown"), None])
        result = await snapshot_main._upload_single_file(
            script, "/tmp/test.txt", "prefix/test.txt"
        )
        assert result == 100
        assert script.calls == 2

    @pytest.mark.asyncio
    async def test_permanent_client_error_not_retried(
        self,
        fake_sleep: _SleepRecorder,
        _patch_file_size: None,
        _patch_aiofiles_read: None,
    ) -> None:
        script = _UploadScript([_client_error("AccessDenied")])
        with pytest.raises(ClientError):
            await snapshot_main._upload_single_file(
                script, "/tmp/test.txt", "prefix/test.txt"
            )
        assert script.calls == 1
        assert fake_sleep.calls == []

    @pytest.mark.asyncio
    async def test_unrelated_exception_not_retried(
        self,
        fake_sleep: _SleepRecorder,
        _patch_file_size: None,
        _patch_aiofiles_read: None,
    ) -> None:
        script = _UploadScript([TypeError("bug")])
        with pytest.raises(TypeError, match="bug"):
            await snapshot_main._upload_single_file(
                script, "/tmp/test.txt", "prefix/test.txt"
            )
        assert script.calls == 1
        assert fake_sleep.calls == []

    @pytest.mark.asyncio
    async def test_retry_exhaustion(
        self,
        fake_sleep: _SleepRecorder,
        _patch_file_size: None,
        _patch_aiofiles_read: None,
    ) -> None:
        script = _UploadScript(
            [_make_connection_reset()] * snapshot_main._UPLOAD_MAX_RETRIES
        )
        with pytest.raises(ConnectionResetError):
            await snapshot_main._upload_single_file(
                script, "/tmp/test.txt", "prefix/test.txt"
            )
        assert script.calls == snapshot_main._UPLOAD_MAX_RETRIES
        assert len(fake_sleep.calls) == snapshot_main._UPLOAD_MAX_RETRIES - 1

    @pytest.mark.asyncio
    async def test_backoff_bounded(
        self,
        fake_sleep: _SleepRecorder,
        _patch_file_size: None,
        _patch_aiofiles_read: None,
    ) -> None:
        script = _UploadScript(
            [_make_connection_reset()] * snapshot_main._UPLOAD_MAX_RETRIES
        )
        with pytest.raises(ConnectionResetError):
            await snapshot_main._upload_single_file(
                script, "/tmp/test.txt", "prefix/test.txt"
            )
        # Full-jitter: attempt=0 → [0, 4], attempt=1 → [0, 8]
        assert 0.0 <= fake_sleep.calls[0] <= 4.0
        assert 0.0 <= fake_sleep.calls[1] <= 8.0


# ── per-attempt upload timeout ───────────────────────────────────────


class _HangingObject:
    """An upload that never completes, optionally ignoring cancellation.

    Hangs on an ``Event`` rather than ``asyncio.sleep`` because the
    ``fake_sleep`` fixture patches the real ``asyncio.sleep`` — a sleep-based
    hang would return instantly under it. ``swallow_cancel`` models the case
    the timeout exists for: a stall parked somewhere that does not unwind
    promptly on cancel. ``asyncio.wait_for`` would block on that cancellation;
    ``_upload_with_timeout`` must not.
    """

    def __init__(self, swallow_cancel: bool = False) -> None:
        self.swallow_cancel = swallow_cancel
        self.started = 0
        self._release = asyncio.Event()

    def release(self) -> None:
        """Let any swallowed-cancel hang finish so the loop closes cleanly."""
        self._release.set()

    async def put(self, Body: bytes) -> None:
        await self._hang()

    async def upload_file(self, local_path: str, Config: object = None) -> None:
        await self._hang()

    async def _hang(self) -> None:
        self.started += 1
        try:
            await self._release.wait()
        except asyncio.CancelledError:
            if not self.swallow_cancel:
                raise
            await self._release.wait()  # keep hanging through the cancel


class _HangingBucket:
    def __init__(self, obj: _HangingObject) -> None:
        self._obj = obj

    async def Object(self, key: str) -> _HangingObject:
        return self._obj


async def _drain_abandoned() -> None:
    """Await abandoned upload tasks. Uses asyncio.wait, not sleep (see above)."""
    tasks = set(snapshot_main._abandoned_uploads)
    if tasks:
        await asyncio.wait(tasks, timeout=1.0)


class TestUploadTimeoutSeconds:
    def test_shipped_floor_default(self) -> None:
        """Pin the tuned default so an accidental retune surfaces here.

        300s is ~2x the slowest observed successful single-file upload (143s for
        a 1.4 GiB DB under 7-way contention). The original 120s killed
        72-468 MiB files three times over on sandboxes that then completed on
        the retry pass. Override per-deployment with
        SNAPSHOT_UPLOAD_TIMEOUT_FLOOR_SECONDS.
        """
        assert snapshot_main._UPLOAD_TIMEOUT_FLOOR_SECONDS == 300.0

    def test_floor_covers_the_slowest_observed_success(self) -> None:
        """Every file below the size crossover must outlast a 143s upload."""
        slowest_observed_success = 143.0
        for mib in (1, 72, 107, 468, 1416):
            assert (
                snapshot_main._upload_timeout_seconds(mib * 1024 * 1024)
                > slowest_observed_success
            )

    def test_small_file_gets_the_floor(self) -> None:
        assert (
            snapshot_main._upload_timeout_seconds(1024)
            == snapshot_main._UPLOAD_TIMEOUT_FLOOR_SECONDS
        )

    def test_large_file_scales_with_size(self) -> None:
        """A 4 GiB DB gets far more than a small file's allowance."""
        four_gib = 4 * 1024 * 1024 * 1024
        expected = 4096 / snapshot_main._UPLOAD_MIN_THROUGHPUT_MIB_S
        assert snapshot_main._upload_timeout_seconds(four_gib) == pytest.approx(
            expected
        )
        assert snapshot_main._upload_timeout_seconds(
            four_gib
        ) > snapshot_main._upload_timeout_seconds(1024)

    def test_monotonic_in_size(self) -> None:
        sizes = [0, 10 * 1024**2, 100 * 1024**2, 1024**3, 8 * 1024**3]
        timeouts = [snapshot_main._upload_timeout_seconds(s) for s in sizes]
        assert timeouts == sorted(timeouts)


class TestUploadWithTimeout:
    @pytest.mark.asyncio
    async def test_returns_on_success(self) -> None:
        async def _ok() -> None:
            return None

        await snapshot_main._upload_with_timeout(_ok(), 5.0, "prefix/a")

    @pytest.mark.asyncio
    async def test_propagates_upload_error(self) -> None:
        async def _boom() -> None:
            raise ConnectionResetError("reset")

        with pytest.raises(ConnectionResetError):
            await snapshot_main._upload_with_timeout(_boom(), 5.0, "prefix/a")

    @pytest.mark.asyncio
    async def test_raises_timeout_and_names_the_key(self) -> None:
        obj = _HangingObject()
        with pytest.raises(TimeoutError, match="prefix/big.db"):
            await snapshot_main._upload_with_timeout(
                obj.put(b"x"), 0.01, "prefix/big.db"
            )

    @pytest.mark.asyncio
    async def test_escapes_even_when_cancellation_is_ignored(self) -> None:
        """The whole point: we must not wait for the cancel to land."""
        obj = _HangingObject(swallow_cancel=True)

        # Outer wait_for is the test's own safety net: if _upload_with_timeout
        # awaited the cancellation (as asyncio.wait_for does), it would hang and
        # this would fail on the outer 5s instead of the inner 0.01s.
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                snapshot_main._upload_with_timeout(obj.put(b"x"), 0.01, "prefix/a"),
                timeout=5.0,
            )
        obj.release()
        await _drain_abandoned()

    @pytest.mark.asyncio
    async def test_abandoned_task_is_strongly_referenced(self) -> None:
        obj = _HangingObject()
        before = len(snapshot_main._abandoned_uploads)

        with pytest.raises(TimeoutError):
            await snapshot_main._upload_with_timeout(obj.put(b"x"), 0.01, "prefix/a")
        assert len(snapshot_main._abandoned_uploads) == before + 1

        # The reference is released once the task finishes unwinding.
        await _drain_abandoned()
        assert len(snapshot_main._abandoned_uploads) == before

    @pytest.mark.asyncio
    async def test_cancelling_the_waiter_cancels_the_upload(self) -> None:
        """asyncio.wait leaves the awaited task running when the waiter is
        cancelled — an in-flight upload must not outlive the snapshot job."""
        obj = _HangingObject()
        inflight = asyncio.ensure_future(
            snapshot_main._upload_with_timeout(obj.put(b"x"), 60.0, "prefix/a")
        )
        # Let it reach the asyncio.wait, then cancel the waiter itself.
        await asyncio.wait({inflight}, timeout=0.05)
        inflight.cancel()
        with pytest.raises(asyncio.CancelledError):
            await inflight

        # The inner upload was cancelled too, and tracked while it unwinds.
        await _drain_abandoned()
        assert snapshot_main._abandoned_uploads == set()


class TestUploadSingleFileTimeout:
    @pytest.mark.asyncio
    async def test_hanging_upload_times_out_and_retries(
        self,
        fake_sleep: _SleepRecorder,
        _patch_file_size: None,
        _patch_aiofiles_read: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A stalled upload is cut loose and retried, not awaited forever."""
        monkeypatch.setattr(snapshot_main, "_UPLOAD_TIMEOUT_FLOOR_SECONDS", 0.01)
        obj = _HangingObject()

        with pytest.raises(TimeoutError):
            await snapshot_main._upload_single_file(
                _HangingBucket(obj), "/tmp/test.txt", "prefix/test.txt"
            )

        # Every attempt was made, and each one gave up on its own budget.
        assert obj.started == snapshot_main._UPLOAD_MAX_RETRIES
        assert len(fake_sleep.calls) == snapshot_main._UPLOAD_MAX_RETRIES - 1

    @pytest.mark.asyncio
    async def test_uncancellable_hang_still_gives_up(
        self,
        fake_sleep: _SleepRecorder,
        _patch_file_size: None,
        _patch_aiofiles_read: None,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(snapshot_main, "_UPLOAD_TIMEOUT_FLOOR_SECONDS", 0.01)
        obj = _HangingObject(swallow_cancel=True)

        with pytest.raises(TimeoutError):
            await asyncio.wait_for(
                snapshot_main._upload_single_file(
                    _HangingBucket(obj), "/tmp/test.txt", "prefix/test.txt"
                ),
                timeout=10.0,
            )
        assert obj.started == snapshot_main._UPLOAD_MAX_RETRIES
        obj.release()
        await _drain_abandoned()

    @pytest.mark.asyncio
    async def test_timeout_is_retryable(self) -> None:
        """The retry loop must treat our TimeoutError as transient."""
        assert snapshot_main._is_retryable_upload_error(TimeoutError("too slow"))


# ── _log_upload_progress ─────────────────────────────────────────────


class TestLogUploadProgress:
    @staticmethod
    def _progress(**sent: int) -> Any:
        progress = snapshot_main._UploadProgress()
        for key, n in sent.items():
            advance = progress.restart(key)
            if n:
                advance(n)
        return progress

    @pytest.mark.asyncio
    async def _drain(self, progress: Any, total: int) -> list[str]:
        messages: list[str] = []
        handler_id = logger.add(lambda m: messages.append(m), level="INFO")
        try:
            task = asyncio.create_task(
                snapshot_main._log_upload_progress(progress, total=total, interval=0.01)
            )
            await asyncio.sleep(0.05)
            task.cancel()
        finally:
            logger.remove(handler_id)
        return messages

    @pytest.mark.asyncio
    async def test_reports_outstanding_keys(self) -> None:
        progress = snapshot_main._UploadProgress()
        progress.restart("prefix/big.db")
        messages = await self._drain(progress, total=3)

        assert any("prefix/big.db" in m for m in messages)
        assert any("2/3 uploaded" in m for m in messages)

    @pytest.mark.asyncio
    async def test_reports_throughput_so_a_wedge_is_distinguishable(self) -> None:
        """Elapsed time alone cannot separate a long hang from a long crawl —
        both just print a growing age. The byte counter is what makes
        "0.0 MiB/s" mean "wedged"."""
        messages = await self._drain(
            self._progress(**{"prefix/big.db": 64 * 1024 * 1024}), total=3
        )

        assert any("64 MiB" in m for m in messages)
        assert any("MiB/s" in m for m in messages)

    @pytest.mark.asyncio
    async def test_wedged_upload_reports_zero_throughput(self) -> None:
        messages = await self._drain(self._progress(**{"prefix/stuck.db": 0}), total=2)

        assert any("0 MiB, 0.0 MiB/s" in m for m in messages)

    @pytest.mark.asyncio
    async def test_silent_when_nothing_is_pending(self) -> None:
        messages = await self._drain(snapshot_main._UploadProgress(), total=3)

        assert messages == []


class TestUploadProgressRestart:
    """A retry re-sends the file from byte zero.

    Carrying the previous attempt's bytes forward would report throughput that
    was never achieved, and would let a wedged retry keep showing a healthy
    MiB/s off the back of what the failed attempt managed — defeating the one
    signal the watchdog exists to give, on exactly the expiry retries this
    change made retryable.
    """

    def test_restart_zeroes_bytes_from_the_previous_attempt(self) -> None:
        progress = snapshot_main._UploadProgress()
        advance = progress.restart("k")
        advance(500 * 1024 * 1024)

        progress.restart("k")

        [(_key, _age, sent)] = progress.oldest(5)
        assert sent == 0

    def test_restart_also_resets_the_clock(self) -> None:
        """Age must reset too: a stale start time inflates the denominator and
        turns a wedged retry into a plausible-looking slow one."""
        progress = snapshot_main._UploadProgress()
        progress.restart("k")
        progress._started_at["k"] -= 3600.0  # pretend the first attempt ran an hour

        progress.restart("k")

        [(_key, age, _sent)] = progress.oldest(5)
        assert age < 1.0

    def test_a_straggler_from_an_abandoned_attempt_is_ignored(self) -> None:
        """A timed-out upload is abandoned, not awaited, so its part uploaders
        keep firing their callback afterwards — measured at five further calls
        against a real multipart upload. Those bytes must not land on the retry
        that has since started, or restart buys nothing."""
        progress = snapshot_main._UploadProgress()
        stale_sink = progress.restart("k")
        stale_sink(10 * 1024 * 1024)

        fresh_sink = progress.restart("k")  # the retry begins
        stale_sink(500 * 1024 * 1024)  # the abandoned upload catches up
        fresh_sink(1 * 1024 * 1024)

        [(_key, _age, sent)] = progress.oldest(5)
        assert sent == 1 * 1024 * 1024

    def test_a_straggler_cannot_resurrect_a_finished_file(self) -> None:
        progress = snapshot_main._UploadProgress()
        stale_sink = progress.restart("k")
        progress.finish("k")

        stale_sink(64 * 1024 * 1024)

        assert progress.outstanding() == 0
        assert progress.oldest(5) == []

    def test_finish_drops_the_file_entirely(self) -> None:
        progress = snapshot_main._UploadProgress()
        progress.restart("k")
        progress.finish("k")
        assert progress.outstanding() == 0
        assert progress.oldest(5) == []

    @pytest.mark.asyncio
    async def test_each_upload_attempt_restarts_progress(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_sleep: _SleepRecorder,
        _patch_file_size: None,
        _patch_aiofiles_read: None,
    ) -> None:
        """The wiring, not just the primitive: every attempt inside
        _upload_single_file must reset before it starts sending."""
        progress = snapshot_main._UploadProgress()
        restarts: list[str] = []
        original_restart = progress.restart

        def _record(key: str) -> None:
            restarts.append(key)
            original_restart(key)

        monkeypatch.setattr(progress, "restart", _record)
        script = _UploadScript(
            [_client_error("SlowDown"), _client_error("SlowDown"), None]
        )

        await snapshot_main._upload_single_file(
            script, "/tmp/x", "prefix/x", progress=progress
        )

        assert restarts == ["prefix/x"] * 3, "one restart per attempt"


# ── _transfer_config_for ─────────────────────────────────────────────


class TestTransferConfigForSize:
    """aioboto3's upload_file is upload_fileobj: it reads forward in
    io_chunksize slices and queues multipart_chunksize parts. Read size drives
    thread-pool churn and queue depth drives memory — neither is what the old
    single config was tuned for."""

    @pytest.mark.parametrize("size_gib", [0.05, 1, 4, 18, 40])
    def test_read_size_cuts_thread_hops_at_every_size(self, size_gib: float) -> None:
        size = int(size_gib * 1024**3)
        cfg = snapshot_main._transfer_config_for(size)
        assert cfg.io_chunksize == 4 * 1024 * 1024
        at_default = -(-size // (256 * 1024))
        reads = -(-size // cfg.io_chunksize)
        assert reads * 16 <= at_default + 16, "~16x fewer aiofiles hops"

    @pytest.mark.parametrize("size_gib", [0.05, 1, 4, 18, 40])
    def test_bytes_in_flight_per_file_are_bounded(self, size_gib: float) -> None:
        """Counting only the queue understates this ~2x.

        `upload_fileobj`'s uploaders pop each part body OFF the queue and hold
        it for the duration of the PUT, so the real figure is the queue plus
        one part per uploader. Measured: 8 concurrent 1.2 GB uploads peak at
        3198 MB RSS, which matches this arithmetic and not the queue alone.
        """
        cfg = snapshot_main._transfer_config_for(int(size_gib * 1024**3))
        queued = cfg.max_io_queue_size * cfg.multipart_chunksize
        in_flight = queued + cfg.max_request_concurrency * cfg.multipart_chunksize
        assert in_flight <= 400 * 1024 * 1024
        # 8 is the file-level gate on _upload_single_file.
        assert 8 * in_flight <= 4 * 1024**3, "worst case across the 8-file gate"

    def test_the_queue_no_longer_dominates_the_footprint(self) -> None:
        """main left max_io_queue at 100, so the queue alone was 2 GiB/file and
        could reach 17.6 GiB across the gate — bounded only by a fast link
        keeping the reader from filling it."""
        cfg = snapshot_main._transfer_config_for(4 * 1024**3)
        queued = cfg.max_io_queue_size * cfg.multipart_chunksize
        uploaders = cfg.max_request_concurrency * cfg.multipart_chunksize
        assert queued <= uploaders, "the queue must not be the dominant term"

    @pytest.mark.parametrize("size_gib", [0.05, 1, 18, 40])
    def test_part_size_is_left_alone_at_realistic_sizes(self, size_gib: float) -> None:
        """Growing parts cost ~1 GB of RSS on a 1.2 GB upload and bought no
        throughput, so the long-standing 20 MiB is retained."""
        cfg = snapshot_main._transfer_config_for(int(size_gib * 1024**3))
        assert cfg.multipart_chunksize == 20 * 1024 * 1024

    def test_part_size_grows_only_to_respect_the_s3_part_ceiling(self) -> None:
        """A correctness guard, not a knob: S3 refuses >10,000 parts."""
        huge = 400 * 1024**3  # past what 20 MiB parts can express
        cfg = snapshot_main._transfer_config_for(huge)
        assert cfg.multipart_chunksize > 20 * 1024 * 1024
        assert -(-huge // cfg.multipart_chunksize) <= snapshot_main._S3_MAX_PARTS

    @pytest.mark.parametrize("size_gib", [0.05, 1, 18, 40, 400])
    def test_no_size_ever_exceeds_the_part_ceiling(self, size_gib: float) -> None:
        size = int(size_gib * 1024**3)
        cfg = snapshot_main._transfer_config_for(size)
        assert -(-size // cfg.multipart_chunksize) <= snapshot_main._S3_MAX_PARTS

    def test_queue_is_never_shallower_than_the_uploader_count(self) -> None:
        """A queue shallower than the uploaders starves them between parts."""
        cfg = snapshot_main._transfer_config_for(2 * 1024**3)
        assert cfg.max_io_queue_size >= cfg.max_request_concurrency

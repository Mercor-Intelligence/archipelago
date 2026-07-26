"""Tests for snapshot upload retry logic.

Covers ``_is_retryable_upload_error``, ``_upload_single_file``, and
``_retry_failed_uploads`` in ``runner.data.snapshot.main``.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

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
        ["AccessDenied", "NoSuchKey", "NoSuchBucket"],
    )
    def test_permanent_s3_error_codes_are_not_retryable(self, code: str) -> None:
        assert not snapshot_main._is_retryable_upload_error(_client_error(code))

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

    async def _fake_upload(_bucket: Any, local_path: str, s3_key: str) -> int:
        retried.append(s3_key)
        return 42

    monkeypatch.setattr(snapshot_main, "_upload_single_file", _fake_upload)
    return retried


@pytest.fixture
def _patch_upload_fail(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replace _upload_single_file with a fake that always fails."""
    retried: list[str] = []

    async def _fake_upload(_bucket: Any, local_path: str, s3_key: str) -> int:
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

        with pytest.raises(RuntimeError, match="1 file.*failed after retry"):
            await snapshot_main._retry_failed_uploads(
                _FakeBucket(), self.files_to_upload, failed, sizes
            )

        # Only file 0 was retried
        assert _patch_upload == ["prefix/a.txt"]
        # File 0 succeeded on retry → its size appended
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

        with pytest.raises(RuntimeError, match="2 file.*failed after retry"):
            await snapshot_main._retry_failed_uploads(
                _FakeBucket(), self.files_to_upload, failed, sizes
            )

        assert _patch_upload == []

    @pytest.mark.asyncio
    async def test_transient_retry_still_fails(
        self, _patch_upload_fail: list[str]
    ) -> None:
        """Transient errors that fail again on retry are reported."""
        failed: list[tuple[int, BaseException]] = [
            (0, _make_connection_reset()),
        ]
        sizes: list[int] = []

        with pytest.raises(RuntimeError, match="1 file.*failed after retry"):
            await snapshot_main._retry_failed_uploads(
                _FakeBucket(), self.files_to_upload, failed, sizes
            )

        assert _patch_upload_fail == ["prefix/a.txt"]
        assert sizes == []

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

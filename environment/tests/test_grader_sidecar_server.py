"""Tests for runner/grader_sidecar_server.py: one collect, over a real socket and inputs stream."""

from __future__ import annotations

import contextlib
import http.client
import http.server
import importlib
import io
import json
import os
import socket
import sys
import tarfile
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from runner import grader_sidecar_server as server

# The engine reads its credentials at import, as the real one reads its settings, and logs one.
FAKE_COLLECT = """
import json, os, sys
KEY = os.environ.get("OPENAI_API_KEY")
if __name__ == "__main__":
    argv = sys.argv[1:]
    final = open(argv[argv.index("--final-snapshot") + 1]).read()
    print("collect logged this with", KEY, file=sys.stderr)
    result = {
        "relay_prompts": [{"verifier_id": "v"}],
        "final": final,
        "key": KEY,
        "other": os.environ.get("SIDECAR_TEST_OTHER"),
        "inherited": os.environ.get("ANTHROPIC_API_KEY"),
        "base_url": os.environ.get("OPENAI_BASE_URL"),
    }
    json.dump(result, open(argv[argv.index("--output") + 1], "w"))
"""
MANIFEST = {
    "version": 1,
    "args": [
        "--final-snapshot",
        "{grade_dir}/final.zip",
        "--output",
        "{grade_dir}/result.json",
    ],
}
CREDENTIALS = json.dumps(
    {
        "OPENAI_API_KEY": "from-hosted-envs",
        "OPENAI_BASE_URL": "https://api.example.com/v1",
        "SIDECAR_TEST_OTHER": "not-allowed",
    }
)


def _tar(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for name, data in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _inputs(manifest: dict[str, Any] = MANIFEST) -> bytes:
    return _tar(
        {"manifest.json": json.dumps(manifest).encode(), "final.zip": b"the final tree"}
    )


class _Runner:
    """The sandbox's env runner as the sidecar reaches it: answers POSTs with ``body``."""

    def __init__(
        self,
        status: int = 200,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.requests: list[tuple[dict[str, str], bytes]] = []
        runner, reply = self, _inputs() if body is None else body

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers["Content-Length"])
                runner.requests.append((dict(self.headers), self.rfile.read(length)))
                self.send_response(status)
                sent = {"Content-Length": str(len(reply)), **(headers or {})}
                for name, value in sent.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        port = self._server.server_address[1]
        self.url = f"http://127.0.0.1:{port}{server.INPUTS_ROUTE}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture(autouse=True)
def runner_host(monkeypatch: pytest.MonkeyPatch) -> None:
    """The runner these tests serve, on this host rather than Modal's ``main``."""
    monkeypatch.setenv(server.RUNNER_HOST_ENV, "127.0.0.1")


@pytest.fixture
def engine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The engine's collect CLI and some broken ones, on the path; env and imports restored."""
    root = tmp_path / "engine"
    root.mkdir()
    (root / "fake_collect.py").write_text(FAKE_COLLECT)
    (root / "fake_fail.py").write_text("raise SystemExit(3)\n")
    (root / "fake_crash.py").write_text("raise ValueError('engine bug')\n")
    (root / "fake_clean.py").write_text("raise SystemExit(0)\n")
    (root / "fake_silent.py").write_text("")
    (root / "fake_sleep.py").write_text("import time\ntime.sleep(5)\n")
    (root / "fake_list.py").write_text(
        "import sys\nopen(sys.argv[sys.argv.index('--output') + 1], 'w').write('[]')\n"
    )
    monkeypatch.syspath_prepend(str(root))
    monkeypatch.setattr(server, "ENGINE_MODULE", "fake_collect")
    for key in (*server.CREDENTIAL_KEYS, "SIDECAR_TEST_OTHER"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "inherited-by-the-sidecar")
    monkeypatch.delitem(sys.modules, "fake_collect", raising=False)


@pytest.fixture
def runner() -> Iterator[_Runner]:
    runner = _Runner()
    yield runner
    runner.close()


def _request(url: str, **extra: Any) -> bytes:
    return json.dumps(
        {
            "url": url,
            "headers": {"X-Studio-Signature": "sig"},
            "body": "{}",
            "credentials_json": CREDENTIALS,
            "timeout_s": 5,
            "engine_timeout_s": 5,
            **extra,
        }
    ).encode()


def _refused(reason: str, body: bytes, work: Path) -> str:
    """Why one collect against a runner answering ``body`` is refused, asserting ``reason``."""
    served = _Runner(body=body)
    try:
        with pytest.raises(server.Refused) as refused:
            server.OneCollect(work).run(_request(served.url))
    finally:
        served.close()
    assert refused.value.reason == reason
    return str(refused.value)


@pytest.mark.usefixtures("engine")
def test_one_collect_runs_what_the_runner_prepared(
    runner: _Runner, tmp_path: Path
) -> None:
    work = tmp_path / "work"
    work.mkdir()
    collect = server.OneCollect(work)
    # Loaded before the credentials arrive, as main() loads the engine while the agent works.
    assert importlib.import_module("fake_collect").KEY is None

    result, log = collect.run(_request(runner.url))

    assert result == {
        "relay_prompts": [{"verifier_id": "v"}],
        "final": "the final tree",
        "key": "from-hosted-envs",
        "other": None,
        "inherited": None,
        "base_url": "https://api.example.com/v1",
    }, "the engine imports again under the allowed credentials, and only those"
    assert "collect logged this with from-hosted-envs" in log
    assert collect.mask(log).endswith("collect logged this with ***\n")
    assert collect.mask(result)["base_url"] == "https://api.example.com/v1", "no secret"
    assert collect.done
    assert sorted(p.name for p in work.iterdir()) == ["final.zip", "result.json"]
    ((headers, body),) = runner.requests
    assert headers["X-Studio-Signature"] == "sig" and body == b"{}"


@pytest.mark.usefixtures("engine")
def test_one_collect_is_all_a_grader_runs(runner: _Runner, tmp_path: Path) -> None:
    collect = server.OneCollect(tmp_path)
    collect.run(_request(runner.url))

    with pytest.raises(server.Refused, match="already collected") as refused:
        collect.run(_request(runner.url))
    assert refused.value.reason == "spent"


@pytest.mark.parametrize(
    "raw",
    [b"not json", b"[]", json.dumps({"url": "u"}).encode()],
    ids=["not-json", "not-an-object", "missing-fields"],
)
def test_a_malformed_request_is_refused(raw: bytes, tmp_path: Path) -> None:
    with pytest.raises(server.Refused) as refused:
        server.OneCollect(tmp_path).run(raw)
    assert refused.value.reason == "request"


@pytest.mark.parametrize("deadline", [None, 0, -1], ids=["absent", "zero", "negative"])
def test_a_collect_without_a_deadline_is_refused(
    deadline: float | None, tmp_path: Path
) -> None:
    raw = json.loads(_request("http://127.0.0.1:9"))
    if deadline is None:
        del raw["engine_timeout_s"]
    else:
        raw["engine_timeout_s"] = deadline

    with pytest.raises(server.Refused) as refused:
        server.OneCollect(tmp_path).run(json.dumps(raw).encode())

    assert refused.value.reason == "request"


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://elsewhere:8080/grade/relay/inputs",
        "http://127.0.0.1:8080/somewhere/else",
        "http://127.0.0.1:8080/grade/relay/inputs?next=/admin",
        "https://127.0.0.1:8080/grade/relay/inputs",
        "file:///etc/passwd",
    ],
    ids=["metadata", "another-host", "another-path", "query", "https", "file"],
)
def test_the_sidecar_fetches_only_its_runners_inputs(url: str, tmp_path: Path) -> None:
    with pytest.raises(server.Refused, match="is not http://127.0.0.1") as refused:
        server.fetch_inputs(url, {}, b"{}", tmp_path, 5)
    assert refused.value.reason == "request"


@pytest.mark.parametrize(
    ("reply", "reason", "error"),
    [
        ({"status": 409, "body": b"named capture"}, "inputs", "HTTP 409"),
        ({"status": 404, "body": b"Not Found"}, "unsupported", "HTTP 404"),
        ({"body": _tar({"final.zip": b""})}, "inputs", "no manifest"),
        (
            {"body": _inputs({**MANIFEST, "version": 2})},
            "unsupported",
            "version 2, not 1",
        ),
        ({"body": b"not a tar"}, "inputs", "inputs unreadable"),
        (
            {"body": b"short", "headers": {"Content-Length": "4096"}},
            "inputs",
            "inputs unreadable",
        ),
        (
            {"status": 302, "body": b"", "headers": {"Location": "http://elsewhere/"}},
            "inputs",
            "HTTP 302",
        ),
    ],
    ids=[
        "refused",
        "no-route",
        "no-manifest",
        "newer-version",
        "not-a-tar",
        "cut-short",
        "redirect",
    ],
)
def test_inputs_the_runner_did_not_prepare_are_refused(
    reply: dict[str, Any], reason: str, error: str, tmp_path: Path
) -> None:
    runner = _Runner(**reply)
    try:
        with pytest.raises(server.Refused, match=error) as refused:
            server.fetch_inputs(runner.url, {}, b"{}", tmp_path, 5)
    finally:
        runner.close()
    assert refused.value.reason == reason


def test_an_unreachable_runner_is_refused(tmp_path: Path) -> None:
    url = f"http://127.0.0.1:9{server.INPUTS_ROUTE}"
    with pytest.raises(server.Refused, match="inputs unreadable") as refused:
        server.fetch_inputs(url, {}, b"{}", tmp_path, 5)
    assert refused.value.reason == "inputs"


@pytest.mark.usefixtures("engine")
@pytest.mark.parametrize(
    ("args", "error"),
    [
        ("--output x", "not a list of strings"),
        (None, "not a list of strings"),
        (["--final-snapshot", "x"], "no single --output"),
        (["--output"], "no single --output"),
        (["--output", "a", "--output", "b"], "no single --output"),
        (["--output", "/tmp/elsewhere.json"], "outside"),
        (["--output", "{grade_dir}/../x.json"], "outside"),
        (["--output", "x.json"], "outside"),
        (["--trajectory", "/etc/passwd", "--output", "{grade_dir}/r.json"], "outside"),
        (["--trajectory", "../x", "--output", "{grade_dir}/r.json"], "outside"),
    ],
    ids=[
        "a-string",
        "none",
        "no-output",
        "dangling",
        "two",
        "absolute",
        "parent-dir",
        "relative",
        "absolute-input",
        "relative-parent-input",
    ],
)
def test_the_engine_sees_no_path_outside_the_grade_dir(
    args: object, error: str, tmp_path: Path
) -> None:
    manifest = {"version": 1, "args": args}
    assert error in _refused("inputs", _inputs(manifest), tmp_path)


def test_only_a_leading_placeholder_is_the_grade_dir(tmp_path: Path) -> None:
    args = ["{grade_dir}/a.zip", "x{grade_dir}/b", "{grade_dir}", "run-{grade_dir}"]
    assert server.in_grade_dir(args, tmp_path) == [
        f"{tmp_path}/a.zip",
        "x{grade_dir}/b",
        "{grade_dir}",
        "run-{grade_dir}",
    ]


@pytest.mark.usefixtures("engine")
@pytest.mark.parametrize(
    ("module", "error"),
    [
        ("fake_fail", "fake_fail exited 3"),
        ("fake_crash", "ValueError: engine bug"),
        ("fake_silent", "wrote no result"),
        ("fake_list", "not a JSON object"),
    ],
    ids=["exits", "crashes", "no-result", "not-an-object"],
)
def test_an_engine_that_fails_is_refused(
    module: str, error: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(server, "ENGINE_MODULE", module)
    assert error in _refused("engine", _inputs(), tmp_path)


@pytest.mark.usefixtures("engine")
def test_an_engine_past_its_deadline_is_a_timeout() -> None:
    with pytest.raises(server.Refused, match="ran past its 0s budget") as refused:
        server.run_cli("fake_sleep", [], timeout_s=0.2)
    assert refused.value.reason == "timeout"


@pytest.mark.usefixtures("engine")
def test_a_timed_out_engine_leaves_nothing_running(
    runner: _Runner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ended: list[bool] = []
    monkeypatch.setattr(server, "ENGINE_MODULE", "fake_sleep")
    monkeypatch.setattr(server, "_end_strays", lambda: ended.append(True))

    with pytest.raises(server.Refused) as refused:
        server.OneCollect(tmp_path).run(_request(runner.url, engine_timeout_s=0.2))

    assert refused.value.reason == "timeout"
    assert ended == [True]


@pytest.mark.usefixtures("engine")
def test_off_the_main_thread_the_engine_does_not_run_unbounded() -> None:
    refusals: list[server.Refused] = []

    def collect() -> None:
        try:
            server.run_cli("fake_clean", [], timeout_s=5)
        except server.Refused as e:
            refusals.append(e)

    thread = threading.Thread(target=collect)
    thread.start()
    thread.join(timeout=5)

    assert [r.reason for r in refusals] == ["error"]


@pytest.mark.usefixtures("engine")
@pytest.mark.parametrize("module", ["fake_clean", "fake_fail"], ids=["ok", "fails"])
def test_the_cli_runs_as_python_dash_m_and_restores_argv_and_stderr(
    module: str,
) -> None:
    argv, stream, fd = sys.argv, sys.stderr, os.fstat(2).st_ino
    fails = pytest.raises(server.Refused)

    with fails if module == "fake_fail" else contextlib.nullcontext():
        server.run_cli(module, ["--flag"], timeout_s=5)

    assert sys.argv is argv and sys.stderr is stream
    assert os.fstat(2).st_ino == fd


class _Strays:
    """``/proc`` as it reads on each sweep, and the kills that sweep sends."""

    def __init__(
        self, sweeps: list[dict[int, int]], me: int = 4242, group: int = 4242
    ) -> None:
        self._sweeps, self.me, self.group = list(sweeps), me, group
        self._current: dict[int, int] = {}
        self.killed: list[int] = []

    def listdir(self, _path: str) -> list[str]:
        procs = self._sweeps.pop(0) if self._sweeps else {}
        self._current = procs
        return ["self", str(self.me), *(str(pid) for pid in procs)]

    def getpgid(self, pid: int) -> int:
        return self._current[pid]


@pytest.mark.parametrize(
    ("group", "sweeps", "killed"),
    [
        (4242, [{4243: 4242, 1: 1, 4244: 99}, {}], [4243]),
        (4242, [{4243: 4242}, {4245: 4242}, {}], [4243, 4245]),
        (77, [{4243: 77}], []),
    ],
    ids=["its-group-only", "a-late-fork", "not-its-groups-leader"],
)
def test_strays_are_the_other_processes_in_the_group_this_one_leads(
    group: int,
    sweeps: list[dict[int, int]],
    killed: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    procs = _Strays(sweeps, group=group)
    monkeypatch.setattr(server.os, "listdir", procs.listdir)
    monkeypatch.setattr(server.os, "getpid", lambda: procs.me)
    monkeypatch.setattr(server.os, "getpgrp", lambda: procs.group)
    monkeypatch.setattr(server.os, "getpgid", procs.getpgid)
    monkeypatch.setattr(server.os, "kill", lambda pid, _sig: procs.killed.append(pid))

    server._end_strays()

    assert procs.killed == killed


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path: str) -> None:
        super().__init__("grader", timeout=10)
        self._path = path

    def connect(self) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(self._path)


def _call(
    path: str, method: str, route: str, body: bytes | None = None
) -> tuple[int, Any]:
    conn = _UnixConnection(path)
    conn.request(method, route, body=body, headers={"Content-Type": "application/json"})
    response = conn.getresponse()
    status, payload = response.status, json.loads(response.read())
    conn.close()
    return status, payload


@pytest.fixture
def socket_path() -> str:
    """A path short enough for AF_UNIX, which caps it near 104 bytes."""
    return os.path.join(tempfile.mkdtemp(prefix="gs", dir="/tmp"), "g.sock")


def _served[T](path: str, collect: server.OneCollect, client: Callable[[], T]) -> T:
    """What ``client`` gets from ``collect`` served on ``path`` by this, the main, thread, which
    serve returns from once the collect has run."""
    outcome: list[T] = []
    errors: list[BaseException] = []

    def drive() -> None:
        try:
            for _ in range(500):
                if os.path.exists(path):
                    break
                time.sleep(0.01)
            outcome.append(client())
        except BaseException as e:
            errors.append(e)
            with contextlib.suppress(OSError):  # let serve return
                _call(path, "POST", "/collect", b"{}")

    thread = threading.Thread(target=drive, daemon=True)
    thread.start()
    server.serve(path, collect, {"load_s": 6.0})
    thread.join(timeout=10)
    if errors:
        raise errors[0]
    return outcome[0]


@pytest.mark.usefixtures("engine")
def test_the_socket_serves_ready_then_one_collect(
    runner: _Runner, socket_path: str, tmp_path: Path
) -> None:
    def client() -> list[tuple[int, Any]]:
        assert os.stat(socket_path).st_mode & 0o777 == 0o600
        return [
            _call(socket_path, "GET", "/ready"),
            _call(socket_path, "GET", "/elsewhere"),
            _call(socket_path, "POST", "/elsewhere", b"{}"),
            _call(socket_path, "POST", "/collect", _request(runner.url)),
        ]

    ready, get_404, post_404, (status, reply) = _served(
        socket_path, server.OneCollect(tmp_path), client
    )

    assert ready == (200, {"load_s": 6.0})
    assert get_404[0] == 404 and post_404[0] == 404
    assert status == 200 and reply["ok"] is True
    assert reply["result"]["final"] == "the final tree"
    assert reply["result"]["key"] == "***", "a secret never leaves the sidecar"
    assert reply["result"]["base_url"] == "https://api.example.com/v1"
    assert "from-hosted-envs" not in reply["log"]


@pytest.mark.parametrize(
    ("body", "reason"),
    [(b"{}", "request"), (None, "error")],
    ids=["refused", "unexpected"],
)
def test_a_failed_collect_answers_why_and_spends_the_grader(
    body: bytes | None,
    reason: str,
    socket_path: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collect = server.OneCollect(tmp_path)
    if body is None:

        def broken(_raw: bytes) -> Any:
            raise RuntimeError("unexpected")

        monkeypatch.setattr(collect, "run", broken)

    status, reply = _served(
        socket_path,
        collect,
        lambda: _call(socket_path, "POST", "/collect", body or b"{}"),
    )

    assert status == 200 and reply["ok"] is False and reply["reason"] == reason
    assert collect.done


def test_main_loads_the_engine_serves_and_leaves_no_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    served: list[tuple[str, dict[str, object], Path]] = []

    def serve(path: str, collect: server.OneCollect, ready: dict[str, object]) -> None:
        work = collect._work
        (work / "final.zip").write_bytes(b"a snapshot")
        served.append((path, ready, work))

    monkeypatch.setattr(server, "ENGINE_MODULE", "json")
    monkeypatch.setattr(server, "serve", serve)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv(server.ENGINE_VERSION_ENV, "abc123")
    monkeypatch.chdir(tmp_path)

    server.main(str(tmp_path / "g.sock"))

    ((path, ready, work),) = served
    assert path == str(tmp_path / "g.sock")
    assert sys.path[0] == str(tmp_path)
    assert set(ready) == {"load_s", "engine_version"}
    assert ready["engine_version"] == "abc123"
    assert not work.exists(), "the collected snapshots go with the sidecar"


@pytest.mark.parametrize(
    ("module", "error"),
    [("no_such_engine_module", "no_such_engine_module"), ("json", "address in use")],
    ids=["engine-import", "socket-bind"],
)
def test_a_sidecar_that_never_listens_leaves_a_failure_beside_the_socket(
    module: str, error: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def no_bind(*_args: object) -> None:
        raise OSError("address in use")

    monkeypatch.setattr(server, "ENGINE_MODULE", module)
    monkeypatch.setattr(server, "serve", no_bind)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.chdir(tmp_path)

    with pytest.raises((ModuleNotFoundError, OSError)):
        server.main(str(tmp_path / "g.sock"))

    assert error in (tmp_path / "g.sock.failed").read_text()


def test_the_sidecar_leads_its_own_process_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(server.os, "setpgid", lambda *a: calls.append(("setpgid", a)))
    monkeypatch.setattr(server, "main", lambda path: calls.append(("main", path)))

    server.run("/tmp/g.sock")

    assert calls == [("setpgid", (0, 0)), ("main", "/tmp/g.sock")]

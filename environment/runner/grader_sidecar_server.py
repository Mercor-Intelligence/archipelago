"""The grader sidecar: loads the grading engine, then runs one relayed grade's collect, as prepared by
``POST /grade/relay/inputs``, on a Unix socket. Stdlib only: the deploy ships this one file."""

from __future__ import annotations

import contextlib
import http.client
import http.server
import importlib
import json
import os
import runpy
import signal
import socketserver
import sys
import tarfile
import tempfile
import threading
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import IO, Any, Literal, cast

# The inputs format, shared with grade_inputs.py: bump INPUTS_VERSION on any change to it.
INPUTS_VERSION = 1
INPUTS_ROUTE = "/grade/relay/inputs"
INPUTS_MANIFEST = "manifest.json"
GRADE_DIR_PLACEHOLDER = "{grade_dir}"
ENGINE_MODULE = "runner.main"
# A copy of grade._ALLOWED_GRADING_CRED_KEYS, which a test holds equal: this file ships alone, and
# rl-studio's tests read that literal from grade.py.
CREDENTIAL_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_BASE_URL",
        "GOOGLE_API_KEY",
        "LITELLM_PROXY_API_BASE",
        "LITELLM_PROXY_API_KEY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "MERCOR_DOCUMENT_API",
        "MERCOR_DOCUMENT_API_KEY",
        "REDUCTO_API_KEY",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "GEMINI_API_KEY",
        "CAS_ENDPOINT",
        "CAS_PRODUCER_TOKEN",
        "CAS_TENANT",
        "CAS_SOURCE",
        "CAS_TAG_SCHEMA_VERSION",
        "CAS_KEY_HMAC_SALT",
    }
)
# Set on the sidecar's image by the deploy that baked its engine.
ENGINE_VERSION_ENV = "GRADING_ENGINE_VERSION"
# The only host the sidecar fetches inputs from: the sandbox's container, as Modal's sidecar network
# names it.
RUNNER_HOST_ENV = "GRADER_SIDECAR_RUNNER_HOST"
_RUNNER_HOST = "main"
# Masked out of what the sidecar returns: secrets only, as a base URL may well be in the graded files.
_SECRET_SUFFIXES = ("_KEY", "_TOKEN", "_SALT")
# Shorter values would mask ordinary text.
_MASKED_MIN = 8
_TEXT_MAX = 2000

# Why a collect is given back: ``timeout`` fails the grade, any other sends it to the sandbox.
Reason = Literal[
    "request", "inputs", "unsupported", "engine", "timeout", "spent", "error"
]


class Refused(Exception):
    """A collect this grader gives back, for ``reason``."""

    def __init__(self, reason: Reason, detail: str) -> None:
        super().__init__(detail)
        self.reason: Reason = reason


class _EngineTimeout(BaseException):
    """A BaseException, so the engine's own handlers cannot swallow its deadline."""


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_NoRedirects)


def unpack(stream: IO[bytes], work: Path) -> dict[str, Any]:
    """A prepared collect's tar, its files extracted into ``work``; its manifest."""
    manifest: object = None
    with tarfile.open(fileobj=stream, mode="r|") as tar:
        for member in tar:
            if member.name == INPUTS_MANIFEST:
                data = tar.extractfile(member)
                manifest = json.loads(data.read()) if data is not None else None
            else:
                tar.extract(member, work, filter="data")
    if not isinstance(manifest, dict):
        raise Refused("inputs", "the inputs carry no manifest")
    manifest = cast(dict[str, Any], manifest)
    if manifest.get("version") != INPUTS_VERSION:
        raise Refused(
            "unsupported",
            f"inputs version {manifest.get('version')!r}, not {INPUTS_VERSION}",
        )
    return manifest


def command(manifest: dict[str, Any], work: Path) -> tuple[list[str], Path]:
    """The engine arguments a manifest names, and the result file they write; every path they
    name is inside ``work``."""
    args = manifest.get("args")
    if not isinstance(args, list) or not all(
        isinstance(a, str) for a in cast(list[Any], args)
    ):
        raise Refused("inputs", "the manifest's args are not a list of strings")
    argv = in_grade_dir(cast(list[str], args), work)
    root = work.resolve()
    for arg in argv:
        path = Path(arg)
        if ".." in path.parts or (
            path.is_absolute() and not path.resolve().is_relative_to(root)
        ):
            raise Refused("inputs", f"the manifest names {arg!r}, outside {work}")
    if argv.count("--output") != 1 or argv[-1] == "--output":
        raise Refused("inputs", "the manifest's args name no single --output")
    output = Path(argv[argv.index("--output") + 1]).resolve()
    if not output.is_relative_to(root):
        raise Refused("inputs", f"the engine would write its result outside {work}")
    return argv, output


def in_grade_dir(args: list[str], work: Path) -> list[str]:
    """``args`` with ``work`` in place of each leading ``GRADE_DIR_PLACEHOLDER``."""
    prefix = GRADE_DIR_PLACEHOLDER + "/"
    return [f"{work}/{a[len(prefix) :]}" if a.startswith(prefix) else a for a in args]


def fetch_inputs(
    url: str, headers: dict[str, str], body: bytes, work: Path, timeout_s: float
) -> dict[str, Any]:
    """The env runner's prepared collect, unpacked into ``work``; its manifest."""
    parsed = urllib.parse.urlsplit(url)
    host = os.environ.get(RUNNER_HOST_ENV, _RUNNER_HOST)
    if (parsed.scheme, parsed.hostname, parsed.path, parsed.query) != (
        "http",
        host,
        INPUTS_ROUTE,
        "",
    ):
        raise Refused("request", f"{url!r} is not http://{host}:<port>{INPUTS_ROUTE}")
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with _OPENER.open(request, timeout=timeout_s) as response:
            return unpack(response, work)
    except urllib.error.HTTPError as e:
        reason: Reason = (
            "unsupported" if e.code == 404 else "inputs"
        )  # a runner without the route
        raise Refused(
            reason, f"inputs answered HTTP {e.code}: {e.read()[:300]!r}"
        ) from e
    except (OSError, http.client.HTTPException, tarfile.TarError, ValueError) as e:
        raise Refused("inputs", f"inputs unreadable ({type(e).__name__}): {e}") from e


@contextlib.contextmanager
def _stderr_into(log: IO[bytes]) -> Iterator[None]:
    """Fd 2 and ``sys.stderr``, so every logger and child process the engine starts, into ``log``."""
    sys.stderr.flush()
    saved_fd, saved_stream = os.dup(2), sys.stderr
    os.dup2(log.fileno(), 2)
    stream = open(log.fileno(), "w", closefd=False, errors="replace")  # noqa: SIM115
    sys.stderr = stream
    try:
        yield
    finally:
        stream.close()
        sys.stderr = saved_stream
        os.dup2(saved_fd, 2)
        os.close(saved_fd)


@contextlib.contextmanager
def _deadline(seconds: float) -> Iterator[None]:
    """``_EngineTimeout`` after ``seconds``; a signal, so only on the main thread."""

    def expire(_signum: int, _frame: object) -> None:
        raise _EngineTimeout

    previous = signal.signal(signal.SIGALRM, expire)
    _ = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        _ = signal.setitimer(signal.ITIMER_REAL, 0)
        _ = signal.signal(signal.SIGALRM, previous)


def _tail(log: IO[bytes]) -> str:
    size = log.seek(0, os.SEEK_END)
    _ = log.seek(max(0, size - _TEXT_MAX))
    return log.read().decode(errors="replace")


def run_cli(module: str, argv: list[str], timeout_s: float) -> str:
    """``python -m <module> <argv>`` in this interpreter, within ``timeout_s``; its stderr's tail."""
    if threading.current_thread() is not threading.main_thread():
        raise Refused(
            "error",
            "the engine's deadline is a signal, which only the main thread takes",
        )
    failure: tuple[Reason, str] | None = None
    saved_argv = sys.argv
    with tempfile.TemporaryFile() as log:
        with _stderr_into(log):
            sys.argv = [module, *argv]
            try:
                with _deadline(timeout_s):
                    _ = runpy.run_module(module, run_name="__main__")
            except _EngineTimeout:
                failure = ("timeout", f"{module} ran past its {timeout_s:.0f}s budget")
            except SystemExit as e:
                if e.code not in (0, None):
                    failure = ("engine", f"{module} exited {e.code}")
            except Exception:  # the engine's own crash, reported like an exit
                failure = ("engine", f"{module} raised\n{traceback.format_exc()}")
            finally:
                sys.argv = saved_argv
        tail = _tail(log)
    if failure is not None:
        reason, detail = failure
        raise Refused(reason, f"{detail}\n{tail}")
    return tail


def _forget(module: str) -> None:
    """Drop ``module``'s package from the import cache, keeping what it imported (~0.2 s to redo).
    The engine must keep its environment-derived state inside that package for this to refresh it."""
    package = module.split(".")[0]
    for name in [n for n in sys.modules if n == package or n.startswith(f"{package}.")]:
        del sys.modules[name]


def _masked(value: object, secrets: tuple[str, ...]) -> object:
    """``value`` with every secret in its strings, however deep, replaced by ``***``."""
    if isinstance(value, str):
        for secret in secrets:
            value = value.replace(secret, "***")
        return value
    if isinstance(value, list):
        return [_masked(v, secrets) for v in cast(list[object], value)]
    if isinstance(value, dict):
        return {
            k: _masked(v, secrets) for k, v in cast(dict[str, object], value).items()
        }
    return value


def _end_strays() -> None:
    """SIGKILL every other process in this one's group, as the env runner kills a timed-out grade's
    group: only once ``run`` made this process its group's leader, so never its parent's group.
    Sweeps until one finds nothing, as a process can fork between a listing and its kill."""
    me, group = os.getpid(), os.getpgrp()
    if group != me:
        return
    for _ in range(50):
        killed = 0
        for name in os.listdir("/proc"):
            if name.isdigit() and int(name) != me:
                with contextlib.suppress(OSError):
                    if os.getpgid(int(name)) == group:
                        os.kill(int(name), signal.SIGKILL)
                        killed += 1
        if not killed:
            return
        time.sleep(0.01)


class OneCollect:
    """The one collect this grader runs; any request after it is refused."""

    def __init__(self, work: Path) -> None:
        self._work = work
        self._secrets: tuple[str, ...] = ()
        self.done = False

    def mask(self, value: object) -> Any:
        """``value`` without any secret this collect was given."""
        return _masked(value, self._secrets)

    def run(self, raw: bytes) -> tuple[dict[str, Any], str]:
        """The engine's record for the collect ``raw`` asks for, and the tail of its stderr."""
        if self.done:
            raise Refused("spent", "this grader has already collected")
        self.done = True
        try:
            request = json.loads(raw)
            url, timeout_s = str(request["url"]), float(request["timeout_s"])
            engine_timeout_s = float(request["engine_timeout_s"])
            headers = {str(k): str(v) for k, v in request["headers"].items()}
            credentials = json.loads(request.get("credentials_json") or "{}")
            body = str(request["body"]).encode()
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            raise Refused(
                "request", f"malformed collect request ({type(e).__name__})"
            ) from e
        if not engine_timeout_s > 0:
            raise Refused(
                "request", f"engine_timeout_s {engine_timeout_s} is not positive"
            )
        argv, output = command(
            fetch_inputs(url, headers, body, self._work, timeout_s), self._work
        )
        granted = credentials.items() if isinstance(credentials, dict) else ()
        given = {str(k): str(v) for k, v in cast(Iterable[tuple[Any, Any]], granted)}
        allowed = {k: v for k, v in given.items() if k in CREDENTIAL_KEYS}
        # As the env runner does: a request's base URL never meets a key this process inherited.
        for key in CREDENTIAL_KEYS:
            os.environ.pop(key, None)
        os.environ.update(allowed)
        self._secrets = tuple(
            v
            for k, v in allowed.items()
            if k.endswith(_SECRET_SUFFIXES) and len(v) >= _MASKED_MIN
        )
        # The engine reads its settings from the environment at import: import it again under these.
        _forget(ENGINE_MODULE)
        try:
            log = run_cli(ENGINE_MODULE, argv, engine_timeout_s)
        except Refused as e:
            if e.reason == "timeout":
                _end_strays()
            raise
        try:
            result = json.loads(output.read_text())
        except (OSError, ValueError) as e:
            raise Refused(
                "engine", f"{ENGINE_MODULE} wrote no result: {e}\n{log}"
            ) from e
        if not isinstance(result, dict):
            raise Refused(
                "engine", f"{ENGINE_MODULE} wrote a result that is not a JSON object"
            )
        return cast(dict[str, Any], result), log


def handler(
    collect: OneCollect, ready: dict[str, object]
) -> type[http.server.BaseHTTPRequestHandler]:
    """The request handler for ``collect``: ``GET /ready`` and ``POST /collect``."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/ready":
                self._reply(200, ready)
            else:
                self._reply(404, {"error": f"no route {self.path}"})

        def do_POST(self) -> None:
            # Read before answering: a reply to an unread body resets the client mid-send.
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if self.path != "/collect":
                self._reply(404, {"error": f"no route {self.path}"})
                return
            try:
                result, log = collect.run(body)
                reply: dict[str, object] = {
                    "ok": True,
                    "result": collect.mask(result),
                    "log": collect.mask(log),
                }
            except Refused as e:
                error = collect.mask(str(e))[-_TEXT_MAX:]
                reply = {"ok": False, "reason": e.reason, "error": error}
            except Exception as e:  # any other failure is still the caller's cue to grade in the sandbox
                collect.done = True
                error = collect.mask(f"{type(e).__name__}: {e}")[-_TEXT_MAX:]
                reply = {"ok": False, "reason": "error", "error": error}
            self._reply(200, reply)

        def _reply(self, status: int, body: object) -> None:
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            _ = self.wfile.write(data)

        def log_message(self, format: str, *args: Any) -> None:
            """Silent: the default reads a client address a Unix socket does not have."""

    return Handler


def serve(path: str, collect: OneCollect, ready: dict[str, object]) -> None:
    """Answer on ``path`` until the collect has run; the socket appears only once it listens."""
    staging = f"{path}.{os.getpid()}"
    with socketserver.UnixStreamServer(staging, handler(collect, ready)) as server:
        os.chmod(staging, 0o600)
        os.replace(staging, path)
        while not collect.done:
            server.handle_request()


def main(socket_path: str) -> None:
    """Load the engine, then serve one collect. A sidecar that fails before it listens leaves
    ``<socket>.failed``, which ends hosted-envs' wait for it."""
    started = time.monotonic()
    try:
        # As `python -m` from the engine root: its `runner` before the editable install's build path.
        sys.path.insert(0, os.getcwd())
        _ = importlib.import_module(ENGINE_MODULE)
        ready: dict[str, object] = {
            "load_s": round(time.monotonic() - started, 3),
            "engine_version": os.environ.get(ENGINE_VERSION_ENV, ""),
        }
        with tempfile.TemporaryDirectory(prefix="grade-") as work:
            serve(socket_path, OneCollect(Path(work)), ready)
    except BaseException as e:
        if not os.path.exists(socket_path):
            failure = f"{type(e).__name__}: {e}"[-_TEXT_MAX:]
            _ = Path(f"{socket_path}.failed").write_text(failure)
        raise


def run(socket_path: str) -> None:
    """``main``, leading its own process group, so a timed-out engine's children can be found."""
    with contextlib.suppress(
        PermissionError
    ):  # a session leader already leads its group
        os.setpgid(0, 0)
    main(socket_path)


if __name__ == "__main__":
    run(sys.argv[1])

"""HTTP endpoint that triggers a populate cycle via ``mise run <task>``.

**Intended for tests, not production ingest.** The endpoint stages
caller-supplied input files into an ephemeral ``/tmp`` scratch
directory, spawns ``mise run <task> <scratch_dir>`` as a detached
subprocess, and deletes the scratch directory after the subprocess
exits. It's the HTTP-triggerable equivalent of "here's a small
fixture, populate the DB with it" for integration-test harnesses. No
concessions were made for huge corpora — everything is a copy, not a
symlink.

**Layered configuration composition.** ``input_paths`` is a list
because the primary use case is *baseline + overlay*: a test keeps
common seed data in one directory and case-specific deltas in
another, and applies both without pre-materialising every
combination. Callers list paths in **baseline-first, overlay-last
order**::

    POST /_internal/populate
    {"input_paths": ["/data/baseline", "/data/case_high_load"]}

Same-name entries across paths are merged by extension. For each
unique *relative path* (basename for file inputs, path-relative-to-
the-input-root for directory inputs) the stager applies exactly one
mode:

* **CSV — header-aware concatenation.** ``.csv`` files with the same
  relative path across multiple ``input_paths`` are concatenated in
  input-path order: the first source's header row is written once,
  data rows from every source follow, headers on files 2..N are
  dropped. This is the "baseline rows + overlay rows" behaviour: the
  baseline provides the common seed, later paths append case-specific
  rows. If headers disagree across sources the request fails with
  HTTP 400 (structural mismatch — refusing to splice a broken CSV
  silently).
* **``.db`` — collision rejection.** SQLite databases are page graphs,
  not appendable byte streams; concatenating them would corrupt the
  file. In practice a ``.db`` sidecar is either the *whole* dataset
  or nothing, so pre-built DBs with the same name under multiple
  ``input_paths`` return HTTP 400 rather than silently overlaying
  (which would mask caller errors). Callers who need a case-specific
  ``.db`` should not include one under the baseline path.
* **Other extensions — last-wins overlay.** JSON config, YAML, image
  fixtures, or any other reader-recognised type falls back to
  last-wins: the file from the last ``input_path`` in the list is
  written, earlier ones are ignored, and a warning is logged so
  operators see the overlap happened. This is the "case-specific
  overrides baseline" behaviour for structured config files that
  can't be usefully concatenated.

Same-name matching keys on the **full relative path** from the input
root, not the basename — so ``baseline/sub/x.csv`` and
``overlay/sub/x.csv`` concatenate but ``baseline/foo/x.csv`` and
``overlay/bar/x.csv`` do not. That's symmetric with how the facade's
readers walk the tree; two folders that happen to share a leaf
basename are usually different entities. The endpoint returns a
``staged`` map in the response documenting exactly which sources
contributed to each staged output and in which mode, so callers can
audit the fusion.

**Scratch-dir lifecycle.** Every call gets its own
``tempfile.mkdtemp(prefix="mcp_populate_")`` scratch directory; it
holds the staged inputs, the subprocess's ``populate.log``, and any
files ``mise run populate`` writes back (canonical DB, etc.). After
the subprocess exits, the scratch directory is deleted:

* On ``wait: true`` the request handler waits inline, reads the log
  tail into the response, then ``shutil.rmtree``s the scratch.
* On the default fire-and-forget path (HTTP 202), a background
  daemon thread ``proc.wait()`` s and then deletes the scratch.

Tests that need the populated DB to *outlive* cleanup should either:

* set ``DATABASE_PATH`` in the app's environment to a path outside
  ``$STATE_LOCATION`` (so ``mise run populate`` writes the DB
  somewhere durable), OR
* use ``wait: true`` and read the log tail / verify DB state before
  the response returns.

**Why not run the pipeline in-process?** The populate cycle takes
minutes on real data and dispose-drains SQLAlchemy pools mid-flight;
running it inside the request-serving worker would freeze the whole
server. A detached subprocess with ``start_new_session=True`` lets
populate outlive the request that kicked it off.

**Why stage input files first?** The ``mise run populate`` shell task
reads from a well-known state directory (``$STATE_LOCATION``); it does
not accept an "input path" argument. Rather than plumb a new arg
through every consumer's ``populate.sh``, this endpoint stages the
caller-supplied files into the scratch directory and points
``STATE_LOCATION`` at it before spawning the subprocess. Consumers
that stray from the boilerplate convention (e.g. ``populate.sh``
hardcoded to ``/.apps_data/...``) still work, because we pass the
scratch path as the positional arg too.

**ADOPTER CHECKLIST — silent-misfire hazard.** ``mise.toml``'s
``[env]`` block is applied *after* the endpoint's environment on the
subprocess, so an app that pins ``STATE_LOCATION`` in ``[env]`` will
silently override the endpoint's scratch path and read from its
production directory instead. The subprocess returns 0 (it found real
CSVs there, just not the ones the endpoint staged), and the endpoint
happily reports ``status="completed"``. The positional-arg fallback
is the **only** reliable channel — every ``populate.sh`` in a
consumer repo that uses this endpoint MUST consume ``$1`` as its
state location, with the env-var as fallback::

    # populate.sh — required shape for endpoint compatibility
    STATE_LOCATION="${1:-${STATE_LOCATION:-/.apps_data/appname}}"

Consumers that hardcode ``STATE_LOCATION`` without honouring ``$1``
will fail this test silently. The
:func:`register_runtime_db_routes` docstring links back to this note.

**Not for arbitrary shell execution.** The endpoint spawns exactly one
command (``mise run <task> <scratch_dir> [extra_args...]``); ``extra_args``
is validated as a list of strings and appended positionally, not
interpolated into a shell. There's no user-controlled ``mise`` task
name at the HTTP layer — the task name is fixed at
:func:`register_runtime_db_routes` registration time.

**Registration is opt-in.** :func:`register_runtime_db_routes` mounts
this route only when the caller passes ``populate_working_dir=`` (the
directory containing ``mise.toml``). Consumers that don't want a
populate endpoint just omit the kwarg.
"""

from __future__ import annotations

import csv
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Any, TypedDict

logger = logging.getLogger(__name__)

__all__ = [
    "PopulateCompletedResponse",
    "PopulateStartedResponse",
    "StagedEntry",
    "handle_populate_request",
]


DEFAULT_MISE_TASK = "populate"
# Prefix for the auto-created scratch directories. Fully qualified so
# operators grepping /tmp can attribute leaked scratches to this endpoint.
_SCRATCH_PREFIX = "mcp_populate_"
# Tail of the log file surfaced to synchronous (``wait: true``) callers.
# Bounded so a runaway populate doesn't return a multi-MB JSON body.
_LOG_TAIL_BYTES = 64 * 1024


class StagedEntry(TypedDict):
    """One entry in the ``staged`` audit map on the populate response.

    Keys:
        sources: Absolute paths of every input file that contributed to
            this staged output, in input-path order. Length 1 for
            single-source entries, ≥2 for concatenations / last-wins
            overlays.
        mode: How the sources were combined:

            * ``"copied"`` — single source, plain ``shutil.copy2``.
            * ``"concatenated"`` — CSV files fused via header-aware
              merge. First source's header preserved, data rows from
              all sources appended in input-path order.
            * ``"last_wins"`` — non-CSV, non-``.db`` files with the
              same relative path across multiple input paths; only the
              last source's file was written.
    """

    sources: list[str]
    mode: str


class PopulateStartedResponse(TypedDict):
    """Response body for fire-and-forget (``wait=false``, default) calls."""

    status: str  # always ``"started"``
    pid: int
    state_dir: str  # the ephemeral /tmp scratch dir; cleaned up when the subprocess exits
    log_path: str
    staged: dict[str, StagedEntry]


class PopulateCompletedResponse(TypedDict):
    """Response body for synchronous (``wait=true``) calls."""

    status: str  # ``"completed"`` if returncode==0, ``"failed"`` otherwise
    pid: int
    returncode: int
    state_dir: str  # the ephemeral /tmp scratch dir; already cleaned up by the time this returns
    log_path: str
    staged: dict[str, StagedEntry]
    log_tail: str


def handle_populate_request(
    body: dict[str, Any],
    *,
    working_dir: Path,
    mise_task: str = DEFAULT_MISE_TASK,
    subprocess_module: Any = subprocess,
    scratch_root: Path | None = None,
) -> tuple[dict[str, Any], int]:
    """Process a ``POST /_internal/populate`` request body.

    The pure-Python entry point behind the mounted route. Returns
    ``(json_body, http_status_code)`` so the route handler is a thin
    ``JSONResponse`` wrapper. Split from the route so tests can drive
    the logic without an HTTP client.

    Args:
        body: Parsed JSON request body. Recognised keys:

            * ``input_paths`` (list[str], required, non-empty):
              Absolute paths to inputs. Each element may be either a
              directory (walked recursively) or a single ``.db`` file.
              Same-name entries across paths are merged per the CSV /
              ``.db`` / last-wins policy in the module docstring.
            * ``wait`` (bool, optional, default ``False``): If ``True``,
              block until the subprocess exits and include its
              returncode + log tail in the response. Cleanup happens
              inline before the response returns.
            * ``extra_args`` (list[str], optional): Forwarded to
              ``mise run <task>`` after ``<scratch_dir>``.
        working_dir: Directory in which ``mise`` runs — must contain
            the ``mise.toml`` that defines the target task. Populated by
            :func:`register_runtime_db_routes` from its
            ``populate_working_dir=`` kwarg.
        mise_task: Task name. Fixed at registration time; not
            user-controllable via the HTTP body. Defaults to
            ``"populate"``.
        subprocess_module: Test injection point. Defaults to the stdlib
            :mod:`subprocess`.
        scratch_root: Test injection point. When provided, the scratch
            directory is created *inside* this root (via
            :func:`tempfile.mkdtemp`) instead of the OS-default
            ``/tmp``. Lets tests point staging at ``pytest`` 's
            per-test ``tmp_path`` so file-level assertions are simple.

    Returns:
        Tuple of ``(json_body, http_status_code)``. Status codes:

        * ``202`` — subprocess spawned successfully (default, fire-and-forget)
        * ``200`` — ``wait=true`` and subprocess exited with returncode 0
        * ``400`` — validation error (missing ``input_paths``, empty list,
          bad type, ``.db`` collision across paths, CSV header mismatch)
        * ``404`` — one of ``input_paths`` doesn't exist on disk
        * ``500`` — file-copy or subprocess-spawn failure, or ``wait=true``
          and subprocess exited non-zero
    """
    # 0. Validate body is a mapping. Valid JSON like `null`, `[...]`, or a
    #    top-level scalar deserialises to a non-dict, and every subsequent
    #    body.get(...) would AttributeError. Return a structured 400
    #    instead of leaking a 500 with an opaque traceback.
    if not isinstance(body, dict):
        return (
            {
                "status": "error",
                "error": (f"request body must be a JSON object, got {type(body).__name__}"),
            },
            400,
        )

    # 1. Validate input_paths — required, non-empty list of strings, all exist.
    raw_input_paths = body.get("input_paths")
    if not isinstance(raw_input_paths, list) or not raw_input_paths:
        return (
            {
                "status": "error",
                "error": "input_paths is required and must be a non-empty list of strings",
            },
            400,
        )
    input_paths: list[Path] = []
    for i, raw in enumerate(raw_input_paths):
        if not isinstance(raw, str) or not raw.strip():
            return (
                {
                    "status": "error",
                    "error": f"input_paths[{i}] must be a non-empty string",
                },
                400,
            )
        resolved = Path(raw).expanduser().resolve()
        if not resolved.exists():
            return (
                {
                    "status": "error",
                    "error": f"input_paths[{i}] does not exist: {resolved}",
                },
                404,
            )
        input_paths.append(resolved)

    # 2. Validate extra_args + wait early so a bad shape doesn't leak
    #    a scratch dir or an orphan subprocess.
    extra_args = body.get("extra_args", [])
    if not isinstance(extra_args, list) or not all(isinstance(a, str) for a in extra_args):
        return (
            {"status": "error", "error": "extra_args must be a list of strings"},
            400,
        )
    # Require a real bool: `bool("false")` is True (non-empty string),
    # so if a caller sends `"wait": "false"` in the JSON we'd silently
    # block for the whole populate instead of returning 202. JSON-native
    # types only.
    raw_wait = body.get("wait", False)
    if not isinstance(raw_wait, bool):
        return (
            {
                "status": "error",
                "error": f"wait must be a JSON boolean (true/false), got {type(raw_wait).__name__}",
            },
            400,
        )
    wait = raw_wait

    # 3. Create the ephemeral scratch directory. Every call gets a
    #    unique path so concurrent populate requests can't stomp on
    #    each other's staged inputs.
    try:
        scratch_dir = Path(
            tempfile.mkdtemp(
                prefix=_SCRATCH_PREFIX,
                dir=str(scratch_root) if scratch_root is not None else None,
            )
        )
    except OSError as exc:
        return (
            {"status": "error", "error": f"failed to create scratch directory: {exc}"},
            500,
        )

    # 4. Stage the inputs into the scratch dir. On any staging failure
    #    the scratch dir is cleaned up before returning — half-populated
    #    scratches are worse than none for later debugging. The catch is
    #    broad (BaseException minus reraise-only classes) because scratch
    #    hygiene is more important than surgical error typing: a bug in
    #    the walker (e.g. `Path.relative_to` raising ValueError when a
    #    symlink escapes the input root) mustn't leave a `/tmp` corpse.
    #    Expected failures land in the typed _StageError branch and
    #    surface with the mapped status; unexpected ones become a
    #    generic 500 but still clean up.
    try:
        staged = _stage_inputs(input_paths, scratch_dir)
    except _StageError as exc:
        shutil.rmtree(scratch_dir, ignore_errors=True)
        return ({"status": "error", "error": str(exc)}, exc.status_code)
    except Exception as exc:  # noqa: BLE001 - scratch hygiene trumps surgical typing
        shutil.rmtree(scratch_dir, ignore_errors=True)
        logger.exception("populate: unexpected staging failure")
        return (
            {
                "status": "error",
                "error": f"unexpected staging failure: {type(exc).__name__}: {exc}",
            },
            500,
        )

    # 5. Build the command and spawn the subprocess.
    cmd = ["mise", "run", mise_task, str(scratch_dir), *extra_args]
    log_path = scratch_dir / "populate.log"
    env = {**os.environ, "STATE_LOCATION": str(scratch_dir)}

    try:
        # Log fd is owned by the child once Popen dup's it; we close our
        # own copy right after (fire-and-forget) or hold it until wait()
        # returns (synchronous mode).
        log_fd = open(log_path, "ab")  # noqa: SIM115 - deliberately owned by subprocess
    except OSError as exc:
        shutil.rmtree(scratch_dir, ignore_errors=True)
        return (
            {"status": "error", "error": f"failed to open log file {log_path}: {exc}"},
            500,
        )

    try:
        proc = subprocess_module.Popen(
            cmd,
            cwd=str(working_dir),
            env=env,
            stdout=log_fd,
            stderr=subprocess_module.STDOUT,
            # Detach from the request-serving process so populate
            # outlives the HTTP request that kicked it off. Without
            # this the subprocess would inherit the server's process
            # group and die when the request handler returns.
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        log_fd.close()
        shutil.rmtree(scratch_dir, ignore_errors=True)
        return (
            {
                "status": "error",
                "error": f"mise not found on PATH — is mise installed on this container? ({exc})",
            },
            500,
        )
    except OSError as exc:
        log_fd.close()
        shutil.rmtree(scratch_dir, ignore_errors=True)
        return (
            {"status": "error", "error": f"failed to spawn subprocess: {exc}"},
            500,
        )

    if not wait:
        # Fire-and-forget: hand ownership of the scratch dir to a
        # daemon thread that ``proc.wait()`` s on the subprocess and
        # cleans up afterwards. Daemon so the thread doesn't block
        # server shutdown; leaked /tmp entries are cleaned by the OS
        # on reboot anyway.
        log_fd.close()
        threading.Thread(
            target=_wait_and_cleanup,
            args=(proc, scratch_dir),
            daemon=True,
            name=f"populate-cleanup-{proc.pid}",
        ).start()
        logger.info(
            "populate: spawned pid=%s scratch=%s log=%s staged=%d entries",
            proc.pid,
            scratch_dir,
            log_path,
            len(staged),
        )
        return (
            {
                "status": "started",
                "pid": proc.pid,
                "state_dir": str(scratch_dir),
                "log_path": str(log_path),
                "staged": staged,
            },
            202,
        )

    # wait=True: block until the subprocess exits, read the log tail
    # BEFORE cleanup (obvious), then delete the scratch dir. Not the
    # default because a real populate takes minutes; primarily used
    # by tests and by callers that need synchronous completion.
    returncode = proc.wait()
    log_fd.close()
    log_tail = _read_log_tail(log_path)
    shutil.rmtree(scratch_dir, ignore_errors=True)
    if returncode == 0:
        status = "completed"
        http_status = 200
    else:
        status = "failed"
        http_status = 500
    logger.info(
        "populate: pid=%s returncode=%s status=%s scratch=%s (cleaned)",
        proc.pid,
        returncode,
        status,
        scratch_dir,
    )
    return (
        {
            "status": status,
            "pid": proc.pid,
            "returncode": returncode,
            "state_dir": str(scratch_dir),
            "log_path": str(log_path),
            "staged": staged,
            "log_tail": log_tail,
        },
        http_status,
    )


def _wait_and_cleanup(proc: Any, scratch_dir: Path) -> None:
    """Background thread body: wait on ``proc`` then delete ``scratch_dir``.

    Runs as a daemon thread so it doesn't block server shutdown.
    Failures are logged and swallowed — the ``/tmp`` cleanup is
    best-effort; the OS reaps the directory on reboot if the thread
    dies before it runs.
    """
    try:
        returncode = proc.wait()
        logger.info(
            "populate: subprocess pid=%s exited returncode=%s; cleaning scratch %s",
            proc.pid,
            returncode,
            scratch_dir,
        )
    except Exception as exc:  # noqa: BLE001 - background thread must never raise
        logger.warning("populate: proc.wait() failed for pid=%s: %s", proc.pid, exc)
    try:
        shutil.rmtree(scratch_dir, ignore_errors=True)
    except Exception as exc:  # noqa: BLE001 - defensive; rmtree already ignores errors
        logger.warning("populate: cleanup of %s failed: %s", scratch_dir, exc)


class _StageError(Exception):
    """Raised by :func:`_stage_inputs` to signal a caller-facing failure.

    Carries an HTTP status code so the top-level handler can surface
    the right one (400 for bad input shape / structural issues, 500
    for filesystem failures) without a second layer of translation.
    """

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


def _stage_inputs(
    input_paths: list[Path],
    state_dir: Path,
) -> dict[str, StagedEntry]:
    """Group same-named files across input_paths and stage into state_dir.

    Two-pass:

    1. Walk each ``input_path`` to build a ``{relative_key: [sources]}``
       map. File inputs contribute one entry keyed by basename;
       directory inputs contribute every file found by ``rglob("*")``,
       keyed by its path relative to the input root. Within one
       ``input_path`` the walk is sorted for reproducibility; across
       inputs the caller's list order is preserved (so concat happens
       in input-path order, as the API contract promises).
    2. Dispatch each group to :func:`_stage_group`, which applies the
       CSV / ``.db`` / last-wins policy documented at module level.

    Returns the staged manifest — the same map that lands in the
    ``staged`` field of the HTTP response.

    Raises:
        _StageError: On bad input shape (400) or filesystem failure
            (500). Propagates from :func:`_stage_group`. Callers should
            also expect ``ValueError`` etc. from the walker — e.g.
            ``Path.relative_to`` when a symlink escapes the input root —
            and clean up the scratch dir accordingly. See
            :func:`handle_populate_request` for the fallback guard.
    """
    grouped: dict[str, list[Path]] = {}
    for base in input_paths:
        if base.is_file():
            grouped.setdefault(base.name, []).append(base)
            continue
        if base.is_dir():
            # Sort inside a single input_path so file order is
            # deterministic (rglob's order is arbitrary). Across
            # input_paths, insertion order preserves the caller's list.
            for file_path in sorted(base.rglob("*")):
                if file_path.is_file():
                    key = str(file_path.relative_to(base))
                    grouped.setdefault(key, []).append(file_path)
            continue
        # is_file / is_dir both false — symlink to nowhere, device
        # node, etc. Surface as a 400 so operators see the specific
        # entry rather than a generic 500 from copy2.
        raise _StageError(
            f"input path is neither a file nor a directory: {base}",
            status_code=400,
        )

    staged: dict[str, StagedEntry] = {}
    for key in sorted(grouped):
        staged[key] = _stage_group(key, grouped[key], state_dir)
    return staged


def _stage_group(
    key: str,
    sources: list[Path],
    state_dir: Path,
) -> StagedEntry:
    """Stage one same-named group of sources into ``state_dir / key``.

    Dispatch by extension:

    * ``.db`` + more than one source → 400 (SQLite pages can't be
      byte-concatenated; refuse to silently overlay).
    * ``.csv`` + more than one source → header-aware concat via
      :func:`_concat_csvs`.
    * Otherwise → last-wins overlay (write the last source, log a
      warning if there was more than one).
    * Single-source groups always fall through to plain
      :func:`shutil.copy2`.
    """
    dst = state_dir / key
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise _StageError(
            f"failed to create staging directory for {key}: {exc}",
            status_code=500,
        ) from exc

    ext = dst.suffix.lower()
    source_strs = [str(s) for s in sources]

    if ext == ".db" and len(sources) > 1:
        raise _StageError(
            f"cannot concatenate .db files with the same relative path: "
            f"{key!r} sourced from {source_strs}",
            status_code=400,
        )

    if ext == ".csv" and len(sources) > 1:
        _concat_csvs(sources, dst)
        return StagedEntry(sources=source_strs, mode="concatenated")

    # Single-source case OR non-CSV/non-.db multi-source (last-wins).
    if len(sources) > 1:
        logger.warning(
            "populate: last-wins overlay for %r (sources=%s; concat only supported for .csv)",
            key,
            source_strs,
        )
    try:
        shutil.copy2(sources[-1], dst)
    except OSError as exc:
        raise _StageError(
            f"failed to copy {sources[-1]} → {dst}: {exc}",
            status_code=500,
        ) from exc
    mode = "last_wins" if len(sources) > 1 else "copied"
    return StagedEntry(sources=source_strs, mode=mode)


def _concat_csvs(sources: list[Path], dst: Path) -> None:
    """Concatenate CSV files: keep the first source's header, append data rows.

    Uses stdlib :mod:`csv` so quoted fields and embedded newlines are
    handled correctly (a naïve byte concat would break on any CSV cell
    containing an embedded newline). Headers on sources 2..N are read
    and compared to the first — a mismatch raises :class:`_StageError`
    with HTTP 400 so callers see the structural problem instead of
    silently getting a broken merged CSV.

    Empty source files (zero rows including no header) are silently
    skipped — this matches ``import_directory``'s behaviour and avoids
    forcing the caller to filter empties before POSTing.
    """
    first_header: list[str] | None = None
    try:
        with dst.open("w", newline="", encoding="utf-8") as out:
            writer = csv.writer(out)
            for src in sources:
                with src.open("r", newline="", encoding="utf-8") as f:
                    reader = csv.reader(f)
                    try:
                        header = next(reader)
                    except StopIteration:
                        continue
                    if first_header is None:
                        first_header = header
                        writer.writerow(header)
                    elif header != first_header:
                        raise _StageError(
                            f"CSV header mismatch while concatenating {dst.name}: "
                            f"{src} has {header!r} but first source has "
                            f"{first_header!r}",
                            status_code=400,
                        )
                    for row in reader:
                        writer.writerow(row)
    except _StageError:
        raise
    except OSError as exc:
        raise _StageError(
            f"failed to concatenate CSVs into {dst}: {exc}",
            status_code=500,
        ) from exc


def _read_log_tail(log_path: Path) -> str:
    """Read the last ``_LOG_TAIL_BYTES`` bytes of ``log_path``.

    Used to attach a truncated log excerpt to ``wait=true`` responses so
    the caller can diagnose failures without a follow-up file read.
    Decodes as UTF-8 with ``errors="replace"`` — populate output is
    virtually always UTF-8, but the boundary at ``_LOG_TAIL_BYTES``
    might split a multibyte sequence.
    """
    try:
        size = log_path.stat().st_size
        with log_path.open("rb") as f:
            if size > _LOG_TAIL_BYTES:
                f.seek(-_LOG_TAIL_BYTES, os.SEEK_END)
            return f.read().decode("utf-8", errors="replace")
    except OSError as exc:
        return f"<log read failed: {exc}>"

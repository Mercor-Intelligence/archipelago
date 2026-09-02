"""In-container grading: run the grading engine against the LIVE sandbox state.

``POST /grade`` grades the episode IN the same container it ran in, against the live filesystem —
no dispatch to the separate Modal grading lane, no snapshot round-trip, no queue. It captures the
live ``filesystem`` + ``.apps_data`` (streamed to a temp file), writes the caller-supplied grading
config to temp files, and runs the grading engine's CLI (``runner.main``) as a SUBPROCESS in its own venv, then
returns the parsed result. It never persists — the caller (the hosted-envs servicer) records it in
Studio. Signature-protected automatically: ``/grade`` is not in ``_SIGNING_EXEMPT_PATHS``.

Why subprocess (not in-process import): the grading engine's package is ALSO named ``runner`` (it
imports itself as ``from runner.main import …``), so it cannot be imported alongside this env runner
(also ``runner``) without renaming its whole package. Running it in its own venv via the CLI —
exactly how GDM docker worlds run ``score.command`` — sidesteps the collision and keeps this endpoint
free of any grading-package import (the config is opaque JSON, passed straight to the CLI as files).

PACKAGING (deploy-gating): the grading engine is MOUNTED into the sandbox as a Modal Volume at
``/app/grading`` (see hosted-envs ``sandbox.py`` / ``grading_volume.py``) — NOT baked into the shared
platform image. Its interpreter is ``GRADING_VENV_PYTHON`` (default below), with ``runner.main``
importable there; ``grade()`` creates ``GRADING_WORK_DIR`` (default ``/app/.grading``, root-owned
``0700``) inside the model-denied ``/app`` tree so the rubric scratch inherits the two-user boundary.
"""

import asyncio
import json
import os
import shutil
import signal
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import zstandard
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .data.snapshot.streaming import create_tar_gz_stream
from .data.snapshot.utils import iter_paths

router = APIRouter()

# Serialize /grade: a grade captures the live filesystem + runs verifiers against ONE mutable
# sandbox, so overlapping requests would race (and double the resource load). One process per
# sandbox, so a module-level lock is a per-sandbox lock.
_GRADE_LOCK = asyncio.Lock()

# The two subsystems the env snapshots — the live agent workspace + per-app state.
_SNAPSHOT_SUBSYSTEMS = ["filesystem", ".apps_data"]
# Interpreter of the grading venv MOUNTED at /app/grading (where `runner` == grading engine).
_GRADING_VENV_PYTHON = os.environ.get(
    "GRADING_VENV_PYTHON", "/app/grading/.venv/bin/python"
)
# Root of the mounted grading project (parent of its .venv, e.g. /app/grading) — the CLI runs with this
# as cwd so `runner` resolves to the grading engine, not the env runner (same package name).
_GRADING_INSTALL_DIR = os.path.dirname(
    os.path.dirname(os.path.dirname(_GRADING_VENV_PYTHON))
)
_GRADING_TIMEOUT_S = float(os.environ.get("GRADING_SUBPROCESS_TIMEOUT_S", "900"))
# Root-owned 0700 scratch base for the grade run, INSIDE the model-denied `/app` tree (the two-user
# boundary: start.sh's build-time `setfacl -R -m u:$CODE_EXEC_RUN_AS_USER:--- /app` denies the model
# user access to /app). Writing the rubric (verifiers/eval/scoring) + result.json here — instead of a
# world-traversable /tmp — keeps them unreadable/untamperable by the unprivileged model user. Falls
# back to the system default only when absent (dev/test, single-user worlds). See PR security notes.
_GRADE_WORK_DIR = os.environ.get("GRADING_WORK_DIR", "/app/.grading")
# SSRF boundary for snapshot downloads: /grade can be POSTed by the confined model over localhost,
# and _download fetches URLs AS ROOT. Only fetch https URLs whose host ends with an allowed suffix
# (the presigned-snapshot S3 host) so a crafted /grade can't turn the downloader into an SSRF against
# internal services / cloud metadata. Comma-separated; default covers AWS S3. Empty disables the
# suffix check ONLY for a custom single-host deploy that sets GRADING_SNAPSHOT_URL_HOSTS instead.
_SNAPSHOT_HOST_SUFFIXES = tuple(
    s.strip()
    for s in os.environ.get("GRADING_SNAPSHOT_HOST_SUFFIXES", ".amazonaws.com").split(
        ","
    )
    if s.strip()
)
# Exact-host allowlist (comma-separated) for custom S3 endpoints (MinIO, etc.); empties by default.
_SNAPSHOT_HOSTS = frozenset(
    h.strip()
    for h in os.environ.get("GRADING_SNAPSHOT_URL_HOSTS", "").split(",")
    if h.strip()
)
_SNAPSHOT_DOWNLOAD_TIMEOUT_S = float(
    os.environ.get("GRADING_SNAPSHOT_TIMEOUT_S", "120")
)
# Allowlist of env-var names accepted from the request's grading_credentials_json — LLM grading
# credentials ONLY. Mirrors GRADING_CREDENTIAL_ENV_NAMES + DIRECT_GRADING_RUNTIME_ENV_NAMES in
# rl-studio packages/islands/shared/grading_credentials.py (duplicated because the env-runner cannot
# import from the server). This is a SECURITY BOUNDARY, not a convenience filter: /grade is
# unauthenticated in hosted-envs (no API_SIGNING_PUBLIC_KEY) and the runner binds 0.0.0.0, so the
# confined model user CAN POST /grade over localhost. Merging arbitrary keys into the ROOT grade
# subprocess env would let it inject loader/exec controls (LD_PRELOAD, LD_LIBRARY_PATH, PYTHONPATH,
# PATH) and run code AS ROOT — bypassing the uid split this endpoint depends on. Only these names
# pass; every other key (loader/exec controls included) is dropped.
_ALLOWED_GRADING_CRED_KEYS = frozenset(
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
    }
)


def _snapshot_url_allowed(url: str) -> bool:
    """True if ``url`` is safe for the root downloader to fetch (SSRF guard). Requires https and a
    host on the exact-host allowlist or ending with an allowed suffix (see _SNAPSHOT_HOST_*)."""
    try:
        p = urlparse(url)
    except ValueError:
        return False
    if p.scheme != "https" or not p.hostname:
        return False
    host = p.hostname
    if host in _SNAPSHOT_HOSTS:
        return True
    return any(host.endswith(sfx) for sfx in _SNAPSHOT_HOST_SUFFIXES)


def _transcode_archive_to_zip(src: Path, dest: Path) -> None:
    """Normalize a downloaded snapshot archive at ``src`` into a ZIP at ``dest`` (the grading CLI
    reads ZIP). Detects the format by magic bytes: a ``.tar.zst`` (the current prebuilt form) is
    zstd-decompressed and re-zipped; an already-ZIP archive is copied through. Streamed throughout
    (bounded memory) so a large snapshot can't OOM the sandbox."""
    with open(src, "rb") as fh:
        magic = fh.read(4)
    if magic[:2] == b"PK":  # already a zip
        shutil.copyfile(src, dest)
        return
    if magic != b"\x28\xb5\x2f\xfd":  # zstd magic
        raise HTTPException(
            status_code=502, detail="snapshot archive is neither zip nor zstd"
        )
    dctx = zstandard.ZstdDecompressor()
    with (
        open(src, "rb") as raw,
        dctx.stream_reader(raw) as reader,
        tarfile.open(fileobj=reader, mode="r|") as tf,  # streaming tar (no seek)
        # ZIP_STORED, not DEFLATE: the source is already zstd-compressed and this zip is a transient
        # artifact the grader reads once — recompressing wastes CPU in the sandbox for a temporary
        # size win. Mirrors the lane's transcode (modal_helpers) + the grading-probe zip.
        zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as zf,
    ):
        for member in tf:
            if not member.isfile():
                continue
            fsrc = tf.extractfile(member)
            if fsrc is not None:
                with zf.open(member.name, "w", force_zip64=True) as zdst:
                    shutil.copyfileobj(fsrc, zdst)


async def _download_snapshot(url: str, dest: Path) -> None:
    """Download a presigned snapshot archive and normalize it to a ZIP at ``dest`` (streamed to disk,
    bounded memory). Rejects a URL that fails the SSRF allowlist and does NOT follow redirects (a
    redirect could point the root downloader past the host allowlist). Raises on a non-2xx so the
    caller falls back to the async lane rather than grading against a missing baseline."""
    if not _snapshot_url_allowed(url):
        raise HTTPException(
            status_code=400,
            detail=f"snapshot URL host not allowed: {urlparse(url).hostname!r}",
        )
    with tempfile.NamedTemporaryFile(dir=dest.parent, suffix=".dl") as tmp:
        async with httpx.AsyncClient(
            timeout=_SNAPSHOT_DOWNLOAD_TIMEOUT_S, follow_redirects=False
        ) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes():
                    tmp.write(chunk)
        tmp.flush()
        await asyncio.to_thread(_transcode_archive_to_zip, Path(tmp.name), dest)


def _filter_grading_credentials(raw_json: str) -> dict[str, str]:
    """Parse ``grading_credentials_json`` and keep ONLY allowlisted LLM credential keys.

    Security boundary (see ``_ALLOWED_GRADING_CRED_KEYS``): everything not on the allowlist — most
    importantly loader/exec controls like ``LD_PRELOAD`` / ``PYTHONPATH`` / ``PATH`` — is dropped, so
    a crafted ``/grade`` cannot steer the root grade subprocess. Malformed or non-object JSON yields
    an empty dict (grade proceeds with no injected creds → LLM verifiers fail → lane fallback)."""
    try:
        creds = json.loads(raw_json or "{}")
    except json.JSONDecodeError:
        return {}
    if not isinstance(creds, dict):
        return {}
    return {
        str(k): str(v) for k, v in creds.items() if str(k) in _ALLOWED_GRADING_CRED_KEYS
    }


def grading_available() -> bool:
    """True when the grading venv is present (mounted at /app/grading), so ``/grade`` can actually run.

    ``main.py`` gates ``include_router`` on this, so a sandbox WITHOUT the grader never surfaces the
    endpoint (404) rather than exposing a ``/grade`` that only 500s at request time — smaller attack
    surface, and a clearer signal than a runtime subprocess failure.
    """
    return os.path.exists(_GRADING_VENV_PYTHON)


class GradeRequest(BaseModel):
    """Everything the grading CLI needs, supplied by Studio/servicer as opaque JSON (never parsed
    here — written straight to files for the CLI). The graded (FINAL) state is captured live in this
    container; the diff BASELINE + goldens arrive as presigned ZIP URLs (optional — empty when the
    world provides none, or for a golden a sandbox that lacks uid separation)."""

    grading_run_id: str
    trajectory_id: str
    trajectory_json: str
    grading_settings_json: str
    verifiers_json: str
    eval_configs_json: str
    scoring_config_json: str
    # JSON object {env_name: value} of LLM grading credentials, injected into the grade subprocess
    # env ONLY (see grade()). Defaults to empty so an older Studio that doesn't send it still grades
    # (deterministic verifiers work; LLM verifiers then auth-fail → the run errors → lane fallback).
    grading_credentials_json: str = "{}"
    # Presigned snapshot ZIP URLs (empty = unsupported/none). initial_snapshot_url is the diff
    # BASELINE (the world-seed state); golden_snapshot_urls are golden end states. Downloaded here
    # for the CLI's --initial-snapshot / --golden-snapshot. Empty initial → an empty baseline (every
    # seeded file reads as agent-created) so diff verifiers should be gated off upstream in that case.
    initial_snapshot_url: str = ""
    golden_snapshot_urls: list[str] = []


class GradeResponse(BaseModel):
    """The grading CLI's raw output JSON (verifier_results + scoring_results), passed through for the
    caller to record in Studio."""

    result: dict[str, Any]


def _capture_live_final_snapshot(dest: Path) -> None:
    """Zip the LIVE sandbox state (``filesystem`` + ``.apps_data``) to ``dest`` — no S3 round-trip.

    The env produces ``tar.gz`` but the grading engine reads ``zip`` (see the environment README), so
    convert in place: this is the snapshot the lane would otherwise upload + re-download.

    Streams throughout so peak memory is a small copy buffer, NOT the whole workspace: the tar goes
    to a temp FILE (not an in-RAM BytesIO), and each member is copied into the zip with
    ``copyfileobj`` (not read fully into RAM). Runs in a worker thread (see ``grade()``), off the
    event loop. The temp tar lives next to ``dest`` (the root-owned 0700 grade dir) and is removed on
    close.
    """
    with tempfile.NamedTemporaryFile(dir=dest.parent, suffix=".tar.gz") as tmp:
        for chunk in create_tar_gz_stream(
            _SNAPSHOT_SUBSYSTEMS, "live-grade", iter_paths
        ):
            tmp.write(chunk)
        tmp.flush()
        tmp.seek(0)
        with (
            tarfile.open(tmp.name, mode="r:gz") as tf,
            # ZIP_STORED (see _transcode_archive_to_zip): the source is already gzip'd and this zip is
            # read once by the grader — don't spend sandbox CPU recompressing a transient artifact.
            zipfile.ZipFile(dest, "w", zipfile.ZIP_STORED) as zf,
        ):
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                src = tf.extractfile(member)
                if src is not None:
                    # force_zip64: streaming with zf.open() writes members of UNKNOWN size, so
                    # zipfile can't infer ZIP64 the way writestr(len(data)) did — a >2 GiB live-state
                    # file would raise LargeZipFile on close and 500 the grade. Mirrors the rest of
                    # the snapshot-zip path, which passes this for the same reason.
                    with zf.open(member.name, "w", force_zip64=True) as zdst:
                        shutil.copyfileobj(src, zdst)


@router.post("/grade")
async def grade(request: GradeRequest) -> GradeResponse:
    """Grade the live episode in-container via the grading CLI and return its result JSON.

    Serialized: overlapping grades would race the one mutable sandbox filesystem (see _GRADE_LOCK).
    """
    async with _GRADE_LOCK:
        return await _grade(request)


async def _grade(request: GradeRequest) -> GradeResponse:
    # Scratch dir INSIDE the model-denied /app tree (root-owned, 0700) so the rubric config +
    # result.json can't be read or raced by the unprivileged model user. Create it if absent — the
    # grading engine now arrives via a mounted volume, so nothing pre-makes this dir (the runner runs
    # as root, and /app is root-owned, so the model cannot pre-empt it).
    work_base: str | None = _GRADE_WORK_DIR
    try:
        os.makedirs(work_base, mode=0o700, exist_ok=True)
        os.chmod(work_base, 0o700)
    except OSError:
        work_base = None
    # FAIL CLOSED, not open: falling back to a model-readable /tmp is only safe with NO separate
    # model user. Under uid separation (CODE_EXEC_RUN_AS_USER set) an unusable GRADING_WORK_DIR is a
    # misconfig — refuse rather than silently leak the rubric to /tmp.
    if work_base is None and os.environ.get("CODE_EXEC_RUN_AS_USER"):
        raise HTTPException(
            status_code=500,
            detail=(
                f"GRADING_WORK_DIR {_GRADE_WORK_DIR!r} is missing under uid separation "
                "(CODE_EXEC_RUN_AS_USER set); refusing to grade into a model-readable /tmp"
            ),
        )
    with tempfile.TemporaryDirectory(prefix="grade-", dir=work_base) as tmp:
        # Defensive: TemporaryDirectory is already 0700, but re-assert it so a permissive umask or a
        # pre-existing work_base can't leave the rubric group/other-readable.
        os.chmod(tmp, 0o700)
        d = Path(tmp)

        # 1. Caller-supplied config -> files (opaque; the CLI validates them).
        (d / "trajectory.json").write_text(request.trajectory_json)
        (d / "grading_settings.json").write_text(request.grading_settings_json)
        (d / "verifiers.json").write_text(request.verifiers_json)
        (d / "eval_configs.json").write_text(request.eval_configs_json)
        (d / "scoring_config.json").write_text(request.scoring_config_json)

        # 2. Snapshots. The FINAL state is captured LIVE here. The BASELINE (initial) + goldens arrive
        # as presigned ZIP URLs (already zips — the prebuilt one-GET grading artifact), downloaded
        # here for diff / golden-state verifiers. No URL → an empty baseline (task-only grade; final
        # is what matters), so diff verifiers should be gated off upstream when there's no baseline.
        initial = d / "initial.zip"
        # The baseline is the world-SEED state (not secret — the model already ran against those
        # seeded files), so it's fetched whenever provided.
        if request.initial_snapshot_url:
            await _download_snapshot(request.initial_snapshot_url, initial)
        else:
            with zipfile.ZipFile(initial, "w"):
                pass
        # Goldens are the ANSWER KEY. They land in the root-owned 0700 grade dir, but that only hides
        # them from the model under uid separation. WITHOUT a separate model user the model runs as
        # root and could read them, so REFUSE to download goldens then — golden-state verifiers fall
        # back to the async lane rather than leaking the key. (Same boundary as the rubric scratch.)
        goldens: list[Path] = []
        if request.golden_snapshot_urls and not os.environ.get("CODE_EXEC_RUN_AS_USER"):
            raise HTTPException(
                status_code=409,
                detail=(
                    "golden snapshots require uid separation (CODE_EXEC_RUN_AS_USER); "
                    "refusing to download the answer key into a model-readable sandbox"
                ),
            )
        for i, url in enumerate(request.golden_snapshot_urls):
            g = d / f"golden_{i}.zip"
            await _download_snapshot(url, g)
            goldens.append(g)
        final = d / "final.zip"
        # Offload the synchronous tar→zip capture to a thread: it is CPU-bound and would otherwise
        # block the FastAPI event loop (stalling /health + MCP) for the whole capture.
        await asyncio.to_thread(_capture_live_final_snapshot, final)

        # 3. Run the grading engine CLI in its own venv (no in-process import — see module docstring).
        out = d / "result.json"
        cmd = [
            _GRADING_VENV_PYTHON,
            "-m",
            "runner.main",
            "--grading-run-id",
            request.grading_run_id,
            "--trajectory-id",
            request.trajectory_id,
            "--initial-snapshot",
            str(initial),
            "--final-snapshot",
            str(final),
            "--trajectory",
            str(d / "trajectory.json"),
            "--grading-settings",
            str(d / "grading_settings.json"),
            "--verifiers",
            str(d / "verifiers.json"),
            "--eval-configs",
            str(d / "eval_configs.json"),
            "--scoring-config",
            str(d / "scoring_config.json"),
            "--output",
            str(out),
        ]
        for g in goldens:
            cmd += ["--golden-snapshot", str(g)]

        # Run in the grading install dir with PYTHONPATH stripped so `-m runner.main` resolves to the
        # GRADING runner (mounted at _GRADING_INSTALL_DIR/.venv), NOT the env runner package — which is
        # ALSO named `runner` (/app/runner on the server's path). Without this, cwd/PYTHONPATH inherited
        # from the env runner would shadow the grading engine. Config paths passed to the CLI are
        # absolute, so the cwd change is safe.
        # Start from the runner's env MINUS PYTHONPATH (see above) AND minus every grading-credential
        # name. Dropping the inherited creds is a SECURITY boundary, not cleanup: the whole credential
        # set (keys AND *_BASE_URL endpoints) must come only from the request, together. Otherwise a
        # crafted /grade could supply just a BASE_URL override (allowlisted) and pair it with a REAL
        # key inherited from os.environ — root grading would then send that key to an attacker's URL.
        sub_env = {
            k: v
            for k, v in os.environ.items()
            if k != "PYTHONPATH" and k not in _ALLOWED_GRADING_CRED_KEYS
        }
        # LLM grading credentials arrive in the request body and are injected into THIS subprocess's
        # env only — never into os.environ (which the model agent inherits via env=os.environ.copy())
        # and never to disk. The subprocess runs as root inside model-denied /app, so the unprivileged
        # model user can't read them. Empty when Studio omits them.
        #
        # ALLOWLIST (security-critical): /grade is unauthenticated in hosted-envs and reachable by the
        # confined model over localhost, so only KNOWN credential names pass — never loader/exec
        # controls (LD_PRELOAD, PYTHONPATH, PATH, …) that would let a crafted /grade run code as root.
        sub_env.update(_filter_grading_credentials(request.grading_credentials_json))
        sub_cwd = _GRADING_INSTALL_DIR if os.path.isdir(_GRADING_INSTALL_DIR) else None
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=sub_cwd,
            env=sub_env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # Own process group so a timeout can kill the WHOLE tree, not just the CLI parent —
            # code-executing verifiers (llm_code_verifier / agentic_verifier) spawn grandchildren
            # that would otherwise keep consuming sandbox resources after we've fallen back.
            start_new_session=True,
        )
        try:
            _, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=_GRADING_TIMEOUT_S
            )
        except TimeoutError:
            # Kill the process GROUP, not just proc, so verifier grandchildren die too.
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass  # already gone
            # reap the killed child so repeated timeouts don't accumulate zombies
            await proc.wait()
            raise HTTPException(status_code=504, detail="grading timed out") from None
        if proc.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail=f"grading failed (exit {proc.returncode}): {stderr.decode()[-500:]}",
            )

        # 4. Return the CLI's result JSON verbatim for the caller to record in Studio.
        return GradeResponse(result=json.loads(out.read_text()))

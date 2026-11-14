"""Builds the keyword index for this environment, never failing the caller.

The normal path is the app's ``populate`` task (``python -m keyword_search.populate``),
which Studio runs after the world and task files are in place and before the
agent starts. It copies any index uploaded to ``STATE_LOCATION`` and indexes only
the files those copies miss, or builds one from scratch when nothing was uploaded.
When no populate build happened, the first search starts the build in a
background thread.

The working indexes live in a private scratch folder, never in ``STATE_LOCATION``:
Studio saves ``/.apps_data`` with every run, and uploads must stay untouched.
"""

import os
import shutil
import stat
import threading
from dataclasses import dataclass
from pathlib import Path

from loguru import logger
from mcp_actor import paths as actor_paths
from utils.path_utils import resolve_under_root

from keyword_search.config import KeywordSearchConfig, state_location
from keyword_search.index import (
    INDEX_SUFFIX,
    BuildSummary,
    RefreshSummary,
    build_index,
    find_index_files,
    is_current_index,
    refresh_index,
)

RUNTIME_INDEX_NAME = "keyword_index_built.sqlite"
_UPLOAD_COPY_PREFIX = "uploaded_"
BUILD_BASE = Path("/tmp")


def build_dir(*, create: bool = False) -> Path | None:
    """This app user's private scratch folder for the built index, if it is safe.

    It must be a real folder (not a link) owned by this user; anything else could
    let another app read or swap the index, so it is refused.
    """
    folder = BUILD_BASE / f"keyword_index-{os.getuid()}"
    try:
        if create:
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = os.lstat(folder)
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.warning(f"keyword index folder {folder} is not usable: {exc}")
        return None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid():
        logger.error(
            f"keyword index folder {folder} is not a private folder; not using it"
        )
        return None
    if info.st_mode & 0o077:
        os.chmod(folder, 0o700)
    return folder


def working_indexes() -> list[Path]:
    """This run's indexes: copies of uploads first, then the built one."""
    folder = build_dir()
    return find_index_files(folder) if folder is not None else []


@dataclass
class BuildProgress:
    done: int = 0
    total: int = 0


_build_finished = threading.Event()
_build_finished.set()
_fallback_lock = threading.Lock()
_refresh_lock = threading.Lock()
_fallback_used = False
progress = BuildProgress()


def build_in_progress() -> bool:
    return not _build_finished.is_set()


def wait_for_build(seconds: float) -> bool:
    """Wait up to ``seconds`` for a background build; True once it has finished."""
    return _build_finished.wait(seconds)


def _resolve_folder(root: Path, folder: str) -> Path:
    try:
        resolved = Path(
            resolve_under_root(
                folder, root=str(root), check_exists=True, must_be_dir=True
            )
        )
    except (FileNotFoundError, ValueError) as exc:
        logger.warning(
            f"keyword search folder {folder!r} is not usable ({exc}); indexing {root}"
        )
        return root
    return resolved


def _copy_private(source: Path, target: Path) -> None:
    target.unlink(missing_ok=True)
    os.close(os.open(target, os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600))
    with source.open("rb") as src, target.open("r+b") as dst:
        shutil.copyfileobj(src, dst)


def _copy_uploads(folder_for_index: Path) -> list[Path]:
    """Copy every current-format uploaded index into the private folder."""
    for stale in folder_for_index.glob(f"*{INDEX_SUFFIX}*"):
        stale.unlink(missing_ok=True)
    copies: list[Path] = []
    for number, upload in enumerate(find_index_files(Path(state_location()))):
        if not is_current_index(upload):
            logger.warning(f"{upload} is not a current keyword index; skipped")
            continue
        target = folder_for_index / f"{_UPLOAD_COPY_PREFIX}{number:02d}{INDEX_SUFFIX}"
        _copy_private(upload, target)
        copies.append(target)
    return copies


def build_for_config(
    config: KeywordSearchConfig, built_by: str = "populate"
) -> BuildSummary | RefreshSummary | None:
    """Prepare this run's index. Logs and returns None on any failure."""
    try:
        root = Path(actor_paths.TARGET_AGENT_FILESYSTEM_ROOT)
        folder_for_index = build_dir(create=True)
        if folder_for_index is None:
            logger.error(
                "keyword index build skipped: no private folder to write it to"
            )
            return None
        folder = _resolve_folder(root, config.folder)
        copies = _copy_uploads(folder_for_index)
        if copies:
            summary = refresh_index(
                root,
                copies,
                folder=folder,
                file_types=config.file_types,
                max_seconds_per_file=config.max_seconds_per_file,
                max_seconds=config.max_build_minutes * 60,
                max_total_chars=int(config.max_total_text_mb * 1024 * 1024),
                trust_size=True,
            )
            logger.info(
                f"keyword index from {len(copies)} uploaded index(es) in "
                f"{summary.seconds:.1f}s: {summary.unchanged} already covered, "
                f"{summary.added} added, {summary.updated} re-indexed, "
                f"{summary.removed} dropped, {summary.pending} left for later, "
                f"{summary.over_text_limit} over the text limit"
            )
            if summary.unchanged == 0 and (summary.added or summary.pending):
                logger.warning(
                    "uploaded keyword index matched none of the files; check its --prefix"
                )
            return summary
        out_path = folder_for_index / RUNTIME_INDEX_NAME
        logger.info(f"keyword index build started: {folder} -> {out_path}")

        def on_progress(done: int, total: int) -> None:
            progress.done, progress.total = done, total

        summary = build_index(
            root,
            out_path,
            folder=folder,
            file_types=config.file_types,
            max_seconds_per_file=config.max_seconds_per_file,
            max_total_chars=int(config.max_total_text_mb * 1024 * 1024),
            max_build_seconds=config.max_build_minutes * 60,
            on_progress=on_progress,
            built_by=built_by,
        )
        progress.done = progress.total = summary.files_total
        logger.info(
            f"keyword index build finished: {summary.files_total} files in "
            f"{summary.seconds:.1f}s {summary.counts}"
        )
        return summary
    except Exception as exc:
        logger.exception(f"keyword index build failed: {exc}")
        return None


def refresh_built_index(config: KeywordSearchConfig) -> RefreshSummary | None:
    """Update this run's indexes for files added, changed or removed since; None if there are none."""
    if build_in_progress():
        return None
    indexes = working_indexes()
    if not indexes:
        return None
    with _refresh_lock:
        try:
            root = Path(actor_paths.TARGET_AGENT_FILESYSTEM_ROOT)
            summary = refresh_index(
                root,
                indexes,
                folder=_resolve_folder(root, config.folder),
                file_types=config.file_types,
                max_seconds_per_file=config.max_seconds_per_file,
                max_seconds=config.max_refresh_seconds,
                max_total_chars=int(config.max_total_text_mb * 1024 * 1024),
            )
        except Exception as exc:
            logger.exception(f"keyword index refresh failed: {exc}")
            return None
    if summary.added or summary.updated or summary.removed or summary.pending:
        logger.info(
            f"keyword index refreshed in {summary.seconds:.2f}s: {summary.added} added, "
            f"{summary.updated} updated, {summary.removed} removed, {summary.pending} pending"
        )
    return summary


def _run_in_background(config: KeywordSearchConfig) -> None:
    try:
        build_for_config(config, built_by="search_fallback")
    finally:
        _build_finished.set()


def start_background_build(config: KeywordSearchConfig) -> threading.Thread:
    _build_finished.clear()
    thread = threading.Thread(
        target=_run_in_background,
        args=(config,),
        name="keyword-index-build",
        daemon=True,
    )
    thread.start()
    return thread


def start_fallback_build_once(config: KeywordSearchConfig) -> bool:
    """Start one background build per server process; True if this call started it."""
    global _fallback_used
    with _fallback_lock:
        if _fallback_used or build_in_progress():
            return False
        _fallback_used = True
    start_background_build(config)
    return True

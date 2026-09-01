"""Shared snapshot utility functions for file-based verifiers."""

import json
import zipfile
from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput

EXPORTS_BASE = "filesystem/exports"

# The same files the gateway reads, so they are the ground truth for what the
# agent saw. Two layers at two filenames (world defaults, task overrides): the
# initial snapshot merges the world and task snapshots key-by-key, so one
# filename would mean a task renaming a single tool discards the world's.
TOOL_ALIASES_FILENAME = "tool_aliases.json"
WORLD_TOOL_ALIASES_FILENAME = "tool_aliases.world.json"
# World first: later entries win the dict update, and the task layer overrides.
_TOOL_ALIAS_FILENAMES = (WORLD_TOOL_ALIASES_FILENAME, TOOL_ALIASES_FILENAME)


def load_tool_aliases(snapshot_zip: zipfile.ZipFile) -> dict[str, dict[str, str]]:
    """Load per-app tool aliases from .apps_data/<app>/.config/tool_aliases*.json.

    Returns ``{app_name: {alias: canonical}}`` — inverted from the files'
    canonical->alias, because grading normalizes in that direction. Absent for
    normal (non-aliased) rollouts, so an empty dict means "no normalization
    needed". Malformed files are skipped: grading must never crash on a bad
    config.

    Unioned in the FILES' direction and inverted ONCE. Inverting each layer and
    merging the results is wrong: a world ``{read: fetch}`` overridden by a task
    ``{read: grab}`` would keep ``fetch -> read``, a name never served.
    """
    canonical_to_alias: dict[str, dict[str, str]] = {}
    by_filename: dict[str, list[str]] = {f: [] for f in _TOOL_ALIAS_FILENAMES}
    for name in snapshot_zip.namelist():
        parts = name.split("/")
        if (
            len(parts) == 4
            and parts[0] == ".apps_data"
            and parts[2] == ".config"
            and parts[3] in by_filename
        ):
            by_filename[parts[3]].append(name)

    for filename in _TOOL_ALIAS_FILENAMES:
        for name in by_filename[filename]:
            try:
                payload = json.loads(snapshot_zip.read(name))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue
            mapping = payload.get("aliases") if isinstance(payload, dict) else None
            if isinstance(mapping, dict):
                app = name.split("/")[1]
                canonical_to_alias.setdefault(app, {}).update(
                    {str(c): str(a) for c, a in mapping.items()}
                )

    # Drop an app whose union is unroutable, mirroring the gateway: it serves
    # that app CANONICAL, so normalizing here would rewrite calls made under
    # names never offered. The inversion would otherwise keep only the last.
    out: dict[str, dict[str, str]] = {}
    for app, mapping in canonical_to_alias.items():
        if not mapping:
            continue
        if len(set(mapping.values())) != len(mapping):
            logger.warning(
                f"Skipping tool aliases for {app!r}: the world and task layers "
                "do not compose into a routable map, so the rollout served "
                "canonical names for it"
            )
            continue
        out[app] = {alias: canonical for canonical, alias in mapping.items()}
    return out


def _tool_aliases_from(snapshot: Any, which: str) -> dict[str, dict[str, str]]:
    """Read alias configs out of one snapshot, or {} on any failure."""
    if snapshot is None:
        return {}
    try:
        snapshot.seek(0)
        with zipfile.ZipFile(snapshot, "r") as zf:
            return load_tool_aliases(zf)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Failed to load tool aliases from {which} snapshot: {e}")
        return {}


def load_snapshot_tool_aliases(input: EvalImplInput) -> dict[str, dict[str, str]]:
    """Best-effort load of the rollout's tool alias configs from the snapshot.

    Shared by every verifier that matches on trajectory tool names, since the
    trajectory records the ALIAS the agent emitted while verifiers are
    authored against canonical names. Returns {} for normal (non-aliased)
    rollouts and on any read failure — a missing or corrupt config must
    degrade to today's behavior, never fail grading.

    Reads the INITIAL snapshot, not the final one. The config is staged before
    the agent starts and is what the gateway already applied, so the initial
    snapshot is both the correct record and — unlike the final snapshot, which
    is agent-visible, writable state under ``.apps_data`` — one the agent
    cannot edit. Trusting the final snapshot would let an agent delete or
    rewrite its own ``tool_aliases.json`` to empty the remap, leaving
    trajectory names aliased so canonical matching silently fails: the
    ``expect_absent`` guard would pass while the forbidden tool was in fact
    called (Bugbot: "Grading trusts mutable final snapshot").

    Falls back to the final snapshot only when there is no initial snapshot at
    all, since normalizing from a tamperable source still beats not
    normalizing — but it says so in the log.
    """
    aliases = _tool_aliases_from(input.initial_snapshot_bytes, "initial")
    if aliases or input.initial_snapshot_bytes is not None:
        return aliases
    fallback = _tool_aliases_from(input.final_snapshot_bytes, "final")
    if fallback:
        logger.warning(
            "No initial snapshot; read tool aliases from the agent-writable "
            "final snapshot. Verifier matching is only as trustworthy as that "
            f"file ({len(fallback)} app(s))."
        )
    return fallback


def find_files_in_snapshot(
    snapshot_zip: zipfile.ZipFile, extension: str, base_path: str = ""
) -> list[str]:
    """Find all files with given extension in the snapshot zip.

    base_path should be the full prefix as it appears in the zip
    (e.g. '.apps_data/kicad_mcp/projects' or 'filesystem/exports').
    """
    prefix = f"{base_path}/" if base_path else ""
    return [
        name
        for name in snapshot_zip.namelist()
        if name.startswith(prefix) and name.lower().endswith(extension.lower())
    ]


def export_file_exists(snapshot_zip: zipfile.ZipFile, file_path: str) -> bool:
    """Check if an export file exists in the snapshot."""
    full_path = f"{EXPORTS_BASE}/{file_path.lstrip('/')}"
    return full_path in snapshot_zip.namelist()


def count_export_files(
    snapshot_zip: zipfile.ZipFile, directory: str, extension: str = ""
) -> int:
    """Count export files in a directory."""
    prefix = f"{EXPORTS_BASE}/{directory.strip('/')}/"
    files = [
        name
        for name in snapshot_zip.namelist()
        if name.startswith(prefix) and not name.endswith("/")
    ]
    if extension:
        files = [f for f in files if f.lower().endswith(extension.lower())]
    return len(files)

"""Shared snapshot utility functions for file-based verifiers."""

import json
import zipfile
from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput

EXPORTS_BASE = "filesystem/exports"

# Per-task tool-name aliasing config, authored in Studio and staged into the
# task snapshot (see the tool-alias-config endpoints in packages/tasks). The
# same file the MCP gateway reads to decide what to serve, so it is also the
# ground truth for what the agent saw.
TOOL_ALIASES_FILENAME = "tool_aliases.json"


def load_tool_aliases(snapshot_zip: zipfile.ZipFile) -> dict[str, dict[str, str]]:
    """Load per-app tool aliases from .apps_data/<app>/.config/tool_aliases.json.

    Returns ``{app_name: {alias: canonical}}`` — inverted from the file's
    canonical->alias, because grading normalizes in that direction. Absent for
    normal (non-aliased) rollouts, so an empty dict means "no normalization
    needed". Malformed files are skipped: grading must never crash on a bad
    config.
    """
    aliases: dict[str, dict[str, str]] = {}
    for name in snapshot_zip.namelist():
        parts = name.split("/")
        if not (
            len(parts) == 4
            and parts[0] == ".apps_data"
            and parts[2] == ".config"
            and parts[3] == TOOL_ALIASES_FILENAME
        ):
            continue
        try:
            payload = json.loads(snapshot_zip.read(name))
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue
        mapping = payload.get("aliases") if isinstance(payload, dict) else None
        if isinstance(mapping, dict):
            aliases[parts[1]] = {str(a): str(c) for c, a in mapping.items()}
    return aliases


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

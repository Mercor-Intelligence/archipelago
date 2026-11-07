"""Shared snapshot utility functions for file-based verifiers."""

import json
import re
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

# Mirrors TOOL_ALIAS_NAME_RE and ToolAliasSpec's field set in the gateway.
# Re-declared, not imported: this package ships separately from the runner, the
# same reason the gateway re-declares them from Studio's ToolAliasConfig.
_ALIAS_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_ALIAS_CONFIG_KEYS = frozenset({"aliases", "disabled_tools"})


def _routable_within_one_file(aliases: dict[str, str]) -> bool:
    """The two rules ``ToolAliasSpec`` applies to ONE file's map.

    The post-union routability check does not subsume these. It runs on the
    MERGED map, so a bad file's entries have already been folded in with a good
    file's — the collision then condemns the whole app, where the gateway
    rejected only the offending file and kept the other layer.
    """
    canonicals = set(aliases)
    owners: dict[str, str] = {}
    for canonical, alias in aliases.items():
        # A renamed tool frees its own name, so a swap ({a: b, b: c}) is
        # routable; an identity entry ({b: b}) frees nothing.
        if alias != canonical and alias in canonicals and aliases[alias] == alias:
            return False
        owner = owners.get(alias)
        if owner is not None and owner != canonical:
            return False
        owners[alias] = canonical
    return True


def _one_file_as_the_gateway_reads_it(
    payload: dict[str, Any],
) -> tuple[dict[str, str], list[str]]:
    """``(aliases, withheld)`` for ONE file, as ``ToolAliasSpec`` would take it.

    The gateway parses each file with a model that FORBIDS unknown keys and
    charset-checks every name. A file it rejects serves that app canonical —
    so grading must not normalize on that file's renames, or it credits a name
    the agent was never offered. Returning the renames unchecked made a
    presence check pass on a call that errored, and an ``expect_absent`` guard
    fail on a run that never made the call.

    Withheld names are kept even from a rejected file, mirroring the gateway's
    own salvage: dropping a rename costs a cosmetic name, dropping a withhold
    serves a tool. Only ever REMOVES entries from the rename map, so this can
    never credit a call to a tool it was not.

    THE ORDER IS THE CONTRACT, and it is not the obvious one. The model runs:
    coerce ``aliases`` to ``dict[str, str]`` (pydantic, before the validator) ->
    charset-check ``disabled_tools`` -> DELETE alias entries whose canonical is
    disabled -> charset-check what SURVIVED -> collision checks. So the drop
    sits between the two checks, and only the second one ever sees a shrunken
    map. Six review rounds moved this line and each time it landed one step
    short.

    Not a reimplementation of the whole model: the duplicate-alias and
    canonical-collision rules are already covered by the routability check that
    runs on the merged map, and a charset-invalid withheld name matches no live
    tool, so dropping it would buy nothing.
    """
    withheld: list[str] = []
    raw_disabled = payload.get("disabled_tools", [])
    disabled_ok = isinstance(raw_disabled, list)
    if disabled_ok:
        for name in raw_disabled:
            if isinstance(name, str) and _ALIAS_NAME_RE.match(name):
                if name not in withheld:
                    withheld.append(name)
            else:
                # The model refuses the file over this, but the names it CAN
                # read still withhold.
                disabled_ok = False

    rejected = bool(set(payload) - _ALIAS_CONFIG_KEYS) or not disabled_ok
    mapping = payload.get("aliases", {})
    if not isinstance(mapping, dict):
        rejected, mapping = True, {}
    # TYPE first, and only type. ``aliases`` is declared ``dict[str, str]``, so
    # a non-string entry fails pydantic's COERCION — the model refuses the file
    # before the validator body runs, and no disable can rescue it. Everything
    # below this point is the validator, which the disable-drop sits inside.
    aliases: dict[str, str] = {}
    for canonical, alias in mapping.items():
        if not (isinstance(canonical, str) and isinstance(alias, str)):
            rejected = True
            break
        aliases[canonical] = alias
    # Disable wins BEFORE the charset and routability checks, which is where the
    # model puts it: a withheld tool has no served name, so its rename is deleted
    # and never judged at all. Judging the raw map rejected a whole file over an
    # entry the gateway had already dropped, and took every OTHER rename in that
    # file down with it — the agent called the served alias and grading, reading
    # no renames, never mapped it back.
    if withheld:
        still_served = set(withheld)
        aliases = {c: a for c, a in aliases.items() if c not in still_served}
    if not rejected and not all(
        _ALIAS_NAME_RE.match(c) and _ALIAS_NAME_RE.match(a) for c, a in aliases.items()
    ):
        rejected = True
    if not rejected and not _routable_within_one_file(aliases):
        rejected = True
    return ({} if rejected else aliases), withheld


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

    DISABLE WINS, exactly as ``ToolAliasSpec`` applies it in the gateway: a
    withheld tool has no served name, so its alias is dead config and must be
    dropped BEFORE the routability check below. Reading only ``aliases`` made
    grading disagree with the rollout in both directions — a stale
    ``fetch -> read`` for a name nothing answered to, and, when the withheld
    entry was the only thing making the union collide, an app dropped here that
    the gateway served aliased.
    """
    canonical_to_alias: dict[str, dict[str, str]] = {}
    disabled_by_app: dict[str, set[str]] = {}
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
            if not isinstance(payload, dict):
                continue
            app = name.split("/")[1]
            aliases, withheld = _one_file_as_the_gateway_reads_it(payload)
            if aliases:
                canonical_to_alias.setdefault(app, {}).update(aliases)
            # Additive across layers, unlike aliases: a task may withhold more
            # than its world does, never less.
            if withheld:
                disabled_by_app.setdefault(app, set()).update(withheld)

    # Disable wins, before routability is judged: the gateway drops these
    # aliases too, so an app whose ONLY collision was a withheld entry is still
    # served aliased and must still be normalized here.
    for app, withheld_names in disabled_by_app.items():
        mapping = canonical_to_alias.get(app)
        if mapping:
            canonical_to_alias[app] = {
                c: a for c, a in mapping.items() if c not in withheld_names
            }

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

import json
import zipfile
from typing import IO, Any

from pydantic import BaseModel, Field

from runner.models import AgentTrajectoryOutput, EvaluationTarget

# These mirror coordinator path constants defined in
# archipelago/environment/runner/coordinator/state/store.py under
# @apg_environment_path_constants. They are duplicated here until that
# environment-owned constants block is synced into the grading environment.
COORDINATOR_PREFIXES = (
    ".apps_data/.coordinator/",
    "tools/runner/gateway/coordinator/",
)
CONFIG_PATH = "config/config.json"
EVENT_OCCURRENCES_DIR = "event_occurrences/"
AGENT_CONFIGS_DIR = "agent_configs/"
RUNS_SEGMENT = "/runs/"
LEGACY_TRAJECTORIES_SEGMENT = "/trajectories/"
INITIAL_MESSAGES_FILENAME = "initial_messages.json"
RUN_RECORD_FILENAME = "run.json"
AGENT_OUTPUT_FILENAME = "output.json"
VCA_RUN_LOGS_FILENAME = "logs.jsonl"
MCP_CALLS_PATH = "checkpoint_observations/mcp_calls.jsonl"


class VcaRunContext(BaseModel):
    vca_id: str
    run_id: str
    initial_messages: list[Any] = Field(default_factory=list)
    run_record: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    logs: list[dict[str, Any]] = Field(default_factory=list)


class VcaContext(BaseModel):
    config: dict[str, Any] | None = None
    event_occurrences: list[dict[str, Any]] = Field(default_factory=list)
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    runs: list[VcaRunContext] = Field(default_factory=list)
    parse_errors: list[str] = Field(default_factory=list)


async def vca_context_helper(
    initial_snapshot_bytes: IO[bytes],
    final_snapshot_bytes: IO[bytes],
    trajectory: AgentTrajectoryOutput,
) -> VcaContext:
    files, errors = _read_coordinator_files(final_snapshot_bytes)
    runs_by_key: dict[tuple[str, str], VcaRunContext] = {}

    for path, raw in files.items():
        if path == CONFIG_PATH:
            continue
        if path == MCP_CALLS_PATH:
            continue
        if path.startswith(EVENT_OCCURRENCES_DIR) and path.endswith(".json"):
            continue

        run_key = _run_key(path)
        if run_key is None:
            continue
        vca_id, run_id, filename = run_key
        run = runs_by_key.setdefault(
            (vca_id, run_id), VcaRunContext(vca_id=vca_id, run_id=run_id)
        )
        try:
            if filename == INITIAL_MESSAGES_FILENAME:
                value = json.loads(raw.decode("utf-8"))
                if isinstance(value, list):
                    run.initial_messages = value
            elif filename == RUN_RECORD_FILENAME:
                value = json.loads(raw.decode("utf-8"))
                if isinstance(value, dict):
                    run.run_record = value
            elif filename == AGENT_OUTPUT_FILENAME:
                value = json.loads(raw.decode("utf-8"))
                if isinstance(value, dict):
                    run.output = value
            elif filename == VCA_RUN_LOGS_FILENAME:
                run.logs = _read_jsonl(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            errors.append(f"{path}: {e}")

    context = VcaContext(
        config=_read_json_object(files, CONFIG_PATH, errors),
        event_occurrences=[
            value
            for path in sorted(files)
            if path.startswith(EVENT_OCCURRENCES_DIR)
            and path.endswith(".json")
            and (value := _read_json_object(files, path, errors)) is not None
        ],
        tool_calls=_read_jsonl(files.get(MCP_CALLS_PATH, b"")),
        runs=sorted(runs_by_key.values(), key=lambda run: (run.vca_id, run.run_id)),
        parse_errors=errors,
    )
    if trajectory.vca_id:
        context.runs = [run for run in context.runs if run.vca_id == trajectory.vca_id]
    elif trajectory.evaluation_target == EvaluationTarget.VIRTUAL_COWORKER_AGENT:
        context.parse_errors.append("VCA grading run is missing vca_id metadata")
        context.runs = []
    return context


def _read_coordinator_files(
    snapshot_bytes: IO[bytes],
) -> tuple[dict[str, bytes], list[str]]:
    files: dict[str, bytes] = {}
    errors: list[str] = []
    snapshot_bytes.seek(0)
    try:
        with zipfile.ZipFile(snapshot_bytes, "r") as zf:
            for name in zf.namelist():
                normalized = _coordinator_relative_path(name)
                if normalized is None:
                    continue
                try:
                    files[normalized] = zf.read(name)
                except OSError as e:
                    errors.append(f"{name}: {e}")
    except zipfile.BadZipFile as e:
        errors.append(f"invalid final snapshot zip: {e}")
    return files, errors


def _coordinator_relative_path(path: str) -> str | None:
    normalized = path.lstrip("/")
    for prefix in COORDINATOR_PREFIXES:
        index = normalized.find(prefix)
        if index >= 0:
            return normalized[index + len(prefix) :]
    return None


def _run_key(path: str) -> tuple[str, str, str] | None:
    if not path.startswith(AGENT_CONFIGS_DIR):
        return None
    segment = RUNS_SEGMENT if RUNS_SEGMENT in path else LEGACY_TRAJECTORIES_SEGMENT
    if segment not in path:
        return None
    prefix, suffix = path.split(segment, 1)
    vca_id = prefix.removeprefix(AGENT_CONFIGS_DIR).strip("/")
    parts = suffix.split("/", 1)
    if len(parts) != 2:
        return None
    run_id, filename = parts
    if not vca_id or not run_id:
        return None
    return vca_id, run_id, filename


def _read_json_object(
    files: dict[str, bytes], path: str, errors: list[str]
) -> dict[str, Any] | None:
    raw = files.get(path)
    if not raw:
        return None
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        errors.append(f"{path}: {e}")
        return None
    return value if isinstance(value, dict) else None


def _read_jsonl(raw: bytes) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not raw:
        return rows
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows

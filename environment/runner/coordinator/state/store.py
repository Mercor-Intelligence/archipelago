import json
import os
from collections.abc import Iterable
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from loguru import logger
from pydantic import JsonValue, ValidationError

from ..agents.models import (
    AgentConfig,
    AgentRunInput,
    AgentRunRecord,
    VirtualCoworkerAgent,
)
from ..checkpoints.models import (
    CheckpointObservations,
    EventOccurrence,
    PhysicalTimeCheckpointObservation,
    ToolCallCheckpointObservation,
)
from ..config.models import CoordinatorConfig
from ..utils import chown_tree, user_home, utc_now, write_json
from ..vca_prompt import build_vca_system_prompt, build_vca_user_prompt

# NOTE: We copy these constants into the Foundry util package
# mercor-mcp-shared. Keep them in sync with:
# - mercor-mcp-shared/packages/mcp_actor/mcp_actor/paths.py
# @apg_environment_path_constants:start
COORDINATOR_ROOT_ENV = "COORDINATOR_ROOT"
DEFAULT_COORDINATOR_ROOT = "/.apps_data/.coordinator"
# Base VCA spawn command. ``--no-sync`` is inserted at spawn time so runtime
# never re-resolves the baked agents venv (see ``prepare_agent_run``).
AGENT_RUNNER_COMMAND = ("uv", "run", "python", "-m", "runner.main")
VCA_FILESYSTEM_DIR_ENV = "VCA_FILESYSTEM_DIR"
# Hands the actor id to the agent runner off argv. Must match
# ``MCP_GATEWAY_ACTOR_ID_ENV`` in ``archipelago/agents/runner/main.py``.
MCP_GATEWAY_ACTOR_ID_ENV = "MCP_GATEWAY_ACTOR_ID"
ARCHIPELAGO_AGENT_DIR_NAME = "archipelago_agent"
AGENT_CONFIG_FILENAME = "agent_config.json"
ORCHESTRATOR_MODEL_FILENAME = "orchestrator_model.txt"
INITIAL_MESSAGES_FILENAME = "initial_messages.json"
ORCHESTRATOR_EXTRA_ARGS_FILENAME = "orchestrator_extra_args.json"
TASK_CUSTOM_FIELDS_FILENAME = "task_custom_fields.json"
INNER_AGENT_CONFIG_FILENAME = "inner_agent_config.json"
RUN_RECORD_FILENAME = "run.json"
AGENT_OUTPUT_FILENAME = "output.json"
VCA_RUN_LOGS_FILENAME = "logs.jsonl"
# The persona's raw stdio. A hard crash writes its traceback to stderr, never
# through the agent's logger, so logs.jsonl cannot hold it.
VCA_RUN_STDOUT_FILENAME = "stdout.txt"
VCA_RUN_STDERR_FILENAME = "stderr.txt"
CONFIG_DIR_NAME = "config"
CHECKPOINT_OBSERVATIONS_DIR_NAME = "checkpoint_observations"
EVENT_OCCURRENCES_DIR_NAME = "event_occurrences"
AGENT_CONFIGS_DIR_NAME = "agent_configs"
AGENT_FILESYSTEMS_DIR_NAME = "agent_filesystems"
AGENT_RUNS_DIR_NAME = "runs"


class StatePlane(StrEnum):
    """Who inside the sandbox may touch a subtree. The grader is outside, and reads all of them."""

    CONTROL = "control"
    RECORD = "record"
    AGENT = "agent"


COORDINATOR_SUBTREE_PLANES: dict[str, StatePlane] = {
    CONFIG_DIR_NAME: StatePlane.CONTROL,
    CHECKPOINT_OBSERVATIONS_DIR_NAME: StatePlane.RECORD,
    EVENT_OCCURRENCES_DIR_NAME: StatePlane.RECORD,
    AGENT_CONFIGS_DIR_NAME: StatePlane.AGENT,
    AGENT_FILESYSTEMS_DIR_NAME: StatePlane.AGENT,
}

# Agent-plane subtrees are per-principal, so one root-only mode is the wrong shape for them.
ROOT_ONLY_SUBTREE_NAMES = tuple(
    name
    for name, plane in COORDINATOR_SUBTREE_PLANES.items()
    if plane is not StatePlane.AGENT
)
# @apg_environment_path_constants:end

# Identifies this Coordinator process in actor lock files. Regenerated on every
# import, so a lock restored from a snapshot never looks like ours.
COORDINATOR_INSTANCE_ID = uuid4().hex


# -------------------------------------------------------------------------------------
# Config
# -------------------------------------------------------------------------------------


class CoordinatorConfigStore:
    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir
        self.config_dir.mkdir(parents=True, exist_ok=True)
        # Cache of the last parsed config, keyed on the file's mtime. config.json is
        # written once at start and then read on the hot path (every tool call:
        # record_tool_call, the shadow authz check, the bearer actor fallback), so
        # re-parsing ~100KB each call is wasted work. A cheap stat() decides hit/miss;
        # write() clears it. The returned CoordinatorConfig is frozen, so the shared
        # instance can't be reassigned in place. Assumes a single in-process writer:
        # write() clears the cache on THIS instance only, so config.json written from
        # elsewhere is picked up solely via the mtime check.
        self._cache: tuple[int, CoordinatorConfig] | None = None

    @property
    def path(self) -> Path:
        return self.config_dir / "config.json"

    def read(self) -> CoordinatorConfig:
        if not self.path.exists():
            return CoordinatorConfig(enabled=False)
        try:
            mtime = self.path.stat().st_mtime_ns
            cached = self._cache
            if cached is not None and cached[0] == mtime:
                return cached[1]
            config = CoordinatorConfig.model_validate_json(
                self.path.read_text(encoding="utf-8")
            )
            self._cache = (mtime, config)
            return config
        except (OSError, ValidationError, ValueError) as e:
            raise RuntimeError(
                f"Invalid Environment Coordinator config at {self.path}: {e}"
            ) from e

    def write(self, config: CoordinatorConfig) -> None:
        write_json(self.path, config.model_dump(mode="json"))
        self._cache = None


# -------------------------------------------------------------------------------------
# CheckpointObservations
# -------------------------------------------------------------------------------------


class ToolCallCheckpointObservationStore:
    def __init__(self, checkpoint_observations_dir: Path) -> None:
        self.checkpoint_observations_dir = checkpoint_observations_dir
        self._lock = Lock()
        self._sequence: int | None = None

    @property
    def calls_path(self) -> Path:
        return self.checkpoint_observations_dir / "mcp_calls.jsonl"

    def record(
        self,
        *,
        actor_id: str,
        tool_name: str,
        arguments: dict[str, JsonValue],
        result_summary: dict[str, JsonValue] | None,
        error: str | None,
    ) -> ToolCallCheckpointObservation:
        with self._lock:
            event = ToolCallCheckpointObservation(
                sequence=self._next_sequence(),
                actor_id=actor_id,
                tool_name=tool_name,
                arguments=arguments,
                result_summary=result_summary,
                error=error,
                timestamp=utc_now(),
            )
            with self.calls_path.open("a", encoding="utf-8") as handle:
                handle.write(event.model_dump_json() + "\n")
        return event

    def read(self) -> list[ToolCallCheckpointObservation]:
        if not self.calls_path.exists():
            return []
        with self.calls_path.open(encoding="utf-8") as handle:
            return [
                ToolCallCheckpointObservation.model_validate(json.loads(line))
                for line in handle
                if line.strip()
            ]

    def _next_sequence(self) -> int:
        if self._sequence is None:
            self._sequence = self._last_recorded_sequence()
        self._sequence += 1
        return self._sequence

    def _last_recorded_sequence(self) -> int:
        """Resume from the log itself, so a restart cannot disagree with it."""
        last = 0
        if not self.calls_path.exists():
            return last
        with self.calls_path.open(encoding="utf-8") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    sequence = json.loads(line).get("sequence")
                except (json.JSONDecodeError, AttributeError):
                    continue
                if isinstance(sequence, int) and sequence > last:
                    last = sequence
        return last


class PhysicalTimeCheckpointObservationStore:
    def __init__(self, checkpoint_observations_dir: Path) -> None:
        self.checkpoint_observations_dir = checkpoint_observations_dir
        if not self.path.exists():
            write_json(
                self.path,
                PhysicalTimeCheckpointObservation(
                    trajectory_started_at=utc_now(),
                ).model_dump(mode="json"),
            )

    @property
    def path(self) -> Path:
        return self.checkpoint_observations_dir / "physical_time.json"

    def read(self) -> PhysicalTimeCheckpointObservation:
        return PhysicalTimeCheckpointObservation.model_validate_json(
            self.path.read_text(encoding="utf-8")
        )


class CoordinatorCheckpointObservationStore:
    def __init__(self, checkpoint_observations_dir: Path) -> None:
        self.checkpoint_observations_dir = checkpoint_observations_dir
        self.checkpoint_observations_dir.mkdir(parents=True, exist_ok=True)
        self.tool_calls = ToolCallCheckpointObservationStore(
            checkpoint_observations_dir
        )
        self.physical_time = PhysicalTimeCheckpointObservationStore(
            checkpoint_observations_dir
        )

    def read(self) -> CheckpointObservations:
        return CheckpointObservations(
            tool_calls=self.tool_calls.read(),
            physical_time=self.physical_time.read(),
        )


# -------------------------------------------------------------------------------------
# EventOccurrences
# -------------------------------------------------------------------------------------


class CoordinatorEventOccurrenceStore:
    """Scoped to the run, not the process: a restart mid-run is the same run."""

    def __init__(self, event_occurrences_dir: Path, run_id: str | None = None) -> None:
        self.event_occurrences_dir = event_occurrences_dir
        self.event_occurrences_dir.mkdir(parents=True, exist_ok=True)
        self.run_id = run_id
        self._replace_lock = Lock()

    def read(self, event_id: str) -> EventOccurrence | None:
        path = self._path(event_id)
        if not path.exists():
            return None
        return EventOccurrence.model_validate_json(path.read_text(encoding="utf-8"))

    def read_all(self) -> list[EventOccurrence]:
        return [
            EventOccurrence.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(self.event_occurrences_dir.glob("*.json"))
            if self._is_own(path)
        ]

    def write(self, occurrence: EventOccurrence) -> None:
        write_json(self._path(occurrence.event.event_id), self._stamped(occurrence))

    def create(self, occurrence: EventOccurrence) -> bool:
        path = self._path(occurrence.event.event_id)
        if self._create_new(path, occurrence):
            return True
        if self._is_own(path):
            return False
        with self._replace_lock:
            # Re-check: another caller may have replaced it since the O_EXCL lost.
            if self._is_own(path):
                return False
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return self._create_new(path, occurrence)

    def _create_new(self, path: Path, occurrence: EventOccurrence) -> bool:
        try:
            with path.open("x", encoding="utf-8") as handle:
                json.dump(self._stamped(occurrence), handle, indent=2, sort_keys=True)
            return True
        except FileExistsError:
            return False

    def discard(self, event_id: str) -> None:
        """Forget an occurrence so its trigger can match again."""
        try:
            self._path(event_id).unlink()
        except FileNotFoundError:
            pass

    def event_ids(self) -> set[str]:
        return {
            path.stem
            for path in self.event_occurrences_dir.glob("*.json")
            if self._is_own(path)
        }

    def _stamped(self, occurrence: EventOccurrence) -> dict[str, JsonValue]:
        return occurrence.model_copy(update={"run_id": self.run_id}).model_dump(
            mode="json"
        )

    def _is_own(self, path: Path) -> bool:
        """Whether this run wrote the occurrence at ``path``.

        Reads the one field rather than validating the model: this runs for every
        occurrence on every checkpoint.
        """
        if self.run_id is None:
            return True
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return isinstance(stored, dict) and stored.get("run_id") == self.run_id

    def _path(self, event_id: str) -> Path:
        return self.event_occurrences_dir / f"{event_id}.json"


# -------------------------------------------------------------------------------------
# Agent Configs
# -------------------------------------------------------------------------------------


def _reclaim_stale_lock(lock_path: Path, actor_id: str) -> bool:
    """Remove an actor lock left behind by a dead Coordinator. Returns whether
    it was freed.

    The lock only ever serializes concurrent tasks inside one Coordinator, and
    it is released in a ``finally`` that a SIGKILL or a container restart skips.
    So a lock stamped by this process is genuinely held, and a lock stamped by
    any other is a leftover whose holder cannot still be running.

    Identity is the process instance, not the pid: the state dir travels in the
    snapshot, and a restored container starts pid numbering again, so a
    leftover pid routinely matches some unrelated live process.
    """
    try:
        holder = lock_path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    if not holder:
        # The stamp lands just after the file is created, so an empty read is
        # more likely a holder mid-acquire than a dead one. Never steal it.
        return False
    if holder.endswith(f":{COORDINATOR_INSTANCE_ID}"):
        return False
    logger.warning(
        f"Environment Coordinator reclaiming stale VCA lock actor={actor_id} "
        + f"holder={holder!r} instance={COORDINATOR_INSTANCE_ID}"
    )
    try:
        lock_path.unlink()
    except FileNotFoundError:
        pass
    return True


class CoordinatorAgentConfigStore:
    def __init__(self, agent_configs_dir: Path) -> None:
        self.agent_configs_dir = agent_configs_dir
        self.agent_configs_dir.mkdir(parents=True, exist_ok=True)

    def validate_configs(self, agents: Iterable[VirtualCoworkerAgent]) -> None:
        for vca in agents:
            (self.agent_configs_dir / vca.actor_id / "runs").mkdir(
                parents=True, exist_ok=True
            )

    def write_run(self, actor_id: str, record: AgentRunRecord) -> str:
        path = self._run_dir(actor_id, record.run_id) / RUN_RECORD_FILENAME
        write_json(path, record.model_dump(mode="json"))
        return str(path)

    def run_stdio_paths(self, actor_id: str, run_id: str) -> tuple[Path, Path]:
        """(stdout, stderr) for a run. Redirected to files rather than pipes:
        nothing has to drain them, so the Coordinator can kill the process group
        without deadlocking on an unread buffer."""
        run_dir = self._run_dir(actor_id, run_id)
        return (
            run_dir / VCA_RUN_STDOUT_FILENAME,
            run_dir / VCA_RUN_STDERR_FILENAME,
        )

    def run_output_path(self, actor_id: str, run_id: str) -> Path:
        return self._run_dir(actor_id, run_id) / AGENT_OUTPUT_FILENAME

    def read_run_output(self, actor_id: str, run_id: str) -> dict[str, Any] | None:
        path = self.run_output_path(actor_id, run_id)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise RuntimeError(
                f"Invalid VCA run output for {actor_id} at {path}: {e}"
            ) from e
        if not isinstance(value, dict):
            raise RuntimeError(
                f"VCA run output for {actor_id} at {path} must be an object"
            )
        return value

    def prepare_agent_run(
        self,
        *,
        vca: VirtualCoworkerAgent,
        run_id: str,
        mcp_gateway_url: str,
        filesystem_dir: str,
        run_as_user: str | None = None,
        agent_timeout_seconds: int | None = None,
    ) -> tuple[list[str], dict[str, str]]:
        harness_config = vca.vca_harness_config
        run_input = AgentRunInput.model_validate(
            {
                "trajectory_id": run_id,
                "initial_messages": [
                    {
                        "role": "system",
                        "content": build_vca_system_prompt(vca),
                    },
                    {
                        "role": "user",
                        "content": build_vca_user_prompt(),
                    },
                ],
                "mcp_gateway_url": mcp_gateway_url,
                "mcp_gateway_auth_token": None,
                "mcp_gateway_actor_id": vca.actor_id,
                "orchestrator_model": harness_config.orchestrator_model,
                "orchestrator_extra_args": harness_config.orchestrator_extra_args,
                "agent_config_values": harness_config.agent_config.agent_config_values,
                "task_custom_fields": harness_config.task_custom_fields,
                "inner_agent_config": harness_config.inner_agent_config.model_dump(
                    mode="json"
                )
                if harness_config.inner_agent_config
                else None,
            }
        )
        run_dir = self._run_dir(vca.actor_id, run_input.trajectory_id)
        if run_as_user is not None:
            # Gate the run dir before any inputs (the VCA's instructions) are
            # written below, so nothing is group/other-readable in the window
            # between these writes and the chown_tree hand-over at spawn.
            run_dir.chmod(0o700)
        initial_messages_path = run_dir / INITIAL_MESSAGES_FILENAME
        agent_config_path = run_dir / AGENT_CONFIG_FILENAME
        orchestrator_extra_args_path = run_dir / ORCHESTRATOR_EXTRA_ARGS_FILENAME
        task_custom_fields_path = run_dir / TASK_CUSTOM_FIELDS_FILENAME
        inner_agent_config_path = run_dir / INNER_AGENT_CONFIG_FILENAME
        output_path = run_dir / AGENT_OUTPUT_FILENAME

        initial_messages_path.write_text(
            json.dumps(run_input.initial_messages, indent=2), encoding="utf-8"
        )
        write_json(
            agent_config_path, harness_config.agent_config.model_dump(mode="json")
        )
        if run_input.orchestrator_extra_args is not None:
            write_json(orchestrator_extra_args_path, run_input.orchestrator_extra_args)
        if run_input.task_custom_fields is not None:
            write_json(task_custom_fields_path, run_input.task_custom_fields)
        if run_input.inner_agent_config is not None:
            write_json(inner_agent_config_path, run_input.inner_agent_config)

        runner_command: list[str] = list(AGENT_RUNNER_COMMAND)
        # The agents venv is baked during platform image build; runtime ``uv sync``
        # would ignore the coordinator's VIRTUAL_ENV, hit CodeArtifact without
        # credentials, and fail before the persona runs.
        if runner_command[:2] == ["uv", "run"]:
            runner_command.insert(2, "--no-sync")
        command = [
            *runner_command,
            "--trajectory-id",
            run_input.trajectory_id,
            "--initial-messages",
            str(initial_messages_path),
            "--mcp-gateway-url",
            run_input.mcp_gateway_url or "",
            "--agent-config",
            str(agent_config_path),
            "--orchestrator-model",
            run_input.orchestrator_model,
            "--output",
            str(output_path),
        ]
        if run_input.orchestrator_extra_args is not None:
            command.extend(
                [
                    "--orchestrator-extra-args",
                    str(orchestrator_extra_args_path),
                ]
            )
        if run_input.task_custom_fields is not None:
            command.extend(
                [
                    "--task-custom-fields",
                    str(task_custom_fields_path),
                ]
            )
        if run_input.inner_agent_config is not None:
            command.extend(
                [
                    "--inner-agent-config",
                    str(inner_agent_config_path),
                ]
            )

        env = os.environ.copy()
        env.update(vca.env)
        # Not on argv: /proc/<pid>/cmdline is world-readable, so a sibling actor
        # could read this one's identity straight off it. /proc/<pid>/environ is
        # 0400, which narrows the reader to the same uid — closed outright once
        # each actor has its own user.
        env[MCP_GATEWAY_ACTOR_ID_ENV] = run_input.mcp_gateway_actor_id or ""
        env[VCA_FILESYSTEM_DIR_ENV] = filesystem_dir
        env["FILE_LOGGING"] = "true"
        env["FILE_LOG_PATH"] = str(run_dir / VCA_RUN_LOGS_FILENAME)
        # Without this the agent inherits its own 22h default and the
        # Coordinator's kill always wins, so the agent never gets to write
        # output.json. Keep it strictly under the Coordinator's deadline.
        if agent_timeout_seconds is not None:
            env["AGENT_TIMEOUT_SECONDS"] = str(agent_timeout_seconds)

        # Hand the confined user only its own run dir + filesystem; the rest of the state tree
        # stays Coordinator-owned so the VCA can't tamper with the grading observation log.
        if run_as_user is not None:
            # Confined user can't write the Coordinator's HOME (e.g. /root) — give it its own.
            env["HOME"] = user_home(run_as_user)
            chown_tree(run_dir, run_as_user)
            fs_path = Path(filesystem_dir)
            if fs_path.exists():
                chown_tree(fs_path, run_as_user)

        return command, env

    @contextmanager
    def lock(self, actor_id: str):
        lock_path = self.agent_configs_dir / actor_id / "lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd: int | None = None
        try:
            lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            if not _reclaim_stale_lock(lock_path, actor_id):
                yield False
                return
            try:
                lock_fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                # Another holder won the reclaim race — theirs is live.
                yield False
                return
        try:
            os.write(lock_fd, f"{os.getpid()}:{COORDINATOR_INSTANCE_ID}".encode())
            yield True
        finally:
            if lock_fd is not None:
                os.close(lock_fd)
                try:
                    lock_path.unlink()
                except FileNotFoundError:
                    pass

    def _agent_file_path(self, actor_id: str, filename: str) -> Path:
        return self.agent_configs_dir / actor_id / ARCHIPELAGO_AGENT_DIR_NAME / filename

    def _run_dir(self, actor_id: str, run_id: str) -> Path:
        path = self.agent_configs_dir / actor_id / "runs" / run_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _read_agent_config(self, actor_id: str) -> AgentConfig:
        path = self._agent_file_path(actor_id, AGENT_CONFIG_FILENAME)
        try:
            return AgentConfig.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError) as e:
            raise RuntimeError(
                f"Invalid VCA agent config for {actor_id} at {path}: {e}"
            ) from e

    def _read_orchestrator_model(self, actor_id: str) -> str:
        path = self._agent_file_path(actor_id, ORCHESTRATOR_MODEL_FILENAME)
        try:
            model = path.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise RuntimeError(
                f"Invalid VCA orchestrator model for {actor_id} at {path}: {e}"
            ) from e
        if not model:
            raise RuntimeError(f"Empty VCA orchestrator model for {actor_id} at {path}")
        return model

    def _read_optional_json(
        self, actor_id: str, filename: str
    ) -> dict[str, Any] | None:
        path = self._agent_file_path(actor_id, filename)
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            raise RuntimeError(
                f"Invalid VCA JSON config for {actor_id} at {path}: {e}"
            ) from e
        if not isinstance(value, dict):
            raise RuntimeError(
                f"VCA JSON config for {actor_id} at {path} must be an object"
            )
        return value


# -------------------------------------------------------------------------------------
# Agent Filesystems
# -------------------------------------------------------------------------------------


class CoordinatorAgentFilesystemStore:
    def __init__(self, agent_filesystems_dir: Path) -> None:
        self.agent_filesystems_dir = agent_filesystems_dir
        self.agent_filesystems_dir.mkdir(parents=True, exist_ok=True)

    def filesystem_dir(self, actor_id: str) -> str:
        path = self.agent_filesystems_dir / actor_id
        path.mkdir(parents=True, exist_ok=True)
        return str(path)


# -------------------------------------------------------------------------------------
# Coordinator Store
# -------------------------------------------------------------------------------------


class CoordinatorStore:
    """
    CoordinatorStore maps the coordinator filesystem into typed Python objects.

    <coordinator_root>/
    ├── config/
    │   └── config.json
    ├── checkpoint_observations/
    │   ├── mcp_calls.jsonl
    │   ├── sequence.txt
    │   └── physical_time.json
    ├── event_occurrences/
    │   └── <event_id>.json
    ├── agent_configs/
    │   └── <vca_id>/
    │       ├── archipelago_agent/
    │       │   ├── agent_config.json
    │       │   ├── orchestrator_model.txt
    │       │   ├── orchestrator_extra_args.json
    │       │   ├── task_custom_fields.json
    │       │   └── inner_agent_config.json
    │       ├── lock
    │       └── runs/
    │           └── <run_id>/
    │               ├── run.json
    │               ├── initial_messages.json
    │               └── output.json
    └── agent_filesystems/
        └── <vca_id>/
    """

    def __init__(
        self,
        *,
        root: Path | None = None,
    ) -> None:
        self.root = root or Path(
            os.environ.get(COORDINATOR_ROOT_ENV, DEFAULT_COORDINATOR_ROOT)
        )
        self.config = CoordinatorConfigStore(self.config_dir)
        self.observations = CoordinatorCheckpointObservationStore(
            self.checkpoint_observations_dir
        )
        self.event_occurrences = CoordinatorEventOccurrenceStore(
            self.event_occurrences_dir
        )
        self.agent_configs = CoordinatorAgentConfigStore(self.agent_configs_dir)
        self.agent_filesystems = CoordinatorAgentFilesystemStore(
            self.agent_filesystems_dir
        )

    @property
    def config_dir(self) -> Path:
        return self.root / CONFIG_DIR_NAME

    @property
    def event_occurrences_dir(self) -> Path:
        return self.root / EVENT_OCCURRENCES_DIR_NAME

    @property
    def agent_configs_dir(self) -> Path:
        return self.root / AGENT_CONFIGS_DIR_NAME

    @property
    def agent_filesystems_dir(self) -> Path:
        return self.root / AGENT_FILESYSTEMS_DIR_NAME

    @property
    def checkpoint_observations_dir(self) -> Path:
        return self.root / CHECKPOINT_OBSERVATIONS_DIR_NAME

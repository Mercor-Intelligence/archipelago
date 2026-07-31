import json
import os
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path
from threading import Lock
from typing import Any

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
# Base VCA spawn command. ``--no-sync`` is inserted at spawn time for confined users
# (their agents venv is baked read-only); see ``prepare_agent_run``.
AGENT_RUNNER_COMMAND = ("uv", "run", "python", "-m", "runner.main")
VCA_FILESYSTEM_DIR_ENV = "VCA_FILESYSTEM_DIR"
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
CONFIG_DIR_NAME = "config"
EVENT_OCCURRENCES_DIR_NAME = "event_occurrences"
AGENT_CONFIGS_DIR_NAME = "agent_configs"
AGENT_FILESYSTEMS_DIR_NAME = "agent_filesystems"
AGENT_RUNS_DIR_NAME = "runs"
# @apg_environment_path_constants:end


# -------------------------------------------------------------------------------------
# Config
# -------------------------------------------------------------------------------------


class CoordinatorConfigStore:
    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir
        self.config_dir.mkdir(parents=True, exist_ok=True)

    @property
    def path(self) -> Path:
        return self.config_dir / "config.json"

    def read(self) -> CoordinatorConfig:
        if not self.path.exists():
            return CoordinatorConfig(enabled=False)
        try:
            return CoordinatorConfig.model_validate_json(
                self.path.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError, ValueError) as e:
            raise RuntimeError(
                f"Invalid Environment Coordinator config at {self.path}: {e}"
            ) from e

    def write(self, config: CoordinatorConfig) -> None:
        write_json(self.path, config.model_dump(mode="json"))


# -------------------------------------------------------------------------------------
# CheckpointObservations
# -------------------------------------------------------------------------------------


class ToolCallCheckpointObservationStore:
    def __init__(self, checkpoint_observations_dir: Path) -> None:
        self.checkpoint_observations_dir = checkpoint_observations_dir
        self._lock = Lock()

    @property
    def calls_path(self) -> Path:
        return self.checkpoint_observations_dir / "mcp_calls.jsonl"

    @property
    def sequence_path(self) -> Path:
        return self.checkpoint_observations_dir / "sequence.txt"

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
        current = 0
        if self.sequence_path.exists():
            raw = self.sequence_path.read_text(encoding="utf-8").strip()
            current = int(raw or "0")
        next_sequence = current + 1
        self.sequence_path.write_text(str(next_sequence), encoding="utf-8")
        return next_sequence


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
    def __init__(self, event_occurrences_dir: Path) -> None:
        self.event_occurrences_dir = event_occurrences_dir
        self.event_occurrences_dir.mkdir(parents=True, exist_ok=True)

    def read(self, event_id: str) -> EventOccurrence | None:
        path = self._path(event_id)
        if not path.exists():
            return None
        return EventOccurrence.model_validate_json(path.read_text(encoding="utf-8"))

    def read_all(self) -> list[EventOccurrence]:
        return [
            EventOccurrence.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(self.event_occurrences_dir.glob("*.json"))
        ]

    def write(self, occurrence: EventOccurrence) -> None:
        write_json(
            self._path(occurrence.event.event_id), occurrence.model_dump(mode="json")
        )

    def create(self, occurrence: EventOccurrence) -> bool:
        path = self._path(occurrence.event.event_id)
        try:
            with path.open("x", encoding="utf-8") as handle:
                json.dump(
                    occurrence.model_dump(mode="json"),
                    handle,
                    indent=2,
                    sort_keys=True,
                )
            return True
        except FileExistsError:
            return False

    def event_ids(self) -> set[str]:
        return {path.stem for path in self.event_occurrences_dir.glob("*.json")}

    def _path(self, event_id: str) -> Path:
        return self.event_occurrences_dir / f"{event_id}.json"


# -------------------------------------------------------------------------------------
# Agent Configs
# -------------------------------------------------------------------------------------


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

    def read_run_output(self, actor_id: str, run_id: str) -> dict[str, Any] | None:
        path = self._run_dir(actor_id, run_id) / AGENT_OUTPUT_FILENAME
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
        # Confined users get a read-only baked venv → insert ``--no-sync`` (guarded on the
        # uv prefix so non-uv test/local overrides are untouched).
        if run_as_user is not None and runner_command[:2] == ["uv", "run"]:
            runner_command.insert(2, "--no-sync")
        command = [
            *runner_command,
            "--trajectory-id",
            run_input.trajectory_id,
            "--initial-messages",
            str(initial_messages_path),
            "--mcp-gateway-url",
            run_input.mcp_gateway_url or "",
            "--mcp-gateway-actor-id",
            run_input.mcp_gateway_actor_id or "",
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
        env[VCA_FILESYSTEM_DIR_ENV] = filesystem_dir
        env["FILE_LOGGING"] = "true"
        env["FILE_LOG_PATH"] = str(run_dir / VCA_RUN_LOGS_FILENAME)

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
            yield False
            return
        try:
            os.write(lock_fd, str(os.getpid()).encode())
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
        return self.root / "config"

    @property
    def event_occurrences_dir(self) -> Path:
        return self.root / "event_occurrences"

    @property
    def agent_configs_dir(self) -> Path:
        return self.root / "agent_configs"

    @property
    def agent_filesystems_dir(self) -> Path:
        return self.root / "agent_filesystems"

    @property
    def checkpoint_observations_dir(self) -> Path:
        return self.root / "checkpoint_observations"

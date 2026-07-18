"""Harbor environment backed by Studio's existing Modal sandbox lifecycle."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from loguru import logger

from modal_helpers import (
    capture_rootfs_layer,
    configure_mcp_servers,
    create_environment_sandbox,
    create_snapshot,
    fetch_agent_config,
    mark_rootfs_baseline,
    populate_environment,
    wait_for_environment_ready,
)
from runner.agents.models import AgentStatus

try:
    from harbor.environments.base import (  # pyright: ignore[reportMissingImports]
        BaseEnvironment,
        ExecResult,
    )
    from harbor.models.environment_type import (  # pyright: ignore[reportMissingImports]
        EnvironmentType,
    )
except ModuleNotFoundError as exc:
    # Unit-test import; the dedicated Modal image installs Harbor. Do not hide a
    # missing Harbor submodule or transitive dependency when Harbor is present.
    if exc.name != "harbor":
        raise
    BaseEnvironment = object  # type: ignore[assignment,misc]

    class EnvironmentType:  # type: ignore[no-redef]
        MODAL = "modal"

    class ExecResult:  # type: ignore[no-redef]
        def __init__(self, *, stdout: str, stderr: str, return_code: int) -> None:
            self.stdout = stdout
            self.stderr = stderr
            self.return_code = return_code


class StudioModalEnvironment(
    BaseEnvironment  # pyright: ignore[reportGeneralTypeIssues]
):  # type: ignore[misc]
    """Expose a pinned Studio environment sandbox through Harbor's API.

    ``trajectory_id`` is supplied only by Studio's ``batch_harbor`` worker via
    Harbor's ``--environment-kwarg`` option. The adapter ignores any Docker
    definition in the task package and resolves the image and snapshots from the
    server-owned trajectory record instead.
    """

    def __init__(self, *args: Any, trajectory_id: str, **kwargs: Any) -> None:
        self.trajectory_id = trajectory_id
        self._sandbox: Any | None = None
        self._sandbox_url: str | None = None
        self._auth_token: str | None = None
        self._config: Any | None = None
        self._snapshot_ready = False
        self._rootfs_baseline_marked = False
        super().__init__(*args, **kwargs)

    @staticmethod
    def type() -> Any:
        return EnvironmentType.MODAL

    @property
    def is_mounted(self) -> bool:
        return False

    @property
    def supports_gpus(self) -> bool:
        return False

    @property
    def can_disable_internet(self) -> bool:
        return True

    def _validate_definition(self) -> None:
        # Studio owns the image reference; task-controlled Dockerfiles are ignored.
        return None

    def _write_snapshot_ref(self, filename: str, snapshot_id: str) -> None:
        self.trial_paths.artifacts_dir.mkdir(parents=True, exist_ok=True)
        (self.trial_paths.artifacts_dir / filename).write_text(
            json.dumps({"snapshot_id": snapshot_id}),
            encoding="utf-8",
        )

    def _write_env_image_layer_ref(self, s3_uri: str) -> None:
        self.trial_paths.artifacts_dir.mkdir(parents=True, exist_ok=True)
        (self.trial_paths.artifacts_dir / "studio_env_image_layer.json").write_text(
            json.dumps({"env_image_layer_s3_uri": s3_uri}),
            encoding="utf-8",
        )

    def _agent_completed(self) -> bool:
        try:
            payload = json.loads(
                (self.trial_paths.agent_dir / "trajectory.native.json").read_text(
                    encoding="utf-8"
                )
            )
        except (AttributeError, OSError, json.JSONDecodeError):
            return False
        return (
            isinstance(payload, dict)
            and payload.get("status") == AgentStatus.COMPLETED.value
        )

    async def _capture_snapshot(self, filename: str) -> None:
        if (
            self._sandbox_url is None
            or self._auth_token is None
            or self._config is None
            or not self._snapshot_ready
        ):
            return
        result = await create_snapshot(
            self._sandbox_url,
            self._auth_token,
            pre_snapshot_hooks=self._config.snapshot_hooks or None,
            snapshot_zip_enabled=self._config.snapshot_zip_enabled,
            deadline_seconds=self._config.environment_snapshot_deadline_seconds,
        )
        if result.files_uploaded > 0:
            self._write_snapshot_ref(filename, result.snapshot_id)

    async def start(self, force_build: bool) -> None:
        del force_build
        self._snapshot_ready = False
        self._rootfs_baseline_marked = False
        config = await fetch_agent_config(self.trajectory_id)
        self._config = config
        if not config.platform_image_url:
            raise ValueError(
                f"Harbor trajectory {self.trajectory_id} has no platform image"
            )

        sandbox, sandbox_url, auth_token = await create_environment_sandbox(
            config.platform_image_url,
            cpu=config.environment_sandbox_cpu,
            memory=config.environment_sandbox_memory_mb,
            disable_web_access=config.disable_web_access,
            lifecycle_hook_timeout_seconds=(
                config.environment_populate_hook_timeout_seconds
            ),
        )
        self._sandbox = sandbox
        self._sandbox_url = sandbox_url
        self._auth_token = auth_token

        try:
            await wait_for_environment_ready(sandbox_url, auth_token)
            await populate_environment(
                sandbox_url=sandbox_url,
                auth_token=auth_token,
                world_snapshot_id=config.world_snapshot_id,
                task_data_id=config.task_data_id,
                task_data_prefix=config.task_data_prefix,
                post_populate_hooks=config.populate_hooks or None,
                deadline_seconds=config.environment_populate_deadline_seconds,
            )
            await configure_mcp_servers(
                sandbox_url=sandbox_url,
                auth_token=auth_token,
                mcp_server_configs=config.mcp_server_configs,
                coordinator_config=config.coordinator_config,
            )
            self._snapshot_ready = True
            if config.populate_hooks:
                await self._capture_snapshot("studio_post_populate_snapshot.json")
            if bool(getattr(config, "capture_env_image", False)):
                try:
                    await mark_rootfs_baseline(sandbox_url, auth_token)
                except Exception as exc:
                    logger.warning(
                        "Failed to mark Harbor rootfs baseline; "
                        f"skipping env image capture: {exc!r}"
                    )
                else:
                    self._rootfs_baseline_marked = True
        except BaseException:
            try:
                await self.stop(delete=True)
            except BaseException:
                logger.exception(
                    "Failed to clean up Harbor environment after startup error; "
                    "preserving the original failure"
                )
            raise

    async def stop(self, delete: bool) -> None:
        del delete
        sandbox = self._sandbox
        if sandbox is None:
            return

        try:
            if self._agent_completed():
                await self._capture_snapshot("studio_final_snapshot.json")
                if (
                    self._rootfs_baseline_marked
                    and self._sandbox_url is not None
                    and self._auth_token is not None
                ):
                    try:
                        result = await capture_rootfs_layer(
                            self._sandbox_url,
                            self._auth_token,
                            trajectory_id=self.trajectory_id,
                        )
                    except Exception as exc:
                        logger.warning(
                            "Failed to capture Harbor rootfs layer; grading will "
                            f"fall back to the platform image: {exc!r}"
                        )
                    else:
                        self._write_env_image_layer_ref(result.s3_uri)
        finally:
            await sandbox.terminate.aio()
            self._sandbox = None

    def _require_sandbox(self) -> Any:
        if self._sandbox is None:
            raise RuntimeError("Studio Modal environment has not started")
        return self._sandbox

    @property
    def sandbox_url(self) -> str:
        if self._sandbox_url is None:
            raise RuntimeError("Studio Modal environment has not started")
        return self._sandbox_url

    @property
    def auth_token(self) -> str:
        if self._auth_token is None:
            raise RuntimeError("Studio Modal environment has not started")
        return self._auth_token

    async def exec(
        self,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout_sec: int | None = None,
        user: str | int | None = None,
    ) -> ExecResult:
        sandbox = self._require_sandbox()
        if user not in (None, "root", 0):
            raise ValueError("Studio Modal environment does not support user override")
        kwargs: dict[str, Any] = {"workdir": cwd, "timeout": timeout_sec}
        if env:
            kwargs["env"] = env
        process = await sandbox.exec.aio("bash", "-lc", command, **kwargs)
        stdout = await process.stdout.read.aio()
        stderr = await process.stderr.read.aio()
        return_code = await process.wait.aio()
        return ExecResult(
            stdout=stdout,
            stderr=stderr,
            return_code=return_code,
        )

    async def upload_file(self, source_path: Path | str, target_path: str) -> None:
        sandbox = self._require_sandbox()
        await sandbox.filesystem.copy_from_local.aio(source_path, target_path)

    async def upload_dir(self, source_dir: Path | str, target_dir: str) -> None:
        sandbox = self._require_sandbox()
        await sandbox.filesystem.copy_from_local.aio(source_dir, target_dir)

    async def download_file(self, source_path: str, target_path: Path | str) -> None:
        sandbox = self._require_sandbox()
        await sandbox.filesystem.copy_to_local.aio(source_path, target_path)

    async def download_dir(self, source_dir: str, target_dir: Path | str) -> None:
        sandbox = self._require_sandbox()
        await sandbox.filesystem.copy_to_local.aio(source_dir, target_dir)

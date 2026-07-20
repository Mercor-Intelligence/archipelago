"""One-trajectory Modal worker for Studio's Harbor execution framework."""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import modal
from loguru import logger

from modal_helpers import (
    fetch_agent_config,
    upload_harbor_artifacts,
)
from runner.agents.models import AgentStatus, AgentTrajectoryOutput
from runner.harbor.artifacts import (
    artifact_prefix,
    build_redacted_jobs_tar,
    build_redacted_native_artifacts,
    redact_bytes,
)
from runner.harbor.runtime import (
    build_harbor_command,
    load_env_image_layer_s3_uri,
    load_native_output,
    load_snapshot_id,
    raise_for_harbor_result_error,
    run_harbor_command,
)
from runner.harbor.task_package import materialize_task_package
from runner.save.main import save_results
from runner.utils.logging.main import setup_logger, teardown_logger
from runner.utils.settings import get_settings

_HARBOR_LIFECYCLE_HEADROOM_SECONDS = 60 * 60
_HARBOR_MODAL_TIMEOUT_SECONDS = 24 * 60 * 60 - 60
_HARBOR_FINALIZATION_HEADROOM_SECONDS = 30 * 60
_MAX_STORED_LOG_BYTES = 64 * 1024 * 1024
_LOG_TRUNCATION_RECORD = (
    b'{"message":"Harbor log artifact truncated at size limit","source":"studio"}\n'
)
_CANCELLATION_ERRORS = (
    asyncio.CancelledError,
    modal.exception.FunctionTimeoutError,
    modal.exception.InputCancellation,
)

type ArtifactStatus = Literal["complete", "partial", "failed"]


def _harbor_command_timeout_seconds() -> int:
    return min(
        get_settings().AGENT_TIMEOUT_SECONDS + _HARBOR_LIFECYCLE_HEADROOM_SECONDS,
        _HARBOR_MODAL_TIMEOUT_SECONDS - _HARBOR_FINALIZATION_HEADROOM_SECONDS,
    )


def _secret_values() -> list[str]:
    markers = ("KEY", "TOKEN", "SECRET", "PASSWORD")
    return [
        value
        for name, value in os.environ.items()
        if value and any(marker in name.upper() for marker in markers)
    ]


class _BoundedLogBuffer:
    def __init__(self, *, limit_bytes: int) -> None:
        if limit_bytes < len(_LOG_TRUNCATION_RECORD):
            raise ValueError("Harbor log buffer limit is too small")
        self._content: bytearray = bytearray()
        self._limit_bytes: int = limit_bytes
        self._truncated: bool = False

    def append(self, record: dict[str, str]) -> None:
        if self._truncated:
            return
        encoded = (json.dumps(record, sort_keys=True) + "\n").encode()
        record_limit = self._limit_bytes - len(_LOG_TRUNCATION_RECORD)
        if len(self._content) + len(encoded) <= record_limit:
            self._content.extend(encoded)
        else:
            self._truncated = True

    def to_bytes(self) -> bytes:
        if self._truncated:
            return bytes(self._content) + _LOG_TRUNCATION_RECORD
        return bytes(self._content)


def _output_for_error(
    output: AgentTrajectoryOutput | None,
    error: BaseException | None,
) -> AgentTrajectoryOutput:
    if isinstance(error, _CANCELLATION_ERRORS):
        final_status = AgentStatus.CANCELLED
    elif error is not None:
        final_status = AgentStatus.ERROR
    else:
        final_status = None

    if output is None:
        return AgentTrajectoryOutput(
            messages=[],
            status=final_status or AgentStatus.ERROR,
            time_elapsed=0.0,
        )
    if final_status is not None:
        output.status = final_status
    return output


def _is_non_authoritative_finalization_error(
    output: AgentTrajectoryOutput | None,
    error: BaseException,
) -> bool:
    return (
        output is not None
        and output.status is AgentStatus.COMPLETED
        and isinstance(error, Exception)
        and not isinstance(
            error,
            (
                modal.exception.FunctionTimeoutError,
                modal.exception.InputCancellation,
            ),
        )
    )


def _artifact_objects(
    *,
    manifest_path: Path | None,
    jobs_dir: Path,
    log_content: bytes,
    output: AgentTrajectoryOutput,
) -> tuple[dict[str, bytes], bool]:
    secret_values = _secret_values()
    objects = {
        "studio/logs.ndjson": redact_bytes(log_content, secret_values),
        "studio/trajectory.json": redact_bytes(
            output.model_dump_json(indent=2).encode(), secret_values
        ),
    }
    partial = False

    try:
        objects["native/jobs.tar.gz"] = build_redacted_jobs_tar(
            jobs_dir, secret_values=secret_values
        )
    except Exception as artifact_error:
        partial = True
        logger.error(
            "Failed to package Harbor jobs archive; continuing with safe artifacts: "
            f"{artifact_error!r}"
        )

    try:
        native_artifacts, native_partial = build_redacted_native_artifacts(
            jobs_dir,
            secret_values=secret_values,
        )
        objects.update(native_artifacts)
        partial = partial or native_partial
    except Exception as artifact_error:
        partial = True
        logger.error(
            "Failed to package native Harbor artifacts; continuing with safe "
            f"artifacts: {artifact_error!r}"
        )

    if manifest_path is not None and manifest_path.is_file():
        try:
            objects["manifest.json"] = redact_bytes(
                manifest_path.read_bytes(), secret_values
            )
        except Exception as artifact_error:
            partial = True
            logger.error(
                "Failed to package Harbor manifest; continuing with safe artifacts: "
                f"{artifact_error!r}"
            )
    return objects, partial


def _set_artifact_metadata(
    output: AgentTrajectoryOutput,
    *,
    status: ArtifactStatus,
    prefix: str | None = None,
) -> None:
    native_output = dict(output.output or {})
    artifact_metadata = {
        "artifact_status": status,
        "framework": "harbor",
    }
    if prefix is not None:
        artifact_metadata["artifact_prefix"] = prefix
    native_output["harbor"] = artifact_metadata
    output.output = native_output


async def run_harbor_trajectory(trajectory_id: str) -> None:
    """Run one Studio trajectory through Harbor and report native output."""

    output: AgentTrajectoryOutput | None = None
    snapshot_id: str | None = None
    post_populate_snapshot_id: str | None = None
    env_image_layer_s3_uri: str | None = None
    error: BaseException | None = None
    config: Any | None = None
    log_buffer = _BoundedLogBuffer(limit_bytes=_MAX_STORED_LOG_BYTES)

    with logger.contextualize(
        trajectory_id=trajectory_id,
        execution_framework="harbor",
        function_call_id=modal.current_function_call_id(),
    ):
        try:
            setup_logger()
            config = await fetch_agent_config(trajectory_id)
            if not config.trajectory_batch_id:
                raise ValueError("Harbor trajectories must belong to a batch")

            with tempfile.TemporaryDirectory(prefix="studio-harbor-") as raw_dir:
                work_dir = Path(raw_dir)
                package_dir = work_dir / "package"
                jobs_dir = work_dir / "jobs"
                manifest_path: Path | None = None

                try:
                    package_dir.mkdir()
                    task_dir = materialize_task_package(package_dir, config)
                    manifest_path = task_dir / "manifest.json"
                    command = build_harbor_command(
                        task_dir=str(task_dir),
                        jobs_dir=str(jobs_dir),
                        trajectory_id=trajectory_id,
                        orchestrator_model=config.orchestrator_model,
                    )

                    def on_line(source: str, message: str) -> None:
                        record = {
                            "timestamp": datetime.now(UTC).isoformat(),
                            "source": source,
                            "message": message,
                        }
                        log_buffer.append(record)
                        bound = logger.bind(harbor_stream=source)
                        if source == "stderr":
                            bound.warning(message)
                        else:
                            bound.info(message)

                    await run_harbor_command(
                        command,
                        cwd=work_dir,
                        timeout_seconds=_harbor_command_timeout_seconds(),
                        on_line=on_line,
                    )

                    raise_for_harbor_result_error(jobs_dir)
                    output = load_native_output(jobs_dir)
                    if output.status in {AgentStatus.PENDING, AgentStatus.RUNNING}:
                        raise RuntimeError(
                            "Harbor native output has non-terminal status: "
                            f"{output.status.value}"
                        )
                    if output.status is AgentStatus.COMPLETED:
                        snapshot_id = load_snapshot_id(
                            jobs_dir, "studio_final_snapshot.json"
                        )
                        env_image_layer_s3_uri = load_env_image_layer_s3_uri(
                            jobs_dir, trajectory_id
                        )
                    post_populate_snapshot_id = load_snapshot_id(
                        jobs_dir, "studio_post_populate_snapshot.json"
                    )
                except BaseException as exc:
                    error = exc
                    if output is None:
                        try:
                            output = load_native_output(jobs_dir)
                        except Exception as recovery_error:
                            logger.debug(
                                "No recoverable Harbor native output after failure: "
                                f"{recovery_error!r}"
                            )
                    if post_populate_snapshot_id is None:
                        try:
                            post_populate_snapshot_id = load_snapshot_id(
                                jobs_dir, "studio_post_populate_snapshot.json"
                            )
                        except (AttributeError, OSError, ValueError) as recovery_error:
                            logger.debug(
                                "No recoverable Harbor post-populate snapshot after "
                                f"failure: {recovery_error!r}"
                            )
                    if snapshot_id is None:
                        try:
                            snapshot_id = load_snapshot_id(
                                jobs_dir, "studio_final_snapshot.json"
                            )
                        except (AttributeError, OSError, ValueError) as recovery_error:
                            logger.debug(
                                "No recoverable Harbor final snapshot after failure: "
                                f"{recovery_error!r}"
                            )
                    if env_image_layer_s3_uri is None:
                        try:
                            env_image_layer_s3_uri = load_env_image_layer_s3_uri(
                                jobs_dir, trajectory_id
                            )
                        except (AttributeError, OSError, ValueError) as recovery_error:
                            logger.debug(
                                "No recoverable Harbor env image layer after failure: "
                                f"{recovery_error!r}"
                            )
                    logger.error(
                        f"Harbor batch run failed: {exc!r}\n{traceback.format_exc()}"
                    )

                output = _output_for_error(output, error)
                try:
                    prefix = artifact_prefix(snapshot_id or trajectory_id)
                    objects, partial_artifacts = _artifact_objects(
                        manifest_path=manifest_path,
                        jobs_dir=jobs_dir,
                        log_content=log_buffer.to_bytes(),
                        output=output,
                    )
                    uploaded_prefix = await upload_harbor_artifacts(
                        prefix=prefix,
                        objects=objects,
                    )
                except _CANCELLATION_ERRORS:
                    raise
                except Exception as artifact_error:
                    logger.error(
                        f"Failed to upload Harbor artifacts: {artifact_error!r}"
                    )
                    # Native Harbor artifacts are optional exports. Preserve the
                    # authoritative Studio trajectory status when their packaging
                    # or upload fails.
                    _set_artifact_metadata(output, status="failed")
                else:
                    _set_artifact_metadata(
                        output,
                        status="partial" if partial_artifacts else "complete",
                        prefix=uploaded_prefix,
                    )
        except BaseException as exc:
            if error is None and _is_non_authoritative_finalization_error(output, exc):
                logger.error(
                    "Harbor post-run finalization failed after a completed result; "
                    f"preserving completion: {exc!r}\n{traceback.format_exc()}"
                )
            else:
                logger.error(
                    f"Harbor batch run failed: {exc!r}\n{traceback.format_exc()}"
                )
                if error is None:
                    error = exc
        finally:
            output = _output_for_error(output, error)

            try:
                try:
                    await save_results(
                        trajectory_id=trajectory_id,
                        output=output,
                        snapshot_id=snapshot_id,
                        post_populate_snapshot_id=post_populate_snapshot_id,
                        env_image_layer_s3_uri=env_image_layer_s3_uri,
                    )
                except BaseException as save_error:
                    logger.exception(f"Failed to save Harbor result: {save_error!r}")
                    if isinstance(save_error, _CANCELLATION_ERRORS):
                        error = save_error
                        output = _output_for_error(output, error)
                    elif error is None and not isinstance(save_error, Exception):
                        error = save_error
                    try:
                        await save_results(
                            trajectory_id=trajectory_id,
                            output=output,
                            snapshot_id=snapshot_id,
                            post_populate_snapshot_id=post_populate_snapshot_id,
                            env_image_layer_s3_uri=env_image_layer_s3_uri,
                        )
                    except BaseException as terminal_save_error:
                        logger.bind(
                            error_type=type(terminal_save_error).__name__,
                            save_phase="retry",
                        ).exception(
                            "Failed to save Harbor result after retry: "
                            f"{terminal_save_error!r}"
                        )
                        if isinstance(terminal_save_error, _CANCELLATION_ERRORS):
                            error = terminal_save_error
                        elif error is None:
                            error = terminal_save_error
            finally:
                await teardown_logger()

    if error is not None:
        raise error

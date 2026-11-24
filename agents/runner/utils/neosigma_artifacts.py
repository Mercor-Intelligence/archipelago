"""Attach versioned Archipelago workspace files to the active NeoSigma span."""

import asyncio
import hashlib
import json
import shutil
import stat
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from tempfile import SpooledTemporaryFile
from typing import BinaryIO, Literal
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import httpx
import neosigma
from loguru import logger
from opentelemetry.trace import get_current_span

from runner.harbor_world_source import is_reserved_answer_path
from runner.utils.signing import sign_env_runner_request

Direction = Literal["input", "output"]
Artifact = tuple[str, str]
FileVersion = tuple[str, int]
_UPLOAD_PART_BYTES = 16 * 1024 * 1024
_SNAPSHOT_BODY = b'{"exclude_globs":[".apps_data",".apps_data/*"]}'


def _workspace_path(member: tarfile.TarInfo) -> str | None:
    path = PurePosixPath(member.name)
    if (
        not member.isfile()
        or path.is_absolute()
        or "\\" in member.name
        or ".." in path.parts
        or len(path.parts) < 2
        or path.parts[0] != "filesystem"
        or is_reserved_answer_path(path.parts[1:])
    ):
        return None
    return str(PurePosixPath(*path.parts[1:]))


def workspace_delta(
    snapshot: BinaryIO, previous: dict[str, FileVersion], destination: BinaryIO
) -> tuple[dict[str, FileVersion], list[str], list[str]]:
    """Write added or modified file bytes and a manifest into a ZIP archive."""
    current: dict[str, FileVersion] = {}
    changed: list[str] = []
    with (
        tarfile.open(fileobj=snapshot, mode="r:gz") as source,
        ZipFile(destination, mode="w", compression=ZIP_DEFLATED) as output,
    ):
        for member in source:
            path = _workspace_path(member)
            if path is None:
                continue
            content = source.extractfile(member)
            assert content is not None
            with content:
                digest = hashlib.file_digest(content, "sha256").hexdigest()
                mode = member.mode & 0o777
                current[path] = (digest, mode)
                if previous.get(path) == current[path]:
                    continue
                content.seek(0)
                entry = ZipInfo(f"files/{path}")
                entry.create_system = 3
                entry.external_attr = (stat.S_IFREG | mode) << 16
                entry.compress_type = ZIP_DEFLATED
                with output.open(entry, "w", force_zip64=True) as file:
                    shutil.copyfileobj(content, file)
                changed.append(path)
        deleted = sorted(previous.keys() - current.keys())
        output.writestr(
            "manifest.json",
            json.dumps(
                {
                    "files": {
                        path: {"sha256": current[path][0], "mode": current[path][1]}
                        for path in sorted(changed)
                    },
                    "deleted": deleted,
                },
                ensure_ascii=False,
            ),
        )
    return current, changed, deleted


def upload_parts(
    data: BinaryIO, name: str, direction: Direction
) -> tuple[Artifact, ...]:
    size = data.seek(0, 2)
    data.seek(0)
    parts: list[Artifact] = []
    for index in range((size + _UPLOAD_PART_BYTES - 1) // _UPLOAD_PART_BYTES):
        part_name = (
            name if size <= _UPLOAD_PART_BYTES else f"{name}.part-{index + 1:05d}"
        )
        uploaded = neosigma.upload_artifact(
            data.read(_UPLOAD_PART_BYTES), name=part_name, direction=direction
        )
        parts.append((uploaded.location, part_name))
    return tuple(parts)


def _file_arguments(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value.removeprefix("/filesystem/").removeprefix("./")
    elif isinstance(value, dict):
        for item in value.values():
            yield from _file_arguments(item)
    elif isinstance(value, list):
        for item in value:
            yield from _file_arguments(item)


@dataclass
class WorkspaceArtifacts:
    sandbox_url: str
    auth_token: str | None
    files: dict[str, FileVersion] = field(default_factory=dict)
    starting_files: dict[str, FileVersion] = field(default_factory=dict)
    versions: dict[str, tuple[Artifact, ...]] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: asyncio.Task[None] | None = None
    needs_baseline: bool = False

    async def drain(self) -> None:
        if self.pending is not None:
            await asyncio.shield(self.pending)
            self.pending = None

    async def capture(
        self,
        name: str,
        direction: Direction,
        *,
        starting: bool = False,
        final: bool = False,
    ) -> None:
        url = f"{self.sandbox_url}/data/snapshot"
        try:
            headers = {
                "Content-Type": "application/json",
                **sign_env_runner_request("POST", url, body=_SNAPSHOT_BODY),
            }
            if self.auth_token:
                headers["Authorization"] = f"Bearer {self.auth_token}"
            with SpooledTemporaryFile(max_size=_UPLOAD_PART_BYTES) as snapshot:
                async with httpx.AsyncClient(timeout=30) as client:
                    async with client.stream(
                        "POST", url, content=_SNAPSHOT_BODY, headers=headers
                    ) as response:
                        response.raise_for_status()
                        async for chunk in response.aiter_bytes():
                            snapshot.write(chunk)
                snapshot.seek(0)
                # The thread borrows the archive until it finishes.
                task = asyncio.create_task(
                    asyncio.to_thread(
                        self._attach,
                        snapshot,
                        name,
                        direction,
                        starting=starting,
                        final=final,
                    )
                )
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    await task
                    raise
            self.needs_baseline = False
        except Exception as exc:
            self.needs_baseline = True
            get_current_span().set_attribute(
                "archipelago.artifacts.capture_error", type(exc).__name__
            )
            logger.warning(
                "NeoSigma workspace artifact capture failed ({})", type(exc).__name__
            )

    def _attach(
        self,
        snapshot: BinaryIO,
        name: str,
        direction: Direction,
        *,
        starting: bool,
        final: bool,
    ) -> None:
        # shortcut: snapshots capture files that survive each call, use sandbox file events
        # if artifacts created and removed within one call are required.
        previous = self.starting_files if final else self.files
        with SpooledTemporaryFile(max_size=_UPLOAD_PART_BYTES) as destination:
            current, changed, deleted = workspace_delta(snapshot, previous, destination)
            if not changed and not deleted:
                return
            parts = upload_parts(destination, name, direction)
        self.versions.update({path: parts for path in changed})
        for path in deleted:
            self.versions.pop(path, None)
        self.files = current
        if starting:
            self.starting_files.update({path: current[path] for path in changed})
            for path in deleted:
                self.starting_files.pop(path, None)

    def attach_referenced_inputs(self, arguments: object) -> None:
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                return
        paths = sorted(set(_file_arguments(arguments)) & self.versions.keys())
        for location, name in sorted(
            {part for path in paths for part in self.versions[path]}
        ):
            neosigma.artifact(location, name=name, direction="input")
        if paths:
            get_current_span().set_attribute("archipelago.artifacts.input_paths", paths)

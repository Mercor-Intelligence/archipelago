"""Read and publish workspace snapshots outside the timed agent execution."""

import asyncio
import hashlib
import json
from pathlib import Path, PurePosixPath
from tempfile import TemporaryDirectory, TemporaryFile
from typing import BinaryIO

import httpx

from runner.utils.signing import sign_env_runner_request

_PART_BYTES = 16 * 1024 * 1024
_PART_CONCURRENCY = 4


class SnapshotUploadError(RuntimeError):
    """A workspace snapshot could not be published in full."""


async def read_workspace(sandbox_url: str, token: str | None) -> BinaryIO:
    """Spool the environment's read-only tar stream to disk."""
    url = f"{sandbox_url}/data/snapshot/workspace"
    headers = sign_env_runner_request("POST", url)
    if token:
        headers["Authorization"] = f"Bearer {token}"

    snapshot = TemporaryFile()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            async with client.stream("POST", url, headers=headers) as response:
                response.raise_for_status()
                async for chunk in response.aiter_bytes():
                    snapshot.write(chunk)
        snapshot.seek(0)
        return snapshot
    except BaseException:
        snapshot.close()
        raise


def _stage_part(snapshot: BinaryIO, path: Path) -> dict:
    contents = snapshot.read(_PART_BYTES)
    path.write_bytes(contents)
    return {
        "name": path.name,
        "size_bytes": len(contents),
        "sha256": hashlib.sha256(contents).hexdigest(),
    }


async def _confirmed_location(
    sdk, source: Path | bytes, name: str, media_type: str
) -> str:
    receipt = sdk.upload_artifact(
        source, name=name, content_type=media_type, attach=False
    )
    await receipt.wait()
    return receipt.location


async def publish_artifact(
    sdk, snapshot: BinaryIO, name: str, media_type: str
) -> tuple[str, str]:
    """Return a managed reference only after every part is confirmed by the SDK."""
    size = snapshot.seek(0, 2)
    snapshot.seek(0)
    if size == 0:
        raise SnapshotUploadError("The environment returned an empty snapshot stream")

    filename = PurePosixPath(name).name
    count = (size + _PART_BYTES - 1) // _PART_BYTES
    parts = []
    part_media_type = media_type if count == 1 else "application/octet-stream"
    for start in range(1, count + 1, _PART_CONCURRENCY):
        with TemporaryDirectory(prefix="archipelago-neosigma-") as directory:
            batch = []
            uploads = []
            for index in range(start, min(count + 1, start + _PART_CONCURRENCY)):
                part_name = filename if count == 1 else f"{filename}.part-{index:05d}"
                path = Path(directory) / part_name
                batch.append(await asyncio.to_thread(_stage_part, snapshot, path))
                uploads.append((path, part_name))

            results = await asyncio.gather(
                *(
                    _confirmed_location(sdk, path, part_name, part_media_type)
                    for path, part_name in uploads
                ),
                return_exceptions=True,
            )
            for index, (part, result) in enumerate(
                zip(batch, results, strict=True), start
            ):
                if isinstance(result, BaseException):
                    raise SnapshotUploadError(
                        f"Snapshot part {index} of {count} was not confirmed"
                    ) from result
                parts.append({**part, "location": result})

    if count == 1:
        return parts[0]["location"], name

    manifest_name = f"{filename}.manifest.json"
    manifest = json.dumps({"filename": name, "parts": parts}).encode()
    try:
        location = await _confirmed_location(
            sdk, manifest, manifest_name, "application/json"
        )
    except Exception as exc:
        raise SnapshotUploadError("Snapshot manifest was not confirmed") from exc
    return location, manifest_name

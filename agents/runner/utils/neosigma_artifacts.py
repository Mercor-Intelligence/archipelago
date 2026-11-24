"""Read and publish workspace snapshots outside the timed agent execution."""

import hashlib
import json
import os
from pathlib import PurePosixPath
from tempfile import TemporaryFile
from typing import BinaryIO
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from runner.utils.signing import sign_env_runner_request

_PART_BYTES = 16 * 1024 * 1024


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


async def _upload_file(
    client: httpx.AsyncClient,
    base_url: str,
    headers: dict[str, str],
    workspace_id: str,
    name: str,
    contents: bytes,
    media_type: str,
) -> str:
    file_id = str(uuid4())
    prepared = await client.post(
        f"{base_url}/files.create",
        headers=headers,
        json={
            "file_id": file_id,
            "workspace_id": workspace_id,
            "filename": name,
            "media_type": media_type,
            "size_bytes": len(contents),
        },
    )
    prepared.raise_for_status()
    reservation = prepared.json()
    if (
        reservation["file"]["id"] != file_id
        or reservation["file"]["status"] != "pending"
    ):
        raise SnapshotUploadError("NeoSigma did not reserve the snapshot file")
    if urlsplit(reservation["upload_url"]).scheme != "https":
        raise SnapshotUploadError("NeoSigma returned an invalid upload URL")

    uploaded = await client.put(
        reservation["upload_url"],
        headers=reservation["upload_headers"],
        content=contents,
    )
    uploaded.raise_for_status()
    confirmed = await client.post(
        f"{base_url}/files.complete", headers=headers, json={"file_id": file_id}
    )
    confirmed.raise_for_status()
    file = confirmed.json()["file"]
    if file["id"] != file_id or file["status"] != "complete":
        raise SnapshotUploadError("NeoSigma did not confirm the snapshot file")
    return f"neosigma://artifacts/{file_id}"


async def publish_artifact(
    snapshot: BinaryIO, name: str, media_type: str
) -> tuple[str, str]:
    """Return a managed reference only after every part is confirmed."""
    base_url = os.getenv(
        "NEOSIGMA_MANAGED_AGENTS_BASE_URL", "https://api.neosigma.ai"
    ).rstrip("/")
    headers = {"Authorization": f"Bearer {os.environ['NEOSIGMA_API_KEY']}"}
    project = os.environ["NEOSIGMA_PROJECT"]
    size = snapshot.seek(0, 2)
    snapshot.seek(0)
    if size == 0:
        raise SnapshotUploadError("The environment returned an empty snapshot stream")

    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(
            f"{base_url}/projects.get", headers=headers, params={"project_id": project}
        )
        response.raise_for_status()
        workspace_id = response.json()["project"]["workspace_id"]
        if not workspace_id:
            raise SnapshotUploadError("The NeoSigma project has no workspace")

        parts = []
        count = (size + _PART_BYTES - 1) // _PART_BYTES
        for index in range(1, count + 1):
            data = snapshot.read(_PART_BYTES)
            filename = name if count == 1 else f"{name}.part-{index:05d}"
            try:
                location = await _upload_file(
                    client,
                    base_url,
                    headers,
                    workspace_id,
                    filename,
                    data,
                    media_type if count == 1 else "application/octet-stream",
                )
            except Exception as exc:
                raise SnapshotUploadError(
                    f"Snapshot part {index} of {count} was not confirmed"
                ) from exc
            parts.append(
                {
                    "name": filename,
                    "location": location,
                    "size_bytes": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                }
            )
        if count == 1:
            return parts[0]["location"], name

        manifest_name = f"{PurePosixPath(name).name}.manifest.json"
        manifest = json.dumps({"filename": name, "parts": parts}).encode()
        location = await _upload_file(
            client,
            base_url,
            headers,
            workspace_id,
            manifest_name,
            manifest,
            "application/json",
        )
        return location, manifest_name

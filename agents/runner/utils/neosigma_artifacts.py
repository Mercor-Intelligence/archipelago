"""Read and publish workspace snapshots outside the timed agent execution."""

import asyncio
import hashlib
import json
import os
from pathlib import PurePosixPath
from tempfile import TemporaryFile
from typing import BinaryIO
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from loguru import logger

from runner.utils.signing import sign_env_runner_request

_PART_BYTES = 16 * 1024 * 1024
_PART_CONCURRENCY = 4
_MAX_ATTEMPTS = 3
_BACKOFF_SECONDS = 0.5


class SnapshotUploadError(RuntimeError):
    """A workspace snapshot could not be published in full."""


async def _request(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    accept: tuple[int, ...] = (),
    **kwargs,
) -> httpx.Response:
    for attempt in range(_MAX_ATTEMPTS):
        try:
            response = await client.request(method, url, **kwargs)
            if response.status_code in accept:
                return response
            response.raise_for_status()
            return response
        except httpx.HTTPError as error:
            transient = isinstance(error, httpx.TransportError) or (
                isinstance(error, httpx.HTTPStatusError)
                and (
                    error.response.status_code in (408, 429)
                    or error.response.status_code >= 500
                )
            )
            if not transient or attempt == _MAX_ATTEMPTS - 1:
                raise
            await asyncio.sleep(_BACKOFF_SECONDS * 2**attempt)
    raise AssertionError("No request attempts")


async def _rollback(client, base_url, headers, file_ids) -> None:
    if not file_ids:
        return
    try:
        async with asyncio.timeout(2):
            for start in range(0, len(file_ids), _PART_CONCURRENCY):
                results = await asyncio.gather(
                    *(
                        _request(
                            client,
                            "DELETE",
                            f"{base_url}/files.delete",
                            headers=headers,
                            json={"file_id": file_id},
                            accept=(404,),
                        )
                        for file_id in file_ids[start : start + _PART_CONCURRENCY]
                    ),
                    return_exceptions=True,
                )
                if any(isinstance(result, Exception) for result in results):
                    logger.warning(
                        "NeoSigma could not clean up some unpublished artifact files"
                    )
    except TimeoutError:
        logger.warning("NeoSigma cleanup of unpublished artifact files timed out")


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
    file_ids: list[str],
) -> str:
    file_id = str(uuid4())
    file_ids.append(file_id)
    prepared = await _request(
        client,
        "POST",
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
    reservation = prepared.json()
    if (
        reservation["file"]["id"] != file_id
        or reservation["file"]["status"] != "pending"
    ):
        raise SnapshotUploadError("NeoSigma did not reserve the snapshot file")
    if urlsplit(reservation["upload_url"]).scheme != "https":
        raise SnapshotUploadError("NeoSigma returned an invalid upload URL")

    await _request(
        client,
        "PUT",
        reservation["upload_url"],
        headers=reservation["upload_headers"],
        content=contents,
        # A retry may see the object from a PUT whose response was lost.
        accept=(412,),
    )
    confirmed = await _request(
        client,
        "POST",
        f"{base_url}/files.complete",
        headers=headers,
        json={"file_id": file_id},
    )
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
        response = await _request(
            client,
            "GET",
            f"{base_url}/projects.get",
            headers=headers,
            params={"project_id": project},
        )
        workspace_id = response.json()["project"]["workspace_id"]
        if not workspace_id:
            raise SnapshotUploadError("The NeoSigma project has no workspace")

        count = (size + _PART_BYTES - 1) // _PART_BYTES
        file_ids: list[str] = []

        async def upload_part(index: int, data: bytes):
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
                    file_ids,
                )
            except Exception as exc:
                raise SnapshotUploadError(
                    f"Snapshot part {index} of {count} was not confirmed"
                ) from exc
            return {
                "name": filename,
                "location": location,
                "size_bytes": len(data),
                "sha256": hashlib.sha256(data).hexdigest(),
            }

        try:
            parts = []
            for start in range(1, count + 1, _PART_CONCURRENCY):
                batch = await asyncio.gather(
                    *(
                        upload_part(index, snapshot.read(_PART_BYTES))
                        for index in range(
                            start, min(count + 1, start + _PART_CONCURRENCY)
                        )
                    ),
                    return_exceptions=True,
                )
                for part in batch:
                    if isinstance(part, BaseException):
                        raise part
                    parts.append(part)
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
                file_ids,
            )
            return location, manifest_name
        except BaseException:
            await _rollback(client, base_url, headers, file_ids)
            raise

"""Fingerprint reserved world files from their original snapshot, before task overlays."""

import asyncio
import hashlib
import json
from pathlib import PurePosixPath
from typing import Any

# Answer material in a Harbor bundle, relative to its root. Long-horizon tasks keep a
# reference and verifier per step (steps/<n>/solution, steps/<n>/tests), and a root
# golden patch is a reference the AutoQC resolver also recognises. The populate filter
# and the in-sandbox probe in harbor_answer_isolation apply this same rule.
_ANSWER_DIRS = frozenset({"solution", "tests"})
_ROOT_ANSWER_FILES = frozenset({"golden.patch", "golden_patch"})


def is_reserved_answer_path(parts: tuple[str, ...]) -> bool:
    """Whether a bundle-relative path is, or lies under, reserved answer material."""
    if not parts:
        return False
    if parts[0] in _ANSWER_DIRS:
        return True
    if len(parts) == 1:
        return parts[0] in _ROOT_ANSWER_FILES
    return parts[0] == "steps" and len(parts) >= 3 and parts[2] in _ANSWER_DIRS


def fingerprint_world_files(files: dict[str, bytes]) -> str | None:
    entries: dict[tuple[str, ...], bytes | None] = {}
    for name, content_hash in files.items():
        path = PurePosixPath(name)
        if str(path) != name or path.is_absolute() or ".." in path.parts:
            raise ValueError("Non-canonical world snapshot path")
        if not is_reserved_answer_path(path.parts):
            raise ValueError("World evidence must be scoped to reserved directories")
        if path.parts in entries and entries[path.parts] is None:
            raise ValueError(
                "World snapshot contains conflicting file and directory paths"
            )
        entries[path.parts] = content_hash
        for parent in path.parents:
            if parent.parts:
                if entries.get(parent.parts) is not None:
                    raise ValueError(
                        "World snapshot contains conflicting file and directory paths"
                    )
                entries[parent.parts] = None
    digest = hashlib.sha256()
    for parts, content_hash in sorted(entries.items()):
        digest.update(
            json.dumps(
                ["/".join(parts), "dir" if content_hash is None else "file"]
            ).encode()
            + b"\0"
        )
        if content_hash is not None:
            digest.update(content_hash)
    return digest.hexdigest() if entries else None


async def _root_objects(s3: Any, bucket: str, prefix: str) -> list[dict[str, Any]]:
    paginator = s3.get_paginator("list_objects_v2")
    root_objects: list[dict[str, Any]] = []
    async for page in paginator.paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
        root_objects.extend(page.get("Contents", []))
    return root_objects


def _is_harbor_root(root_objects: list[dict[str, Any]], prefix: str) -> bool:
    root_names = {obj["Key"].removeprefix(prefix) for obj in root_objects}
    return {"task.toml", "instruction.md"} <= root_names


async def is_harbor_bundle(s3: Any, bucket: str, prefix: str) -> bool:
    """Whether a snapshot half is a raw Harbor bundle, judged from its root files."""
    async with asyncio.timeout(60):
        return _is_harbor_root(await _root_objects(s3, bucket, prefix), prefix)


async def read_world_answer_digest(s3: Any, bucket: str, prefix: str) -> str | None:
    """Harbor-shaped world sources never authorize their own hidden tests or solutions."""
    async with asyncio.timeout(60):
        paginator = s3.get_paginator("list_objects_v2")
        root_objects = await _root_objects(s3, bucket, prefix)
        if _is_harbor_root(root_objects, prefix):
            return None
        objects = [
            obj
            for obj in root_objects
            if is_reserved_answer_path((obj["Key"].removeprefix(prefix),))
        ]
        for name in ("tests", "solution", "steps"):
            async for page in paginator.paginate(
                Bucket=bucket, Prefix=f"{prefix}{name}/"
            ):
                objects.extend(
                    obj
                    for obj in page.get("Contents", [])
                    if is_reserved_answer_path(
                        PurePosixPath(obj["Key"].removeprefix(prefix)).parts
                    )
                )
        hashes: dict[str, bytes] = {}
        semaphore = asyncio.Semaphore(8)

        async def hash_object(obj: dict[str, Any]) -> None:
            key = obj["Key"]
            if not key.startswith(prefix):
                raise ValueError("World evidence escaped its snapshot prefix")
            name = key.removeprefix(prefix)
            fingerprint_world_files({name: b""})
            async with semaphore:
                response = await s3.get_object(
                    Bucket=bucket, Key=key, IfMatch=obj["ETag"]
                )
                digest = hashlib.sha256()
                async with response["Body"] as body:
                    while chunk := await body.read(1024 * 1024):
                        digest.update(chunk)
                hashes[name] = digest.digest()

        async with asyncio.TaskGroup() as group:
            for obj in objects:
                group.create_task(hash_object(obj))
        return fingerprint_world_files(hashes)

"""Merge the two halves of a diff baseline into the single zip the engine reads.

The lane builds this in ``download_world_snapshot`` while it downloads, because it
holds S3 credentials and can list the task prefix. An in-sandbox grade holds two
presigned archives instead, so the same merge happens here, over zips.

The subtraction rule stays in ``file_subtraction``. Both callers ask it the same
question and neither re-derives it: the arming answer travels from the server as
``subtraction_resolved``.

CANONICAL SOURCE, with ``file_subtraction``: ``scripts/vendor_snapshot_layers.py``
copies both verbatim into the pipelines runner and the server, so every reader of
a task's world merges it as grading does. Edit only these copies.
"""

from __future__ import annotations

import shutil
import tempfile
import zipfile
from typing import IO

# Relative, so the vendored copies import their own sibling.
from .file_subtraction import baseline_removal, is_removed

#: Bounds what one member costs. A world snapshot can hold a file bigger than
#: the sandbox's memory.
_COPY_BUFFER = 1024 * 1024


def _copy_member(source: zipfile.ZipFile, out: zipfile.ZipFile, name: str) -> None:
    """One member across through a buffer, keeping mode, timestamp and encoding.

    `ZipFile.read` returns the whole decompressed member, so a single large file
    is enough to exhaust the grade subprocess.
    """
    info = source.getinfo(name)
    written = zipfile.ZipInfo(info.filename, date_time=info.date_time)
    written.compress_type = info.compress_type
    written.external_attr = info.external_attr
    written.internal_attr = info.internal_attr
    written.create_system = info.create_system
    # Set so ZipFile can decide ZIP64 up front; it cannot infer a size from a stream.
    written.file_size = info.file_size
    with source.open(info) as src, out.open(written, "w") as dst:
        shutil.copyfileobj(src, dst, _COPY_BUFFER)


def merge_baseline_layers(
    world: IO[bytes],
    task: IO[bytes] | None,
    *,
    subtraction_resolved: bool,
) -> IO[bytes]:
    """One baseline zip from the world seed and the authored ``tasks/`` overlay.

    A task entry wins over a world entry of the same name, which is the
    last-write-wins merge the lane produces. Markers are always dropped, and
    their targets only when the run that produced the capture resolved them.

    The result is a temporary FILE. A world snapshot can be gigabytes, and a
    merged copy in memory beside the two halves is what exhausts a sandbox.
    """
    if task is None:
        return world

    world.seek(0)
    task.seek(0)
    with zipfile.ZipFile(world) as world_zip, zipfile.ZipFile(task) as task_zip:
        kept_world, kept_task = overlay(
            world_zip.namelist(),
            task_zip.namelist(),
            subtraction_resolved=subtraction_resolved,
        )

        # A file and not a BytesIO: a merged copy in memory beside the two
        # halves is a third of a world snapshot held at once.
        merged = tempfile.NamedTemporaryFile(suffix=".zip")  # noqa: SIM115
        # Each member carries its own compress_type, so the mode here only
        # covers anything written without one.
        with zipfile.ZipFile(merged, "w") as out:
            for name in kept_world:
                _copy_member(world_zip, out, name)
            for name in kept_task:
                _copy_member(task_zip, out, name)

    merged.seek(0)
    return merged


def overlay(
    world_names: list[str],
    task_names: list[str],
    *,
    subtraction_resolved: bool,
) -> tuple[list[str], list[str]]:
    """The world names and task names the merged baseline keeps, in order.

    A task name hides the world name it equals, and ``baseline_removal``
    decides which markers and targets drop from both halves.
    """
    removal = baseline_removal(
        world_names, task_names, resolve_targets=subtraction_resolved
    )
    overlaid = set(task_names)
    kept_world = [
        name
        for name in world_names
        if name not in overlaid and not is_removed(name, removal)
    ]
    kept_task = [name for name in task_names if not is_removed(name, removal)]
    return kept_world, kept_task

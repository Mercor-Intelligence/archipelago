"""Where the grade's local files live, for the two modules that write them.

A leaf, because both ends of the diff need it and they cannot import each
other: `grade.py` imports `.data.snapshot.streaming`, and importing anything
under `data/snapshot/` runs its `__init__`, which pulls `.jobs` and so `main`.

Both directories sit under a 0700 root-owned dir inside `/app`, which the
grading engine's mount does not pre-make, so every writer creates it.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

GRADE_WORK_DIR = os.environ.get("GRADING_WORK_DIR", "/app/.grading")

#: Minted where the file is written, never taken from a caller's text. Enforced
#: on the read side too, because the id leaves the sandbox in a result body and
#: comes back in a request.
LOCAL_ID_RE = re.compile(r"^[0-9a-f]{32}$")

#: The kept post-populate archive's filename inside its baseline dir. One
#: module writes it and another reads it, so it is spelled once.
BASELINE_ARCHIVE_NAME = "initial.tar.zst"

#: Read size for digesting the kept archive, which can be hundreds of MB.
BASELINE_DIGEST_CHUNK = 1024 * 1024


def _under(kind: str, local_id: str) -> Path:
    if not LOCAL_ID_RE.fullmatch(local_id):
        raise ValueError(f"malformed {kind} id")
    base = Path(GRADE_WORK_DIR).resolve()
    resolved = (base / f"{kind}-{local_id}").resolve()
    # The regex already forbids a separator, so this cannot fire today. It is
    # the containment check the path deserves, and not a second reading of the
    # same pattern.
    if not resolved.is_relative_to(base):
        raise ValueError(f"{kind} id escapes the work dir")
    return resolved


def capture_dir(capture_id: str) -> Path:
    """Where a pre-taken final snapshot lives, under the root-owned work dir."""
    return _under("capture", capture_id)


def baseline_dir(baseline_id: str) -> Path:
    """Where a kept post-populate archive lives, under the root-owned work dir.

    Written when the snapshot is taken and read when the grade runs, so unlike a
    capture this sits on disk for the whole agent loop.
    """
    return _under("baseline", baseline_id)

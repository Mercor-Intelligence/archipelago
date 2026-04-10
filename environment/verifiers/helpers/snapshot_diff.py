# flake8: noqa
#!/usr/bin/env python3
"""GDM Docker World snapshot diff helper.

Standalone script packaged into the GDM export ZIP. Runs inside the
single-container GDM environment at grading time.

Reconstructs the pre-trajectory filesystem state using start.sh's
backup/overlay system, then diffs it against the current (post-trajectory)
state. Outputs a SnapshotDiffOutput-conformant JSON file.

Env vars:
    TASK_SLUG       - task slug for overlay reconstruction
    CACHE_DIR       - helper cache directory (default: /app/output/.helpers_cache)
    DIFF_DIRS       - colon-separated directories to diff (default: /app/files:/.apps_data)
"""

import difflib
import hashlib
import json
import os
import sys
from pathlib import Path


CACHE_DIR = Path(os.environ.get("CACHE_DIR", "/app/output/.helpers_cache"))
CACHE_FILE = "snapshot_diff.json"

WORLD_FILES_BASE = Path("/app/_world_files_base")
WORLD_APPS_DATA_BASE = Path("/app/_world_apps_data_base")
TASK_OVERLAY_FILES = Path("/app/tools/files")
TASK_OVERLAY_APPS_DATA = Path("/app/tools/.apps_data")

DIFF_TARGETS = [
    (WORLD_FILES_BASE, TASK_OVERLAY_FILES, Path("/app/files")),
    (WORLD_APPS_DATA_BASE, TASK_OVERLAY_APPS_DATA, Path("/.apps_data")),
]

MAX_CONTENT_SIZE = 512 * 1024


def _is_text_file(path: Path) -> bool:
    try:
        with open(path, "rb") as f:
            chunk = f.read(8192)
        chunk.decode("utf-8")
        return True
    except (UnicodeDecodeError, OSError):
        return False


def _read_text_safe(path: Path) -> str | None:
    if not path.is_file() or path.stat().st_size > MAX_CONTENT_SIZE:
        return None
    if not _is_text_file(path):
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _walk_directory(root: Path) -> dict[str, Path]:
    if not root.exists():
        return {}
    result: dict[str, Path] = {}
    for p in root.rglob("*"):
        if p.is_file():
            result[str(p.relative_to(root))] = p
    return result


def _reconstruct_pre_state(
    world_base: Path, task_overlay: Path, task_slug: str
) -> dict[str, Path]:
    pre: dict[str, Path] = {}
    pre.update(_walk_directory(world_base))
    overlay_dir = task_overlay / task_slug
    if overlay_dir.exists():
        pre.update(_walk_directory(overlay_dir))
    return pre


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()


def _compute_diff(
    pre_files: dict[str, Path], post_files: dict[str, Path]
) -> list[dict[str, object]]:
    changes: list[dict[str, object]] = []
    all_keys = sorted(set(pre_files) | set(post_files))

    for rel in all_keys:
        pre_path = pre_files.get(rel)
        post_path = post_files.get(rel)

        if pre_path and not post_path:
            old_content = _read_text_safe(pre_path)
            changes.append(
                {
                    "path": rel,
                    "change_type": "deleted",
                    "old_content": old_content,
                    "old_size": pre_path.stat().st_size if pre_path.exists() else None,
                }
            )
        elif post_path and not pre_path:
            new_content = _read_text_safe(post_path)
            changes.append(
                {
                    "path": rel,
                    "change_type": "created",
                    "new_content": new_content,
                    "new_size": post_path.stat().st_size
                    if post_path.exists()
                    else None,
                }
            )
        elif pre_path and post_path:
            pre_hash = _file_hash(pre_path)
            post_hash = _file_hash(post_path)
            if pre_hash == post_hash:
                changes.append(
                    {
                        "path": rel,
                        "change_type": "unchanged",
                        "old_size": pre_path.stat().st_size,
                        "new_size": post_path.stat().st_size,
                    }
                )
            else:
                old_text = _read_text_safe(pre_path)
                new_text = _read_text_safe(post_path)
                content_diff = None
                if old_text is not None and new_text is not None:
                    diff_lines = difflib.unified_diff(
                        old_text.splitlines(keepends=True),
                        new_text.splitlines(keepends=True),
                        fromfile=f"a/{rel}",
                        tofile=f"b/{rel}",
                    )
                    content_diff = "".join(diff_lines)

                changes.append(
                    {
                        "path": rel,
                        "change_type": "modified",
                        "old_content": old_text,
                        "new_content": new_text,
                        "content_diff": content_diff,
                        "old_size": pre_path.stat().st_size,
                        "new_size": post_path.stat().st_size,
                    }
                )

    return changes


def main() -> int:
    cache_path = CACHE_DIR / CACHE_FILE
    if cache_path.exists():
        print(f"snapshot_diff: cache hit at {cache_path}", file=sys.stderr)
        return 0

    task_slug = os.environ.get("TASK_SLUG", "")

    all_pre_files: dict[str, Path] = {}
    all_post_files: dict[str, Path] = {}

    for world_base, task_overlay, runtime_dir in DIFF_TARGETS:
        pre = _reconstruct_pre_state(world_base, task_overlay, task_slug)
        post = _walk_directory(runtime_dir)
        prefix = str(runtime_dir) + "/"
        all_pre_files.update({f"{prefix}{k}": v for k, v in pre.items()})
        all_post_files.update({f"{prefix}{k}": v for k, v in post.items()})

    changes_list = _compute_diff(all_pre_files, all_post_files)

    changes_by_type: dict[str, list[dict[str, object]]] = {
        "created": [],
        "deleted": [],
        "modified": [],
        "unchanged": [],
    }
    for c in changes_list:
        changes_by_type[str(c["change_type"])].append(c)

    summary = {k: len(v) for k, v in changes_by_type.items()}

    pre_id = f"pre_{task_slug}" if task_slug else "pre_world_baseline"
    post_id = f"post_{task_slug}" if task_slug else "post_current"

    output = {
        "pre_snapshot_id": pre_id,
        "post_snapshot_id": post_id,
        "changes": changes_by_type,
        "summary": summary,
        "total_files_pre": len(all_pre_files),
        "total_files_post": len(all_post_files),
    }

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(output, indent=2), encoding="utf-8")
    print(f"snapshot_diff: wrote {cache_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

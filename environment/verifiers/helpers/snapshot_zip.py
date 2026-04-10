# flake8: noqa
#!/usr/bin/env python3
"""GDM Docker World snapshot zip helper.

Standalone script packaged into the GDM export ZIP. Runs inside the
single-container GDM environment at shutdown / grading time.

Collects files matching extension filters from the runtime filesystem
and produces a zip archive for the grader to consume as a dependency.

Env vars:
    TASK_SLUG        - task slug for cache keying
    FILE_EXTENSIONS  - comma-separated extensions to include (e.g. ".xlsx,.xls,.xlsm")
                       empty string means include all files
    OUTPUT_PATH      - path to write the output zip (e.g. /app/output/snapshot.zip)
"""

import os
import sys
import zipfile
from pathlib import Path

SCAN_DIRS = [
    Path("/app/files"),
    Path("/.apps_data"),
]

MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB per file


def _parse_extensions(raw: str) -> set[str]:
    if not raw.strip():
        return set()
    return {ext.strip().lower() for ext in raw.split(",") if ext.strip()}


def _collect_files(
    scan_dirs: list[Path], extensions: set[str]
) -> list[tuple[Path, str]]:
    collected: list[tuple[Path, str]] = []
    for scan_dir in scan_dirs:
        if not scan_dir.is_dir():
            continue
        for file_path in sorted(scan_dir.rglob("*")):
            if not file_path.is_file():
                continue
            if file_path.stat().st_size > MAX_FILE_SIZE:
                continue
            if extensions and file_path.suffix.lower() not in extensions:
                continue
            rel = str(file_path.relative_to(scan_dir))
            arcname = f"{scan_dir.name}/{rel}"
            collected.append((file_path, arcname))
    return collected


def main() -> int:
    output_path = os.environ.get("OUTPUT_PATH", "")
    if not output_path:
        print("snapshot_zip: OUTPUT_PATH not set", file=sys.stderr)
        return 1

    output = Path(output_path)
    if output.exists():
        print(f"snapshot_zip: output already exists at {output}", file=sys.stderr)
        return 0

    extensions = _parse_extensions(os.environ.get("FILE_EXTENSIONS", ""))
    scan_dirs = [d for d in SCAN_DIRS if d.is_dir()]

    files = _collect_files(scan_dirs, extensions)
    print(
        f"snapshot_zip: collected {len(files)} files "
        f"(extensions={extensions or 'all'})",
        file=sys.stderr,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path, arcname in files:
            try:
                zf.write(file_path, arcname)
            except OSError as e:
                print(f"snapshot_zip: skipping {file_path}: {e}", file=sys.stderr)

    print(f"snapshot_zip: wrote {output} ({len(files)} files)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Snapshot discovery + digesting for the CAD LLM judge.

Reads the final snapshot zip directly rather than through the snapshot-diff
helper, which drops CAD extensions from its artifact extraction
(``runner/helpers/snapshot_diff/main.py``), so a diff-based judge never sees
the deliverable this eval exists to grade.
"""

from __future__ import annotations

import fnmatch
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

from runner.evals.agentic_verifier.cad import (
    MAX_CAD_FILE_BYTES,
    is_cad_file,
    read_cad_bytes,
)
from runner.utils.grading_log import logger

# Prompt-priority order: FreeCAD MCP project store, its legacy sibling,
# explicit exports; a workspace sweep runs after these.
KNOWN_CAD_BASES: tuple[str, ...] = (
    ".apps_data/freecad_mcp/projects/",
    ".apps_data/freecad/",
    "filesystem/exports/",
)
SWEEP_BASE = "filesystem/"
SWEEP_EXCLUDE = "filesystem/input/"

_PARSE_FAILURE_PREFIX = "Could not parse "
_MAX_REFERENCE_FILES = 6
_MAX_REFERENCE_TEXT_CHARS = 20_000
# Parse-failed digests are one-line errors, so they ride along without spending
# the prompt-file cap. The bound also stops the scan: a snapshot of thousands of
# broken members must not be decompressed end-to-end looking for a parseable one.
_MAX_PARSE_FAILURE_ENTRIES = 50

# A corrupt member (bad CRC, unsupported compression, truncated stream) must
# surface as a failed digest, never abort the whole grade.
_MEMBER_READ_ERRORS = (
    zipfile.BadZipFile,
    NotImplementedError,
    OSError,
    RuntimeError,
    ValueError,
    zlib.error,
)


@dataclass(frozen=True)
class StagedCadFile:
    path: str
    size: int
    fmt: str
    digest: str
    parse_failed: bool
    seed_identical: bool = False


def _fmt(path: str) -> str:
    return PurePosixPath(path).suffix.lstrip(".").lower() or "unknown"


def _seed_signatures(initial_zip: zipfile.ZipFile | None) -> dict[str, tuple[int, int]]:
    if initial_zip is None:
        return {}
    return {
        info.filename: (info.CRC, info.file_size)
        for info in initial_zip.infolist()
        if not info.is_dir()
    }


def discover_cad_files(
    final_zip: zipfile.ZipFile,
    *,
    expected_cad_files: list[str],
    cad_base_path: str,
    initial_zip: zipfile.ZipFile | None = None,
) -> list[str]:
    """CAD file paths in prompt-priority order.

    Tiers: author-listed paths/globs, then the authored base path, then the
    known CAD bases, then a sweep of the workspace. The sweep keeps a
    ``filesystem/input/`` file only when the initial snapshot shows it changed
    or new, so an agent that edits a task input in place is still graded while
    untouched inputs stay out. Files byte-identical to the initial snapshot
    (matched by zip central-directory CRC + size, no decompression) sort last
    so agent-authored work gets the prompt budget first.
    """
    infos = [info for info in final_zip.infolist() if not info.is_dir()]
    by_name = {info.filename: info for info in infos}
    cad_names = sorted(info.filename for info in infos if is_cad_file(info.filename))
    has_baseline = initial_zip is not None
    seed = _seed_signatures(initial_zip)

    def seed_identical(name: str) -> bool:
        info = by_name[name]
        return seed.get(name) == (info.CRC, info.file_size)

    ordered: list[str] = []
    seen: set[str] = set()

    def take(candidates: list[str]) -> None:
        for name in candidates:
            if name not in seen:
                seen.add(name)
                ordered.append(name)

    for pattern in expected_cad_files:
        clean = pattern.lstrip("/")
        take(
            [
                name
                for name in cad_names
                if fnmatch.fnmatch(name, clean)
                or fnmatch.fnmatch(name, f"filesystem/{clean}")
            ]
        )
    if cad_base_path.strip("/"):
        base = cad_base_path.strip("/") + "/"
        take([name for name in cad_names if name.startswith(base)])
    for base in KNOWN_CAD_BASES:
        take([name for name in cad_names if name.startswith(base)])
    take(
        [
            name
            for name in cad_names
            if name.startswith(SWEEP_BASE)
            and (
                not name.startswith(SWEEP_EXCLUDE)
                or (has_baseline and not seed_identical(name))
            )
        ]
    )

    if not seed:
        return ordered
    return [n for n in ordered if not seed_identical(n)] + [
        n for n in ordered if seed_identical(n)
    ]


def seed_identical_paths(
    final_zip: zipfile.ZipFile,
    initial_zip: zipfile.ZipFile | None,
    names: list[str],
) -> frozenset[str]:
    """Which of ``names`` are byte-identical to the initial snapshot."""
    seed = _seed_signatures(initial_zip)
    if not seed:
        return frozenset()
    by_name = {info.filename: info for info in final_zip.infolist()}
    return frozenset(
        name
        for name in names
        if seed.get(name) == (by_name[name].CRC, by_name[name].file_size)
    )


def _failed_entry(
    name: str, size: int, reason: str, *, seed_identical: bool = False
) -> StagedCadFile:
    return StagedCadFile(
        path=name,
        size=size,
        fmt=_fmt(name),
        digest=reason,
        parse_failed=True,
        seed_identical=seed_identical,
    )


def _digest_one(
    final_zip: zipfile.ZipFile,
    name: str,
    *,
    seed_identical: bool = False,
    sink: Callable[[str, bytes], None] | None = None,
) -> StagedCadFile:
    info = final_zip.getinfo(name)
    if info.file_size > MAX_CAD_FILE_BYTES:
        return _failed_entry(
            name,
            info.file_size,
            f"{name} is {info.file_size:,} bytes, over the "
            f"{MAX_CAD_FILE_BYTES:,}-byte cap for CAD parsing.",
            seed_identical=seed_identical,
        )
    try:
        data = final_zip.read(name)
    except _MEMBER_READ_ERRORS as e:
        return _failed_entry(
            name,
            info.file_size,
            f"Could not parse {name}: unreadable zip member ({e})",
            seed_identical=seed_identical,
        )
    if sink is not None:
        sink(name, data)
    digest = read_cad_bytes(data, name, "digest")
    return StagedCadFile(
        path=name,
        size=len(data),
        fmt=_fmt(name),
        digest=digest,
        parse_failed=digest.startswith(_PARSE_FAILURE_PREFIX),
        seed_identical=seed_identical,
    )


def stage_cad_files(
    final_zip: zipfile.ZipFile,
    ordered: list[str],
    max_files: int,
    *,
    seed_identical: frozenset[str] = frozenset(),
    sink: Callable[[str, bytes], None] | None = None,
) -> tuple[list[StagedCadFile], list[str]]:
    """Digest up to ``max_files`` parseable paths; the remainder is skipped.

    Only parseable digests spend the cap, so a run of malformed files at the
    front of the order cannot push a valid deliverable off the prompt; the
    failure bound stops the scan so junk cannot force unbounded decompression.
    ``sink`` receives every member's raw bytes as it is read (probe staging).
    """
    staged: list[StagedCadFile] = []
    skipped: list[str] = []
    parseable = failures = 0
    for name in ordered:
        if parseable >= max_files or failures >= _MAX_PARSE_FAILURE_ENTRIES:
            skipped.append(name)
            continue
        entry = _digest_one(
            final_zip, name, seed_identical=name in seed_identical, sink=sink
        )
        if entry.parse_failed:
            failures += 1
        else:
            parseable += 1
        staged.append(entry)
    return staged, skipped


def _bounded_artifact_read(zf: zipfile.ZipFile, name: str) -> bytes | None:
    """Read an ArtifactSelection name (full path or legacy bare relative path),
    refusing members whose central-directory size is over the CAD parse cap
    before any bytes are decompressed."""
    normalized = name.lstrip("/")
    for candidate in (normalized, f"filesystem/{normalized}"):
        try:
            info = zf.getinfo(candidate)
        except KeyError:
            continue
        if info.file_size > MAX_CAD_FILE_BYTES:
            logger.warning(
                f"[CAD_LLM_JUDGE] reference {candidate} is {info.file_size:,} "
                f"bytes, over the {MAX_CAD_FILE_BYTES:,}-byte cap; skipped"
            )
            return None
        try:
            return zf.read(candidate)
        except _MEMBER_READ_ERRORS as e:
            logger.warning(f"[CAD_LLM_JUDGE] unreadable reference {candidate}: {e}")
            return None
    return None


def _digest_reference_bytes(name: str, data: bytes) -> StagedCadFile:
    if is_cad_file(name):
        digest = read_cad_bytes(data, name, "digest")
        parse_failed = digest.startswith(_PARSE_FAILURE_PREFIX)
    else:
        text = data.decode("utf-8", errors="replace")
        if len(text) > _MAX_REFERENCE_TEXT_CHARS:
            text = text[:_MAX_REFERENCE_TEXT_CHARS] + "\n... (reference truncated)"
        digest = text
        parse_failed = False
    return StagedCadFile(
        path=name,
        size=len(data),
        fmt=_fmt(name),
        digest=digest,
        parse_failed=parse_failed,
    )


def _golden_reference_names(golden: zipfile.ZipFile) -> list[str]:
    """CAD paths in a golden snapshot, including task inputs: a golden's
    ``filesystem/input/`` model is ground truth, not seed noise."""
    ordered = discover_cad_files(golden, expected_cad_files=[], cad_base_path="")
    inputs = sorted(
        info.filename
        for info in golden.infolist()
        if not info.is_dir()
        and info.filename.startswith(SWEEP_EXCLUDE)
        and is_cad_file(info.filename)
    )
    return ordered + [name for name in inputs if name not in ordered]


def stage_reference_files(
    reference_names: list[str],
    initial_zip: zipfile.ZipFile | None,
    golden_zips: list[zipfile.ZipFile],
    sink: Callable[[str, bytes], None] | None = None,
) -> list[StagedCadFile]:
    """Ground-truth digests, capped at ``_MAX_REFERENCE_FILES``: named artifacts
    from the initial snapshot (falling back to golden snapshots), then CAD
    files discovered in the goldens. ``sink`` receives each CAD reference's raw
    bytes (probe staging)."""
    staged: list[StagedCadFile] = []
    seen: set[str] = set()
    for name in reference_names:
        if len(staged) >= _MAX_REFERENCE_FILES:
            return staged
        data = _bounded_artifact_read(initial_zip, name) if initial_zip else None
        if data is None:
            for golden in golden_zips:
                data = _bounded_artifact_read(golden, name)
                if data is not None:
                    break
        if data is None:
            logger.warning(f"[CAD_LLM_JUDGE] reference artifact not found: {name}")
            continue
        if name not in seen:
            seen.add(name)
            if sink is not None and is_cad_file(name):
                sink(name, data)
            staged.append(_digest_reference_bytes(name, data))
    for golden in golden_zips:
        for name in _golden_reference_names(golden):
            if len(staged) >= _MAX_REFERENCE_FILES:
                return staged
            if name not in seen:
                seen.add(name)
                staged.append(_digest_one(golden, name, sink=sink))
    return staged

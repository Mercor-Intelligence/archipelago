"""Snapshot discovery + digesting for the CAD LLM judge.

Reads the final snapshot zip directly rather than through the snapshot-diff
helper, which drops CAD extensions from its artifact extraction
(``runner/helpers/snapshot_diff/main.py``), so a diff-based judge never sees
the deliverable this eval exists to grade.
"""

from __future__ import annotations

import base64
import fnmatch
import html
import io
import re
import struct
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath

from PIL import Image

from runner.evals.agentic_verifier.cad import (
    MAX_CAD_FILE_BYTES,
    declared_length_unit,
    is_cad_file,
    read_cad_bytes,
)
from runner.evals.agentic_verifier.kicad import (
    is_kicad_file,
    kicad_format,
    read_kicad_bytes,
)
from runner.utils.grading_log import logger

# Prompt-priority order: FreeCAD MCP project store, its legacy sibling,
# explicit exports; a workspace sweep runs after these.
KNOWN_CAD_BASES: tuple[str, ...] = (
    ".apps_data/freecad_mcp/projects/",
    ".apps_data/freecad/",
    "filesystem/exports/",
)
# Searched ahead of KNOWN_CAD_BASES only with `kicad_digest`. Reports lead so
# multi-sheet projects or Gerber layers cannot crowd them off the file cap.
KICAD_CAD_BASES: tuple[str, ...] = (
    "filesystem/exports/reports/",
    "filesystem/exports/netlist/",
    "filesystem/exports/bom/",
    ".apps_data/kicad_mcp/projects/",
    ".apps_data/pcb_design/projects/",
    ".apps_data/kicad/",
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
    # Distinguishes a file refused for its SIZE from one whose bytes would not
    # parse: the ungradeable cause for an all-failed run depends on which.
    over_cap: bool = False
    # Set only when the verifier reports units; None means not read.
    length_unit: str | None = None


def _fmt(path: str, *, kicad: bool = False) -> str:
    if kicad and (kind := kicad_format(path)):
        return kind
    return PurePosixPath(path).suffix.lstrip(".").lower() or "unknown"


def _is_candidate(name: str, *, kicad: bool) -> bool:
    return is_cad_file(name) or (kicad and is_kicad_file(name))


def _read_digest(
    data: bytes, name: str, *, kicad: bool, report_units: bool = False
) -> str:
    if kicad and not is_cad_file(name) and is_kicad_file(name):
        return read_kicad_bytes(data, name)
    return read_cad_bytes(data, name, "digest", report_units=report_units)


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
    kicad: bool = False,
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
    cad_names = sorted(
        info.filename for info in infos if _is_candidate(info.filename, kicad=kicad)
    )
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
    for base in (*KICAD_CAD_BASES, *KNOWN_CAD_BASES) if kicad else KNOWN_CAD_BASES:
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
    name: str,
    size: int,
    reason: str,
    *,
    seed_identical: bool = False,
    over_cap: bool = False,
    kicad: bool = False,
) -> StagedCadFile:
    return StagedCadFile(
        path=name,
        size=size,
        fmt=_fmt(name, kicad=kicad),
        digest=reason,
        parse_failed=True,
        seed_identical=seed_identical,
        over_cap=over_cap,
    )


def _digest_one(
    final_zip: zipfile.ZipFile,
    name: str,
    *,
    seed_identical: bool = False,
    sink: Callable[[str, bytes], None] | None = None,
    kicad: bool = False,
    report_units: bool = False,
) -> StagedCadFile:
    info = final_zip.getinfo(name)
    if info.file_size > MAX_CAD_FILE_BYTES:
        return _failed_entry(
            name,
            info.file_size,
            f"{name} is {info.file_size:,} bytes, over the "
            f"{MAX_CAD_FILE_BYTES:,}-byte cap for CAD parsing.",
            seed_identical=seed_identical,
            over_cap=True,
            kicad=kicad,
        )
    try:
        data = final_zip.read(name)
    except _MEMBER_READ_ERRORS as e:
        return _failed_entry(
            name,
            info.file_size,
            f"Could not parse {name}: unreadable zip member ({e})",
            seed_identical=seed_identical,
            kicad=kicad,
        )
    if sink is not None and is_cad_file(name):  # KiCad files have no geometry
        sink(name, data)
    digest = _read_digest(data, name, kicad=kicad, report_units=report_units)
    return StagedCadFile(
        path=name,
        size=len(data),
        fmt=_fmt(name, kicad=kicad),
        digest=digest,
        parse_failed=digest.startswith(_PARSE_FAILURE_PREFIX),
        seed_identical=seed_identical,
        length_unit=declared_length_unit(data, name) if report_units else None,
    )


def stage_cad_files(
    final_zip: zipfile.ZipFile,
    ordered: list[str],
    max_files: int,
    *,
    seed_identical: frozenset[str] = frozenset(),
    sink: Callable[[str, bytes], None] | None = None,
    kicad: bool = False,
    report_units: bool = False,
) -> tuple[list[StagedCadFile], list[str]]:
    """Digest up to ``max_files`` parseable paths; the remainder is skipped.

    Only parseable digests spend the cap, so a run of malformed files at the
    front of the order cannot push a valid deliverable off the prompt; the
    failure bound stops the scan so junk cannot force unbounded decompression.
    ``sink`` receives each geometry member's raw bytes as it is read (probe staging).
    """
    staged: list[StagedCadFile] = []
    skipped: list[str] = []
    parseable = failures = 0
    for name in ordered:
        if parseable >= max_files or failures >= _MAX_PARSE_FAILURE_ENTRIES:
            skipped.append(name)
            continue
        entry = _digest_one(
            final_zip,
            name,
            seed_identical=name in seed_identical,
            sink=sink,
            kicad=kicad,
            report_units=report_units,
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


def _digest_reference_bytes(
    name: str, data: bytes, *, kicad: bool = False, report_units: bool = False
) -> StagedCadFile:
    unit: str | None = None
    if _is_candidate(name, kicad=kicad):
        digest = _read_digest(data, name, kicad=kicad, report_units=report_units)
        parse_failed = digest.startswith(_PARSE_FAILURE_PREFIX)
        unit = declared_length_unit(data, name) if report_units else None
    else:
        text = data.decode("utf-8", errors="replace")
        if len(text) > _MAX_REFERENCE_TEXT_CHARS:
            text = text[:_MAX_REFERENCE_TEXT_CHARS] + "\n... (reference truncated)"
        digest = text
        parse_failed = False
    return StagedCadFile(
        path=name,
        size=len(data),
        fmt=_fmt(name, kicad=kicad),
        digest=digest,
        parse_failed=parse_failed,
        length_unit=unit,
    )


def _golden_reference_names(
    golden: zipfile.ZipFile, *, kicad: bool = False
) -> list[str]:
    """CAD paths in a golden snapshot, including task inputs: a golden's
    ``filesystem/input/`` model is ground truth, not seed noise."""
    ordered = discover_cad_files(
        golden, expected_cad_files=[], cad_base_path="", kicad=kicad
    )
    inputs = sorted(
        info.filename
        for info in golden.infolist()
        if not info.is_dir()
        and info.filename.startswith(SWEEP_EXCLUDE)
        and _is_candidate(info.filename, kicad=kicad)
    )
    return ordered + [name for name in inputs if name not in ordered]


def stage_reference_files(
    reference_names: list[str],
    initial_zip: zipfile.ZipFile | None,
    golden_zips: list[zipfile.ZipFile],
    sink: Callable[[str, bytes], None] | None = None,
    kicad: bool = False,
    *,
    report_units: bool = False,
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
            staged.append(
                _digest_reference_bytes(
                    name, data, kicad=kicad, report_units=report_units
                )
            )
    for golden in golden_zips:
        for name in _golden_reference_names(golden, kicad=kicad):
            if len(staged) >= _MAX_REFERENCE_FILES:
                return staged
            if name not in seen:
                seen.add(name)
                staged.append(
                    _digest_one(
                        golden,
                        name,
                        sink=sink,
                        kicad=kicad,
                        report_units=report_units,
                    )
                )
    return staged


REPORT_TEXT_FORMATS = frozenset({"md", "txt", "csv", "json", "log"})
SCREENSHOT_FORMATS = frozenset({"png"})
MAX_REPORT_FILES = _MAX_REFERENCE_FILES
MAX_SCREENSHOTS = 6
# Provider per-image request limit.
MAX_SCREENSHOT_BYTES = 5 * 1024 * 1024
# Provider per-image side limit; also bounds decoded pixel memory.
MAX_SCREENSHOT_SIDE = 8000
MAX_SKIPPED_REPORTS_LISTED = 100
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
REPORT_TAG = "AGENT_REPORT"
_REPORT_TAG_RE = re.compile(rf"<(?=\s*/?\s*{REPORT_TAG})", re.IGNORECASE)
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")


def safe_report_path(path: str) -> str:
    """Agent-chosen path made inert inside a prompt tag, header or label."""
    return _CONTROL_CHARS_RE.sub("?", html.escape(path))


@dataclass(frozen=True)
class StagedScreenshot:
    path: str
    png_bytes: bytes
    seed_identical: bool = False

    def label(self, index: int) -> str:
        seed = " (unchanged from initial snapshot)" if self.seed_identical else ""
        return f"[SCREENSHOT_{index}: {safe_report_path(self.path)}{seed}]"

    def estimated_tokens(self, floor: int) -> int:
        """Pixel-based token estimate (width * height / 750), never below floor."""
        if len(self.png_bytes) < 24:
            return floor
        width, height = struct.unpack(">II", self.png_bytes[16:24])
        return max(floor, -(-width * height // 750))

    def as_image(self, index: int) -> dict[str, str]:
        encoded = base64.b64encode(self.png_bytes).decode("ascii")
        return {
            "url": f"data:image/png;base64,{encoded}",
            "placeholder": self.label(index),
        }


@dataclass(frozen=True)
class StagedReports:
    texts: list[StagedCadFile]
    screenshots: list[StagedScreenshot]
    skipped: list[str]


def _neutralize_report_tags(text: str) -> str:
    """Stop report text from closing or opening its own evidence block."""
    return _REPORT_TAG_RE.sub("&lt;", text)


def _report_text(
    final_zip: zipfile.ZipFile, info: zipfile.ZipInfo, seed_identical: bool
) -> StagedCadFile:
    # Bounded read: the prompt keeps at most the cap, so never inflate more.
    byte_cap = _MAX_REFERENCE_TEXT_CHARS * 4
    with final_zip.open(info) as member:
        raw = member.read(byte_cap + 1)
    text = raw[:byte_cap].decode("utf-8", errors="replace")
    if len(raw) > byte_cap or len(text) > _MAX_REFERENCE_TEXT_CHARS:
        text = text[:_MAX_REFERENCE_TEXT_CHARS] + "\n... (report truncated)"
    return StagedCadFile(
        path=info.filename,
        size=info.file_size,
        fmt=_fmt(info.filename),
        digest=_neutralize_report_tags(text),
        parse_failed=False,
        seed_identical=seed_identical,
    )


def stage_report_files(
    final_zip: zipfile.ZipFile,
    patterns: list[str],
    initial_zip: zipfile.ZipFile | None = None,
) -> StagedReports:
    """Agent-written reports and PNG screenshots matching ``patterns``.

    Only the final snapshot is read; CAD files are left to the artifact
    digests. Files unchanged from the initial snapshot
    sort last and carry ``seed_identical``; every refused match is named in
    ``skipped`` with its reason.
    """
    infos = [info for info in final_zip.infolist() if not info.is_dir()]
    seed = _seed_signatures(initial_zip)
    matched: list[zipfile.ZipInfo] = []
    seen: set[str] = set()
    for pattern in patterns:
        clean = pattern.lstrip("/")
        for info in sorted(infos, key=lambda i: i.filename):
            name = info.filename
            if name in seen or is_cad_file(name):
                continue
            if fnmatch.fnmatch(name, clean) or fnmatch.fnmatch(
                name, f"filesystem/{clean}"
            ):
                seen.add(name)
                matched.append(info)

    def is_seed(info: zipfile.ZipInfo) -> bool:
        return seed.get(info.filename) == (info.CRC, info.file_size)

    matched.sort(key=is_seed)
    texts: list[StagedCadFile] = []
    screenshots: list[StagedScreenshot] = []
    skipped: list[str] = []
    for info in matched:
        name = info.filename
        fmt = _fmt(name)
        if fmt in REPORT_TEXT_FORMATS:
            if len(texts) >= MAX_REPORT_FILES:
                skipped.append(f"{name}: over the {MAX_REPORT_FILES}-report cap")
                continue
            try:
                texts.append(_report_text(final_zip, info, is_seed(info)))
            except _MEMBER_READ_ERRORS as e:
                skipped.append(f"{name}: unreadable zip member ({e})")
        elif fmt in SCREENSHOT_FORMATS:
            if len(screenshots) >= MAX_SCREENSHOTS:
                skipped.append(f"{name}: over the {MAX_SCREENSHOTS}-screenshot cap")
                continue
            if info.file_size > MAX_SCREENSHOT_BYTES:
                skipped.append(
                    f"{name}: {info.file_size:,} bytes, over the "
                    f"{MAX_SCREENSHOT_BYTES:,}-byte image cap"
                )
                continue
            try:
                data = final_zip.read(name)
            except _MEMBER_READ_ERRORS as e:
                skipped.append(f"{name}: unreadable zip member ({e})")
                continue
            problem = _png_problem(data)
            if problem:
                skipped.append(f"{name}: {problem}")
                continue
            screenshots.append(StagedScreenshot(name, data, is_seed(info)))
        else:
            skipped.append(f"{name}: unsupported report type .{fmt}")
    if len(skipped) > MAX_SKIPPED_REPORTS_LISTED:
        extra = len(skipped) - MAX_SKIPPED_REPORTS_LISTED
        skipped = skipped[:MAX_SKIPPED_REPORTS_LISTED]
        skipped.append(f"... and {extra:,} more matched files not shown")
    return StagedReports(texts=texts, screenshots=screenshots, skipped=skipped)


def _png_problem(data: bytes) -> str | None:
    """Why ``data`` cannot be sent as a screenshot, or None when it can."""
    if not data.startswith(_PNG_SIGNATURE):
        return "not a decodable PNG image"
    try:
        with Image.open(io.BytesIO(data)) as image:
            if max(image.size) > MAX_SCREENSHOT_SIDE:
                return (
                    f"{image.width}x{image.height} px, over the "
                    f"{MAX_SCREENSHOT_SIDE}-px image side cap"
                )
            image.load()
    except Exception:  # agent-written bytes: any parser error means unusable
        return "not a decodable PNG image"
    return None

"""Resolve the external binaries a verifier shells out to.

``GRADING_TOOLS_PREFIX`` names the directory a consumer read the staged tools
at. Unset, this returns None and the caller leaves the tool to PATH, which is
what the grading lane wants: apt put poppler there.

Set, it is a claim that the toolchain was mounted, and a tool missing under it
is a misconfiguration. This raises instead of returning None, because PATH in a
world's platform image has no poppler and the failure that follows is quiet:
``pdf_to_base64_images`` catches ``Exception`` and returns no images, so a
grade would be scored against a document the judge could not see. Raising costs
a grade, which the controller then dispatches to the lane. Returning None would
cost a wrong score.

The value is whatever path the mount landed at. Nothing in the staged tree
carries a compiled-in path, so it runs from any of them.

Unset AND mounted beside this source, the tree is found by its position
relative to this file. ``POST /grade`` runs the grade with the environment
stripped, so nothing can pass the variable in, and falling through to PATH
there is the quiet-wrong case above.

One thing the tree cannot place itself: poppler reads its CMaps from a
compile-time ``/usr/share/poppler`` and honours no environment variable, so the
consumer that mounts this also has to present ``tools/poppler/share/poppler``
there. ``verify_staged_tools`` checks it did, because a PDF with a predefined
CJK encoding otherwise renders differently from the lane and nothing says so.
"""

import os
from collections.abc import Iterator
from pathlib import Path

TOOLS_PREFIX_ENV = "GRADING_TOOLS_PREFIX"

# What each tool has to provide for its callers. An existing bin/ is not enough:
# pdf2image runs pdfinfo for the page count and pdftoppm or pdftocairo for the
# raster, and any one of them missing fails inside a handler that scores anyway.
REQUIRED_BINARIES: dict[str, tuple[str, ...]] = {
    "poppler": ("pdfinfo", "pdftoppm", "pdftocairo"),
}

# Paths a tool compiles in and reads no environment variable for, so the
# consumer that mounts the tree has to present them. Checked, not assumed: a
# missing one renders differently from the lane and raises nothing by itself.
REQUIRED_DATA_DIRS: dict[str, str] = {
    "poppler": "/usr/share/poppler",
}


class StagedToolsError(RuntimeError):
    """``GRADING_TOOLS_PREFIX`` names a toolchain that is not there."""


def _mounted_prefix() -> str | None:
    """The staged tree beside this source, when there is one."""
    candidate = _mount_root() / "grading_tools"
    return str(candidate) if candidate.is_dir() else None


# Where a binary can be after the apt tree moves. `usr/lib/libreoffice/program`
# is there because Debian ships /usr/bin/libreoffice as a symlink into it, and
# an absolute symlink dangles once the tree is mounted elsewhere. The real
# script lives in program/ and computes its own directory, so it survives.
_MOUNTED_BIN_DIRS = (
    "usr/bin",
    "usr/local/bin",
    "bin",
    "usr/lib/libreoffice/program",
)


def _mount_root() -> Path:
    """The mounted tree's root: this file is <mount>/runner/utils/tool_paths.py.

    Derived and not passed in, because ``POST /grade`` runs the grade with the
    environment stripped.
    """
    return Path(__file__).resolve().parents[2]


def mounted_binaries(*names: str) -> Iterator[str]:
    """Executables named ``names`` inside the mounted image, in order.

    The image carries its own apt tree, so a binary at /usr/bin reads as
    <mount>/usr/bin. Nothing puts that on PATH, and ``shutil.which`` would find
    the world's copy or nothing: a different version, or an empty result that
    still gets scored.
    """
    root = _mount_root()
    for name in names:
        for bin_dir in _MOUNTED_BIN_DIRS:
            candidate = root / bin_dir / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                yield str(candidate)


def staged_bin_dir(tool: str) -> str | None:
    """The staged ``bin`` directory for ``tool``, or None when there is none."""
    prefix = os.environ.get(TOOLS_PREFIX_ENV) or _mounted_prefix()
    if not prefix:
        return None
    bin_dir = Path(prefix) / "tools" / tool / "bin"
    if not bin_dir.is_dir():
        raise StagedToolsError(
            f"{TOOLS_PREFIX_ENV}={prefix} but {bin_dir} is missing, "
            f"so {tool} was not staged there"
        )
    missing = [
        name
        for name in REQUIRED_BINARIES.get(tool, ())
        if not os.access(bin_dir / name, os.X_OK)
    ]
    if missing:
        raise StagedToolsError(
            f"{bin_dir} is missing {', '.join(missing)}, so {tool} is incomplete"
        )
    return str(bin_dir)


def verify_staged_tools() -> None:
    """Check every staged tool once, before a grade starts. No-op when unset.

    The per-call lookups raise, but four of the five callers of
    ``pdf_to_base64_images`` sit under an ``except Exception`` that logs and
    moves on, so a raise there drops an artifact and still scores. Checking here
    puts the failure outside every one of those handlers.
    """
    for tool in REQUIRED_BINARIES:
        if staged_bin_dir(tool) is None:
            return
        data_dir = REQUIRED_DATA_DIRS.get(tool)
        if data_dir is not None and not Path(data_dir).is_dir():
            raise StagedToolsError(
                f"{tool} is staged but {data_dir} is missing, so the consumer "
                f"mounted the tools and not their data"
            )

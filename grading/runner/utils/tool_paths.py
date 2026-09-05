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
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

from loguru import logger

# Enough for LibreOffice's wrapper, which is the slowest thing here, and short
# enough that a hung binary does not stall a grade.
_RUNNABLE_PROBE_SECONDS = 30
# What most tools answer. Anything that disagrees says so in MOUNTED_TOOLCHAIN.
_DEFAULT_VERSION_FLAG = "--version"

TOOLS_PREFIX_ENV = "GRADING_TOOLS_PREFIX"

# What each tool has to provide for its callers. An existing bin/ is not enough:
# pdf2image runs pdfinfo for the page count and pdftoppm or pdftocairo for the
# raster, and any one of them missing fails inside a handler that scores anyway.
REQUIRED_BINARIES: dict[str, tuple[str, ...]] = {
    "poppler": ("pdfinfo", "pdftoppm", "pdftocairo"),
    "ffmpeg": ("ffmpeg", "ffprobe"),
}

# Paths a tool compiles in and reads no environment variable for, so the
# consumer that mounts the tree has to present them. Checked, not assumed: a
# missing one renders differently from the lane and raises nothing by itself.
REQUIRED_DATA_DIRS: dict[str, str] = {
    "poppler": "/usr/share/poppler",
}


class StagedToolsError(BaseException):
    """The mounted toolchain cannot serve this grade, so it belongs on the lane.

    BaseException and not Exception, for the reason KeyboardInterrupt is. Nine
    handlers between the verifiers and the extractors catch bare Exception and
    turn what they catch into a scored row, and re-raising at each one is a
    list that goes stale: two were missed the first time and three more the
    second. Deriving from BaseException makes `except Exception` miss this by
    construction, everywhere, including code nobody has written yet.
    """


def _mounted_prefix() -> str | None:
    """The staged tree beside this source, when there is one."""
    candidate = _mount_root() / "grading_tools"
    return str(candidate) if candidate.is_dir() else None


# Where a binary can be after the apt tree moves. `usr/lib/libreoffice/program`
# is there because Debian ships /usr/bin/libreoffice as a symlink into it, and
# an absolute symlink dangles once the tree is mounted elsewhere. The real
# script lives in program/ and computes its own directory, so it survives.
# Staged first. stage_tools.sh relocates a tool and wraps it so it runs from
# anywhere; the apt copies below it hold absolute paths and return 127 once the
# tree moves. Order is the difference between a working tool and a fallback.
_MOUNTED_BIN_DIRS = (
    "grading_tools/tools/poppler/bin",
    "grading_tools/tools/ffmpeg/bin",
    # playwright keys its directory on a build number, so stage_tools.sh puts a
    # stable relative link here rather than making the runtime glob for it.
    "grading_tools/tools/chromium/bin",
    # The real binary, ahead of the symlink to it. Debian ships
    # /usr/bin/libreoffice as a relative link into program/, which does
    # resolve under a mount, so this is not a fix for a dangling link. It
    # means resolution stops depending on a symlink the packaging owns.
    "usr/lib/libreoffice/program",
    "usr/bin",
    "usr/local/bin",
    "bin",
)


# Written into the mountable image only (modal_labs.MOUNTED_MARKER). Its
# presence is the difference between the two worlds this code runs in.
_MOUNTED_MARKER = "mounted_grading_image"


def running_from_a_mount() -> bool:
    """Mounted, PATH is the WORLD's toolchain and off limits. On the lane it is
    the reference, so this is what tells the two apart."""
    return (_mount_root() / _MOUNTED_MARKER).is_file()


def _mount_root() -> Path:
    """The mounted tree's root: this file is <mount>/runner/utils/tool_paths.py.

    Derived and not passed in, because ``POST /grade`` runs the grade with the
    environment stripped.
    """
    return Path(__file__).resolve().parents[2]


# Every external binary the grading tree shells out to, and the flag that
# proves it runs. Keyed by the name a caller asks for, so a new tool is one
# entry here and not a new check somewhere else.
# The flag each tool answers, for _runs. Both run from a mount: LibreOffice
# needs stage_tools.sh to have made its install path-independent first, and
# ffmpeg needs the staged copy, since the apt one holds absolute paths.
MOUNTED_TOOLCHAIN: dict[str, tuple[tuple[str, ...], str]] = {
    "libreoffice": (("libreoffice", "soffice"), "--version"),
    "ffmpeg": (("ffmpeg", "ffprobe"), "-version"),
    # The agentic harnesses. The spike runs all three from a mount, and their
    # resolvers now refuse the world's copies, so the gate has to name them or
    # a mount that cannot run one is never reported.
    "claude": (("claude",), "--version"),
    "opencode": (("opencode",), "--version"),
    # bua_verifier drives a live headless browser. Named here so the gate
    # answers for it too: a mount whose closure is missing sends those grades
    # to the lane instead of failing one verifier at a time.
    "chromium": (("chrome",), "--version"),
}

# What a tool needs in its environment to run from a mount, as paths under the
# staged prefix. LibreOffice is the only one, because poppler and ffmpeg are
# staged with their own loader and libraries while LibreOffice stays where apt
# put it, so it has to be told where its own dependencies and fonts are.
#
# LD_LIBRARY_PATH names ONLY its dependency directory. Pointing it at the
# mount's whole usr/lib/x86_64-linux-gnu put our libc on a search path served
# by the world's loader and killed /bin/sh (spike #20977). FONTCONFIG_FILE is
# not a convenience: the world's font set renders a chart differently from the
# lane's, and a different chart is a different score.
MOUNTED_TOOL_ENV: dict[str, dict[str, str]] = {
    "libreoffice": {
        "LD_LIBRARY_PATH": "tools/libreoffice/lib",
        "FONTCONFIG_FILE": "tools/libreoffice/etc/fonts.conf",
    },
    # Same shape, same reasons. `playwright install --with-deps` put chromium's
    # OS libraries in THIS image, and a world that ships no browser carries
    # none of them: spike #20977 read that as 127. Fonts because a page the
    # judge reads is a page chromium rendered.
    "chromium": {
        "LD_LIBRARY_PATH": "tools/chromium/lib",
        "FONTCONFIG_FILE": "tools/chromium/etc/fonts.conf",
    },
}

# One verdict per path per process. `_runs` execs the binary, which for
# LibreOffice is seconds, and a grading run asks repeatedly.
_RUNNABLE: dict[str, bool] = {}


def _tool_for(path: str) -> str | None:
    """The MOUNTED_TOOLCHAIN entry this binary belongs to, by its name."""
    name = Path(path).name
    for tool, (names, _flag) in MOUNTED_TOOLCHAIN.items():
        if name in names:
            return tool
    return None


def _version_flag(path: str) -> str:
    """The flag this tool answers. ffmpeg reads -v as a loglevel and needs
    -version, so one hardcoded flag reports a working tool as broken."""
    tool = _tool_for(path)
    if tool is None:
        return _DEFAULT_VERSION_FLAG
    return MOUNTED_TOOLCHAIN[tool][1]


def mounted_tool_env(path: str) -> dict[str, str]:
    """The environment ``path`` needs to run from a mount.

    Empty on the lane, and empty for a tool that needs nothing. Merge it OVER
    the caller's environment rather than replacing it: ``POST /grade`` already
    runs the grade with the environment stripped, so what is left is what the
    grade itself needs.

    A path that is not in the mount is not an error here. ``_runs`` calls this,
    and ``unusable_mounted_tools`` calls ``_runs`` to build a list, so raising
    would abort the gate instead of reporting the tool the gate exists to
    report. The probe then fails on its own and the tool is named.
    """
    if not running_from_a_mount():
        return {}
    spec = MOUNTED_TOOL_ENV.get(_tool_for(path) or "")
    if not spec:
        return {}
    # The same prefix mounted_binaries uses. Deriving this from the mount
    # while the binary came from GRADING_TOOLS_PREFIX hands chromium the
    # wrong tree's libraries and the probe then calls a working browser dead.
    prefix = os.environ.get(TOOLS_PREFIX_ENV)
    tools = (
        Path(prefix).resolve()
        if prefix
        else (_mount_root() / "grading_tools").resolve()
    )
    env: dict[str, str] = {}
    for var, relative in spec.items():
        target = (tools / relative).resolve()
        # Inside the tree it came from, so a name cannot walk out with "..".
        if not target.is_relative_to(tools) or not target.exists():
            logger.warning(
                f"{Path(path).name} needs {var}={target} from the mount and it "
                f"is not there, so the probe below will report it unusable"
            )
            continue
        env[var] = str(target)
    return env


def _runs(path: str) -> bool:
    """True when ``path`` executes. os.access is not enough: spike #20977 found
    a mounted LibreOffice that passes it and returns 127."""
    cached = _RUNNABLE.get(path)
    if cached is not None:
        return cached
    try:
        completed = subprocess.run(  # noqa: S603
            [path, _version_flag(path)],
            capture_output=True,
            timeout=_RUNNABLE_PROBE_SECONDS,
            check=False,
            env={**os.environ, **mounted_tool_env(path)},
        )
        ok = completed.returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    if not ok:
        logger.warning(
            f"mounted tool {path} resolves but does not run; "
            f"require_mounted_binary will refuse the grade rather than use "
            f"the world's copy"
        )
    _RUNNABLE[path] = ok
    return ok


def mounted_binaries(*names: str) -> Iterator[str]:
    """Runnable executables inside the mounted image. The image carries its own
    apt tree, so /usr/bin reads as <mount>/usr/bin, which nothing puts on PATH.

    The staged entries follow GRADING_TOOLS_PREFIX when it is set, because
    ``staged_bin_dir`` does and the two disagreeing would resolve one tool from
    the env var and another from the mount.
    """
    root = _mount_root()
    prefix = os.environ.get(TOOLS_PREFIX_ENV)
    for name in names:
        for bin_dir in _MOUNTED_BIN_DIRS:
            if prefix and bin_dir.startswith("grading_tools/"):
                candidate = Path(prefix) / bin_dir[len("grading_tools/") :] / name
            else:
                candidate = root / bin_dir / name
            if not (candidate.is_file() and os.access(candidate, os.X_OK)):
                continue
            # Bounded by the MOUNT, not by the prefix. stage_tools.sh makes
            # chrome a relative symlink out of grading_tools into playwright's
            # cache, so a prefix bound rejects a browser that runs and refuses
            # every browser grade. The mount is the tree we trust; anything
            # resolving outside it is the world's.
            if not candidate.resolve().is_relative_to(root.resolve()):
                continue
            if _runs(str(candidate)):
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


def _remove(path: Path) -> None:
    """Whatever is there, gone. rmtree refuses a symlink to a directory, and
    is_dir() answers True for one, so the order matters."""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path)


def _present_data_dir(tool: str, data_dir: str, staged_share: Path) -> None:
    """Point ``data_dir`` at the mount's copy, because nothing else can.

    poppler reads its CMaps from a compile-time path and honours no
    environment variable, so a mounted grade needs that absolute path to hold
    the data the lane read. ``stage_tools.sh`` ships that data inside the
    staged tree, and this is what puts it where poppler looks. Spike #20977
    refused a grade for exactly this: one world shape had poppler-data of its
    own and the other did not.

    The tree does it rather than the consumer. There is no way to mount an
    image at this path and have the data land inside it, and an obligation on
    every future caller is one the spike already forgot once.

    A world copy is moved aside and not deleted, so the change is reversible
    and nothing the world owns is destroyed. Ours wins, for the same reason
    ``require_mounted_binary`` refuses to fall back to PATH: the world's copy
    is a different build and renders a CJK page differently.

    Off a mount this returns immediately. The lane's own image put the data
    where poppler expects it.
    """
    if not running_from_a_mount():
        return
    target = Path(data_dir)
    # From the staged tree, which is where stage_tools.sh puts it, and not by
    # mirroring the absolute path into the mount.
    source = staged_share / target.name
    if not source.is_dir():
        # Not a warning-and-return. The caller only checks that data_dir
        # exists, and on a world that ships its own copy it does, so the grade
        # proceeded reading the WORLD's CMaps and a CJK page scored differently
        # from the lane with nothing reporting it.
        raise StagedToolsError(
            f"{tool} needs {data_dir} and the mount has no {source}, so it "
            f"would read the world's copy; this grade belongs on the lane"
        )
    if target.is_symlink() and target.resolve() == source.resolve():
        return
    # Whatever the world has here moves aside, never away, whether that is a
    # directory, a file or a symlink. A dangling link counts: it answers
    # is_symlink() and not exists(), and leaving it makes symlink_to raise
    # FileExistsError.
    if target.is_symlink() or target.exists():
        displaced = target.with_name(f"{target.name}.world")
        _remove(displaced)
        target.rename(displaced)
        logger.info(
            f"moved the world's {target} to {displaced}; {tool} reads the "
            f"mount's copy so the score matches the lane's"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(source, target_is_directory=True)


def verify_staged_tools() -> None:
    """Check every staged tool once, before a grade starts. No-op when unset.

    Deliberately NOT a toolchain check. A tool that cannot run should fail the
    grades that need it, not every inline grade, so
    ``require_mounted_binary`` handles that per tool at the point one is asked
    for.

    The per-call lookups raise, but four of the five callers of
    ``pdf_to_base64_images`` sit under an ``except Exception`` that logs and
    moves on, so a raise there drops an artifact and still scores. Checking here
    puts the failure outside every one of those handlers.
    """
    for tool in REQUIRED_BINARIES:
        bin_dir = staged_bin_dir(tool)
        if bin_dir is None:
            return
        data_dir = REQUIRED_DATA_DIRS.get(tool)
        if data_dir is None:
            continue
        _present_data_dir(tool, data_dir, Path(bin_dir).parent / "share")
        if not Path(data_dir).is_dir():
            raise StagedToolsError(
                f"{tool} is staged but {data_dir} is missing, so the consumer "
                f"mounted the tools and not their data"
            )


def require_mounted_binary(*names: str) -> str | None:
    """A runnable path, or None off a mount. Raises when mounted and none runs,
    because callers read None as "nothing to report" and score anyway.

    Per tool and on demand, so a tool nobody asks for cannot refuse a grade.
    """
    found = next(mounted_binaries(*names), None)
    if found is not None:
        return found
    if not running_from_a_mount():
        return None
    raise StagedToolsError(
        f"this grade needs {names[0]} and the mounted image cannot run it, so "
        f"scoring here would omit what it produces; it belongs on the lane"
    )


def unusable_mounted_tools() -> list[str]:
    """The tools this mount cannot run, for a caller deciding whether to grade
    inline at all. Empty on the lane. Cached, so one exec per tool per sandbox."""
    if not running_from_a_mount():
        return []
    return [
        tool
        for tool, (names, _flag) in MOUNTED_TOOLCHAIN.items()
        if next(mounted_binaries(*names), None) is None
    ]

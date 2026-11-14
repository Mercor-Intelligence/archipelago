"""Pure ffmpeg/ffprobe analysis helpers for the video_ffmpeg_verifier eval.

This module deliberately has **no** dependency on the grading ``runner``
package (only the standard library + the ``ffmpeg``/``ffprobe`` binaries). That
keeps the measurement logic unit-testable in isolation and lets the eval glue
in ``main.py`` stay thin.

Design goals:
- Ingest arbitrarily large videos. Every path here operates on a file on
  **disk** and shells out to ``ffprobe``/``ffmpeg``; nothing loads the media
  into memory. ``stream_zip_member`` copies a snapshot entry to disk in bounded
  chunks so a multi-gigabyte ``.mp4`` never has to fit in RAM.
- Answer the deterministic, measurable parts of a video rubric (container /
  stream specs and audio loudness) rather than the subjective ones.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass, field
from typing import Any

from runner.utils.tool_paths import require_mounted_binary


# On the lane these are on PATH, where the runtime image put them. Under a
# mounted grading image they are not: /grade strips PATH, and the world's copy
# would be a different build from the lane's anyway.
#
# `require_mounted_binary` and not `mounted_binaries`: a bare name left as the
# fallback resolves through the world's PATH once mounted, so a staged copy
# that fails its probe would be replaced by a different build and the score
# would stop matching the lane's. Mounted, this raises and the grade goes to
# the lane, which is what LibreOffice already does.
#
# Resolved at USE, not at import: the probe execs the binary, and importing
# this module says nothing about whether a video verifier is in the run. Off a
# mount it returns None and the bare name is kept, so the lane is unchanged.
def ffprobe_bin() -> str:
    return require_mounted_binary("ffprobe") or "ffprobe"


def ffmpeg_bin() -> str:
    return require_mounted_binary("ffmpeg") or "ffmpeg"


# Subprocess wall-clock ceiling. ffprobe is near-instant even on huge files
# (it only reads container/stream headers). ffmpeg loudness passes must decode
# the audio track, so they get a longer ceiling — but these are analysis passes
# over a bounded window, not transcodes, so keep it well short of the grading
# function timeout: a verifier with many checks multiplies this, and grading
# containers stack up to 20 concurrent runs.
FFPROBE_TIMEOUT = 300
FFMPEG_TIMEOUT = 900


def require_ffmpeg() -> None:
    """Raise unless ffprobe and ffmpeg run here: ``StagedToolsError`` on a mount, ``FfmpegMissingError`` elsewhere."""
    for binary in (ffprobe_bin(), ffmpeg_bin()):
        try:
            subprocess.run(
                [binary, "-version"], capture_output=True, check=True, timeout=30
            )
        except (OSError, subprocess.SubprocessError) as e:
            raise FfmpegMissingError(f"{binary} cannot run on this grader: {e}") from e


def jpeg_url(jpeg: bytes) -> str:
    return f"data:image/jpeg;base64,{base64.b64encode(jpeg).decode()}"


def audio_part(audio: bytes, fmt: str) -> dict[str, Any]:
    """An ``input_audio`` message part for a model that listens."""
    return {
        "type": "input_audio",
        "input_audio": {"data": base64.b64encode(audio).decode(), "format": fmt},
    }


def _timeout(ceiling: float, deadline: float | None) -> float:
    """``ceiling``, cut to what is left before ``deadline`` on the ``time.monotonic`` clock."""
    if deadline is None:
        return ceiling
    left = deadline - time.monotonic()
    if left <= 0:
        raise FfmpegTimeoutError("ran past its deadline")
    return min(ceiling, left)


_STREAM_CHUNK = 8 * 1024 * 1024  # 8 MiB — bounded memory while extracting.

# Defaults for VLM/audio content extraction. Bounded on purpose so cost and
# memory stay constant regardless of the source video's size/length.
DEFAULT_FRAME_COUNT = 8
DEFAULT_FRAME_MAX_DIM = 512
DEFAULT_AUDIO_MAX_SECONDS = 600  # cap the audio clip sent to the judge
AUDIO_SAMPLE_RATE = 16000
# Keep sampled frame timestamps this far inside the runtime so a seek to exactly
# `duration` doesn't land past the last frame.
_FRAME_EOF_MARGIN = 0.05

# Normalize codec spellings so a rubric author can write "h.265", "H265" or
# "hevc" and mean the same thing ffprobe reports ("hevc").
_CODEC_ALIASES = {
    "h265": "hevc",
    "h.265": "hevc",
    "x265": "hevc",
    "hevc": "hevc",
    "h264": "h264",
    "h.264": "h264",
    "x264": "h264",
    "avc": "h264",
    "avc1": "h264",
    "mpeg4": "mpeg4",
    "vp8": "vp8",
    "vp9": "vp9",
    "av1": "av1",
    "prores": "prores",
    "aac": "aac",
    "mp3": "mp3",
    "mp2": "mp2",
    "opus": "opus",
    "vorbis": "vorbis",
    "flac": "flac",
    "ac3": "ac3",
    "eac3": "eac3",
    "alac": "alac",
}


class FfmpegError(RuntimeError):
    """Raised when ffprobe/ffmpeg exits non-zero or times out.

    Signals a problem with the *media* (undecodable, truncated, unsupported
    codec), which the eval reports as a legitimate 0.0 — not an eval error.
    """


class FfmpegTimeoutError(FfmpegError):
    """Raised when ffprobe/ffmpeg runs out of time: a grader limit, not a defect in the file."""


class FfmpegMissingError(FfmpegError):
    """Raised when the ffprobe/ffmpeg binary isn't on PATH.

    Split out from :class:`FfmpegError` because a missing binary is a
    grading-runtime fault, not a verdict on the deliverable: the eval reports it
    as ``status=ERROR`` so it surfaces as a broken image rather than a bad video.
    """


def normalize_codec(name: str | None) -> str:
    """Canonicalize a codec name for tolerant comparison."""
    if not name:
        return ""
    key = name.strip().lower().replace(" ", "")
    if key.startswith("pcm"):
        return "pcm"
    return _CODEC_ALIASES.get(key, key)


def parse_frame_rate(value: str | None) -> float | None:
    """Parse an ffprobe frame-rate string like ``24/1`` or ``24000/1001``."""
    if not value:
        return None
    value = value.strip()
    try:
        if "/" in value:
            num, den = value.split("/", 1)
            num_f, den_f = float(num), float(den)
            if den_f == 0:
                return None
            return num_f / den_f
        return float(value)
    except (ValueError, ZeroDivisionError):
        return None


@dataclass
class MediaProbe:
    """Container + stream metadata extracted by a single ffprobe call."""

    duration_seconds: float | None = None
    format_name: str | None = None
    has_video: bool = False
    has_audio: bool = False
    width: int | None = None
    height: int | None = None
    frame_rate: float | None = None
    video_codec: str | None = None
    audio_codec: str | None = None
    audio_channels: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def stream_zip_member(
    zip_file: zipfile.ZipFile,
    member: str,
    dest_path: str,
    chunk_size: int = _STREAM_CHUNK,
) -> int:
    """Copy one zip entry to ``dest_path`` in bounded chunks.

    Uses ``ZipFile.open`` (a streaming reader) + ``shutil.copyfileobj`` so peak
    memory stays at ``chunk_size`` regardless of how large the member is. This
    is what lets the verifier handle multi-gigabyte deliverables. Returns the
    number of bytes written.
    """
    with zip_file.open(member, "r") as src, open(dest_path, "wb") as dst:
        shutil.copyfileobj(src, dst, length=chunk_size)
    # copyfileobj doesn't report a size; stat the result instead.
    return os.path.getsize(dest_path)


def run_ffprobe(
    path: str, deadline: float | None = None, formats: str | None = None
) -> dict[str, Any]:
    """Return parsed ``ffprobe -show_format -show_streams`` JSON for ``path``.

    ``formats`` limits the demuxers ffprobe may pick, as ``-format_whitelist``.
    """
    cmd = [
        ffprobe_bin(),
        "-v",
        "error",
        *(["-format_whitelist", formats] if formats else []),
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        path,
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_timeout(FFPROBE_TIMEOUT, deadline),
        )
    except FileNotFoundError as exc:
        raise FfmpegMissingError("ffprobe binary not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise FfmpegTimeoutError(f"ffprobe timed out after {exc.timeout:.0f}s") from exc
    if proc.returncode != 0:
        raise FfmpegError(f"ffprobe failed: {proc.stderr.strip()[:500]}")
    return json.loads(proc.stdout or "{}")


def probe_media(
    path: str, deadline: float | None = None, formats: str | None = None
) -> MediaProbe:
    """Run ffprobe once and fold the result into a :class:`MediaProbe`."""
    data = run_ffprobe(path, deadline, formats)
    probe = MediaProbe(raw=data)

    fmt = data.get("format") or {}
    probe.format_name = fmt.get("format_name")
    duration = fmt.get("duration")
    if duration is not None:
        try:
            probe.duration_seconds = float(duration)
        except (TypeError, ValueError):
            probe.duration_seconds = None

    for stream in data.get("streams") or []:
        codec_type = stream.get("codec_type")
        if codec_type == "video" and not probe.has_video:
            # Skip cover-art / thumbnail "video" streams (they have a tiny or
            # attached-pic disposition) so we read the real picture stream.
            disposition = stream.get("disposition") or {}
            if disposition.get("attached_pic") == 1:
                continue
            probe.has_video = True
            probe.width = _as_int(stream.get("width"))
            probe.height = _as_int(stream.get("height"))
            probe.video_codec = stream.get("codec_name")
            probe.frame_rate = parse_frame_rate(
                stream.get("avg_frame_rate")
            ) or parse_frame_rate(stream.get("r_frame_rate"))
            # Fall back to stream duration when the container omits it.
            if probe.duration_seconds is None and stream.get("duration"):
                probe.duration_seconds = _as_float(stream.get("duration"))
        elif codec_type == "audio" and not probe.has_audio:
            probe.has_audio = True
            probe.audio_codec = stream.get("codec_name")
            probe.audio_channels = _as_int(stream.get("channels"))

    return probe


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _window_input_args(start: float | None, end: float | None) -> list[str]:
    """Build ffmpeg input-seek args for an optional [start, end) window."""
    args: list[str] = []
    if start is not None:
        args += ["-ss", f"{max(start, 0.0):.3f}"]
    if end is not None:
        duration = end - (start or 0.0)
        if duration > 0:
            args += ["-t", f"{duration:.3f}"]
    return args


def _run_ffmpeg_filter(
    path: str,
    start: float | None,
    end: float | None,
    af: str,
    deadline: float | None = None,
    formats: str | None = None,
) -> str:
    """Run an audio-filter analysis pass and return its stderr, where ffmpeg writes the analysis."""
    args = [*_window_input_args(start, end), "-i", path, "-map", "0:a:0?"]
    return _run_ffmpeg([*args, "-af", af, "-f", "null", "-"], deadline, formats).stderr


_EBUR128_I_RE = re.compile(r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS")
_EBUR128_PEAK_RE = re.compile(r"Peak:\s*(-?\d+(?:\.\d+)?|-inf)\s*dBFS")


def measure_loudness(
    path: str,
    start: float | None = None,
    end: float | None = None,
    deadline: float | None = None,
    formats: str | None = None,
) -> dict[str, float | None]:
    """Measure integrated loudness (LUFS) + true peak (dBFS) via ebur128.

    Parses the trailing ``Summary:`` block so the per-frame progress lines are
    ignored. Returns ``{"integrated_lufs": float|None, "true_peak_dbfs":
    float|None}``.
    """
    stderr = _run_ffmpeg_filter(
        path, start, end, "ebur128=peak=true", deadline, formats
    )
    summary = stderr
    marker = stderr.rfind("Summary:")
    if marker != -1:
        summary = stderr[marker:]

    integrated: float | None = None
    true_peak: float | None = None

    i_matches = _EBUR128_I_RE.findall(summary)
    if i_matches:
        integrated = _to_float_or_neg_inf(i_matches[-1])
    peak_matches = _EBUR128_PEAK_RE.findall(summary)
    if peak_matches:
        # ebur128's summary reports one overall true peak, but be robust to
        # builds that emit a Peak per channel: take the max (the program peak).
        true_peak = max(_to_float_or_neg_inf(m) for m in peak_matches)

    return {"integrated_lufs": integrated, "true_peak_dbfs": true_peak}


_VOL_MEAN_RE = re.compile(r"mean_volume:\s*(-?\d+(?:\.\d+)?|-inf)\s*dB")
_VOL_MAX_RE = re.compile(r"max_volume:\s*(-?\d+(?:\.\d+)?|-inf)\s*dB")


def measure_volume(
    path: str, start: float | None = None, end: float | None = None
) -> dict[str, float | None]:
    """Measure mean + max sample volume (dBFS) via ``volumedetect``."""
    stderr = _run_ffmpeg_filter(path, start, end, "volumedetect")
    mean_match = _VOL_MEAN_RE.search(stderr)
    max_match = _VOL_MAX_RE.search(stderr)
    return {
        "mean_volume_db": _to_float_or_neg_inf(mean_match.group(1))
        if mean_match
        else None,
        "max_volume_db": _to_float_or_neg_inf(max_match.group(1))
        if max_match
        else None,
    }


def _to_float_or_neg_inf(token: str) -> float:
    if token == "-inf":
        return float("-inf")
    return float(token)


def _run_ffmpeg(
    args: list[str], deadline: float | None = None, formats: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run an ffmpeg command, raising :class:`FfmpegError` on failure.

    ``formats`` limits the input's demuxers, as ``-format_whitelist``.
    """
    whitelist = ["-format_whitelist", formats] if formats else []
    cmd = [ffmpeg_bin(), "-hide_banner", "-nostats", "-y", *whitelist, *args]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_timeout(FFMPEG_TIMEOUT, deadline),
        )
    except FileNotFoundError as exc:
        raise FfmpegMissingError("ffmpeg binary not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise FfmpegTimeoutError(f"ffmpeg timed out after {exc.timeout:.0f}s") from exc
    if proc.returncode != 0:
        raise FfmpegError(f"ffmpeg failed: {proc.stderr.strip()[-500:]}")
    return proc


def extract_frames(
    path: str,
    *,
    count: int = DEFAULT_FRAME_COUNT,
    max_dim: int = DEFAULT_FRAME_MAX_DIM,
    duration: float | None = None,
    start: float | None = None,
    end: float | None = None,
    deadline: float | None = None,
    formats: str | None = None,
) -> list[bytes]:
    """The JPEGs of ``extract_timed_frames``, without their timestamps."""
    return [
        jpeg
        for _, jpeg in extract_timed_frames(
            path,
            count=count,
            max_dim=max_dim,
            duration=duration,
            start=start,
            end=end,
            deadline=deadline,
            formats=formats,
        )
    ]


def extract_timed_frames(
    path: str,
    *,
    count: int = DEFAULT_FRAME_COUNT,
    max_dim: int = DEFAULT_FRAME_MAX_DIM,
    duration: float | None = None,
    start: float | None = None,
    end: float | None = None,
    deadline: float | None = None,
    formats: str | None = None,
) -> list[tuple[float, bytes]]:
    """Sample ``count`` downscaled JPEG frames spread across the video, each with its second.

    Frames are seeked individually (fast input seek) and scaled so the longest
    side is at most ``max_dim`` while preserving aspect ratio. The output is a
    bounded, small set of JPEGs regardless of the source resolution/length, so
    it is safe to feed to a VLM. Returns ``(seconds, jpeg)`` pairs in temporal
    order; a timestamp that cannot be read is skipped.
    """
    if count <= 0:
        return []
    lo = start if start is not None else 0.0
    hi = end if end is not None else duration
    # Intersect the requested window with the real runtime, so a window that
    # merely overruns the end (end=60.5 on a 60.2s video) still samples the part
    # that exists. Do NOT slide `lo` back into the tail to make a disjoint window
    # samplable: relocating the frames would let the judge answer a criterion
    # about a time range the deliverable doesn't have. No overlap => no frames,
    # which the caller reports as a failed check.
    if duration is not None:
        if lo >= duration:
            return []
        hi = duration if hi is None else min(hi, duration)
    # A caller-supplied window that is empty or inverted samples NOTHING, for the
    # same reason a disjoint one does: the 1-second-spacing fallback below exists
    # for an UNKNOWN duration, and letting a degenerate window reach it walks the
    # frames forward from `start` instead — exactly the relocation the comment
    # above refuses. Callers validate this first; this is the backstop.
    if end is not None and hi is not None and hi <= lo:
        return []
    frames: list[tuple[float, bytes]] = []
    tmp_dir = tempfile.mkdtemp(prefix="video_frames_")
    try:
        for i in range(count):
            if hi is not None and hi > lo:
                ts = lo + (hi - lo) * (i + 0.5) / count
            else:
                # Unknown duration: fall back to 1-second spacing from start.
                ts = lo + i
            if duration is not None:
                ts = min(ts, max(duration - _FRAME_EOF_MARGIN, 0.0))
            out = os.path.join(tmp_dir, f"f{i}.jpg")
            try:
                _run_ffmpeg(
                    [
                        "-ss",
                        f"{ts:.3f}",
                        "-i",
                        path,
                        "-frames:v",
                        "1",
                        "-vf",
                        f"scale=w={max_dim}:h={max_dim}:force_original_aspect_ratio=decrease",
                        "-q:v",
                        "3",
                        "-f",
                        "image2",
                        out,
                    ],
                    deadline,
                    formats,
                )
            except FfmpegMissingError:
                raise
            except FfmpegTimeoutError:
                # Past a caller's deadline, partial frames would read as a short video.
                if deadline is not None:
                    raise
                continue
            except FfmpegError:
                # One unseekable timestamp shouldn't lose the other frames; the
                # caller fails the check when nothing at all could be sampled.
                continue
            if os.path.exists(out) and os.path.getsize(out) > 0:
                with open(out, "rb") as fh:
                    frames.append((ts, fh.read()))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return frames


def extract_audio(
    path: str,
    *,
    start: float | None = None,
    end: float | None = None,
    max_seconds: float | None = DEFAULT_AUDIO_MAX_SECONDS,
    deadline: float | None = None,
    formats: str | None = None,
) -> bytes | None:
    """Extract the audio track as a compact mono MP3 for a judge model.

    Downmixes to mono at ``AUDIO_SAMPLE_RATE`` and caps the clip length so the
    payload stays small. Returns MP3 bytes, or ``None`` when the extraction
    produced nothing.
    """
    args: list[str] = []
    if start is not None:
        args += ["-ss", f"{max(start, 0.0):.3f}"]
    args += ["-i", path]
    window: float | None = None
    if end is not None:
        window = end - (start or 0.0)
        if window <= 0:
            # Empty/inverted window: extract nothing rather than the whole tail.
            # Callers validate first; this is the backstop.
            return None
    # The cap is UNCONDITIONAL. Folding it into `window` with min() meant a
    # non-positive window carried through as the limit, failed the `> 0` test,
    # and dropped `-t` entirely — so the clip ran to EOF with the cap silently
    # lost, posting an unbounded MP3 to a judge model on a long video.
    if max_seconds is None:
        limit = window
    elif window is None:
        limit = max_seconds
    else:
        limit = min(window, max_seconds)
    if limit is not None and limit > 0:
        args += ["-t", f"{limit:.3f}"]
    fd, tmp_path = tempfile.mkstemp(suffix=".mp3", prefix="video_audio_")
    os.close(fd)
    try:
        args += [
            "-vn",
            "-ac",
            "1",
            "-ar",
            str(AUDIO_SAMPLE_RATE),
            "-b:a",
            "64k",
            "-f",
            "mp3",
            tmp_path,
        ]
        _run_ffmpeg(args, deadline, formats)
        if os.path.getsize(tmp_path) == 0:
            return None
        with open(tmp_path, "rb") as fh:
            return fh.read()
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass

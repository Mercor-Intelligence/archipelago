"""EAN-13 and UPC-A barcodes, with their 2- or 5-digit add-on, read off a page raster.

ZXing-C++ decodes the symbols; this module merges its reads of one symbol and
measures the bar width and the white space beside the bars, which ZXing does not
report.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass
from enum import Enum
from typing import Any

import numpy as np
import zxingcpp
from numpy.typing import NDArray

_EAN13_DIGITS = 13
# Guard 3, six digits of 4 runs, centre 5, six digits, guard 3.
_EAN13_RUNS = 59
_EAN13_MODULES = 95
# ZXing reads one symbol as bands stacked edge to edge, starting on the same bar;
# a second copy of the digits, however close, starts somewhere else.
_MAX_BAND_GAP_MODULES = 2
# EAN-13 bars stand about 0.7 times as tall as the symbol is wide, so bands further apart are two symbols.
_MAX_SYMBOL_HEIGHT_RATIO = 0.8
# Rows of one symbol's bars match; paper or printed digits between two stacked symbols do not.
_MIN_ROW_MATCH = 0.6
# Points compared across a row, enough for 95 modules, and rows compared per pass, to bound memory.
_ROW_SAMPLES = 600
_ROWS_PER_PASS = 256
# Paper white. A cream or grey field reads darker than this, so it is not quiet zone.
_WHITE_THRESHOLD = 248
# Geometry is reported only for a symbol this square to the page, read over this
# many modules of height; a thinner read cannot show the angle.
_MAX_TILT_DEGREES = 0.5
_MIN_HEIGHT_MODULES = 15
# Pixels scanned past each end of ZXing's box, which can sit a pixel inside the bars.
_PAD = 3


class AddonCheck(Enum):
    """What ZXing's one checksum flag per read says about the add-on, which it shares with the main number."""

    CONFIRMED = "confirmed"
    FAILED = "failed"
    UNCONFIRMED = "unconfirmed"


@dataclass(frozen=True)
class Barcode:
    """One symbol, with every length in raster pixels of the page as given."""

    digits: str
    check_ok: bool
    addon: str | None
    # x0, y0, x1, y1 around the 95 modules of the main symbol when measured, else every read.
    box: tuple[int, int, int, int]
    # Bar geometry; None when tilted, or read over too few rows to tell.
    module: float | None
    # Paper-white space beside the main symbol's bars; after it sits any add-on.
    quiet_before: float | None
    quiet_after: float | None
    # Other digits read off the same bars when no read passed the check digit.
    other_reads: tuple[str, ...] = ()
    addon_check: AddonCheck = AddonCheck.CONFIRMED


@dataclass(frozen=True)
class _Read:
    digits: str
    addon: str
    # Top-left, top-right, bottom-right, bottom-left, in the symbol's reading direction.
    corners: NDArray[Any]
    # ZXing kept the read but flagged a checksum, so its add-on may be misread.
    flagged: bool = False


def _check_digit_ok(digits: str) -> bool:
    """From the digits, not ZXing's flag, which an add-on's checksum also raises."""
    if len(digits) != _EAN13_DIGITS or not digits.isdigit():
        return False
    total = sum(int(c) * (3 if i % 2 else 1) for i, c in enumerate(digits[:12]))
    return (10 - total % 10) % 10 == int(digits[12])


def _reads(gray: NDArray[Any]) -> list[_Read]:
    reads = []
    for r in zxingcpp.read_barcodes(
        gray,
        formats=zxingcpp.BarcodeFormats(zxingcpp.BarcodeFormat.EAN13),
        ean_add_on_symbol=zxingcpp.EanAddOnSymbol.Read,
        return_errors=True,
    ):
        if r.error is not None and r.error.type != zxingcpp.ErrorType.Checksum:
            continue
        p = r.position
        corners = np.array(
            [
                (pt.x, pt.y)
                for pt in (p.top_left, p.top_right, p.bottom_right, p.bottom_left)
            ],
            dtype=float,
        )
        reads.append(
            _Read(
                r.text[:_EAN13_DIGITS],
                r.text[_EAN13_DIGITS:],
                corners,
                r.error is not None,
            )
        )
    return reads


def _overlap(a: _Read, b: _Read) -> bool:
    lo = np.maximum(a.corners.min(axis=0), b.corners.min(axis=0))
    hi = np.minimum(a.corners.max(axis=0), b.corners.max(axis=0))
    return bool((lo <= hi).all())


def _reach(read: _Read) -> float:
    module = np.linalg.norm(read.corners[1] - read.corners[0]) / _EAN13_MODULES
    return float(module * _MAX_BAND_GAP_MODULES)


def _gap(a: _Read, b: _Read) -> float:
    """How far one band lies from continuing the other, 0 when their boxes overlap."""
    if _overlap(a, b):
        return 0.0
    return float(
        min(
            np.linalg.norm(b.corners[0] - a.corners[3]),
            np.linalg.norm(a.corners[0] - b.corners[3]),
            np.linalg.norm(b.corners[0] - a.corners[0]),
        )
    )


def _clean_addon(read: _Read) -> set[str]:
    return {read.addon} if read.addon and not read.flagged else set()


def _group(reads: list[_Read], gray: NDArray[Any]) -> list[list[_Read]]:
    """Reads of one physical symbol: same digits, in bands that overlap or continue each other."""
    parent = list(range(len(reads)))
    addons = [_clean_addon(r) for r in reads]

    def root(i: int) -> int:
        while parent[i] != i:
            i = parent[i]
        return i

    reach = [_reach(r) for r in reads]
    pairs: list[tuple[float, int, int]] = []
    for i, a in enumerate(reads):
        for j in range(i):
            if reads[j].digits == a.digits:
                gap = _gap(a, reads[j])
                if gap <= max(reach[i], reach[j]):
                    pairs.append((gap, i, j))
    pairs.sort()
    # Nearest bands join first, and never into one symbol holding two clean add-ons.
    for _, i, j in pairs:
        ri, rj = root(i), root(j)
        if ri != rj and len(addons[ri] | addons[rj]) <= 1:
            parent[ri] = rj
            addons[rj] |= addons[ri]
    groups: dict[int, list[_Read]] = {}
    for i, read in enumerate(reads):
        groups.setdefault(root(i), []).append(read)
    symbols = list(groups.values())
    passing = [r for s in symbols if _check_digit_ok(s[0].digits) for r in s]
    # Scan lines that misread a digit fail the check beside lines of the same symbol that pass it.
    failing = [
        s
        for s in symbols
        if not _check_digit_ok(s[0].digits)
        and not any(_same_symbol(r, p, gray) for r in s for p in passing)
    ]
    return [s for s in symbols if _check_digit_ok(s[0].digits)] + _merge_touching(
        failing, gray
    )


def _same_symbol(a: _Read, b: _Read, gray: NDArray[Any]) -> bool:
    """Bands of one symbol, whatever digits each read: within a symbol's height, with its bars running between them."""
    gap = _gap(a, b)
    width = max(float(np.linalg.norm(r.corners[1] - r.corners[0])) for r in (a, b))
    return gap <= width * _MAX_SYMBOL_HEIGHT_RATIO and _bars_continue(gray, a, b)


def _bars_continue(gray: NDArray[Any], a: _Read, b: _Read) -> bool:
    """Whether every row between two bands matches the bars at the upper band's edge."""
    across = a.corners[1] - a.corners[0]
    across = across / max(float(np.linalg.norm(across)), 1e-9)
    down = np.array([-across[1], across[0]])
    spans = []
    for band in (a, b):
        t, s = band.corners @ across, band.corners @ down
        spans.append((t.min(), t.max(), s.min(), s.max()))
    (ta0, ta1, sa0, sa1), (tb0, tb1, sb0, sb1) = sorted(spans, key=lambda x: x[2])
    if sb0 <= sa1:
        return True
    t0, t1 = max(ta0, tb0), min(ta1, tb1)
    if t1 - t0 < 1:
        return False
    across_pts = np.outer(np.linspace(t0, t1, int(min(t1 - t0, _ROW_SAMPLES))), across)

    def rows_at(s: NDArray[Any]) -> NDArray[Any] | None:
        pts = across_pts[None, :, :] + s[:, None, None] * down[None, None, :]
        if not _inside(gray, pts).all():
            return None
        pts = pts.astype(int)
        pixels = gray[pts[..., 1], pts[..., 0]].astype(float)
        return pixels - pixels.mean(axis=1, keepdims=True)

    edge = rows_at(np.array([sa1]))
    if edge is None or not edge.any():
        return False
    edge = edge[0] / np.linalg.norm(edge[0])
    # Every pixel row, since a white strip between two copies of one barcode can be a row or two.
    rows = np.concatenate([np.arange(math.ceil(sa1), math.floor(sb0) + 1), [sb0]])
    for chunk in np.array_split(rows, max(1, len(rows) // _ROWS_PER_PASS)):
        pixels = rows_at(chunk)
        if pixels is None:
            return False
        spread = np.linalg.norm(pixels, axis=1)
        if (spread == 0).any() or ((pixels @ edge) / spread < _MIN_ROW_MATCH).any():
            return False
    return True


def _merge_touching(
    symbols: list[list[_Read]], gray: NDArray[Any]
) -> list[list[_Read]]:
    """Failing groups that lie on one symbol, merged: misreads of blurry bars disagree on digits."""
    parent = list(range(len(symbols)))

    def root(i: int) -> int:
        while parent[i] != i:
            i = parent[i]
        return i

    for i, a in enumerate(symbols):
        for j in range(i):
            if any(_same_symbol(r, q, gray) for r in a for q in symbols[j]):
                parent[root(i)] = root(j)
    merged: dict[int, list[_Read]] = {}
    for i, symbol in enumerate(symbols):
        merged.setdefault(root(i), []).extend(symbol)
    return list(merged.values())


def _degrees(v: NDArray[Any]) -> float:
    return math.degrees(math.atan2(v[1], v[0]))


def _off_axis(angle: float) -> float:
    return abs((angle + 45) % 90 - 45)


def _inside(gray: NDArray[Any], pts: NDArray[Any]) -> NDArray[Any]:
    h, w = gray.shape
    return (
        (pts[..., 0] >= 0) & (pts[..., 0] < w) & (pts[..., 1] >= 0) & (pts[..., 1] < h)
    )


def _line(
    gray: NDArray[Any], origin: NDArray[Any], step: NDArray[Any], n: int
) -> NDArray[Any]:
    """``n`` pixels from ``origin`` along an axis-aligned ``step``, cut at the page edge."""
    pts = origin + np.outer(np.arange(n), step)
    inside = _inside(gray, pts)
    stop = int(np.argmin(inside)) if not inside.all() else n
    pts = pts[:stop].astype(int)
    return gray[pts[:, 1], pts[:, 0]]


def _white_run(pixels: NDArray[Any], cut: float, fringe: int) -> int:
    # A scaled raster leaves an anti-aliased edge: lighter than the bars, not yet white.
    lead = 0
    while lead < min(fringe, len(pixels)) and cut <= pixels[lead] < _WHITE_THRESHOLD:
        lead += 1
    dark = np.flatnonzero(pixels[lead:] < _WHITE_THRESHOLD)
    if lead and len(dark) and dark[0] == 0:
        return 0  # A tinted field, not an edge: no paper white follows it.
    return lead + (int(dark[0]) if len(dark) else len(pixels) - lead)


def _measure(
    gray: NDArray[Any], reads: list[_Read]
) -> tuple[float, float, float, tuple[int, int, int, int]] | None:
    """Module width, white space before and after, and box of the main symbol, square to the page."""
    tall = max(reads, key=lambda r: np.linalg.norm(r.corners[3] - r.corners[0]))
    tl, tr, _, bl = tall.corners
    across, down = tr - tl, bl - tl
    if not np.linalg.norm(across) or not np.linalg.norm(down):
        return None
    if _off_axis(_degrees(across)) > _MAX_TILT_DEGREES or abs(
        np.dot(across, down)
    ) > np.linalg.norm(across) * np.linalg.norm(down) * math.sin(
        math.radians(_MAX_TILT_DEGREES)
    ):
        return None
    step = np.rint(across / np.linalg.norm(across))
    start = np.rint(tl + down / 2)
    length = max(int(np.dot(r.corners[1] - start, step)) for r in reads)
    # A symbol at the page edge leaves less than the usual 3 pixels of lead-in.
    pad = next(
        (p for p in range(_PAD, -1, -1) if _inside(gray, start - p * step)), None
    )
    if pad is None:
        return None
    row = _line(gray, start - pad * step, step, pad + length + _PAD)
    if not len(row):
        return None
    # The row's first half is the main symbol even when it runs on into an add-on of other ink.
    cut = (int(row[: max(len(row) // 2, 1)].min()) + int(row.max())) / 2
    ink = row < cut
    dark = np.flatnonzero(ink)
    if not len(dark):
        return None
    edges = np.flatnonzero(np.diff(ink.view(np.int8))) + 1
    bounds = np.concatenate(([dark[0]], edges[edges > dark[0]]))
    if len(bounds) <= _EAN13_RUNS:
        return None
    module = float(bounds[_EAN13_RUNS] - bounds[0]) / _EAN13_MODULES
    if np.linalg.norm(down) < _MIN_HEIGHT_MODULES * module:
        return None
    first = start + (int(dark[0]) - pad) * step
    last = start + (int(bounds[_EAN13_RUNS]) - 1 - pad) * step
    reach = max(gray.shape)
    # Up to a module of edge; a tinted field wider than that is not paper white.
    fringe = max(2, int(module))
    before = _white_run(_line(gray, first - step, -step, reach), cut, fringe)
    after = _white_run(_line(gray, last + step, step, reach), cut, fringe)
    pts = np.concatenate([r.corners for r in reads])
    axis = 0 if step[0] else 1
    lo, hi = sorted((first[axis], last[axis]))
    side = pts[:, 1 - axis]
    edges = (
        (lo, side.min(), hi, side.max())
        if axis == 0
        else (side.min(), lo, side.max(), hi)
    )
    x0, y0, x1, y1 = (int(v) for v in edges)
    return module, float(before), float(after), (x0, y0, x1, y1)


def find_barcodes(gray: NDArray[Any]) -> list[Barcode]:
    """Every EAN-13 or UPC-A symbol on a grayscale page, in any orientation."""
    found = []
    for reads in _group(_reads(gray), gray):
        pts = np.concatenate([r.corners for r in reads])
        x0, y0 = pts.min(axis=0)
        x1, y1 = pts.max(axis=0)
        clean = Counter(r.addon for r in reads if r.addon and not r.flagged)
        # A flagged read whose main digits pass can only have failed on its add-on.
        failed = Counter(
            r.addon
            for r in reads
            if r.addon and r.flagged and _check_digit_ok(r.digits)
        )
        addons = clean or failed or Counter(r.addon for r in reads if r.addon)
        measured = _measure(gray, reads)
        module, before, after, box = (
            measured
            if measured
            else (None, None, None, (int(x0), int(y0), int(x1), int(y1)))
        )
        votes = Counter(r.digits for r in reads).most_common()
        found.append(
            Barcode(
                digits=votes[0][0],
                check_ok=_check_digit_ok(votes[0][0]),
                addon=addons.most_common(1)[0][0] if addons else None,
                box=box,
                module=module,
                quiet_before=before,
                quiet_after=after,
                other_reads=tuple(digits for digits, _ in votes[1:]),
                addon_check=AddonCheck.CONFIRMED
                if clean or not addons
                else AddonCheck.FAILED
                if failed
                else AddonCheck.UNCONFIRMED,
            )
        )
    return sorted(found, key=lambda b: (b.box[1], b.box[0]))

"""Offscreen rendering for the CAD LLM judge.

Renders tessellated meshes to PNG with a pure-software rasterizer (numpy
projection + painter's algorithm through Pillow). No GL, EGL or display is
touched, so the same code renders identically on the grading images, CI and dev
machines — a hard requirement after pyrender was ruled out (it pins
PyOpenGL==3.1.0 and breaks under numpy 2, which this tree pins).

Renders are evidence with ``rendered`` provenance: the judge may answer
shape-identity criteria from them with "visual" confidence, never kernel facts.
"""

from __future__ import annotations

import base64
import io
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from runner.utils.grading_log import logger

if TYPE_CHECKING:
    from .probes import GeometryStore

PROVENANCE_RENDER = "rendered"

# Total images one run may put in front of the judge, mirroring the agentic
# verifier's image budget; planner section/view requests spend the same pool.
RENDER_IMAGE_BUDGET = 24
_IMAGE_SIZE = 640
_MARGIN = 0.08
# Painter's algorithm sorts and draws every face; past this count views are
# rendered from a face subsample so one dense mesh cannot eat the grade's clock.
_MAX_RENDER_FACES = 120_000

_BASE_COLOR: tuple[int, int, int] = (176, 196, 222)
_REFERENCE_COLOR: tuple[int, int, int] = (222, 196, 176)
# Distinguishable per-part palette for the color-keyed multi-solid view.
_PART_PALETTE = [
    (66, 133, 244),
    (219, 68, 55),
    (244, 180, 0),
    (15, 157, 88),
    (171, 71, 188),
    (0, 172, 193),
    (255, 112, 67),
    (158, 157, 36),
]

# (direction from the model toward the camera, up hint): "top" renders what an
# observer above the model (+Z) sees.
_VIEWS: dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]] = {
    "front": ((0, -1, 0), (0, 0, 1)),
    "back": ((0, 1, 0), (0, 0, 1)),
    "left": ((-1, 0, 0), (0, 0, 1)),
    "right": ((1, 0, 0), (0, 0, 1)),
    "top": ((0, 0, 1), (0, 1, 0)),
    "bottom": ((0, 0, -1), (0, 1, 0)),
    "isometric": ((1, -1, 1), (0, 0, 1)),
}
_DEFAULT_VIEWS = ("front", "back", "left", "right", "top", "bottom", "isometric")
_EXTRA_ISO_VIEWS: dict[
    str, tuple[tuple[float, float, float], tuple[float, float, float]]
] = {
    "isometric_rear": ((-1, 1, 1), (0, 0, 1)),
    "isometric_below": ((1, -1, -1), (0, 0, 1)),
}

RENDER_MODES = ("default", "extended", "off")


@dataclass(frozen=True)
class RenderedView:
    label: str
    png_bytes: bytes

    def as_image(self, index: int) -> dict[str, Any]:
        encoded = base64.b64encode(self.png_bytes).decode("ascii")
        return {
            "type": "cad_render",
            "url": f"data:image/png;base64,{encoded}",
            "placeholder": f"[RENDER_{index}: {self.label}]",
        }


def _normalized(v: tuple[float, float, float]) -> tuple[float, float, float]:
    length = math.sqrt(sum(c * c for c in v)) or 1.0
    return (v[0] / length, v[1] / length, v[2] / length)


def _camera_basis(
    view: tuple[tuple[float, float, float], tuple[float, float, float]],
) -> Any:
    import numpy as np

    direction = np.array(_normalized(view[0]), dtype=float)
    up_hint = np.array(_normalized(view[1]), dtype=float)
    right = np.cross(up_hint, direction)
    if np.linalg.norm(right) < 1e-9:
        right = np.cross((0.0, 1.0, 0.0), direction)
    right = right / np.linalg.norm(right)
    up = np.cross(direction, right)
    return np.stack([right, up, direction])


def _subsampled(vertices: Any, faces: Any) -> tuple[Any, Any]:
    import numpy as np

    if len(faces) <= _MAX_RENDER_FACES:
        return vertices, faces
    keep = np.random.default_rng(0).choice(
        len(faces), size=_MAX_RENDER_FACES, replace=False
    )
    return vertices, faces[np.sort(keep)]


def render_parts(
    parts: list[tuple[Any, tuple[int, int, int]]],
    view_name: str,
    view: tuple[tuple[float, float, float], tuple[float, float, float]] | None = None,
    size: int = _IMAGE_SIZE,
) -> bytes:
    """Rasterize ``(mesh, color)`` parts from a named view into PNG bytes."""
    import numpy as np
    from PIL import Image, ImageDraw

    basis = _camera_basis(view or _VIEWS.get(view_name) or _EXTRA_ISO_VIEWS[view_name])
    projected: list[tuple[float, Any, tuple[int, int, int]]] = []
    all_xy: list[Any] = []
    for mesh, color in parts:
        vertices, faces = _subsampled(
            np.asarray(mesh.vertices, dtype=float), np.asarray(mesh.faces)
        )
        cam = vertices @ basis.T  # columns: right, up, depth
        tri = cam[faces]  # (n, 3, 3)
        depth = tri[:, :, 2].mean(axis=1)
        # Flat shading off the view-space normal; abs() keeps open meshes and
        # inverted windings visible instead of black.
        edge1 = tri[:, 1, :] - tri[:, 0, :]
        edge2 = tri[:, 2, :] - tri[:, 0, :]
        normals = np.cross(edge1, edge2)
        lengths = np.linalg.norm(normals, axis=1)
        lengths[lengths == 0] = 1.0
        normals = normals / lengths[:, None]
        light = np.array(_normalized((0.35, 0.45, 0.82)))
        brightness = 0.3 + 0.7 * np.abs(normals @ light)
        for i in range(len(faces)):
            shaded = (
                int(min(255, color[0] * brightness[i])),
                int(min(255, color[1] * brightness[i])),
                int(min(255, color[2] * brightness[i])),
            )
            projected.append((float(depth[i]), tri[i, :, :2], shaded))
        all_xy.append(tri[:, :, :2].reshape(-1, 2))

    image = Image.new("RGB", (size, size), (255, 255, 255))
    if not projected:
        return _png(image)
    xy = np.concatenate(all_xy)
    lo, hi = xy.min(axis=0), xy.max(axis=0)
    span = float(max(hi[0] - lo[0], hi[1] - lo[1], 1e-9))
    scale = size * (1 - 2 * _MARGIN) / span
    center = (lo + hi) / 2.0

    def to_px(points: Any) -> list[tuple[float, float]]:
        px = (points - center) * scale
        return [(size / 2 + p[0], size / 2 - p[1]) for p in px]

    draw = ImageDraw.Draw(image)
    # The camera sits on the +direction side (a "top" view shows the +Z side),
    # so LARGER depth is CLOSER to the camera: painter's occlusion draws
    # ascending, nearest faces last. Descending drew the far side over the near
    # side (a top view showed the plate's underside).
    projected.sort(key=lambda item: item[0])
    for _depth, triangle, shaded in projected:
        draw.polygon(to_px(triangle), fill=shaded)
    return _png(image)


def _png(image: Any) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def render_section(
    mesh: Any, plane: str, offset_fraction: float, size: int = _IMAGE_SIZE
) -> bytes | None:
    """A 2D cross-section: the mesh sliced by an axis-aligned plane."""
    import numpy as np
    import trimesh
    from PIL import Image, ImageDraw

    normals = {"xy": (0.0, 0.0, 1.0), "yz": (1.0, 0.0, 0.0), "zx": (0.0, 1.0, 0.0)}
    normal = np.array(normals[plane])
    axis = int(np.argmax(normal))
    lo, hi = mesh.bounds[0][axis], mesh.bounds[1][axis]
    origin = np.array(mesh.bounds.mean(axis=0))
    origin[axis] = lo + (hi - lo) * min(max(offset_fraction, 0.0), 1.0)
    segments = trimesh.intersections.mesh_plane(
        mesh, plane_normal=normal, plane_origin=origin
    )
    if len(segments) == 0:
        return None
    keep = [i for i in range(3) if i != axis]
    flat: Any = np.asarray(segments)[:, :, keep]  # (n, 2, 2)
    points = flat.reshape(-1, 2)
    p_lo, p_hi = points.min(axis=0), points.max(axis=0)
    span = float(max(p_hi[0] - p_lo[0], p_hi[1] - p_lo[1], 1e-9))
    scale = size * (1 - 2 * _MARGIN) / span
    center = (p_lo + p_hi) / 2.0
    image = Image.new("RGB", (size, size), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    for segment in flat:
        a = (
            size / 2 + (segment[0][0] - center[0]) * scale,
            size / 2 - (segment[0][1] - center[1]) * scale,
        )
        b = (
            size / 2 + (segment[1][0] - center[0]) * scale,
            size / 2 - (segment[1][1] - center[1]) * scale,
        )
        draw.line([a, b], fill=(30, 30, 30), width=2)
    return _png(image)


def side_by_side(left: bytes, right: bytes) -> bytes:
    from PIL import Image

    a = Image.open(io.BytesIO(left)).convert("RGB")
    b = Image.open(io.BytesIO(right)).convert("RGB")
    height = max(a.height, b.height)
    combined = Image.new("RGB", (a.width + b.width, height), (255, 255, 255))
    combined.paste(a, (0, 0))
    combined.paste(b, (a.width, 0))
    return _png(combined)


class RenderBudget:
    def __init__(self, budget: int | None = None) -> None:
        self.remaining = RENDER_IMAGE_BUDGET if budget is None else budget

    def take(self) -> bool:
        if self.remaining <= 0:
            return False
        self.remaining -= 1
        return True


def _mesh_parts(
    store: GeometryStore, path: str
) -> list[tuple[Any, tuple[int, int, int]]]:
    """Per-solid meshes with palette colors; falls back to one combined mesh."""
    from .probes import tessellate

    entry = store.loaded(path)
    parts: list[tuple[Any, tuple[int, int, int]]] = []
    if entry.solids:
        for index, (_name, shape) in enumerate(entry.solids):
            mesh = tessellate(shape)
            if mesh is not None:
                parts.append((mesh, _PART_PALETTE[index % len(_PART_PALETTE)]))
    if not parts:
        mesh = store.mesh_for(path)
        if mesh is not None:
            parts = [(mesh, _BASE_COLOR)]
    return parts


def render_default_set(
    store: GeometryStore,
    mode: str,
    budget: RenderBudget,
    paths: list[str] | None = None,
) -> list[RenderedView]:
    """The unplanned render set: per agent file, 6 orthographic + 1 isometric
    view (color-keyed per part when several solids exist), plus extra isometric
    angles and a golden side-by-side under ``extended``. ``paths`` restricts the
    sweep to a subset of agent files (the geometry worker renders one file per
    child)."""
    if mode == "off":
        return []
    views: list[RenderedView] = []
    reference_iso: bytes | None = None
    for reference_path in sorted(store.reference_paths):
        mesh = store.mesh_for(reference_path)
        if mesh is not None:
            reference_iso = _safe_render(
                [(mesh, _REFERENCE_COLOR)], "isometric", reference_path
            )
            break

    for path in paths if paths is not None else store.agent_paths():
        parts = _mesh_parts(store, path)
        if not parts:
            continue
        single: list[tuple[Any, tuple[int, int, int]]] = [
            (m, _BASE_COLOR) for m, _c in parts
        ]
        for view_name in _DEFAULT_VIEWS:
            if not budget.take():
                return views
            # The isometric view carries the per-part color key when the file
            # holds several solids; orthographic views stay single-color.
            use = parts if view_name == "isometric" and len(parts) > 1 else single
            png = _safe_render(use, view_name, path)
            if png is None:
                continue
            suffix = " (parts color-keyed)" if use is parts and len(parts) > 1 else ""
            views.append(
                RenderedView(label=f"{path} — {view_name}{suffix}", png_bytes=png)
            )
        if mode == "extended":
            for view_name in _EXTRA_ISO_VIEWS:
                if not budget.take():
                    return views
                png = _safe_render(single, view_name, path)
                if png is not None:
                    views.append(
                        RenderedView(label=f"{path} — {view_name}", png_bytes=png)
                    )
            if reference_iso is not None:
                mesh = store.mesh_for(path)
                if mesh is not None and budget.take():
                    own = _safe_render([(mesh, _BASE_COLOR)], "isometric", path)
                    if own is not None:
                        views.append(
                            RenderedView(
                                label=(
                                    f"{path} (left) vs ground-truth reference "
                                    "(right) — isometric"
                                ),
                                png_bytes=side_by_side(own, reference_iso),
                            )
                        )
    return views


def _safe_render(
    parts: list[tuple[Any, tuple[int, int, int]]], view_name: str, path: str
) -> bytes | None:
    try:
        return render_parts(parts, view_name)
    except Exception as e:
        logger.warning(f"[CAD_LLM_JUDGE] render {view_name} failed for {path}: {e}")
        return None

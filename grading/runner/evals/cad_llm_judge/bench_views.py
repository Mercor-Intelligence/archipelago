"""BenchCAD-style 4-view composites for a BenchCAD v2 bundle.

Renders every ``parts/<part_id>.step`` and the assembly placed from
``assembly/instances.json`` as a 2x2, 524 px PNG, using the judge's software
renderer. Run: ``python -m runner.evals.cad_llm_judge.bench_views <bundle> <out>``.
"""

from __future__ import annotations

import argparse
import io
import json
from pathlib import Path
from typing import Any

from .probes import _load_brep_file, tessellate
from .render import render_parts

# BenchCAD's cameras sit at the corners of a regular tetrahedron, laid out
# top-left -> bottom-right (envs/common/bench_views.py CAMERA_POSITIONS).
TETRA_VIEWS: tuple[
    tuple[tuple[float, float, float], tuple[float, float, float]], ...
] = (
    ((-1, -1, 1), (0, 0, 1)),
    ((1, 1, 1), (0, 0, 1)),
    ((-1, 1, -1), (0, 0, 1)),
    ((1, -1, -1), (0, 0, 1)),
)
TILE = 256
BORDER = 4
COMPOSITE = TILE * 2 + BORDER * 3
TEAL: tuple[int, int, int] = (110, 195, 192)
_EDGE_DIVISIONS = 60
_MAX_SUBDIVIDE_ITER = 12


def step_mesh(path: Path) -> Any:
    """One trimesh of every solid in a STEP file, in the file's own frame."""
    import trimesh

    meshes = [m for _n, s in _load_brep_file(path, ".step") if (m := tessellate(s))]
    if not meshes:
        raise ValueError(f"no tessellatable geometry in {path}")
    return trimesh.util.concatenate(meshes)


def _rigid(transform: Any, instance_id: str) -> Any:
    import numpy as np

    matrix = np.asarray(transform, dtype=float)
    if matrix.shape != (4, 4):
        raise ValueError(f"{instance_id}: transform is not 4x4")
    rotation = matrix[:3, :3]
    if not np.allclose(rotation @ rotation.T, np.eye(3), atol=1e-6) or not np.isclose(
        np.linalg.det(rotation), 1.0, atol=1e-6
    ):
        raise ValueError(f"{instance_id}: transform is not a proper rotation")
    if not np.allclose(matrix[3], (0.0, 0.0, 0.0, 1.0), atol=1e-9):
        raise ValueError(f"{instance_id}: transform bottom row is not [0, 0, 0, 1]")
    return matrix


def _placed(instances_path: Path, meshes: dict[str, Any]) -> list[Any]:
    raw = json.loads(instances_path.read_text())
    rows = raw.get("instances") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        raise ValueError(f"{instances_path}: expected a list or {{'instances': [...]}}")
    placed: list[Any] = []
    counters: dict[str, int] = {}
    for row in rows:
        part_id = str(row["part_id"])
        counters[part_id] = counters.get(part_id, 0) + 1
        instance_id = str(row.get("instance_id") or f"{part_id}_i{counters[part_id]}")
        if part_id not in meshes:
            raise ValueError(f"{instance_id}: no parts/{part_id}.step")
        mesh = meshes[part_id].copy()
        mesh.apply_transform(_rigid(row.get("transform", row.get("T")), instance_id))
        placed.append(mesh)
    if not placed:
        raise ValueError(f"{instances_path} lists no instances")
    return placed


def _fine(meshes: list[Any]) -> list[Any]:
    """Split long triangles so painter's depth sort holds across parts."""
    import numpy as np
    import trimesh

    lo = np.min([m.bounds[0] for m in meshes], axis=0)
    hi = np.max([m.bounds[1] for m in meshes], axis=0)
    max_edge = float(np.max(hi - lo)) / _EDGE_DIVISIONS or 1.0
    fine = []
    for mesh in meshes:
        split = trimesh.remesh.subdivide_to_size(
            mesh.vertices, mesh.faces, max_edge=max_edge, max_iter=_MAX_SUBDIVIDE_ITER
        )
        fine.append(trimesh.Trimesh(vertices=split[0], faces=split[1], process=False))
    return fine


def composite(meshes: list[Any], color: tuple[int, int, int] = TEAL) -> bytes:
    """Four tetrahedral views of ``meshes`` pasted into one 2x2 PNG."""
    import numpy as np
    from PIL import Image

    out = Image.new("RGB", (COMPOSITE, COMPOSITE), (255, 255, 255))
    fine = _fine(meshes)
    lo = np.min([m.bounds[0] for m in fine], axis=0)
    hi = np.max([m.bounds[1] for m in fine], axis=0)
    # The bbox diagonal bounds every projection, so all four tiles share a scale.
    frame = ((lo + hi) / 2.0, float(np.linalg.norm(hi - lo)))
    parts = [(mesh, color) for mesh in fine]
    for index, view in enumerate(TETRA_VIEWS):
        png = render_parts(parts, "", view=view, size=TILE, max_faces=None, frame=frame)
        tile = Image.open(io.BytesIO(png))
        row, col = divmod(index, 2)
        out.paste(
            tile, (BORDER + col * (TILE + BORDER), BORDER + row * (TILE + BORDER))
        )
    buffer = io.BytesIO()
    out.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def render_bundle(bundle: Path, out_dir: Path) -> list[Path]:
    """Write ``parts/<part_id>.png`` per part type and ``assembly.png``."""
    meshes = {p.stem: step_mesh(p) for p in sorted((bundle / "parts").glob("*.step"))}
    if not meshes:
        raise ValueError(f"no parts/*.step in {bundle}")
    instances_path = bundle / "assembly" / "instances.json"
    placed = _placed(instances_path, meshes) if instances_path.exists() else None

    written: list[Path] = []
    for part_id, mesh in meshes.items():
        target = out_dir / "parts" / f"{part_id}.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(composite([mesh]))
        written.append(target)
    if placed is not None:
        target = out_dir / "assembly.png"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(composite(placed))
        written.append(target)
    return written


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bundle", type=Path, help="bundle root holding parts/")
    parser.add_argument("out_dir", type=Path)
    args = parser.parse_args(argv)
    for path in render_bundle(args.bundle, args.out_dir):
        print(path)


if __name__ == "__main__":
    main()

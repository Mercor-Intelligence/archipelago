"""Opt-in CAD judge probes: hole pattern, edge census, draft, wall thickness v2,
parting line. Offered to the planner only when the verifier's
``extended_probes`` field is on, so existing grades never see them.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from typing import Any

from .probes import PROVENANCE_MESH, PROVENANCE_OCCT, ProbeResult, _ocp, _round, failed

MAX_HOLES_LISTED = 40
MAX_HISTOGRAM_ROWS = 20
MAX_LOOPS_LISTED = 12
MAX_EXPECTED_CENTERS = 200
DEFAULT_HOLE_TOLERANCE = 0.05
DEFAULT_MIN_DRAFT_DEG = 1.0
_AXIS_TOLERANCE = 1e-4
_FULL_TURN = 2.0 * math.pi - 1e-3
# Holes must span about 15 degrees of their circle (R/extent <= 4) to be a bolt circle.
_MAX_PCD_RADIUS_RATIO = 4.0
_THICKNESS_SAMPLES = 512
_BALL_ITERATIONS = 12
# Faces within this angle of perpendicular to the pull count as zero-draft walls.
_SIDE_FACE_DEG = 0.1
_DRAFT_UV_SAMPLES = 5
_DRAFT_BINS_DEG = (-90.0, -10.0, -3.0, -1.0, -0.1, 0.1, 1.0, 3.0, 10.0, 90.0)

PULL_DIRECTIONS: dict[str, tuple[float, float, float]] = {
    "+x": (1.0, 0.0, 0.0),
    "-x": (-1.0, 0.0, 0.0),
    "+y": (0.0, 1.0, 0.0),
    "-y": (0.0, -1.0, 0.0),
    "+z": (0.0, 0.0, 1.0),
    "-z": (0.0, 0.0, -1.0),
}
# In-plane coordinate axes per sketch plane, matching the probe's ``plane`` names.
_PLANE_AXES = {"xy": (0, 1), "yz": (1, 2), "zx": (2, 0)}
_PLANE_NORMAL_AXIS = {"xy": 2, "yz": 0, "zx": 1}


def _result(probe_id: str, target: str, payload: dict[str, Any], provenance: str,
            params: dict[str, Any] | None = None) -> ProbeResult:  # fmt: skip
    return ProbeResult(
        probe_id=probe_id,
        target=target,
        params=params or {},
        outcome=json.dumps(payload, ensure_ascii=False),
        provenance=provenance,
        ok=True,
    )


def _histogram(values: list[float], key: str) -> tuple[list[dict[str, Any]], int]:
    """(top rows by count, distinct value count) for values rounded to 1e-4."""
    counts = Counter(_round(v, 4) for v in values)
    rows = [
        {key: value, "count": count}
        for value, count in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]
    return rows[:MAX_HISTOGRAM_ROWS], len(rows)


def _unique_subshapes(shape: Any, kind: Any) -> list[Any]:
    TopTools = _ocp("TopTools")
    TopExp = _ocp("TopExp").TopExp
    shapes = TopTools.TopTools_IndexedMapOfShape()
    TopExp.MapShapes_s(shape, kind, shapes)
    return [shapes.FindKey(i) for i in range(1, shapes.Extent() + 1)]


def _vec(p: Any) -> tuple[float, float, float]:
    return (p.X(), p.Y(), p.Z())


def _dot(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def _sub(a: tuple[float, ...], b: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(x - y for x, y in zip(a, b, strict=True))


def _canonical(d: tuple[float, float, float]) -> tuple[float, float, float]:
    """One sign per axis line, so opposed directions group together."""
    for c in d:
        if abs(c) > 1e-9:
            return d if c > 0 else (-d[0], -d[1], -d[2])
    return d


def _cylinder_face(face: Any) -> dict[str, Any] | None:
    """Axis, radius, angular span and concavity of one cylindrical face."""
    BRepAdaptor = _ocp("BRepAdaptor").BRepAdaptor_Surface
    GeomAbs = _ocp("GeomAbs")
    TopAbs = _ocp("TopAbs")
    BRepLProp = _ocp("BRepLProp")
    surface = BRepAdaptor(face)
    if surface.GetType() != GeomAbs.GeomAbs_Cylinder:
        return None
    cylinder = surface.Cylinder()
    axis = cylinder.Axis()
    direction = _vec(axis.Direction())
    location = _vec(axis.Location())
    u_mid = (surface.FirstUParameter() + surface.LastUParameter()) / 2.0
    v_mid = (surface.FirstVParameter() + surface.LastVParameter()) / 2.0
    props = BRepLProp.BRepLProp_SLProps(surface, u_mid, v_mid, 1, 1e-7)
    if not props.IsNormalDefined():
        return None
    normal = _vec(props.Normal())
    if face.Orientation() == TopAbs.TopAbs_REVERSED:
        normal = (-normal[0], -normal[1], -normal[2])
    point = _vec(props.Value())
    rel = _sub(point, location)
    along = _dot(rel, direction)
    radial = tuple(r - along * d for r, d in zip(rel, direction, strict=True))
    ends = [
        tuple(o + v * d for o, d in zip(location, direction, strict=True))
        for v in (surface.FirstVParameter(), surface.LastVParameter())
    ]
    return {
        "radius": cylinder.Radius(),
        "direction": _canonical(direction),
        "location": location,
        "ends": ends,
        "u_span": abs(surface.LastUParameter() - surface.FirstUParameter()),
        "concave": _dot(normal, radial) < 0,
    }


def _same_axis(
    a: dict[str, Any], b: dict[str, Any], *, same_radius: bool = True
) -> bool:
    if same_radius and abs(a["radius"] - b["radius"]) > _AXIS_TOLERANCE:
        return False
    if abs(_dot(a["direction"], b["direction"])) < 1.0 - 1e-6:
        return False
    rel = _sub(b["location"], a["location"])
    along = _dot(rel, a["direction"])
    off = math.sqrt(max(0.0, _dot(rel, rel) - along * along))
    return off <= _AXIS_TOLERANCE


def _axial_runs(group: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Split one axis's faces into runs whose axial spans overlap or touch."""
    d, origin = group[0]["direction"], group[0]["location"]

    def span(info: dict[str, Any]) -> tuple[float, float]:
        ts = [_dot(_sub(end, origin), d) for end in info["ends"]]
        return min(ts), max(ts)

    runs: list[list[dict[str, Any]]] = []
    run_end = -math.inf
    for info in sorted(group, key=span):
        lo, hi = span(info)
        if runs and lo <= run_end + _AXIS_TOLERANCE:
            runs[-1].append(info)
            run_end = max(run_end, hi)
        else:
            runs.append([info])
            run_end = hi
    return runs


def _stepped_runs(line: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Chain one axis's bores that meet end to end into stepped holes."""
    d, origin = line[0]["direction"], line[0]["location"]

    def span(bore: dict[str, Any]) -> tuple[float, float]:
        ts = [_dot(_sub(end, origin), d) for end in bore["ends"]]
        return min(ts), max(ts)

    runs: list[tuple[list[dict[str, Any]], float]] = []
    for bore in sorted(line, key=span):
        lo, hi = span(bore)
        # Overlapping walls of different radius have material between them.
        for i, (run, end) in enumerate(runs):
            if abs(lo - end) <= _AXIS_TOLERANCE:
                run.append(bore)
                runs[i] = (run, hi)
                break
        else:
            runs.append(([bore], hi))
    return [run for run, _end in runs]


def _group_by_axis(
    infos: list[dict[str, Any]], *, same_radius: bool
) -> list[list[dict[str, Any]]]:
    groups: list[list[dict[str, Any]]] = []
    for info in infos:
        for group in groups:
            if _same_axis(group[0], info, same_radius=same_radius):
                group.append(info)
                break
        else:
            groups.append([info])
    return groups


def _bore(group: list[dict[str, Any]]) -> dict[str, Any]:
    """One full-turn cylinder run as a single-radius bore with its axial ends."""
    base = group[0]
    d, origin = base["direction"], base["location"]
    ts = [_dot(_sub(end, origin), d) for g in group for end in g["ends"]]
    lo, hi = min(ts), max(ts)
    return {
        "radius": base["radius"],
        "direction": d,
        "location": origin,
        "ends": [
            tuple(o + t * c for o, c in zip(origin, d, strict=True)) for t in (lo, hi)
        ],
    }


def _cylinder_runs(infos: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Same-radius coaxial cylinder faces grouped into axially touching runs."""
    return [
        run
        for axis in _group_by_axis(infos, same_radius=True)
        for run in _axial_runs(axis)
    ]


def _is_full_turn(run: list[dict[str, Any]]) -> bool:
    """True when faces overlapping at one axial height cover a full circumference."""
    d, origin = run[0]["direction"], run[0]["location"]
    spans = [sorted(_dot(_sub(end, origin), d) for end in g["ends"]) for g in run]
    cuts = sorted({t for span in spans for t in span})
    mids = [(a + b) / 2.0 for a, b in zip(cuts, cuts[1:], strict=False)] or cuts
    return any(
        sum(
            g["u_span"] for g, (lo, hi) in zip(run, spans, strict=True) if lo <= m <= hi
        )
        >= _FULL_TURN
        for m in mids
    )


def _holes(solids: list[tuple[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Full-turn concave cylinders, with touching coaxial bores (counterbores,
    stepped holes) merged into one hole: (holes, partial concave cylinder count)."""
    TopAbs = _ocp("TopAbs")
    TopoDS = _ocp("TopoDS").TopoDS
    holes: list[dict[str, Any]] = []
    partial = 0
    for part, shape in solids:
        infos = []
        for raw in _unique_subshapes(shape, TopAbs.TopAbs_FACE):
            info = _cylinder_face(TopoDS.Face_s(raw))
            if info is not None and info["concave"]:
                infos.append(info)
        bores = []
        for run in _cylinder_runs(infos):
            if _is_full_turn(run):
                bores.append(_bore(run))
            else:
                partial += 1
        for hole in (
            run
            for line in _group_by_axis(bores, same_radius=False)
            for run in _stepped_runs(line)
        ):
            d, origin = hole[0]["direction"], hole[0]["location"]
            spans = [
                sorted(_dot(_sub(end, origin), d) for end in bore["ends"])
                for bore in hole
            ]
            t_min = min(lo for lo, _hi in spans)
            t_max = max(hi for _lo, hi in spans)
            mid = (t_min + t_max) / 2.0
            center = tuple(o + mid * c for o, c in zip(origin, d, strict=True))
            entry: dict[str, Any] = {
                "part": part,
                "diameter": _round(2.0 * min(bore["radius"] for bore in hole)),
                "axis_direction": [_round(c) for c in d],
                "center": [_round(c) for c in center],
                "depth": _round(t_max - t_min),
            }
            if len(hole) > 1:
                entry["steps"] = [
                    {"diameter": _round(2.0 * bore["radius"]), "depth": _round(hi - lo)}
                    for bore, (lo, hi) in sorted(
                        zip(hole, spans, strict=True), key=lambda pair: pair[1][0]
                    )
                ]
            holes.append(entry)
    holes.sort(key=lambda h: (h["diameter"], h["center"]))
    return holes, partial


def _in_plane(center: list[float], plane: str) -> tuple[float, float]:
    a, b = _PLANE_AXES[plane]
    return center[a], center[b]


def _pattern(
    centers: list[list[float]], axis: tuple[float, ...], tolerance: float
) -> dict[str, Any]:
    """Classify 2+ parallel-axis hole centres as a circular or linear pattern,
    measured in the plane normal to the axis and reported in model coordinates."""
    import numpy as np

    u, v = _perpendicular_basis(axis)
    basis = np.asarray([u, v], dtype=float)
    pts3 = np.asarray(centers, dtype=float)
    pts = (pts3[:, None, :] * basis[None, :, :]).sum(axis=2)
    centroid = pts.mean(axis=0)
    _u, s, vt = np.linalg.svd(pts - centroid)
    collinear = len(pts) == 2 or (
        len(s) > 1 and float(s[1]) / math.sqrt(len(pts)) <= tolerance
    )
    if not collinear:
        # Algebraic circle fit, so uneven or partial bolt circles keep their centre.
        design = np.column_stack([2 * pts, np.ones(len(pts))])
        (cx, cy, _c), *_ = np.linalg.lstsq(design, (pts**2).sum(axis=1), rcond=None)
        fit_center = np.asarray([cx, cy])
        radii = np.linalg.norm(pts - fit_center, axis=1)
        extent = float(np.linalg.norm(pts[:, None] - pts[None], axis=2).max())
        if (
            radii.mean() > tolerance
            and float(np.ptp(radii)) <= 2 * tolerance
            and radii.mean() <= _MAX_PCD_RADIUS_RATIO * extent
        ):
            angles = np.sort(np.arctan2(*(pts - fit_center).T[::-1]))
            steps = np.diff(np.append(angles, angles[0] + 2 * math.pi))
            axis_v = np.asarray(axis, dtype=float)
            center = (
                fit_center @ basis + float((pts3 * axis_v).sum(axis=1).mean()) * axis_v
            )
            return {
                "kind": "circular",
                "pitch_circle_diameter": _round(2 * float(radii.mean())),
                "pitch_circle_center": [_round(float(c)) for c in center],
                "angular_pitch_deg": [
                    _round(math.degrees(float(step)), 3) for step in steps
                ],
                "evenly_spaced": bool(
                    float(np.ptp(steps)) * float(radii.mean()) <= 2 * tolerance
                ),
            }
    if collinear:
        along = np.sort(((pts - centroid) * vt[0]).sum(axis=1))
        spacing = np.diff(along)
        direction = _canonical(tuple(float(c) for c in vt[0] @ basis))  # pyright: ignore[reportArgumentType]
        return {
            "kind": "linear",
            "direction": [_round(c) for c in direction],
            "spacings": [_round(float(d)) for d in spacing],
            "evenly_spaced": bool(float(np.ptp(spacing)) <= 2 * tolerance),
        }
    return {"kind": "irregular"}


def _match(
    expected: list[tuple[float, float]],
    actual: list[tuple[float, float]],
    tolerance: float,
) -> tuple[list[dict[str, Any]], list[list[float]], list[list[float]]]:
    """Most pairs within tolerance, then least total error: (matched, missing
    expected, extra actual)."""
    import numpy as np
    from scipy.optimize import linear_sum_assignment

    used_e: set[int] = set()
    used_a: set[int] = set()
    matched: list[dict[str, Any]] = []
    pairs: list[tuple[int, int, float]] = []
    if expected and actual:
        dists = np.asarray([[math.dist(e, a) for a in actual] for e in expected])
        within = dists <= tolerance
        # Any extra in-tolerance pair outweighs every in-tolerance error combined.
        cost = np.where(within, dists, float(dists[within].sum()) + 1.0)
        rows, cols = linear_sum_assignment(cost)
        pairs = sorted(
            (int(i), int(j), float(dists[i, j]))
            for i, j in zip(rows, cols, strict=True)
            if within[i, j]
        )
    for i, j, dist in pairs:
        used_e.add(i)
        used_a.add(j)
        matched.append(
            {
                "expected": [_round(v) for v in expected[i]],
                "actual": [_round(v) for v in actual[j]],
                "error": _round(dist),
            }
        )
    missing = [
        [_round(v) for v in e] for i, e in enumerate(expected) if i not in used_e
    ]
    extra = [[_round(v) for v in a] for j, a in enumerate(actual) if j not in used_a]
    return matched, missing, extra


def hole_pattern_probe(
    path: str,
    solids: list[tuple[str, Any]],
    *,
    plane: str | None = None,
    expected_centers: list[list[float]] | None = None,
    diameter: float | None = None,
    tolerance: float = DEFAULT_HOLE_TOLERANCE,
    params: dict[str, Any] | None = None,
) -> ProbeResult:
    """Holes as full-turn concave cylinders, grouped into patterns; with
    expected centres, a matched/missing/extra table in the named plane."""
    holes, partial = _holes(solids)
    group_diameter = [h["diameter"] for h in holes]
    if diameter is not None:
        matches = [
            min(
                (s["diameter"] for s in h.get("steps", [h])),
                key=lambda d: abs(d - diameter),
            )
            for h in holes
        ]
        keep = [i for i, d in enumerate(matches) if abs(d - diameter) <= tolerance]
        holes = [holes[i] for i in keep]
        group_diameter = [matches[i] for i in keep]
    groups: dict[tuple[str, float, tuple[float, ...]], list[dict[str, Any]]] = {}
    for hole, dia in zip(holes, group_diameter, strict=True):
        key = (
            hole["part"],
            _round(dia, 3),
            tuple(_round(c, 3) for c in hole["axis_direction"]),
        )
        groups.setdefault(key, []).append(hole)
    patterns: list[dict[str, Any]] = []
    for (part, dia, axis), members in sorted(groups.items()):
        entry: dict[str, Any] = {
            "part": part,
            "diameter": dia,
            "axis_direction": list(axis),
            "count": len(members),
        }
        if len(members) >= 2:
            entry["pattern"] = _pattern([h["center"] for h in members], axis, tolerance)
        patterns.append(entry)
    payload: dict[str, Any] = {
        "hole_count": len(holes),
        "holes": holes[:MAX_HOLES_LISTED],
        "patterns": patterns,
        "partial_concave_cylinders": partial,
        "tolerance": tolerance,
        "note": (
            "a hole is a full 360-degree concave cylinder; touching coaxial bores "
            "(counterbores, stepped holes) are one hole whose diameter is the "
            "narrowest step; partial concave cylinders (inside fillets, slots) "
            "are counted, not listed"
        ),
    }
    if len(holes) > MAX_HOLES_LISTED:
        payload["holes_truncated"] = f"first {MAX_HOLES_LISTED} of {len(holes)} listed"
    if plane is not None:
        normal_axis = _PLANE_NORMAL_AXIS[plane]
        in_plane = [
            h
            for h in holes
            if abs(h["axis_direction"][normal_axis]) >= math.cos(math.radians(1.0))
        ]
        payload["plane"] = plane
        payload["holes_normal_to_plane"] = len(in_plane)
        if expected_centers is not None:
            expected = [(float(c[0]), float(c[1])) for c in expected_centers]
            actual = [_in_plane(h["center"], plane) for h in in_plane]
            matched, missing, extra = _match(expected, actual, tolerance)
            payload["expected_count"] = len(expected)
            payload["matched"] = matched
            payload["missing"] = missing
            payload["extra"] = extra
            payload["all_expected_matched"] = not missing
    return _result("hole_pattern", path, payload, PROVENANCE_OCCT, params)


def _perpendicular_basis(
    axis: tuple[float, ...],
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    ax = tuple(float(c) for c in axis)
    helper = (1.0, 0.0, 0.0) if abs(ax[0]) < 0.9 else (0.0, 1.0, 0.0)
    u = (
        ax[1] * helper[2] - ax[2] * helper[1],
        ax[2] * helper[0] - ax[0] * helper[2],
        ax[0] * helper[1] - ax[1] * helper[0],
    )
    norm = math.sqrt(_dot(u, u))
    u = (u[0] / norm, u[1] / norm, u[2] / norm)
    v = (
        ax[1] * u[2] - ax[2] * u[1],
        ax[2] * u[0] - ax[0] * u[2],
        ax[0] * u[1] - ax[1] * u[0],
    )
    return u, v


_CURVE_NAMES = {
    "GeomAbs_Line": "line",
    "GeomAbs_Circle": "circle",
    "GeomAbs_Ellipse": "ellipse",
    "GeomAbs_Hyperbola": "hyperbola",
    "GeomAbs_Parabola": "parabola",
    "GeomAbs_BezierCurve": "bezier",
    "GeomAbs_BSplineCurve": "bspline",
    "GeomAbs_OffsetCurve": "offset",
}
_SURFACE_NAMES = {
    "GeomAbs_Plane": "plane",
    "GeomAbs_Cylinder": "cylinder",
    "GeomAbs_Cone": "cone",
    "GeomAbs_Sphere": "sphere",
    "GeomAbs_Torus": "torus",
    "GeomAbs_BezierSurface": "bezier",
    "GeomAbs_BSplineSurface": "bspline",
    "GeomAbs_SurfaceOfRevolution": "revolution",
    "GeomAbs_SurfaceOfExtrusion": "extrusion",
    "GeomAbs_OffsetSurface": "offset",
}


def _type_name(value: Any, names: dict[str, str]) -> str:
    return names.get(getattr(value, "name", str(value)).split(".")[-1], "other")


def edge_census_probe(
    path: str, solids: list[tuple[str, Any]], params: dict[str, Any] | None = None
) -> ProbeResult:
    """Unique edges by curve type with circle radii, faces by surface type with
    partial-cylinder (fillet) radii split convex/concave and torus radii."""
    TopAbs = _ocp("TopAbs")
    TopoDS = _ocp("TopoDS").TopoDS
    BRep_Tool = _ocp("BRep").BRep_Tool
    BRepAdaptor = _ocp("BRepAdaptor")
    edge_types: Counter[str] = Counter()
    face_types: Counter[str] = Counter()
    full_circles: list[float] = []
    arcs: list[float] = []
    convex_fillets: list[float] = []
    concave_fillets: list[float] = []
    torus_radii: list[float] = []
    for _part, shape in solids:
        for raw in _unique_subshapes(shape, TopAbs.TopAbs_EDGE):
            edge = TopoDS.Edge_s(raw)
            if BRep_Tool.Degenerated_s(edge):
                continue
            curve = BRepAdaptor.BRepAdaptor_Curve(edge)
            kind = _type_name(curve.GetType(), _CURVE_NAMES)
            edge_types[kind] += 1
            if kind == "circle":
                span = abs(curve.LastParameter() - curve.FirstParameter())
                (full_circles if span >= _FULL_TURN else arcs).append(
                    curve.Circle().Radius()
                )
        cylinders: list[dict[str, Any]] = []
        for raw in _unique_subshapes(shape, TopAbs.TopAbs_FACE):
            face = TopoDS.Face_s(raw)
            surface = BRepAdaptor.BRepAdaptor_Surface(face)
            kind = _type_name(surface.GetType(), _SURFACE_NAMES)
            face_types[kind] += 1
            if kind == "torus":
                torus_radii.append(surface.Torus().MinorRadius())
            elif kind == "cylinder":
                info = _cylinder_face(face)
                if info is not None:
                    cylinders.append(info)
        # Split walls (two half-turn faces) join into one bore, not fillets.
        for concave, fillets in ((True, concave_fillets), (False, convex_fillets)):
            side = [c for c in cylinders if c["concave"] is concave]
            for run in _cylinder_runs(side) if side else []:
                if not _is_full_turn(run):
                    fillets.extend(info["radius"] for info in run)
    payload: dict[str, Any] = {
        "part_count": len(solids),
        "edge_count": sum(edge_types.values()),
        "edges_by_curve_type": dict(sorted(edge_types.items())),
        "face_count": sum(face_types.values()),
        "faces_by_surface_type": dict(sorted(face_types.items())),
        "note": (
            "partial cylinders are fillet candidates (convex: rounded outer edge, "
            "concave: inside corner); chamfers are planar faces and are not "
            "separated from other planes"
        ),
    }
    for key, values in (
        ("full_circle_edge_radii", full_circles),
        ("arc_edge_radii", arcs),
        ("convex_partial_cylinder_radii", convex_fillets),
        ("concave_partial_cylinder_radii", concave_fillets),
        ("torus_minor_radii", torus_radii),
    ):
        rows, distinct = _histogram(values, "radius")
        payload[key] = rows
        if distinct > len(rows):
            payload[f"{key}_truncated"] = (
                f"top {len(rows)} of {distinct} distinct radii"
            )
    return _result("edge_census", path, payload, PROVENANCE_OCCT, params)


def _face_dots(mesh: Any, pull: tuple[float, float, float]) -> Any:
    import numpy as np

    # Elementwise, not matmul: see symmetry_probe on spurious Accelerate warnings.
    return np.clip((mesh.face_normals * np.asarray(pull)).sum(axis=1), -1.0, 1.0)


def _brep_drafts(
    solids: list[tuple[str, Any]], pull: tuple[float, float, float]
) -> tuple[list[float], list[float], int]:
    """(draft degrees, area weight) per UV sample inside every face, from the
    surface's analytic normal, plus the face count."""
    TopAbs = _ocp("TopAbs")
    TopoDS = _ocp("TopoDS").TopoDS
    BRepAdaptor = _ocp("BRepAdaptor").BRepAdaptor_Surface
    BRepLProp = _ocp("BRepLProp")
    BRepTools = _ocp("BRepTools").BRepTools
    FClass2d = _ocp("BRepTopAdaptor").BRepTopAdaptor_FClass2d
    gp_Pnt2d = _ocp("gp").gp_Pnt2d
    GProp = _ocp("GProp")
    BRepGProp = _ocp("BRepGProp").BRepGProp
    drafts: list[float] = []
    weights: list[float] = []
    faces = 0
    steps = [(i + 0.5) / _DRAFT_UV_SAMPLES for i in range(_DRAFT_UV_SAMPLES)]
    for _part, shape in solids:
        for raw in _unique_subshapes(shape, TopAbs.TopAbs_FACE):
            face = TopoDS.Face_s(raw)
            props = GProp.GProp_GProps()
            BRepGProp.SurfaceProperties_s(face, props)
            area = props.Mass()
            surface = BRepAdaptor(face)
            u0, u1, v0, v1 = BRepTools.UVBounds_s(face)
            reversed_face = face.Orientation() == TopAbs.TopAbs_REVERSED
            classifier = FClass2d(face, 1e-7)
            inside: list[float] = []
            outside: list[float] = []
            for su in steps:
                for sv in steps:
                    u, v = u0 + su * (u1 - u0), v0 + sv * (v1 - v0)
                    lp = BRepLProp.BRepLProp_SLProps(surface, u, v, 1, 1e-7)
                    if not lp.IsNormalDefined():
                        continue
                    n = _vec(lp.Normal())
                    dot = max(-1.0, min(1.0, _dot(n, pull)))
                    out = classifier.Perform(gp_Pnt2d(u, v)) == TopAbs.TopAbs_OUT
                    (outside if out else inside).append(
                        math.degrees(math.asin(-dot if reversed_face else dot))
                    )
            # Trimmed faces: only samples inside the trim; slivers keep every sample.
            sampled = inside or outside
            if sampled:
                faces += 1
                drafts.extend(sampled)
                weights.extend([area / len(sampled)] * len(sampled))
    return drafts, weights, faces


def draft_angle_probe(
    path: str,
    mesh: Any,
    pull_direction: str,
    min_draft_deg: float = DEFAULT_MIN_DRAFT_DEG,
    params: dict[str, Any] | None = None,
    *,
    solids: list[tuple[str, Any]] | None = None,
) -> ProbeResult:
    """Draft relative to a pull direction, area-binned: from B-rep face normals
    when solids are given, else from mesh facet normals."""
    import numpy as np

    pull = PULL_DIRECTIONS[pull_direction]
    if solids:
        raw_drafts, raw_weights, face_count = _brep_drafts(solids, pull)
        draft = np.asarray(raw_drafts, dtype=float)
        areas = np.asarray(raw_weights, dtype=float)
        source, provenance = f"B-rep face normals ({face_count} faces)", PROVENANCE_OCCT
    else:
        draft = np.degrees(np.arcsin(_face_dots(mesh, pull)))
        areas = np.asarray(mesh.area_faces, dtype=float)
        source = (
            "mesh facet normals (coarse facets read curved walls a little low; "
            "smallest wall draft is the area-weighted 5th percentile)"
        )
        provenance = PROVENANCE_MESH
    total = float(areas.sum())
    if total <= 0:
        return failed("draft_angle", path, "the geometry has no face area", params)

    def _fraction(mask: Any) -> float:
        return _round(float(areas[mask].sum()) / total)

    bins = [
        {
            "from_deg": lo,
            "to_deg": hi,
            "area_fraction": _fraction(
                (draft >= lo) & ((draft < hi) | (hi == _DRAFT_BINS_DEG[-1]))
            ),
        }
        for lo, hi in zip(_DRAFT_BINS_DEG, _DRAFT_BINS_DEG[1:], strict=False)
    ]
    walls = (np.abs(draft) < 45.0) & (np.abs(draft) >= _SIDE_FACE_DEG)
    smallest = None
    if walls.any():
        wall_drafts = np.abs(draft[walls])
        if solids:
            smallest = float(wall_drafts.min())
        else:
            # Facet minimum is a tessellation artifact; take the area-weighted p5.
            order = np.argsort(wall_drafts)
            cum = np.cumsum(areas[walls][order])
            smallest = float(wall_drafts[order][np.searchsorted(cum, 0.05 * cum[-1])])
    payload = {
        "pull_direction": pull_direction,
        "measured_from": source,
        "min_draft_deg": min_draft_deg,
        "positive_draft_area_fraction": _fraction(draft >= min_draft_deg),
        "insufficient_draft_area_fraction": _fraction(np.abs(draft) < min_draft_deg),
        "negative_draft_area_fraction": _fraction(draft <= -min_draft_deg),
        "zero_draft_area_fraction": _fraction(np.abs(draft) < _SIDE_FACE_DEG),
        "smallest_nonzero_wall_draft_deg": (
            _round(smallest, 3) if smallest is not None else None
        ),
        "area_by_draft_deg": bins,
        "note": (
            "draft is the surface's angle from parallel to the pull (positive "
            "faces toward it); insufficient means |draft| is below min_draft_deg, "
            "negative faces release only toward the opposite direction"
        ),
    }
    return _result("draft_angle", path, payload, provenance, params)


def _stats(values: Any) -> dict[str, float]:
    import numpy as np

    return {
        "min": _round(float(values.min())),
        "p5": _round(float(np.percentile(values, 5))),
        "median": _round(float(np.median(values))),
        "max": _round(float(values.max())),
    }


def _shrinking_ball(
    mesh: Any, points: Any, inward: Any, radius: Any, contact: Any
) -> tuple[Any, Any]:
    """Largest ball tangent at each point inside the mesh (shrinking-ball
    method), seeded at the inward ray's hit: (radius, outward normal of the
    face that last bounded the ball)."""
    import numpy as np
    import trimesh

    r = radius.copy()
    normals = np.asarray(mesh.face_normals, dtype=float)
    contact = contact.copy()
    for _ in range(_BALL_ITERATIONS):
        centers = points + inward * r[:, None]
        closest, dist, tri = trimesh.proximity.closest_point(mesh, centers)
        offset = np.asarray(closest) - points
        denom = 2.0 * (offset * inward).sum(axis=1)
        shrink = (
            (np.asarray(dist) < r * (1.0 - 1e-3))
            & (np.linalg.norm(offset, axis=1) > 1e-6)
            & (denom > 1e-12)
        )
        if not shrink.any():
            break
        candidate = (offset * offset).sum(axis=1) / np.where(shrink, denom, 1.0)
        r = np.where(shrink, np.minimum(r, candidate), r)
        contact = np.where(shrink[:, None], normals[np.asarray(tri)], contact)
    return r, contact


def wall_thickness_v2_probe(
    path: str,
    mesh: Any,
    min_thickness: float | None = None,
    params: dict[str, Any] | None = None,
) -> ProbeResult:
    """Wall thickness per sampled point: the inscribed-ball diameter where the
    ball rests on an opposite wall, else the inward ray length."""
    import numpy as np
    import trimesh

    sampled: Any = trimesh.sample.sample_surface(mesh, _THICKNESS_SAMPLES, seed=0)
    points, face_index = np.asarray(sampled[0]), sampled[1]
    normals = np.asarray(mesh.face_normals[face_index], dtype=float)
    origins = points - normals * 1e-4
    # multiple_hits=False does not promise the nearest hit; keep it ourselves.
    hits: Any = mesh.ray.intersects_location(origins, -normals, multiple_hits=True)
    locations, all_ids, all_tri = (np.asarray(h) for h in hits)
    if len(locations) == 0:
        return failed(
            "wall_thickness_v2", path, "no inward ray hit the opposite surface", params
        )
    lengths = np.linalg.norm(locations - origins[all_ids], axis=1)
    order = np.lexsort((lengths, all_ids))
    first = order[np.r_[True, np.diff(all_ids[order]) != 0]]
    ray_ids, hit_tri, ray = all_ids[first], all_tri[first], lengths[first]
    p, n = points[ray_ids], normals[ray_ids]
    radius, contact_normals = _shrinking_ball(
        mesh, p, -n, ray / 2.0, np.asarray(mesh.face_normals[hit_tri], dtype=float)
    )
    # A ball touching a face at a right angle sits in a convex edge, not a wall.
    opposite = (contact_normals * n).sum(axis=1) < -0.5
    thickness = np.where(opposite, np.minimum(2.0 * radius, ray), ray)
    payload: dict[str, Any] = {
        "samples": int(len(thickness)),
        "watertight": bool(mesh.is_watertight),
        "min_thickness": _round(float(thickness.min())),
        "thickness": _stats(thickness),
        "inward_ray": _stats(ray),
        "ball_samples": int(opposite.sum()),
        "note": (
            "thickness is the diameter of the largest ball tangent at the point "
            "that rests on an opposite wall (ball_samples of them), else the "
            "inward ray length; inward_ray is the single-ray wall_thickness method"
        ),
    }
    if min_thickness is not None:
        below = thickness < min_thickness
        payload["required_min_thickness"] = min_thickness
        payload["below_min_fraction"] = _round(float(below.mean()))
        payload["meets_min_thickness"] = not bool(below.any())
    return _result("wall_thickness_v2", path, payload, PROVENANCE_MESH, params)


def _components(edges: Any) -> list[list[int]]:
    """Edge indices grouped by connected vertex chains (union-find)."""
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        parent[find(int(a))] = find(int(b))
    groups: dict[int, list[int]] = {}
    for i, (a, _b) in enumerate(edges):
        groups.setdefault(find(int(a)), []).append(i)
    return list(groups.values())


def parting_line_probe(
    path: str, mesh: Any, pull_direction: str, params: dict[str, Any] | None = None
) -> ProbeResult:
    """Candidate parting-line loops: mesh edges where faces turn from facing
    the pull to facing away, or border a zero-draft side band."""
    import numpy as np

    pull = PULL_DIRECTIONS[pull_direction]
    dots = _face_dots(mesh, pull)
    side = math.sin(math.radians(_SIDE_FACE_DEG))
    cls = np.where(dots > side, 1, np.where(dots < -side, -1, 0))
    adjacency = np.asarray(mesh.face_adjacency)
    if len(adjacency) == 0:
        return failed("parting_line", path, "the mesh has no shared edges", params)
    pair_cls = cls[adjacency]
    changes = pair_cls[:, 0] != pair_cls[:, 1]
    edges = np.asarray(mesh.face_adjacency_edges)[changes]
    names = {(-1, 1): "toward|away", (0, 1): "toward|side", (-1, 0): "side|away"}
    kinds = [names[(int(min(p)), int(max(p)))] for p in pair_cls[changes]]
    vertices = np.asarray(mesh.vertices, dtype=float)
    heights = (vertices * np.asarray(pull)).sum(axis=1)
    diag = float(np.linalg.norm(mesh.bounds[1] - mesh.bounds[0]))
    loops: list[dict[str, Any]] = []
    for group in _components(edges):
        chain = edges[group]
        verts = np.unique(chain)
        degree = np.bincount(chain.ravel())[verts]
        h = heights[verts]
        loops.append(
            {
                "boundary": sorted({kinds[i] for i in group}),
                "edge_count": len(group),
                "length": _round(
                    float(
                        np.linalg.norm(
                            vertices[chain[:, 0]] - vertices[chain[:, 1]], axis=1
                        ).sum()
                    )
                ),  # fmt: skip
                "closed": bool((degree == 2).all()),
                "height_min": _round(float(h.min())),
                "height_max": _round(float(h.max())),
                "planar": bool(float(np.ptp(h)) <= 1e-3 * max(diag, 1e-9)),
            }
        )
    loops.sort(key=lambda loop: -loop["length"])
    side_area = float(np.asarray(mesh.area_faces)[cls == 0].sum())
    payload: dict[str, Any] = {
        "pull_direction": pull_direction,
        "loop_count": len(loops),
        "loops": loops[:MAX_LOOPS_LISTED],
        "has_direct_toward_away_split": any(
            "toward|away" in loop["boundary"] for loop in loops
        ),
        "zero_draft_side_area_fraction": _round(
            side_area / float(mesh.area) if mesh.area else 0.0
        ),
        "note": (
            "mesh silhouette estimate, medium reliability: a toward|away loop is "
            "a parting line; toward|side and side|away loops are the two rims of "
            "a zero-draft band, either of which can carry the parting line; "
            "height is measured along the pull direction"
        ),
    }
    if len(loops) > MAX_LOOPS_LISTED:
        payload["loops_truncated"] = (
            f"longest {MAX_LOOPS_LISTED} of {len(loops)} listed"
        )
    return _result("parting_line", path, payload, PROVENANCE_MESH, params)

"""Measured-geometry probes for the CAD LLM judge.

Everything here MEASURES: B-rep facts come from the OCCT kernel bindings
(``cadquery-ocp-novtk``) and mesh facts from ``trimesh``, so the judge cites
"measured (OCCT kernel)" / "measured (mesh)" instead of inferring geometry from
a parsed digest. Every probe returns a :class:`ProbeResult` whose failure is
text (``failed: <why>``), never an exception into the eval layer, and results
are cached per file within the run.

OCP is imported lazily: prebuilt wheels are linux/macOS only and the probe
layer must degrade to visible failures, not import errors, where they are
absent. All loading and probing is CPU-bound and runs off the event loop
(callers go through ``asyncio.to_thread``).
"""

from __future__ import annotations

import concurrent.futures
import importlib
import math
import re
import time
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from io import BytesIO
from pathlib import Path, PurePosixPath
from typing import Any

from runner.evals.agentic_verifier.cad import (
    MAX_XML_ELEMENTS,
    CadParseError,
    _read_member_bytes,  # pyright: ignore[reportPrivateUsage]
    _safe_members,  # pyright: ignore[reportPrivateUsage]
)
from runner.utils.grading_log import logger

PROVENANCE_OCCT = "measured (OCCT kernel)"
PROVENANCE_MESH = "measured (mesh)"

# Appended to a part name whenever a resolved shape is a consumed intermediate
# feature (a Pad eaten by a Fillet, a Sketch) rather than a terminal solid, so
# every probe row built from it declares itself as feature history, not a part.
FEATURE_HISTORY_MARK = " [feature history]"
# Appended to a part name whose object is saved with Visibility=false: excluded
# from part counts, pairwise probes and renders, resolvable only by name.
HIDDEN_MARK = " [hidden]"
_NAME_MARKS = (FEATURE_HISTORY_MARK, HIDDEN_MARK)
# Share of an earlier hidden part's volume the last part must hold to be its stage.
STAGE_OVERLAP_MIN = 0.5


def _strip_marks(name: str) -> str:
    for mark in _NAME_MARKS:
        name = name.removesuffix(mark)
    return name


# Wall-clock ceilings. A probe that starts cannot be preempted (OCCT calls are
# opaque C++), so the per-probe timeout abandons the worker thread and the total
# budget stops new probes from starting; both bound the grade, not the work.
PROBE_TOTAL_BUDGET_S = 300.0
PER_PROBE_TIMEOUT_S = 60.0

# Pairwise checks are quadratic in solids; the cap keeps a 50-part assembly from
# eating the whole probe budget before the planner's own requests run.
MAX_INTERFERENCE_PAIRS = 16
# Hidden-involving pairs spend their OWN cap: hiding many parts must neither
# dodge measurement nor displace visible pairs from the shared budget.
MAX_HIDDEN_INTERFERENCE_PAIRS = 8
MAX_GOLDEN_COMPARISONS = 4
MAX_HOLES_REPORTED = 40

# golden_compare rows carrying one of these reasons never ran a comparison;
# budget accounting (run_standard_battery and main._run_probe_phase) must not
# count them against the comparison cap.
_GOLDEN_NOT_RUN_REASONS = (
    "golden comparison budget exhausted",
    "no mesh could be built for this file",
    "no mesh could be built for the reference",
)


def golden_compare_spent_budget(outcome: object) -> bool:
    """Whether a golden_compare row represents a comparison that actually ran."""
    text = str(outcome)
    return not any(reason in text for reason in _GOLDEN_NOT_RUN_REASONS)


_SAMPLE_POINTS = 2_000
_THICKNESS_RAYS = 256
# Multi-start ICP for golden_compare: coarse passes over subsampled points from
# every candidate orientation, then full-quality refinement of the best few.
_COARSE_ICP_POINTS = 400
_COARSE_ICP_ITERATIONS = 8
_REFINE_CANDIDATES = 2
# Fits this close to the best (as a fraction of the reference diagonal) tie.
_EQUAL_FIT_DIAG_FRACTION = 1e-4
# Above this combined face count only identity + PCA candidates run, keeping the
# candidate sweep bounded on very large meshes.
_MULTISTART_MAX_FACES = 200_000
# Battery rows for assemblies beyond this many solids summarize instead of
# enumerating every part: a 200-solid assembly enumerated verbatim produced one
# ~147k-char prompt row and overflowed the judge context window.
MAX_BATTERY_PARTS_DETAILED = 20
_MESH_FORMATS = frozenset({".stl"})
_BREP_FORMATS = frozenset({".step", ".stp", ".iges", ".igs", ".brep", ".brp"})
_FCSTD_FORMATS = frozenset({".fcstd", ".fcstd1"})

# Total bytes written to the probe scratch dir; snapshot members already pass
# the per-file CAD cap, this bounds their sum.
MAX_EXTRACT_TOTAL_BYTES = 1024 * 1024 * 1024


def _ocp(module: str) -> Any:
    """Import an OCP submodule lazily so absent wheels degrade to probe text."""
    return importlib.import_module(f"OCP.{module}")


# --- placements ------------------------------------------------------------------
# FreeCAD bakes an object's OWN Placement into its saved ``.brp`` (the shape and
# the property are kept in sync, so the member's geometry is already placed
# within its container), but ancestor containers' (App::Part) placements live
# ONLY in Document.xml — verified against native FreeCAD 1.1.3 saves. World
# space is therefore ancestors-composed-onto-the-loaded-shape, never the
# object's own Placement again (that would double-apply it).


@dataclass(frozen=True)
class _Placement:
    """A FreeCAD placement as plain numbers: translate after rotating about
    the origin. Quaternion is (x, y, z, w)."""

    pos: tuple[float, float, float] = (0.0, 0.0, 0.0)
    quat: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 1.0)

    def is_identity(self) -> bool:
        x, y, z, w = self.quat
        return (
            all(abs(c) < 1e-12 for c in self.pos)
            and abs(x) < 1e-12
            and abs(y) < 1e-12
            and abs(z) < 1e-12
            and abs(abs(w) - 1.0) < 1e-12
        )


_IDENTITY_PLACEMENT = _Placement()


def _quat_rotate(
    q: tuple[float, float, float, float], v: tuple[float, float, float]
) -> tuple[float, float, float]:
    x, y, z, w = q
    # q * (v, 0) * q^-1 expanded: t = 2 (q_vec × v); v' = v + w t + q_vec × t
    tx = 2.0 * (y * v[2] - z * v[1])
    ty = 2.0 * (z * v[0] - x * v[2])
    tz = 2.0 * (x * v[1] - y * v[0])
    return (
        v[0] + w * tx + (y * tz - z * ty),
        v[1] + w * ty + (z * tx - x * tz),
        v[2] + w * tz + (x * ty - y * tx),
    )


def _compose_placements(outer: _Placement, inner: _Placement) -> _Placement:
    """outer ∘ inner: apply ``inner`` first, then ``outer``."""
    ox, oy, oz, ow = outer.quat
    ix, iy, iz, iw = inner.quat
    quat = (
        ow * ix + ox * iw + oy * iz - oz * iy,
        ow * iy - ox * iz + oy * iw + oz * ix,
        ow * iz + ox * iy - oy * ix + oz * iw,
        ow * iw - ox * ix - oy * iy - oz * iz,
    )
    rotated = _quat_rotate(outer.quat, inner.pos)
    pos = (
        outer.pos[0] + rotated[0],
        outer.pos[1] + rotated[1],
        outer.pos[2] + rotated[2],
    )
    return _Placement(pos=pos, quat=quat)


def _parse_placement_element(elem: Any) -> _Placement | None:
    """A placement from a ``<PropertyPlacement>`` element, or None when any
    attribute fails to parse. Mirrors FreeCAD's restore: the angle-axis
    attributes (A/Ox/Oy/Oz) win over the quaternion when present."""
    try:
        pos = (
            float(elem.get("Px", 0.0)),
            float(elem.get("Py", 0.0)),
            float(elem.get("Pz", 0.0)),
        )
        angle = elem.get("A")
        if angle is not None:
            a = float(angle)
            axis = (
                float(elem.get("Ox", 0.0)),
                float(elem.get("Oy", 0.0)),
                float(elem.get("Oz", 1.0)),
            )
            length = math.sqrt(sum(c * c for c in axis))
            if length < 1e-12:
                return _Placement(pos=pos) if abs(a) < 1e-12 else None
            half = a / 2.0
            s = math.sin(half) / length
            quat = (axis[0] * s, axis[1] * s, axis[2] * s, math.cos(half))
        else:
            quat = (
                float(elem.get("Q0", 0.0)),
                float(elem.get("Q1", 0.0)),
                float(elem.get("Q2", 0.0)),
                float(elem.get("Q3", 1.0)),
            )
        norm = math.sqrt(sum(c * c for c in quat))
        if not math.isfinite(norm) or norm < 1e-12:
            return None
        if not all(math.isfinite(c) for c in pos):
            return None
        return _Placement(
            pos=pos,
            quat=(quat[0] / norm, quat[1] / norm, quat[2] / norm, quat[3] / norm),
        )
    except (TypeError, ValueError):
        return None


def _apply_world_placement(shape: Any, placement: _Placement) -> Any:
    """``shape`` relocated by ``placement`` (a cheap TopLoc move, no rebuild)."""
    gp = _ocp("gp")
    trsf = gp.gp_Trsf()
    x, y, z, w = placement.quat
    trsf.SetRotation(gp.gp_Quaternion(x, y, z, w))
    trsf.SetTranslationPart(gp.gp_Vec(*placement.pos))
    return shape.Moved(_ocp("TopLoc").TopLoc_Location(trsf))


def ocp_available() -> bool:
    try:
        importlib.import_module("OCP.BRepGProp")
    except Exception:
        return False
    return True


def _round(value: float, digits: int = 6) -> float:
    return round(float(value), digits)


@dataclass(frozen=True)
class ProbeResult:
    probe_id: str
    target: str
    outcome: str
    provenance: str
    ok: bool
    params: dict[str, Any] = field(default_factory=dict)

    def as_row(self) -> dict[str, Any]:
        return {
            "probe_id": self.probe_id,
            "target": self.target,
            "params": self.params,
            "outcome": self.outcome,
            "provenance": self.provenance,
            "ok": self.ok,
        }


def failed(
    probe_id: str, target: str, why: str, params: dict[str, Any] | None = None
) -> ProbeResult:
    return ProbeResult(
        probe_id=probe_id,
        target=target,
        outcome=f"failed: {why}",
        provenance=f"failed: {why}",
        ok=False,
        params=params or {},
    )


@dataclass
class _LoadedFile:
    path: str
    disk_path: Path
    suffix: str = ""
    # (part_name, TopoDS_Shape) pairs for TERMINAL solids — the shapes that are
    # this file's actual parts; empty when the format has no B-rep or loading
    # failed (then ``error`` says why).
    solids: list[tuple[str, Any]] = field(default_factory=list)
    # Consumed intermediate feature shapes (FCStd feature history): kept out of
    # part counts, volume totals, pairwise probes and renders; resolvable only
    # by exact name and always labeled with FEATURE_HISTORY_MARK.
    feature_shapes: list[tuple[str, Any]] = field(default_factory=list)
    # Terminal shapes saved with Visibility=false: same treatment as feature
    # history (name-only resolution, HIDDEN_MARK label).
    hidden_shapes: list[tuple[str, Any]] = field(default_factory=list)
    # (probe_id, reason) markers surfaced as visible failed probe rows.
    issues: list[tuple[str, str]] = field(default_factory=list)
    densities: dict[str, float] = field(default_factory=dict)
    mesh: Any | None = None
    error: str | None = None


class GeometryStore:
    """Per-run cache of untrusted CAD bytes staged on disk plus loaded geometry.

    Owns a scratch directory the caller creates (and removes); file names are
    flattened, so archive paths can never escape it.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._by_path: dict[str, _LoadedFile] = {}
        self._written_bytes = 0
        self._mesh_cache: dict[tuple[str, bool], Any] = {}
        self._hidden_mesh_part: dict[str, str] = {}
        self.reference_paths: set[str] = set()
        # Child-reported part names per path. When set, this store never loads
        # geometry itself: part lookups answer from the inventory so the parent
        # process stays free of GIL-holding kernel work.
        self.part_inventory: dict[str, list[str]] | None = None
        self.feature_inventory: dict[str, list[str]] = {}
        self.hidden_inventory: dict[str, list[str]] = {}
        self._sha_cache: dict[str, str | None] = {}

    def set_inventory(
        self,
        parts: dict[str, list[str]],
        features: dict[str, list[str]],
        hidden: dict[str, list[str]] | None = None,
    ) -> None:
        self.part_inventory = parts
        self.feature_inventory = features
        self.hidden_inventory = hidden or {}

    def sha256(self, path: str) -> str | None:
        """Digest of a staged file's bytes, or None when it was never staged."""
        if path not in self._sha_cache:
            entry = self._by_path.get(path)
            digest: str | None = None
            if entry is not None and entry.disk_path != self._root:
                import hashlib

                try:
                    digest = hashlib.sha256(entry.disk_path.read_bytes()).hexdigest()
                except OSError:
                    digest = None
            self._sha_cache[path] = digest
        return self._sha_cache[path]

    def file_table(self) -> list[dict[str, Any]]:
        """JSON-safe staged-file entries a geometry worker can rebuild from."""
        return [
            {
                "path": entry.path,
                "disk": str(entry.disk_path),
                "suffix": entry.suffix,
                "reference": entry.path in self.reference_paths,
                "error": entry.error,
            }
            for entry in self._by_path.values()
        ]

    @classmethod
    def from_file_table(cls, root: Path, table: list[dict[str, Any]]) -> GeometryStore:
        """A store over files another process already staged under ``root``."""
        store = cls(root)
        for row in table:
            path = str(row["path"])
            store._by_path[path] = _LoadedFile(
                path=path,
                disk_path=Path(str(row["disk"])),
                suffix=str(row.get("suffix") or ""),
                error=row.get("error"),
            )
            if row.get("reference"):
                store.reference_paths.add(path)
        return store

    def add_file(self, path: str, data: bytes, *, reference: bool = False) -> None:
        suffix = PurePosixPath(path).suffix.lower()
        base_name = PurePosixPath(path).name
        if path in self._by_path:
            if not reference or path in self.reference_paths:
                return
            # A golden file often shares its path with the agent's final file;
            # the reference's BYTES differ, so it needs its own entry.
            path = f"{path} [ground-truth reference]"
            if path in self._by_path:
                return
        if self._written_bytes + len(data) > MAX_EXTRACT_TOTAL_BYTES:
            # An unstaged reference must still read as a reference, never as
            # the agent's work.
            self._add_unstaged(
                path,
                suffix,
                "probe scratch budget exhausted; file not staged for probing",
                reference=reference,
            )
            return
        # Flattened, indexed name: nothing an archive-controlled path says can
        # place the file outside the scratch root.
        safe_name = f"{len(self._by_path):03d}_{base_name}"
        disk_path = self._root / safe_name
        try:
            disk_path.write_bytes(data)
        except OSError as e:
            # One file's scratch-write failure is a visible probe error; it
            # must never abort the whole grade mid-staging.
            self._add_unstaged(
                path,
                suffix,
                f"scratch write failed; file not staged for probing ({e})",
                reference=reference,
            )
            return
        self._written_bytes += len(data)
        self._by_path[path] = _LoadedFile(path=path, disk_path=disk_path, suffix=suffix)
        if reference:
            self.reference_paths.add(path)

    def _add_unstaged(
        self, path: str, suffix: str, error: str, *, reference: bool
    ) -> None:
        self._by_path[path] = _LoadedFile(
            path=path, disk_path=self._root, suffix=suffix, error=error
        )
        if reference:
            self.reference_paths.add(path)

    @property
    def root(self) -> Path:
        return self._root

    def paths(self) -> list[str]:
        return list(self._by_path)

    def agent_paths(self) -> list[str]:
        return [p for p in self._by_path if p not in self.reference_paths]

    def disk_path(self, path: str) -> Path | None:
        entry = self._by_path.get(path)
        return entry.disk_path if entry else None

    def resolve(self, name: str) -> str | None:
        """A staged path from a planner-supplied name (exact or basename match)."""
        if name in self._by_path:
            return name
        base = PurePosixPath(name).name
        matches = [p for p in self._by_path if PurePosixPath(p).name == base]
        return matches[0] if len(matches) == 1 else None

    def loaded(self, path: str) -> _LoadedFile:
        entry = self._by_path[path]
        if entry.solids or entry.mesh is not None or entry.error is not None:
            return entry
        try:
            self._load(entry)
        except Exception as e:
            entry.error = f"{type(e).__name__}: {e}"
        if entry.error:
            logger.info(f"[CAD_LLM_JUDGE] probe load failed for {path}: {entry.error}")
        return entry

    def _load(self, entry: _LoadedFile) -> None:
        suffix = entry.suffix
        if suffix in _MESH_FORMATS:
            entry.mesh = _load_mesh_file(entry.disk_path)
            if entry.mesh is None:
                entry.error = "trimesh could not load the mesh"
            return
        if suffix in _FCSTD_FORMATS:
            load = _load_fcstd_breps(entry.disk_path, self._root)
            entry.solids = load.solids
            entry.feature_shapes = load.feature_shapes
            entry.hidden_shapes = load.hidden_shapes
            entry.issues = load.issues
            entry.densities = load.densities
            if not entry.solids:
                if entry.hidden_shapes:
                    entry.error = (
                        "every terminal solid in this FCStd is saved with "
                        "Visibility=false; hidden geometry is excluded from "
                        "the part inventory and renders"
                    )
                elif entry.issues:
                    entry.error = "; ".join(reason for _, reason in entry.issues)
                else:
                    entry.error = "no readable .brp B-rep members in the FCStd archive"
            return
        if suffix in _BREP_FORMATS:
            entry.solids = _load_brep_file(entry.disk_path, suffix)
            if not entry.solids:
                entry.error = "the OCCT reader produced no shape"
            return
        entry.error = f"format {suffix or 'unknown'} has no probe loader"

    def mesh_for(self, path: str, *, include_hidden: bool = False) -> Any | None:
        """A trimesh mesh for ``path``: native for STL, tessellated for B-reps.

        ``include_hidden`` meshes hidden terminals when no visible solid exists:
        the last-created part alone when it contains the rest (build stages).
        """
        entry = self.loaded(path)
        hidden = include_hidden and entry.mesh is None and not entry.solids
        key = (path, hidden)
        if key in self._mesh_cache:
            return self._mesh_cache[key]
        mesh = entry.mesh
        shapes = entry.hidden_shapes if hidden else entry.solids
        if mesh is None and shapes:
            if hidden and (last := _final_stage(shapes)):
                self._hidden_mesh_part[path] = last
                shapes = [(n, s) for n, s in shapes if n == last]
            meshes = [m for _, s in shapes if (m := tessellate(s)) is not None]
            if meshes:
                import trimesh

                mesh = (
                    meshes[0] if len(meshes) == 1 else trimesh.util.concatenate(meshes)
                )
        self._mesh_cache[key] = mesh
        return mesh

    def hidden_only(self, path: str) -> bool:
        """True when the file's only geometry is hidden terminal solids."""
        entry = self.loaded(path)
        return entry.mesh is None and not entry.solids and bool(entry.hidden_shapes)

    def mesh_label(self, path: str, label: str | None = None) -> str:
        """``label`` (default ``path``) marked hidden for a hidden-only mesh."""
        shown = label or path
        if not self.hidden_only(path):
            return shown
        self.mesh_for(path, include_hidden=True)
        if part := self._hidden_mesh_part.get(path):
            shown = f"{shown}::{part}"
        return f"{shown}{HIDDEN_MARK}"

    def part_ids(self) -> list[str]:
        """Loadable AGENT part names, as ``<file path>::<part>`` identifiers.

        From the child-reported inventory when one is set (the parent process
        never loads geometry itself); feature-history shapes are never listed.
        Reference-file parts are excluded: a distance/interference row over
        golden geometry would reach the judge reading as agent work.
        """
        if self.part_inventory is not None:
            return [
                f"{path}::{name}"
                for path, names in self.part_inventory.items()
                if path not in self.reference_paths
                for name in names
            ]
        ids: list[str] = []
        for path in self._by_path:
            if path in self.reference_paths:
                continue
            entry = self.loaded(path)
            ids.extend(f"{path}::{name}" for name, _ in entry.solids)
        return ids

    def part_file(self, part_id: str) -> str:
        """The staged file path a resolved part id belongs to."""
        return _strip_marks(part_id).split("::", 1)[0]

    def resolve_part(self, name: str) -> tuple[str, str, Any] | None:
        """(file path, part name, shape) for an exact/suffix part reference.

        A bare part name matching several parts is ambiguous and resolves to
        None rather than an arbitrary solid. Feature-history and hidden shapes
        resolve only on an exact unambiguous name and come back with their part
        name carrying FEATURE_HISTORY_MARK / HIDDEN_MARK, so every downstream
        label declares them. Reference-file parts resolve ONLY by their exact
        ``path::part`` id: a golden reusing an agent part name (Body) must not
        make the bare name match golden geometry, or read the agent's own name
        as ambiguous.
        """
        name = _strip_marks(name)
        wanted_file, _, wanted_part = name.partition("::")
        matches: list[tuple[str, str, Any]] = []
        marked_matches: list[tuple[str, str, Any]] = []
        for path in self._by_path:
            entry = self.loaded(path)
            is_reference = path in self.reference_paths
            for part_name, shape in entry.solids:
                pid = f"{path}::{part_name}"
                if name == pid:
                    return path, part_name, shape
                if is_reference:
                    continue
                if name == part_name or (
                    wanted_part
                    and self.resolve(wanted_file) == path
                    and part_name == wanted_part
                ):
                    matches.append((path, part_name, shape))
            for shapes, mark in (
                (entry.feature_shapes, FEATURE_HISTORY_MARK),
                (entry.hidden_shapes, HIDDEN_MARK),
            ):
                for part_name, shape in shapes:
                    if name == f"{path}::{part_name}" or (
                        name == part_name and not is_reference
                    ):
                        marked_matches.append((path, f"{part_name}{mark}", shape))
        if len(matches) == 1:
            return matches[0]
        if not matches and len(marked_matches) == 1:
            return marked_matches[0]
        return None

    def resolve_part_id(self, name: str) -> str | None:
        """A ``<file path>::<part>`` id for a planner part reference, or None.

        Inventory-backed when set (no geometry loads in the parent); otherwise
        resolves live. Feature-history and hidden ids keep their mark so the
        executing child re-resolves and labels them the same way. Same rule as
        resolve_part: reference-file parts match ONLY their exact id.
        """
        if self.part_inventory is None:
            resolved = self.resolve_part(name)
            return f"{resolved[0]}::{resolved[1]}" if resolved else None
        bare = _strip_marks(name)
        wanted_file, _, wanted_part = bare.partition("::")
        matches: list[str] = []
        marked_matches: list[str] = []
        for inventory, mark, sink in (
            (self.part_inventory, "", matches),
            (self.feature_inventory, FEATURE_HISTORY_MARK, marked_matches),
            (self.hidden_inventory, HIDDEN_MARK, marked_matches),
        ):
            for path, names in inventory.items():
                is_reference = path in self.reference_paths
                for part_name in names:
                    pid = f"{path}::{part_name}"
                    if bare == pid or (
                        not is_reference
                        and (
                            bare == part_name
                            or (
                                wanted_part
                                and self.resolve(wanted_file) == path
                                and part_name == wanted_part
                            )
                        )
                    ):
                        sink.append(f"{pid}{mark}")
        if len(matches) == 1:
            return matches[0]
        if not matches and len(marked_matches) == 1:
            return marked_matches[0]
        return None


# --- loading -------------------------------------------------------------------


def _load_mesh_file(disk_path: Path) -> Any | None:
    import trimesh

    loaded = trimesh.load(str(disk_path), file_type="stl", force="mesh")
    return loaded if getattr(loaded, "faces", None) is not None else None


def _explode_solids(shape: Any, stem: str) -> list[tuple[str, Any]]:
    TopExp = _ocp("TopExp").TopExp_Explorer
    TopAbs = _ocp("TopAbs")
    solids: list[tuple[str, Any]] = []
    explorer = TopExp(shape, TopAbs.TopAbs_SOLID)
    index = 0
    while explorer.More():
        index += 1
        solids.append((f"{stem}_solid_{index}", explorer.Current()))
        explorer.Next()
    # Shells/faces without a solid still deserve measurement; keep the whole
    # shape as one part so area/bbox probes have something to hold.
    return solids or [(stem, shape)]


def _load_brep_file(disk_path: Path, suffix: str) -> list[tuple[str, Any]]:
    stem = disk_path.stem.split("_", 1)[-1] or disk_path.stem
    if suffix in {".brep", ".brp"}:
        BRepTools = _ocp("BRepTools").BRepTools
        BRep = _ocp("BRep")
        TopoDS = _ocp("TopoDS")
        shape = TopoDS.TopoDS_Shape()
        if not BRepTools.Read_s(shape, str(disk_path), BRep.BRep_Builder()):
            raise CadParseError("BRepTools.Read refused the file")
        return _explode_solids(shape, stem)
    IFSelect = _ocp("IFSelect")
    if suffix in {".step", ".stp"}:
        reader = _ocp("STEPControl").STEPControl_Reader()
    else:
        reader = _ocp("IGESControl").IGESControl_Reader()
    if reader.ReadFile(str(disk_path)) != IFSelect.IFSelect_RetDone:
        raise CadParseError("the OCCT reader could not parse the file")
    reader.TransferRoots()
    return _explode_solids(reader.OneShape(), stem)


@dataclass
class _FcstdGraph:
    """What Document.xml says about the shapes an FCStd carries: which ``.brp``
    member is whose, which are terminal parts, and the containment/placement
    facts needed to put each shape in WORLD coordinates."""

    object_by_member: dict[str, str]
    terminal_members: set[str]
    # Parsed per-object Placement values; an object whose Placement element
    # exists but would not parse lands in placement_errors instead.
    placements: dict[str, _Placement] = field(default_factory=dict)
    placement_errors: set[str] = field(default_factory=set)
    # Group containment (App::Part, PartDesign::Body, plain groups).
    parent: dict[str, str] = field(default_factory=dict)
    ambiguous_parent: set[str] = field(default_factory=set)
    hidden: set[str] = field(default_factory=set)
    link_objects: list[str] = field(default_factory=list)
    # g/cm^3 from an object's own Material/ShapeMaterial Density entry.
    densities: dict[str, float] = field(default_factory=dict)

    def density(self, obj: str) -> float | None:
        """The object's material density, else its nearest container's."""
        seen: set[str] = set()
        current: str | None = obj
        while current is not None and current not in seen:
            if current in self.densities:
                return self.densities[current]
            seen.add(current)
            current = self.parent.get(current)
        return None

    def world_placement(self, obj: str) -> tuple[_Placement | None, str | None]:
        """The composed ANCESTOR placement for ``obj`` (its own is already
        baked into its saved shape), or (None, why) when the chain cannot be
        trusted — then the caller must fail closed, not measure."""
        chain: list[str] = []
        seen = {obj}
        current = obj
        while (parent := self.parent.get(current)) is not None:
            if current in self.ambiguous_parent:
                return None, f"{current!r} is grouped under more than one container"
            if parent in seen:
                return None, f"containment cycle through {parent!r}"
            seen.add(parent)
            chain.append(parent)
            current = parent
        placement = _IDENTITY_PLACEMENT
        for ancestor in reversed(chain):
            if ancestor in self.placement_errors:
                return None, f"unreadable Placement on container {ancestor!r}"
            placement = _compose_placements(
                placement, self.placements.get(ancestor, _IDENTITY_PLACEMENT)
            )
        return placement, None

    def effectively_hidden(self, obj: str) -> bool:
        """Whether ``obj`` or any container above it is saved Visibility=false
        (hiding an App::Part hides its whole subtree in FreeCAD)."""
        seen: set[str] = set()
        current: str | None = obj
        while current is not None and current not in seen:
            if current in self.hidden:
                return True
            seen.add(current)
            current = self.parent.get(current)
        return False


def _fcstd_shape_graph(document_xml: bytes) -> _FcstdGraph | None:
    """The shape graph parsed from Document.xml, or None to fall back.

    FreeCAD writes one ``.brp`` per FEATURE (Body, Sketch, Pad, Fillet...), not
    per physical part. An object's shape is a real part only when no other
    shape-carrying object builds from it (a Body's tip, the last boolean, an
    independent solid); everything consumed downstream is feature history.
    Links on a plain ``Part::Feature`` do not count: its shape is stored.
    Returns None when the XML is absent, unsafe or unparseable so the caller
    falls back to loading every member. Same parse hardening as
    ``agentic_verifier.cad._fcstd_digest``: refuse UTF-16/NUL prologs and any
    DOCTYPE, stream with an element cap.
    """
    if document_xml[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in document_xml[:256]:
        return None
    if b"<!DOCTYPE" in document_xml:
        return None
    member_by_object: dict[str, str] = {}
    links: list[tuple[str, str]] = []
    placements: dict[str, _Placement] = {}
    placement_errors: set[str] = set()
    parent: dict[str, str] = {}
    ambiguous_parent: set[str] = set()
    hidden: set[str] = set()
    link_objects: list[str] = []
    stored_shape_objects: set[str] = set()
    densities: dict[str, float] = {}
    elements = 0
    try:
        for event, elem in ET.iterparse(BytesIO(document_xml), events=("start", "end")):
            if event == "start":
                elements += 1
                if elements > MAX_XML_ELEMENTS:
                    return None
                continue
            if elem.tag != "Object":
                continue
            name = elem.get("name")
            type_attr = elem.get("type")
            # The <Objects> section carries types; link instances have no
            # ``.brp`` of their own, so they are detected here by type.
            if name and type_attr:
                if type_attr.startswith("App::Link"):
                    link_objects.append(name)
                # Part::Feature stores its shape; its links consume nothing.
                elif type_attr == "Part::Feature":
                    stored_shape_objects.add(name)
                elem.clear()
                continue
            # ObjectData entries carry a name but no type attribute.
            if name and not type_attr:
                for prop in elem.iter("Property"):
                    prop_name = prop.get("name")
                    if prop_name == "Shape":
                        for part in prop.iter("Part"):
                            file_attr = part.get("file")
                            if file_attr and name not in member_by_object:
                                member_by_object[name] = file_attr
                    elif prop_name == "Placement" and prop.get("type") == (
                        "App::PropertyPlacement"
                    ):
                        for placement_elem in prop.iter("PropertyPlacement"):
                            parsed = _parse_placement_element(placement_elem)
                            if parsed is None:
                                placement_errors.add(name)
                            else:
                                placements[name] = parsed
                    elif prop_name == "Group":
                        for link in prop.iter("Link"):
                            child = link.get("value")
                            if not child:
                                continue
                            if child in parent and parent[child] != name:
                                ambiguous_parent.add(child)
                            else:
                                parent[child] = name
                    elif prop_name == "Visibility":
                        for bool_elem in prop.iter("Bool"):
                            if bool_elem.get("value") == "false":
                                hidden.add(name)
                    elif prop_name in _MATERIAL_PROPERTIES:
                        density = _material_density(prop)
                        if density is not None:
                            densities[name] = density
                    # LinkSub carries its target on itself (Base/Profile refs);
                    # Link covers PropertyLink and LinkList entries.
                    for link in (*prop.iter("Link"), *prop.iter("LinkSub")):
                        value = link.get("value")
                        if value:
                            links.append((name, value))
            elem.clear()
    except ET.ParseError:
        return None
    if not member_by_object:
        return None
    consumed = {
        target
        for owner, target in links
        if owner != target
        and owner in member_by_object
        and owner not in stored_shape_objects
        and target in member_by_object
    }
    object_by_member = {member: name for name, member in member_by_object.items()}
    terminal_members = {
        member for member, name in object_by_member.items() if name not in consumed
    }
    return _FcstdGraph(
        object_by_member=object_by_member,
        terminal_members=terminal_members,
        placements=placements,
        placement_errors=placement_errors,
        parent=parent,
        ambiguous_parent=ambiguous_parent,
        hidden=hidden,
        link_objects=link_objects,
        densities=densities,
    )


_MATERIAL_PROPERTIES = frozenset({"Material", "ShapeMaterial"})
_DENSITY_VALUE = re.compile(r"^\s*([0-9]*\.?[0-9]+(?:[eE][+-]?[0-9]+)?)\s*(.*?)\s*$")
# Multiplier to g/cm^3 per unit spelling (spaces, '^' and '*' stripped).
_DENSITY_UNITS = {
    "g/cm3": 1.0,
    "kg/m3": 1e-3,
    "kg/dm3": 1.0,
    "t/m3": 1.0,
    "g/mm3": 1e3,
    "kg/mm3": 1e6,
}


def _parse_density(text: str) -> float | None:
    """g/cm^3 from a saved quantity string like '7900 kg/m^3'; None if unitless."""
    match = _DENSITY_VALUE.match(text)
    if match is None:
        return None
    unit = re.sub(r"[\s^*]", "", match.group(2)).lower()
    factor = _DENSITY_UNITS.get(unit)
    if factor is None:
        return None
    value = float(match.group(1)) * factor
    return value if math.isfinite(value) and value > 0 else None


def _material_density(prop: Any) -> float | None:
    """A Density entry (map item key or named element) under a material property."""
    for elem in prop.iter():
        key = elem.get("key") or elem.get("name") or ""
        if key.lower() == "density" and (value := elem.get("value")) is not None:
            return _parse_density(value)
    return None


@dataclass
class FcstdLoad:
    """What an FCStd's ``.brp`` members yield, bucketed and in world space."""

    solids: list[tuple[str, Any]] = field(default_factory=list)
    feature_shapes: list[tuple[str, Any]] = field(default_factory=list)
    hidden_shapes: list[tuple[str, Any]] = field(default_factory=list)
    # (probe_id, reason) markers for anything withheld or unsupported, each
    # surfaced as a visible failed probe row rather than a wrong measurement.
    issues: list[tuple[str, str]] = field(default_factory=list)
    # Part name -> declared material density (g/cm^3).
    densities: dict[str, float] = field(default_factory=dict)


def _load_fcstd_breps(disk_path: Path, scratch: Path) -> FcstdLoad:
    """Terminal solids, feature-history shapes and hidden shapes from an
    FCStd's ``.brp`` members, each moved into WORLD coordinates by composing
    its ancestor containers' placements (the member itself already carries the
    object's own Placement — applying it again would double it).

    A part whose placement chain cannot be trusted (ambiguous parents, cycle,
    unreadable ancestor Placement) is withheld with a visible issue instead of
    being measured in a wrong frame. Without a usable Document.xml graph every
    member loads as a solid under its member stem in its saved frame (the
    pre-graph behavior); the same fallback applies when no terminal member
    survives loading, so a wrong graph degrades to the old over-count rather
    than to an empty file.
    """
    load = FcstdLoad()
    with zipfile.ZipFile(BytesIO(disk_path.read_bytes())) as zf:
        members = _safe_members(zf)
        brp_names = sorted(
            info.filename
            for info in members
            if info.filename.lower().endswith(".brp") and not info.is_dir()
        )
        graph: _FcstdGraph | None = None
        if any(info.filename == "Document.xml" for info in members):
            try:
                graph = _fcstd_shape_graph(_read_member_bytes(zf, "Document.xml"))
            except CadParseError:
                graph = None
        if graph is not None and not (set(graph.object_by_member) & set(brp_names)):
            graph = None
        if graph is not None and graph.link_objects:
            shown = ", ".join(sorted(graph.link_objects)[:8])
            load.issues.append(
                (
                    "link_instances",
                    f"App::Link instance(s) present but unsupported ({shown}): "
                    "instance positions and counts are unknown, so this "
                    "file's pairwise interference/clearance/distance results "
                    "are withheld as unverifiable rather than measured on an "
                    "incomplete assembly; per-part measurements cover only "
                    "each source object's own geometry",
                )
            )
        for index, name in enumerate(brp_names):
            try:
                data = _read_member_bytes(zf, name)
            except CadParseError:
                continue
            member_path = scratch / f"{disk_path.stem}_member_{index}.brp"
            member_path.write_bytes(data)
            try:
                loaded = _load_brep_file(member_path, ".brp")
            except Exception:  # a bad member is skipped, not fatal
                continue
            if graph is None:
                stem = PurePosixPath(name).stem
                load.solids.extend((stem, shape) for _part_name, shape in loaded)
                continue
            # Members no object's Shape property claims are orphans; treat them
            # as feature history rather than inventing a part.
            object_name = graph.object_by_member.get(name)
            part_name = object_name or PurePosixPath(name).stem
            shapes = [shape for _part_name, shape in loaded]
            if object_name is not None:
                placement, why = graph.world_placement(object_name)
                if placement is None:
                    load.issues.append(
                        (
                            "world_placement",
                            f"part {part_name!r} withheld from measurement: "
                            f"{why}; measuring it in a wrong frame could "
                            "report a false clearance or collision",
                        )
                    )
                    continue
                if not placement.is_identity():
                    shapes = [_apply_world_placement(s, placement) for s in shapes]
            # Consumed features are routinely saved hidden; the feature-history
            # bucket already describes them, so "hidden" is for terminal parts.
            if name not in graph.terminal_members:
                bucket = load.feature_shapes
            elif object_name is not None and graph.effectively_hidden(object_name):
                bucket = load.hidden_shapes
            else:
                bucket = load.solids
            bucket.extend((part_name, shape) for shape in shapes)
            if (
                object_name is not None
                and (density := graph.density(object_name)) is not None
            ):
                load.densities[part_name] = density
    if graph is not None and load.hidden_shapes:
        # Member names sort lexically; mesh_for needs creation order.
        order = {name: i for i, name in enumerate(graph.object_by_member.values())}
        load.hidden_shapes.sort(key=lambda item: order.get(item[0], len(order)))
    if load.hidden_shapes:
        shown = _capped_names(sorted({name for name, _ in load.hidden_shapes}))
        load.issues.append(
            (
                "hidden_geometry",
                f"terminal part(s) saved with Visibility=false ({shown}) are "
                "excluded from the part inventory and renders, but the "
                "geometry is physically present, so pairwise probes (and mesh "
                "probes on a file with no visible part) measure them under "
                f"names marked '{HIDDEN_MARK.strip()}'; identity "
                "and count criteria must not count them as delivered parts",
            )
        )
    if (
        graph is not None
        and not load.solids
        and not load.hidden_shapes
        and not any(probe_id == "world_placement" for probe_id, _ in load.issues)
        and load.feature_shapes
    ):
        load.solids, load.feature_shapes = load.feature_shapes, []
    return load


def _final_stage(shapes: list[tuple[str, Any]]) -> str | None:
    """The last part's name when most of every earlier part lies inside it."""
    last = shapes[-1][0]
    final = [s for n, s in shapes if n == last]
    earlier = [s for n, s in shapes if n != last]
    if not earlier:
        return None
    BRepAlgoAPI = _ocp("BRepAlgoAPI")
    for shape in earlier:
        props = _mass_properties(shape)
        key = "volume" if props["volume"] > 0 else "surface_area"
        inside = 0.0
        for target in final:
            common = BRepAlgoAPI.BRepAlgoAPI_Common(shape, target)
            common.Build()
            if not common.IsDone():
                return None
            inside += _mass_properties(common.Shape())[key]
        # Nested but separate parts (a pin in a bore) share ~no volume.
        if props[key] <= 0 or inside < STAGE_OVERLAP_MIN * props[key]:
            return None
    return last


def tessellate(shape: Any, deflection: float | None = None) -> Any | None:
    """A trimesh mesh of ``shape`` via ``BRepMesh_IncrementalMesh``."""
    import numpy as np
    import trimesh

    BRepMesh = _ocp("BRepMesh").BRepMesh_IncrementalMesh
    BRep = _ocp("BRep").BRep_Tool
    TopExp = _ocp("TopExp").TopExp_Explorer
    TopAbs = _ocp("TopAbs")
    TopLoc = _ocp("TopLoc")
    TopoDS = _ocp("TopoDS").TopoDS

    if deflection is None:
        diag = _bbox_diagonal(shape)
        deflection = max(diag / 500.0, 1e-3) if diag else 0.1
    BRepMesh(shape, deflection)

    vertices: list[list[float]] = []
    faces: list[list[int]] = []
    explorer = TopExp(shape, TopAbs.TopAbs_FACE)
    while explorer.More():
        face = TopoDS.Face_s(explorer.Current())
        location = TopLoc.TopLoc_Location()
        triangulation = BRep.Triangulation_s(face, location)
        if triangulation is not None:
            transform = location.Transformation()
            base = len(vertices)
            for i in range(1, triangulation.NbNodes() + 1):
                node = triangulation.Node(i).Transformed(transform)
                vertices.append([node.X(), node.Y(), node.Z()])
            reversed_face = face.Orientation() == TopAbs.TopAbs_REVERSED
            for i in range(1, triangulation.NbTriangles() + 1):
                a, b, c = triangulation.Triangle(i).Get()
                tri = (
                    [base + a - 1, base + c - 1, base + b - 1]
                    if reversed_face
                    else [
                        base + a - 1,
                        base + b - 1,
                        base + c - 1,
                    ]
                )
                faces.append(tri)
        explorer.Next()
    if not faces:
        return None
    mesh = trimesh.Trimesh(
        vertices=np.array(vertices), faces=np.array(faces), process=False
    )
    # OCCT triangulates per face without welding shared vertices, so an unwelded
    # cube would read non-watertight with 24 "non-manifold" edges.
    mesh.merge_vertices()
    return mesh


# --- B-rep measurements ----------------------------------------------------------


def _bbox_diagonal(shape: Any) -> float | None:
    box = _bbox(shape)
    if box is None:
        return None
    (xmin, ymin, zmin), (xmax, ymax, zmax) = box
    return math.dist((xmin, ymin, zmin), (xmax, ymax, zmax))


def _bbox(
    shape: Any,
) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
    Bnd = _ocp("Bnd")
    BRepBndLib = _ocp("BRepBndLib").BRepBndLib
    box = Bnd.Bnd_Box()
    BRepBndLib.Add_s(shape, box)
    if box.IsVoid():
        return None
    xmin, ymin, zmin, xmax, ymax, zmax = box.Get()
    return (xmin, ymin, zmin), (xmax, ymax, zmax)


def _mass_properties(shape: Any) -> dict[str, Any]:
    GProp = _ocp("GProp")
    BRepGProp = _ocp("BRepGProp").BRepGProp
    volume_props = GProp.GProp_GProps()
    BRepGProp.VolumeProperties_s(shape, volume_props)
    surface_props = GProp.GProp_GProps()
    BRepGProp.SurfaceProperties_s(shape, surface_props)
    com = volume_props.CentreOfMass()
    return {
        "volume": _round(volume_props.Mass()),
        "surface_area": _round(surface_props.Mass()),
        "center_of_mass": [_round(com.X()), _round(com.Y()), _round(com.Z())],
    }


def _is_valid(shape: Any) -> bool:
    BRepCheck = _ocp("BRepCheck")
    return bool(BRepCheck.BRepCheck_Analyzer(shape).IsValid())


def _cylindrical_faces(shape: Any) -> tuple[int, list[dict[str, Any]]]:
    """Cylindrical-face census: (true count, capped per-face details)."""
    TopExp = _ocp("TopExp").TopExp_Explorer
    TopAbs = _ocp("TopAbs")
    TopoDS = _ocp("TopoDS").TopoDS
    BRepAdaptor = _ocp("BRepAdaptor").BRepAdaptor_Surface
    GeomAbs = _ocp("GeomAbs")
    count = 0
    found: list[dict[str, Any]] = []
    explorer = TopExp(shape, TopAbs.TopAbs_FACE)
    while explorer.More():
        face = TopoDS.Face_s(explorer.Current())
        surface = BRepAdaptor(face)
        if surface.GetType() == GeomAbs.GeomAbs_Cylinder:
            count += 1
            if len(found) >= MAX_HOLES_REPORTED:
                explorer.Next()
                continue
            cylinder = surface.Cylinder()
            axis = cylinder.Axis()
            direction = axis.Direction()
            location = axis.Location()
            found.append(
                {
                    "diameter": _round(2.0 * cylinder.Radius()),
                    "axis_direction": [
                        _round(direction.X()),
                        _round(direction.Y()),
                        _round(direction.Z()),
                    ],
                    "axis_point": [
                        _round(location.X()),
                        _round(location.Y()),
                        _round(location.Z()),
                    ],
                    "depth": _round(
                        abs(surface.LastVParameter() - surface.FirstVParameter())
                    ),
                }
            )
        explorer.Next()
    return count, found


def _battery_summary(parts: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate stats over every measured part, for rows too big to enumerate."""
    volumes = [p["volume"] for p in parts]
    mins = [p["bbox_min"] for p in parts if "bbox_min" in p]
    maxs = [p["bbox_max"] for p in parts if "bbox_max" in p]
    return {
        "part_count": len(parts),
        "volume_total": _round(sum(volumes)),
        "volume_min": _round(min(volumes)),
        "volume_max": _round(max(volumes)),
        "surface_area_total": _round(sum(p["surface_area"] for p in parts)),
        "invalid_part_count": sum(1 for p in parts if not p["shape_valid_brepcheck"]),
        "cylindrical_face_count_total": sum(p["cylindrical_face_count"] for p in parts),
        "assembly_bbox_min": (
            [_round(min(m[i] for m in mins)) for i in range(3)] if mins else None
        ),
        "assembly_bbox_max": (
            [_round(max(m[i] for m in maxs)) for i in range(3)] if maxs else None
        ),
    }


@dataclass(frozen=True)
class MassSettings:
    """Mass reporting is on; the fallback density when no material declares one."""

    density_g_cm3: float | None = None


@dataclass(frozen=True)
class ProbeOptions:
    """Opt-in verifier settings the geometry child applies; defaults change nothing."""

    mass: MassSettings | None = None
    orientation_tolerance_deg: float | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.mass is not None:
            out["mass"] = {"density_g_cm3": self.mass.density_g_cm3}
        if self.orientation_tolerance_deg is not None:
            out["orientation_tolerance_deg"] = self.orientation_tolerance_deg
        return out

    @classmethod
    def from_json(cls, raw: dict[str, Any]) -> ProbeOptions:
        mass = raw.get("mass")
        tolerance = raw.get("orientation_tolerance_deg")
        return cls(
            mass=(
                MassSettings(density_g_cm3=mass.get("density_g_cm3"))
                if isinstance(mass, dict)
                else None
            ),
            orientation_tolerance_deg=(
                float(tolerance) if tolerance is not None else None
            ),
        )


NO_PROBE_OPTIONS = ProbeOptions()


def _mass_fields(
    volume_mm3: float, material_density: float | None, mass: MassSettings
) -> dict[str, Any]:
    if material_density is not None:
        density, source = material_density, "FCStd material Density"
    elif mass.density_g_cm3 is not None:
        density, source = mass.density_g_cm3, "verifier density_g_cm3"
    else:
        return {"mass_g": None, "density_source": "density unknown"}
    return {
        "density_g_cm3": density,
        "density_source": source,
        "mass_g": _round(volume_mm3 * density / 1000.0),
    }


def brep_battery(
    path: str,
    solids: list[tuple[str, Any]],
    *,
    mass: MassSettings | None = None,
    densities: dict[str, float] | None = None,
) -> ProbeResult:
    detailed = len(solids) <= MAX_BATTERY_PARTS_DETAILED
    parts: list[dict[str, Any]] = []
    for name, shape in solids:
        entry: dict[str, Any] = {"part": name}
        entry.update(_mass_properties(shape))
        if mass is not None:
            entry.update(
                _mass_fields(entry["volume"], (densities or {}).get(name), mass)
            )
        box = _bbox(shape)
        if box is not None:
            entry["bbox_min"], entry["bbox_max"] = list(box[0]), list(box[1])
            entry["bbox_span"] = [_round(box[1][i] - box[0][i]) for i in range(3)]
        entry["shape_valid_brepcheck"] = _is_valid(shape)
        cylinder_count, cylinders = _cylindrical_faces(shape)
        entry["cylindrical_face_count"] = cylinder_count
        if detailed:
            entry["cylindrical_faces"] = cylinders
            if cylinder_count > len(cylinders):
                entry["cylindrical_faces_truncated"] = (
                    f"details shown for the first {len(cylinders)} of {cylinder_count}"
                )
        parts.append(entry)
    payload: dict[str, Any]
    if detailed:
        payload = {"parts": parts}
    else:
        shown = parts[:MAX_BATTERY_PARTS_DETAILED]
        summary = _battery_summary(parts)
        if mass is not None:
            masses = [p["mass_g"] for p in parts]
            summary["mass_total_g"] = None if None in masses else _round(sum(masses))
        payload = {
            "parts_summary": summary,
            "parts": shown,
            "parts_truncated": (
                f"…{len(shown)} of {len(parts)} solids listed (row truncated; "
                "parts_summary aggregates cover all of them, per-solid "
                "cylindrical-face details omitted)"
            ),
        }
    import json

    return ProbeResult(
        probe_id="brep_battery",
        target=path,
        outcome=json.dumps(payload, ensure_ascii=False),
        provenance=PROVENANCE_OCCT,
        ok=True,
    )


def mesh_battery(path: str, mesh: Any) -> ProbeResult:
    import json

    import numpy as np

    edges_sorted = np.sort(mesh.edges, axis=1)
    _unique, counts = np.unique(edges_sorted, axis=0, return_counts=True)
    non_manifold = int((counts != 2).sum())
    com = mesh.center_mass if mesh.is_watertight else mesh.vertices.mean(axis=0)
    payload = {
        "triangle_count": int(len(mesh.faces)),
        "watertight": bool(mesh.is_watertight),
        "winding_consistent": bool(mesh.is_winding_consistent),
        "non_manifold_edges": non_manifold,
        "volume": _round(mesh.volume) if mesh.is_watertight else None,
        "surface_area": _round(mesh.area),
        "center_of_mass": [_round(v) for v in com],
        "bbox_min": [_round(v) for v in mesh.bounds[0]],
        "bbox_max": [_round(v) for v in mesh.bounds[1]],
        "bbox_span": [_round(v) for v in (mesh.bounds[1] - mesh.bounds[0])],
    }
    return ProbeResult(
        probe_id="mesh_battery",
        target=path,
        outcome=json.dumps(payload, ensure_ascii=False),
        provenance=PROVENANCE_MESH,
        ok=True,
    )


def interference_probe(id_a: str, shape_a: Any, id_b: str, shape_b: Any) -> ProbeResult:
    BRepAlgoAPI = _ocp("BRepAlgoAPI")
    common = BRepAlgoAPI.BRepAlgoAPI_Common(shape_a, shape_b)
    common.Build()
    if not common.IsDone():
        # A failed boolean must never read as "no interference".
        return failed(
            "interference",
            f"{id_a} vs {id_b}",
            "the boolean common operation did not complete on these shapes",
        )
    overlap = _mass_properties(common.Shape())["volume"]
    extrema = _ocp("BRepExtrema").BRepExtrema_DistShapeShape(shape_a, shape_b)
    clearance = _round(extrema.Value()) if extrema.IsDone() else None
    interferes = overlap > 1e-9
    outcome = (
        f"parts {id_a} and {id_b}: "
        + (
            f"INTERFERE — shared boolean-common volume {overlap}"
            if interferes
            else f"no interference (common volume {overlap})"
        )
        + (f"; minimum clearance {clearance}" if clearance is not None else "")
    )
    return ProbeResult(
        probe_id="interference",
        target=f"{id_a} vs {id_b}",
        outcome=outcome,
        provenance=PROVENANCE_OCCT,
        ok=True,
    )


def distance_probe(id_a: str, shape_a: Any, id_b: str, shape_b: Any) -> ProbeResult:
    extrema = _ocp("BRepExtrema").BRepExtrema_DistShapeShape(shape_a, shape_b)
    if not extrema.IsDone():
        return failed("distance", f"{id_a} vs {id_b}", "distance computation failed")
    return ProbeResult(
        probe_id="distance",
        target=f"{id_a} vs {id_b}",
        outcome=f"minimum distance between {id_a} and {id_b}: {_round(extrema.Value())}",
        provenance=PROVENANCE_OCCT,
        ok=True,
    )


# --- mesh-space probes -----------------------------------------------------------


def _sample_surface(mesh: Any, count: int) -> Any:
    import trimesh

    # Seeded: probe results must be deterministic across reruns of one input.
    sampled: Any = trimesh.sample.sample_surface(mesh, count, seed=0)
    return sampled[0]


def _surface_distance(mesh: Any, points: Any) -> Any:
    """Distance from each point to the mesh SURFACE (not to sampled points —
    point-set-to-point-set maxima carry sampling noise the judge would misread
    as real deviation)."""
    import trimesh

    _closest, distance, _tri = trimesh.proximity.closest_point(mesh, points)
    return distance


def _orthogonal_rotations() -> list[Any]:
    """The 24 proper axis-aligned rotations (signed permutation matrices)."""
    import itertools

    import numpy as np

    rotations: list[Any] = []
    for perm in itertools.permutations(range(3)):
        for signs in itertools.product((1.0, -1.0), repeat=3):
            matrix = np.zeros((3, 3))
            for row, (col, sign) in enumerate(zip(perm, signs, strict=True)):
                matrix[row, col] = sign
            if np.linalg.det(matrix) > 0:
                rotations.append(matrix)
    return rotations


def _pca_rotations(points_a: Any, points_b: Any) -> list[Any]:
    """Proper rotations mapping the candidate's principal axes onto the
    reference's, over every axis permutation and sign choice (near-degenerate
    eigenvalues make the ordering and signs ambiguous)."""
    import itertools

    import numpy as np

    def _axes(points: Any) -> Any:
        centered = points - points.mean(axis=0)
        _eigenvalues, vectors = np.linalg.eigh(np.cov(centered.T))
        return vectors[:, ::-1]

    try:
        axes_a, axes_b = _axes(points_a), _axes(points_b)
    except np.linalg.LinAlgError:
        return []
    rotations: list[Any] = []
    for perm in itertools.permutations(range(3)):
        for signs in itertools.product((1.0, -1.0), repeat=3):
            rotation = axes_b @ np.diag(signs) @ axes_a[:, perm].T
            if np.linalg.det(rotation) > 0:
                rotations.append(rotation)
    return rotations


def _candidate_rotations(points_a: Any, points_b: Any, *, small: bool) -> list[Any]:
    import numpy as np

    candidates = [np.eye(3), *_pca_rotations(points_a, points_b)]
    if small:
        candidates.extend(_orthogonal_rotations())
    unique: dict[tuple[float, ...], Any] = {}
    for rotation in candidates:
        unique.setdefault(tuple(np.round(rotation, 3).ravel()), rotation)
    return list(unique.values())


def _rotation_angle_axis(matrix: Any) -> tuple[float, Any]:
    """Unrounded (signed angle in radians, axis) of a transform's rotation part."""
    import numpy as np
    import trimesh

    rotation_only = np.eye(4)
    # Nearest proper rotation: ICP output carries numerical noise that trips
    # rotation_from_matrix's exact eigenvalue check.
    u, _s, vt = np.linalg.svd(np.asarray(matrix)[:3, :3])
    rotation_only[:3, :3] = u @ vt
    try:
        angle, direction, _point = trimesh.transformations.rotation_from_matrix(
            rotation_only
        )
    except Exception:
        return 0.0, None
    return float(angle), direction


def _rotation_angle_deg(matrix: Any) -> float:
    return math.degrees(abs(_rotation_angle_axis(matrix)[0]))


def _rotation_summary(
    matrix: Any, min_deg: float = 0.5
) -> tuple[float, list[float] | None]:
    """(angle in degrees, unit axis) of a transform's rotation part, for display."""
    angle, direction = _rotation_angle_axis(matrix)
    angle_deg = math.degrees(abs(angle))
    if direction is None or angle_deg < min_deg or angle_deg == 0.0:
        return 0.0, None
    if float(angle) < 0:
        direction = -direction
    return angle_deg, [_round(float(v), 4) for v in direction]


def _meshes_identical(mesh: Any, reference_mesh: Any) -> bool:
    import numpy as np

    return bool(
        mesh.vertices.shape == reference_mesh.vertices.shape
        and mesh.faces.shape == reference_mesh.faces.shape
        and np.array_equal(mesh.vertices, reference_mesh.vertices)
        and np.array_equal(mesh.faces, reference_mesh.faces)
    )


def _smallest_equal_fit_rotation(
    fits: list[tuple[float, float, float]], diag: float
) -> float:
    """Smallest rotation (deg) among (hausdorff, mean_dev, rotation) fits as good as the best."""
    floor = _EQUAL_FIT_DIAG_FRACTION * diag
    best_h = min(fit[0] for fit in fits)
    near_h = [fit for fit in fits if fit[0] <= best_h + max(0.1 * best_h, floor)]
    # Mean breaks Hausdorff ties: a local defect inflates Hausdorff at every rotation.
    best_m = min(fit[1] for fit in near_h)
    return min(
        rotation
        for _, mean_dev, rotation in near_h
        if mean_dev <= best_m + max(0.1 * best_m, floor)
    )


def _orientation_fields(
    rotation_deg: float, pinned_hausdorff: float, tolerance_deg: float
) -> dict[str, Any]:
    """Pinned-pose evidence and the orientation verdict against a tolerance."""
    within = rotation_deg <= tolerance_deg
    return {
        "pinned_hausdorff_distance": _round(pinned_hausdorff),
        "pinned_note": (
            "Hausdorff with centroids aligned and NO rotation: the as-modelled "
            "orientation against the reference"
        ),
        "orientation_tolerance_deg": tolerance_deg,
        "orientation_ok": within,
        "orientation_verdict": (
            f"PASS — smallest best-fit rotation {rotation_deg:.2f} deg is within "
            f"the {tolerance_deg:g} deg tolerance"
            if within
            else f"FAIL — smallest best-fit rotation {rotation_deg:.2f} deg exceeds the "
            f"{tolerance_deg:g} deg tolerance: the part is mis-oriented against "
            "the reference even if its shape matches"
        ),
    }


def golden_compare_probe(
    path: str,
    mesh: Any,
    reference_path: str,
    reference_mesh: Any,
    *,
    byte_identical: bool = False,
    orientation_tolerance_deg: float | None = None,
) -> ProbeResult:
    """Best-fit the candidate to the reference, then Hausdorff + property deltas.

    With ``orientation_tolerance_deg`` the row also carries the pinned
    (translation-only) Hausdorff and ``orientation_ok`` against the tolerance.

    Invariants the result keeps: an identical pair (same bytes or same
    vertex/face arrays) reads as exactly zero deviation; alignment is
    orientation-invariant (multi-start ICP) with the identity transform always
    among the scored candidates, so an aligned distance never exceeds the
    unaligned one on equal meshes; the applied rotation is reported so
    orientation itself stays judgeable; the result is deterministic per input.
    """
    import json

    import numpy as np
    import trimesh

    def _wt_volume(m: Any) -> float | None:
        return _round(m.volume) if m.is_watertight else None

    if byte_identical or _meshes_identical(mesh, reference_mesh):
        how = (
            "byte-identical files"
            if byte_identical
            else "identical tessellations (same vertex and face arrays)"
        )
        payload = {
            "reference": reference_path,
            "identical": True,
            "icp_translation": [0.0, 0.0, 0.0],
            "icp_rotation_deg": 0.0,
            "icp_rotation_axis": None,
            "orientation_note": "aligned without rotation",
            "hausdorff_distance": 0.0,
            "mean_surface_deviation": 0.0,
            "reference_bbox_diagonal": _round(
                float(
                    np.linalg.norm(reference_mesh.bounds[1] - reference_mesh.bounds[0])
                )
            ),
            "volume_candidate": _wt_volume(mesh),
            "volume_reference": _wt_volume(reference_mesh),
            "area_candidate": _round(mesh.area),
            "area_reference": _round(reference_mesh.area),
            "com_delta": [0.0, 0.0, 0.0],
            "note": (
                f"candidate and reference are {how}; deviation is exactly "
                "zero by identity, no sampling or alignment was needed"
            ),
        }
        if orientation_tolerance_deg is not None:
            payload.update(_orientation_fields(0.0, 0.0, orientation_tolerance_deg))
        return ProbeResult(
            probe_id="golden_compare",
            target=path,
            params={"reference": reference_path},
            outcome=json.dumps(payload, ensure_ascii=False),
            provenance=PROVENANCE_MESH,
            ok=True,
        )

    points_a = _sample_surface(mesh, _SAMPLE_POINTS)
    points_b = _sample_surface(reference_mesh, _SAMPLE_POINTS)
    com_a, com_b = points_a.mean(axis=0), points_b.mean(axis=0)

    def _initial(rotation: Any) -> Any:
        transform = np.eye(4)
        transform[:3, :3] = rotation
        transform[:3, 3] = com_b - rotation @ com_a
        return transform

    small = (len(mesh.faces) + len(reference_mesh.faces)) <= _MULTISTART_MAX_FACES
    coarse_a = points_a[:_COARSE_ICP_POINTS]
    coarse_b = points_b[:_COARSE_ICP_POINTS]
    scored: list[tuple[float, Any]] = []
    for rotation in _candidate_rotations(points_a, points_b, small=small):
        try:
            coarse_matrix, _moved, cost = trimesh.registration.icp(
                coarse_a,
                coarse_b,
                initial=_initial(rotation),
                max_iterations=_COARSE_ICP_ITERATIONS,
                # Rigid only: a scaled or mirrored part must never read as
                # congruent, and the reported rotation must be a pure rotation.
                reflection=False,
                scale=False,
            )
        except Exception:
            continue
        scored.append((float(cost), coarse_matrix))
    scored.sort(key=lambda entry: entry[0])
    candidates = [matrix for _cost, matrix in scored[:_REFINE_CANDIDATES]] or [
        _initial(np.eye(3))
    ]

    best: tuple[float, float, Any, Any, Any] | None = None
    fits: list[tuple[float, float, float]] = []
    # The identity transform is always evaluated as-is (no ICP): when the
    # meshes already coincide, refinement jitter must not beat leaving them
    # alone.
    starts = [(start, True) for start in candidates]
    starts.append((np.eye(4), False))
    for start, refine in starts:
        if refine:
            try:
                matrix, aligned, _cost = trimesh.registration.icp(
                    points_a,
                    points_b,
                    initial=start,
                    max_iterations=30,
                    reflection=False,
                    scale=False,
                )
            except Exception:
                matrix, aligned = start, trimesh.transform_points(points_a, start)
        else:
            matrix, aligned = start, trimesh.transform_points(points_a, start)
        moved = mesh.copy()
        moved.apply_transform(np.asarray(matrix))
        forward = _surface_distance(reference_mesh, aligned)
        backward = _surface_distance(moved, points_b)
        hausdorff = float(max(forward.max(), backward.max()))
        mean_dev = float((forward.mean() + backward.mean()) / 2.0)
        fits.append((hausdorff, mean_dev, _rotation_angle_deg(matrix)))
        if best is None or hausdorff < best[0]:
            best = (hausdorff, mean_dev, matrix, moved, aligned)
    assert best is not None
    hausdorff, mean_dev, matrix, moved, _aligned = best
    diag = float(np.linalg.norm(reference_mesh.bounds[1] - reference_mesh.bounds[0]))
    # A tolerance verdict must see the rotations the display would snap to zero.
    rotation_deg, rotation_axis = _rotation_summary(
        matrix, min_deg=0.5 if orientation_tolerance_deg is None else 0.0
    )

    payload = {
        "reference": reference_path,
        "icp_translation": [_round(v) for v in np.asarray(matrix)[:3, 3]],
        "icp_rotation_deg": _round(rotation_deg, 2),
        "icp_rotation_axis": rotation_axis,
        "orientation_note": (
            "aligned without rotation"
            if rotation_axis is None
            else (
                f"orientation-normalized: the candidate was rotated "
                f"~{rotation_deg:.{0 if rotation_deg >= 0.5 else 2}f} deg about "
                f"axis {rotation_axis} to best "
                "align with the reference. Distances below measure shape "
                "congruence AFTER that rotation; judge orientation itself "
                "from this reported rotation and the rendered views."
            )
        ),
        "hausdorff_distance": _round(hausdorff),
        "mean_surface_deviation": _round(mean_dev),
        "reference_bbox_diagonal": _round(diag),
        "volume_candidate": _wt_volume(mesh),
        "volume_reference": _wt_volume(reference_mesh),
        "area_candidate": _round(mesh.area),
        "area_reference": _round(reference_mesh.area),
        "com_delta": [
            _round(v)
            for v in (
                np.asarray(moved.vertices.mean(axis=0))
                - np.asarray(reference_mesh.vertices.mean(axis=0))
            )
        ],
        "note": (
            "sampled-point-to-surface distances after best-fit (multi-start "
            "ICP) alignment; orientation-invariant, with the applied rotation "
            "reported above; exact only as far as the tessellation is"
        ),
    }
    if orientation_tolerance_deg is not None:
        pinned = _initial(np.eye(3))
        pinned_moved = mesh.copy()
        pinned_moved.apply_transform(pinned)
        pinned_forward = _surface_distance(
            reference_mesh, trimesh.transform_points(points_a, pinned)
        )
        pinned_backward = _surface_distance(pinned_moved, points_b)
        pinned_hausdorff = float(max(pinned_forward.max(), pinned_backward.max()))
        pinned_mean = float((pinned_forward.mean() + pinned_backward.mean()) / 2.0)
        # Symmetric parts fit equally at several rotations; judge the smallest.
        orientation_deg = _smallest_equal_fit_rotation(
            [*fits, (pinned_hausdorff, pinned_mean, 0.0)], diag
        )
        payload.update(
            _orientation_fields(
                orientation_deg, pinned_hausdorff, orientation_tolerance_deg
            )
        )
        if not math.isclose(orientation_deg, rotation_deg, abs_tol=0.01):
            payload["orientation_note"] = (
                f"{payload['orientation_note']} A rotation of {orientation_deg:.2f} deg fits equally well (e.g. "
                "a symmetric part); orientation_verdict judges that smallest one."
            )
    return ProbeResult(
        probe_id="golden_compare",
        target=path,
        params={"reference": reference_path},
        outcome=json.dumps(payload, ensure_ascii=False),
        provenance=PROVENANCE_MESH,
        ok=True,
    )


def wall_thickness_probe(path: str, mesh: Any) -> ProbeResult:
    """Local wall thickness by inward ray casting from sampled surface points."""
    import json

    import numpy as np
    import trimesh

    sampled: Any = trimesh.sample.sample_surface(mesh, _THICKNESS_RAYS, seed=0)
    points, face_index = sampled[0], sampled[1]
    normals = mesh.face_normals[face_index]
    origins = points - normals * 1e-4
    hits: Any = mesh.ray.intersects_location(origins, -normals, multiple_hits=False)
    locations, ray_ids = hits[0], hits[1]
    if len(locations) == 0:
        return failed("wall_thickness", path, "no inward ray hit the opposite surface")
    thickness = np.linalg.norm(locations - origins[ray_ids], axis=1)
    payload = {
        "samples": int(len(thickness)),
        "min_thickness": _round(float(thickness.min())),
        "median_thickness": _round(float(np.median(thickness))),
        "max_thickness": _round(float(thickness.max())),
        "note": "inward ray-cast estimate over sampled surface points",
    }
    return ProbeResult(
        probe_id="wall_thickness",
        target=path,
        outcome=json.dumps(payload, ensure_ascii=False),
        provenance=PROVENANCE_MESH,
        ok=True,
    )


_PLANE_NORMALS = {"xy": (0.0, 0.0, 1.0), "yz": (1.0, 0.0, 0.0), "zx": (0.0, 1.0, 0.0)}


def symmetry_probe(path: str, mesh: Any, plane: str) -> ProbeResult:
    """Mirror sampled points across the named plane through the centroid and
    measure how far the mirrored set falls from the original surface."""
    import json

    import numpy as np

    normal = np.array(_PLANE_NORMALS[plane])
    points = np.asarray(_sample_surface(mesh, _SAMPLE_POINTS), dtype=float)
    centroid = np.asarray(mesh.bounds, dtype=float).mean(axis=0)
    # Elementwise instead of matmul: Accelerate's matmul emits spurious
    # divide-by-zero RuntimeWarnings on small operands (macOS arm64).
    offsets = ((points - centroid) * normal).sum(axis=1)
    mirrored = points - 2.0 * offsets[:, None] * normal[None, :]
    deviation = _surface_distance(mesh, mirrored)
    diag = float(np.linalg.norm(mesh.bounds[1] - mesh.bounds[0]))
    payload = {
        "plane": plane,
        "max_deviation": _round(float(deviation.max())),
        "mean_deviation": _round(float(deviation.mean())),
        "bbox_diagonal": _round(diag),
        "symmetric_within_1pct_of_diagonal": bool(
            diag > 0 and deviation.max() <= 0.01 * diag
        ),
    }
    return ProbeResult(
        probe_id="symmetry",
        target=path,
        params={"plane": plane},
        outcome=json.dumps(payload, ensure_ascii=False),
        provenance=PROVENANCE_MESH,
        ok=True,
    )


def overhang_probe(path: str, mesh: Any, max_angle_deg: float) -> ProbeResult:
    """DFM overhang census for +Z builds: downward faces steeper than the limit."""
    import json

    import numpy as np

    # A downward face tilted β from vertical has normal z = -sin(β), so the
    # overhang test is nz < -sin(max_angle); cos is only equivalent at 45°.
    threshold = math.sin(math.radians(max_angle_deg))
    down = mesh.face_normals[:, 2]
    overhanging = down < -threshold
    areas = mesh.area_faces
    payload = {
        "build_direction": "+Z",
        "max_overhang_angle_deg": max_angle_deg,
        "overhanging_face_count": int(overhanging.sum()),
        "overhanging_area": _round(float(areas[overhanging].sum())),
        "overhanging_area_fraction": _round(
            float(areas[overhanging].sum() / areas.sum()) if areas.sum() else 0.0
        ),
        "lowest_face_normal_z": _round(float(np.min(down))) if len(down) else None,
    }
    return ProbeResult(
        probe_id="overhang_census",
        target=path,
        params={"max_angle_deg": max_angle_deg},
        outcome=json.dumps(payload, ensure_ascii=False),
        provenance=PROVENANCE_MESH,
        ok=True,
    )


# --- runner -----------------------------------------------------------------------


class ProbeRunner:
    """Runs probe callables under the per-probe timeout and total budget.

    Sync by design — the whole probe phase already runs inside one
    ``asyncio.to_thread``. A timed-out probe's thread is abandoned (OCCT work is
    not interruptible); the pool is not joined on shutdown so the grade never
    waits on it.
    """

    def __init__(
        self,
        total_budget_s: float = PROBE_TOTAL_BUDGET_S,
        per_probe_timeout_s: float = PER_PROBE_TIMEOUT_S,
    ) -> None:
        self._deadline = time.monotonic() + total_budget_s
        self._per_probe_timeout_s = per_probe_timeout_s
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=4)

    def remaining_s(self) -> float:
        return self._deadline - time.monotonic()

    def run(
        self,
        probe_id: str,
        target: str,
        fn: Any,
        params: dict[str, Any] | None = None,
    ) -> ProbeResult:
        remaining = self._deadline - time.monotonic()
        if remaining <= 0:
            return failed(
                probe_id, target, "total probe wall-clock budget exhausted", params
            )
        future = self._executor.submit(fn)
        try:
            result = future.result(timeout=min(remaining, self._per_probe_timeout_s))
        except concurrent.futures.TimeoutError:
            future.cancel()
            return failed(probe_id, target, "probe timed out", params)
        except Exception as e:
            return failed(probe_id, target, f"{type(e).__name__}: {e}", params)
        return result

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


def run_file_battery(
    store: GeometryStore,
    runner: ProbeRunner,
    path: str,
    *,
    mass: MassSettings | None = None,
) -> list[ProbeResult]:
    """One file's B-rep and mesh battery rows, plus its shape-graph issues."""
    entry = store.loaded(path)
    issue_rows = [failed(probe_id, path, reason) for probe_id, reason in entry.issues]
    if entry.error and not entry.solids and entry.mesh is None:
        issue_rows.append(failed("brep_battery", path, entry.error))
        if not entry.hidden_shapes:
            return issue_rows
    results: list[ProbeResult] = issue_rows
    if entry.solids and ocp_available():
        results.append(
            runner.run(
                "brep_battery",
                path,
                lambda e=entry: brep_battery(
                    e.path, e.solids, mass=mass, densities=e.densities
                ),
            )
        )
    results.append(
        runner.run(
            "mesh_battery",
            path,
            lambda p=path: (
                mesh_battery(store.mesh_label(p), m)
                if (m := store.mesh_for(p, include_hidden=True)) is not None
                else failed("mesh_battery", p, "no mesh could be built for this file")
            ),
        )
    )
    return results


def _capped_names(names: list[str]) -> str:
    extra = len(names) - 8
    return ", ".join(names[:8]) + (f" and {extra} more" if extra > 0 else "")


def pairwise_withheld_reason(store: GeometryStore, paths: Iterable[str]) -> str | None:
    """Why pairwise probes over parts of these files must not run, or None."""
    for path in sorted(set(paths)):
        for probe_id, reason in store.loaded(path).issues:
            if probe_id == "link_instances":
                return f"withheld: {reason}"
    return None


# (part id, shape, is_hidden) — hidden-ness rides as a boolean because a file
# path or object name may literally contain the mark text.
_Operand = tuple[str, Any, bool]
_Pair = tuple[_Operand, _Operand]

# Marks a battery interference row as debiting the hidden-pair cap, so budget
# accounting never has to infer hidden-ness from the mark text in a target.
HIDDEN_PAIR_PARAM = "hidden_pair"


def _pairwise_parts(store: GeometryStore, path: str) -> list[_Operand]:
    """Operands for pairwise probes: terminal solids plus hidden terminals
    under HIDDEN_MARK — hiding present geometry must not dodge a probe."""
    entry = store.loaded(path)
    return [
        *((f"{path}::{name}", shape, False) for name, shape in entry.solids),
        *(
            (f"{path}::{name}{HIDDEN_MARK}", shape, True)
            for name, shape in entry.hidden_shapes
        ),
    ]


def _split_hidden_pairs(pairs: list[_Pair]) -> tuple[list[_Pair], list[_Pair]]:
    """(visible-only pairs, pairs involving a hidden operand)."""
    visible = [p for p in pairs if not (p[0][2] or p[1][2])]
    hidden = [p for p in pairs if p[0][2] or p[1][2]]
    return visible, hidden


def _interference_rows(
    runner: ProbeRunner,
    pairs: list[_Pair],
    max_pairs: int,
    *,
    hidden: bool = False,
) -> list[ProbeResult]:
    params = {HIDDEN_PAIR_PARAM: True} if hidden else {}
    results: list[ProbeResult] = []
    for (id_a, shape_a, _), (id_b, shape_b, _) in pairs[: max(0, max_pairs)]:
        row = runner.run(
            "interference",
            f"{id_a} vs {id_b}",
            lambda a=shape_a, b=shape_b, ia=id_a, ib=id_b: interference_probe(
                ia, a, ib, b
            ),
            params=params,
        )
        results.append(replace(row, params={**row.params, **params}))
    return results


def run_intra_pairs(
    store: GeometryStore,
    runner: ProbeRunner,
    path: str,
    max_pairs: int = MAX_INTERFERENCE_PAIRS,
    max_hidden_pairs: int = MAX_HIDDEN_INTERFERENCE_PAIRS,
) -> list[ProbeResult]:
    """Pairwise interference between one file's terminal solids (hidden ones
    included under their mark, on their own cap)."""
    if not ocp_available():
        return []
    parts = _pairwise_parts(store, path)
    pairs = [
        (parts[i], parts[j])
        for i in range(len(parts))
        for j in range(i + 1, len(parts))
    ]
    if not pairs:
        return []
    # Links withhold (instance geometry is absent); hidden parts measure
    # under HIDDEN_MARK (geometry is present).
    if (reason := pairwise_withheld_reason(store, [path])) is not None:
        return [failed("interference", path, reason)]
    visible_pairs, hidden_pairs = _split_hidden_pairs(pairs)
    return [
        *_interference_rows(runner, visible_pairs, max_pairs),
        *_interference_rows(runner, hidden_pairs, max_hidden_pairs, hidden=True),
    ]


def run_cross_pairs(
    store: GeometryStore,
    runner: ProbeRunner,
    max_pairs: int = MAX_INTERFERENCE_PAIRS,
    max_hidden_pairs: int = MAX_HIDDEN_INTERFERENCE_PAIRS,
) -> list[ProbeResult]:
    """Pairwise interference between solids of DIFFERENT agent files, capped."""
    if not ocp_available():
        return []
    per_file: list[tuple[str, list[_Operand]]] = []
    withheld: list[tuple[str, str]] = []
    for path in store.agent_paths():
        parts = _pairwise_parts(store, path)
        if not parts:
            continue
        if (reason := pairwise_withheld_reason(store, [path])) is not None:
            withheld.append((path, reason))
        else:
            per_file.append((path, parts))
    results: list[ProbeResult] = []
    if withheld and (per_file or len(withheld) > 1):
        results.extend(
            failed("interference", path, reason) for path, reason in withheld
        )
    pairs = [
        (a, b)
        for i in range(len(per_file))
        for j in range(i + 1, len(per_file))
        for a in per_file[i][1]
        for b in per_file[j][1]
    ]
    visible_pairs, hidden_pairs = _split_hidden_pairs(pairs)
    results.extend(_interference_rows(runner, visible_pairs, max_pairs))
    results.extend(
        _interference_rows(runner, hidden_pairs, max_hidden_pairs, hidden=True)
    )
    return results


def run_golden_compares(
    store: GeometryStore,
    runner: ProbeRunner,
    path: str,
    max_comparisons: int = MAX_GOLDEN_COMPARISONS,
    *,
    orientation_tolerance_deg: float | None = None,
) -> list[ProbeResult]:
    """One agent file compared against each staged reference, capped.

    A comparison that cannot run (no agent mesh, no reference mesh, cap hit)
    is a visible failed row, never a silent absence: a scheduled golden check
    that vanished from the ledger would read as "nothing to compare". Not-run
    rows spend neither ``max_comparisons`` nor the caller's budget: an
    unmeshable golden (a DXF/Gerber reference with no probe mesh) must never
    starve a meshable one of its slot.
    """
    results: list[ProbeResult] = []
    if not store.reference_paths:
        return results
    if max_comparisons <= 0:
        return [failed("golden_compare", path, "golden comparison budget exhausted")]
    mesh = store.mesh_for(path, include_hidden=True)
    if mesh is None:
        return [failed("golden_compare", path, "no mesh could be built for this file")]
    shown = store.mesh_label(path)
    attempted = 0
    for reference_path in sorted(store.reference_paths):
        reference_mesh = store.mesh_for(reference_path, include_hidden=True)
        if reference_mesh is None:
            results.append(
                failed(
                    "golden_compare",
                    path,
                    "no mesh could be built for the reference",
                    {"reference": reference_path},
                )
            )
            continue
        if attempted >= max_comparisons:
            break
        attempted += 1
        identical = file_bytes_identical(store, path, reference_path)
        reference_shown = store.mesh_label(reference_path)
        results.append(
            runner.run(
                "golden_compare",
                path,
                lambda p=shown, m=mesh, rp=reference_shown, rm=reference_mesh, bi=identical: (
                    golden_compare_probe(
                        p,
                        m,
                        rp,
                        rm,
                        byte_identical=bi,
                        orientation_tolerance_deg=orientation_tolerance_deg,
                    )
                ),
                params={"reference": reference_path},
            )
        )
    return results


def file_bytes_identical(store: GeometryStore, path_a: str, path_b: str) -> bool:
    """Whether two staged files hold exactly the same bytes."""
    sha_a = store.sha256(path_a)
    return sha_a is not None and sha_a == store.sha256(path_b)


def run_standard_battery(
    store: GeometryStore, runner: ProbeRunner
) -> list[ProbeResult]:
    """The battery every run gets: per-file B-rep and mesh measurements, pairwise
    interference/clearance between solids, and golden comparison when references
    are staged. In-process composition of the per-file pieces the geometry
    worker runs one file at a time; the pair and comparison caps stay global."""
    results: list[ProbeResult] = []
    if not ocp_available():
        results.append(
            failed(
                "brep_battery",
                "*",
                "OCP (OCCT bindings) not available on this platform; B-rep "
                "measurements skipped",
            )
        )
    for path in store.agent_paths():
        results.extend(run_file_battery(store, runner, path))
    pair_budget = MAX_INTERFERENCE_PAIRS
    hidden_pair_budget = MAX_HIDDEN_INTERFERENCE_PAIRS
    for path in store.agent_paths():
        rows = run_intra_pairs(store, runner, path, pair_budget, hidden_pair_budget)
        hidden_rows = sum(1 for row in rows if row.params.get(HIDDEN_PAIR_PARAM))
        pair_budget -= len(rows) - hidden_rows
        hidden_pair_budget -= hidden_rows
        results.extend(rows)
    results.extend(run_cross_pairs(store, runner, pair_budget, hidden_pair_budget))
    comparison_budget = MAX_GOLDEN_COMPARISONS
    for path in store.agent_paths():
        rows = run_golden_compares(store, runner, path, comparison_budget)
        comparison_budget -= sum(
            1 for row in rows if golden_compare_spent_budget(row.outcome)
        )
        results.extend(rows)
    return results

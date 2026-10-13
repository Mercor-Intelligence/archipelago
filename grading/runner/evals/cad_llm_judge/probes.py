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
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass, field
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

# Wall-clock ceilings. A probe that starts cannot be preempted (OCCT calls are
# opaque C++), so the per-probe timeout abandons the worker thread and the total
# budget stops new probes from starting; both bound the grade, not the work.
PROBE_TOTAL_BUDGET_S = 300.0
PER_PROBE_TIMEOUT_S = 60.0

# Pairwise checks are quadratic in solids; the cap keeps a 50-part assembly from
# eating the whole probe budget before the planner's own requests run.
MAX_INTERFERENCE_PAIRS = 16
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
        self._mesh_cache: dict[str, Any] = {}
        self.reference_paths: set[str] = set()
        # Child-reported part names per path. When set, this store never loads
        # geometry itself: part lookups answer from the inventory so the parent
        # process stays free of GIL-holding kernel work.
        self.part_inventory: dict[str, list[str]] | None = None
        self.feature_inventory: dict[str, list[str]] = {}

    def set_inventory(
        self, parts: dict[str, list[str]], features: dict[str, list[str]]
    ) -> None:
        self.part_inventory = parts
        self.feature_inventory = features

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
            entry.solids, entry.feature_shapes = _load_fcstd_breps(
                entry.disk_path, self._root
            )
            if not entry.solids:
                entry.error = "no readable .brp B-rep members in the FCStd archive"
            return
        if suffix in _BREP_FORMATS:
            entry.solids = _load_brep_file(entry.disk_path, suffix)
            if not entry.solids:
                entry.error = "the OCCT reader produced no shape"
            return
        entry.error = f"format {suffix or 'unknown'} has no probe loader"

    def mesh_for(self, path: str) -> Any | None:
        """A trimesh mesh for ``path``: native for STL, tessellated for B-reps."""
        if path in self._mesh_cache:
            return self._mesh_cache[path]
        entry = self.loaded(path)
        mesh = entry.mesh
        if mesh is None and entry.solids:
            meshes = [m for _, s in entry.solids if (m := tessellate(s)) is not None]
            if meshes:
                import trimesh

                mesh = (
                    meshes[0] if len(meshes) == 1 else trimesh.util.concatenate(meshes)
                )
        self._mesh_cache[path] = mesh
        return mesh

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
        return part_id.removesuffix(FEATURE_HISTORY_MARK).split("::", 1)[0]

    def resolve_part(self, name: str) -> tuple[str, str, Any] | None:
        """(file path, part name, shape) for an exact/suffix part reference.

        A bare part name matching several parts is ambiguous and resolves to
        None rather than an arbitrary solid. Feature-history shapes resolve
        only on an exact unambiguous name and come back with their part name
        carrying FEATURE_HISTORY_MARK, so every downstream label declares them.
        Reference-file parts resolve ONLY by their exact ``path::part`` id: a
        golden reusing an agent part name (Body) must not make the bare name
        match golden geometry, or read the agent's own name as ambiguous.
        """
        name = name.removesuffix(FEATURE_HISTORY_MARK)
        wanted_file, _, wanted_part = name.partition("::")
        matches: list[tuple[str, str, Any]] = []
        feature_matches: list[tuple[str, str, Any]] = []
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
            for part_name, shape in entry.feature_shapes:
                if name == f"{path}::{part_name}" or (
                    name == part_name and not is_reference
                ):
                    feature_matches.append(
                        (path, f"{part_name}{FEATURE_HISTORY_MARK}", shape)
                    )
        if len(matches) == 1:
            return matches[0]
        if not matches and len(feature_matches) == 1:
            return feature_matches[0]
        return None

    def resolve_part_id(self, name: str) -> str | None:
        """A ``<file path>::<part>`` id for a planner part reference, or None.

        Inventory-backed when set (no geometry loads in the parent); otherwise
        resolves live. Feature-history ids keep FEATURE_HISTORY_MARK so the
        executing child re-resolves and labels them the same way. Same rule as
        resolve_part: reference-file parts match ONLY their exact id.
        """
        if self.part_inventory is None:
            resolved = self.resolve_part(name)
            return f"{resolved[0]}::{resolved[1]}" if resolved else None
        bare = name.removesuffix(FEATURE_HISTORY_MARK)
        wanted_file, _, wanted_part = bare.partition("::")
        matches: list[str] = []
        feature_matches: list[str] = []
        for inventory, marked, sink in (
            (self.part_inventory, False, matches),
            (self.feature_inventory, True, feature_matches),
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
                        sink.append(f"{pid}{FEATURE_HISTORY_MARK}" if marked else pid)
        if len(matches) == 1:
            return matches[0]
        if not matches and len(feature_matches) == 1:
            return feature_matches[0]
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


def _fcstd_shape_graph(
    document_xml: bytes,
) -> tuple[dict[str, str], set[str]] | None:
    """(``.brp`` member -> object name, terminal member set) from Document.xml.

    FreeCAD writes one ``.brp`` per FEATURE (Body, Sketch, Pad, Fillet...), not
    per physical part. An object's shape is a real part only when no other
    shape-carrying object links to it (a Body's tip, the last boolean, an
    independent solid); everything consumed downstream is feature history.
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
            # ObjectData entries carry a name but no type attribute.
            if name and not elem.get("type"):
                for prop in elem.iter("Property"):
                    if prop.get("name") == "Shape":
                        for part in prop.iter("Part"):
                            file_attr = part.get("file")
                            if file_attr and name not in member_by_object:
                                member_by_object[name] = file_attr
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
        if owner != target and owner in member_by_object and target in member_by_object
    }
    object_by_member = {member: name for name, member in member_by_object.items()}
    terminal_members = {
        member for member, name in object_by_member.items() if name not in consumed
    }
    return object_by_member, terminal_members


def _load_fcstd_breps(
    disk_path: Path, scratch: Path
) -> tuple[list[tuple[str, Any]], list[tuple[str, Any]]]:
    """(terminal solids, feature-history shapes) from an FCStd's ``.brp`` members.

    Without a usable Document.xml graph every member loads as a solid under its
    member stem (the pre-graph behavior); the same fallback applies when no
    terminal member survives loading, so a wrong graph degrades to the old
    over-count rather than to an empty file.
    """
    solids: list[tuple[str, Any]] = []
    feature_shapes: list[tuple[str, Any]] = []
    with zipfile.ZipFile(BytesIO(disk_path.read_bytes())) as zf:
        members = _safe_members(zf)
        brp_names = sorted(
            info.filename
            for info in members
            if info.filename.lower().endswith(".brp") and not info.is_dir()
        )
        graph: tuple[dict[str, str], set[str]] | None = None
        if any(info.filename == "Document.xml" for info in members):
            try:
                graph = _fcstd_shape_graph(_read_member_bytes(zf, "Document.xml"))
            except CadParseError:
                graph = None
        if graph is not None and not (set(graph[0]) & set(brp_names)):
            graph = None
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
                solids.extend((stem, shape) for _part_name, shape in loaded)
                continue
            object_by_member, terminal_members = graph
            # Members no object's Shape property claims are orphans; treat them
            # as feature history rather than inventing a part.
            part_name = object_by_member.get(name, PurePosixPath(name).stem)
            bucket = solids if name in terminal_members else feature_shapes
            bucket.extend((part_name, shape) for _part_name, shape in loaded)
    if graph is not None and not solids and feature_shapes:
        return feature_shapes, []
    return solids, feature_shapes


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


def brep_battery(path: str, solids: list[tuple[str, Any]]) -> ProbeResult:
    detailed = len(solids) <= MAX_BATTERY_PARTS_DETAILED
    parts: list[dict[str, Any]] = []
    for name, shape in solids:
        entry: dict[str, Any] = {"part": name}
        entry.update(_mass_properties(shape))
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
        payload = {
            "parts_summary": _battery_summary(parts),
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

    sampled: Any = trimesh.sample.sample_surface(mesh, count)
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


def _rotation_summary(matrix: Any) -> tuple[float, list[float] | None]:
    """(angle in degrees, unit axis) of a transform's rotation part."""
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
    angle_deg = math.degrees(abs(float(angle)))
    if angle_deg < 0.5:
        return 0.0, None
    if float(angle) < 0:
        direction = -direction
    return angle_deg, [_round(float(v), 4) for v in direction]


def golden_compare_probe(
    path: str, mesh: Any, reference_path: str, reference_mesh: Any
) -> ProbeResult:
    """Best-fit the candidate to the reference, then Hausdorff + property deltas.

    Shape congruence is measured orientation-invariantly: ICP runs from
    multiple candidate initial rotations (centroid-only initialization left a
    part modeled 90 deg off its reference stuck in a local minimum reading as a
    large Hausdorff), and the rotation the winning alignment applied is
    reported so orientation itself stays judgeable.
    """
    import json

    import numpy as np
    import trimesh

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
    for start in candidates:
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
        moved = mesh.copy()
        moved.apply_transform(np.asarray(matrix))
        forward = _surface_distance(reference_mesh, aligned)
        backward = _surface_distance(moved, points_b)
        hausdorff = float(max(forward.max(), backward.max()))
        mean_dev = float((forward.mean() + backward.mean()) / 2.0)
        if best is None or hausdorff < best[0]:
            best = (hausdorff, mean_dev, matrix, moved, aligned)
    assert best is not None
    hausdorff, mean_dev, matrix, moved, _aligned = best
    diag = float(np.linalg.norm(reference_mesh.bounds[1] - reference_mesh.bounds[0]))
    rotation_deg, rotation_axis = _rotation_summary(matrix)

    def _wt_volume(m: Any) -> float | None:
        return _round(m.volume) if m.is_watertight else None

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
                f"~{rotation_deg:.0f} deg about axis {rotation_axis} to best "
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

    sampled: Any = trimesh.sample.sample_surface(mesh, _THICKNESS_RAYS)
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
    store: GeometryStore, runner: ProbeRunner, path: str
) -> list[ProbeResult]:
    """One file's B-rep and mesh battery rows."""
    entry = store.loaded(path)
    if entry.error and not entry.solids and entry.mesh is None:
        return [failed("brep_battery", path, entry.error)]
    results: list[ProbeResult] = []
    if entry.solids and ocp_available():
        results.append(
            runner.run(
                "brep_battery", path, lambda e=entry: brep_battery(e.path, e.solids)
            )
        )
    results.append(
        runner.run(
            "mesh_battery",
            path,
            lambda p=path: (
                mesh_battery(p, m)
                if (m := store.mesh_for(p)) is not None
                else failed("mesh_battery", p, "no mesh could be built for this file")
            ),
        )
    )
    return results


def _interference_rows(
    runner: ProbeRunner,
    pairs: list[tuple[tuple[str, Any], tuple[str, Any]]],
    max_pairs: int,
) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    for (id_a, shape_a), (id_b, shape_b) in pairs[: max(0, max_pairs)]:
        results.append(
            runner.run(
                "interference",
                f"{id_a} vs {id_b}",
                lambda a=shape_a, b=shape_b, ia=id_a, ib=id_b: interference_probe(
                    ia, a, ib, b
                ),
            )
        )
    return results


def run_intra_pairs(
    store: GeometryStore,
    runner: ProbeRunner,
    path: str,
    max_pairs: int = MAX_INTERFERENCE_PAIRS,
) -> list[ProbeResult]:
    """Pairwise interference between one file's terminal solids, capped."""
    if not ocp_available():
        return []
    parts = [(f"{path}::{name}", shape) for name, shape in store.loaded(path).solids]
    pairs = [
        (parts[i], parts[j])
        for i in range(len(parts))
        for j in range(i + 1, len(parts))
    ]
    return _interference_rows(runner, pairs, max_pairs)


def run_cross_pairs(
    store: GeometryStore,
    runner: ProbeRunner,
    max_pairs: int = MAX_INTERFERENCE_PAIRS,
) -> list[ProbeResult]:
    """Pairwise interference between solids of DIFFERENT agent files, capped."""
    if not ocp_available():
        return []
    per_file = [
        [(f"{path}::{name}", shape) for name, shape in store.loaded(path).solids]
        for path in store.agent_paths()
    ]
    pairs = [
        (a, b)
        for i in range(len(per_file))
        for j in range(i + 1, len(per_file))
        for a in per_file[i]
        for b in per_file[j]
    ]
    return _interference_rows(runner, pairs, max_pairs)


def run_golden_compares(
    store: GeometryStore,
    runner: ProbeRunner,
    path: str,
    max_comparisons: int = MAX_GOLDEN_COMPARISONS,
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
    mesh = store.mesh_for(path)
    if mesh is None:
        return [failed("golden_compare", path, "no mesh could be built for this file")]
    attempted = 0
    for reference_path in sorted(store.reference_paths):
        reference_mesh = store.mesh_for(reference_path)
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
        results.append(
            runner.run(
                "golden_compare",
                path,
                lambda p=path, m=mesh, rp=reference_path, rm=reference_mesh: (
                    golden_compare_probe(p, m, rp, rm)
                ),
                params={"reference": reference_path},
            )
        )
    return results


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
    for path in store.agent_paths():
        rows = run_intra_pairs(store, runner, path, pair_budget)
        pair_budget -= len(rows)
        results.extend(rows)
    results.extend(run_cross_pairs(store, runner, pair_budget))
    comparison_budget = MAX_GOLDEN_COMPARISONS
    for path in store.agent_paths():
        rows = run_golden_compares(store, runner, path, comparison_budget)
        comparison_budget -= sum(
            1 for row in rows if golden_compare_spent_budget(row.outcome)
        )
        results.extend(rows)
    return results

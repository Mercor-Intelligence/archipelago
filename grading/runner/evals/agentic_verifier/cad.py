"""Pure-Python CAD readers for the agentic verifier's `read_cad_document`.

Every parser here reads bytes and returns text. None of them runs a geometry
kernel: there is no recompute, no `Shape.isValid()`, no boolean volume of a
B-rep. What CAN be extracted without a kernel is still most of what a grader
reads first — the FCStd feature tree and parameters, a STEP file's analytic
surfaces with their radii, a mesh's measured volume and watertightness — and
each digest says which kind of fact it is, so a judge cites "parsed from the
feature tree" rather than passing it off as a kernel measurement.

Format notes, and why no dependency is needed:
- FCStd is a ZIP holding `Document.xml` (the full parametric feature tree)
  beside per-object `.brp` B-reps. `zipfile` + `xml.etree` read the tree; the
  `.brp` members are listed but not parsed.
- STEP and IGES are ASCII (ISO-10303-21 / IGES 5.x). Entity records carry
  analytic surfaces (cylinders, tori, planes) with literal parameters.
- STL is either ASCII or a fixed-layout binary of triangles. A closed mesh's
  volume, area and centroid follow from the divergence theorem, and
  watertightness from every edge being shared by exactly two triangles.
- DXF, Gerber and Excellon drill files are line-oriented ASCII; layer,
  entity, aperture and drill-hit counts are cheap.

All input is untrusted — it was written by the agent under grading — so the
ZIP path re-applies the snapshot extractor's discipline (no absolute names,
no `..`, member and total decompression ceilings) and every text parse is
size-capped before reading.
"""

from __future__ import annotations

import json
import re
import struct
import xml.etree.ElementTree as ET
import zipfile
import zlib
from collections import Counter
from collections.abc import Iterator
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any

# Bounds live at module scope so tests can pin them.
MAX_CAD_FILE_BYTES = 256 * 1024 * 1024
MAX_FCSTD_MEMBER_BYTES = 64 * 1024 * 1024
MAX_FCSTD_TOTAL_BYTES = 256 * 1024 * 1024
MAX_FCSTD_MEMBERS = 4_096
# Caps peak parse memory: the STL directed-edge map costs hundreds of bytes
# per triangle, and several grading sessions share one worker.
MAX_STL_TRIANGLES = 1_000_000
# Ceiling on parsed XML elements in an FCStd Document.xml. Bytes on disk do not
# bound an ElementTree: the file is almost entirely small attribute-bearing
# elements, each a Python object with its own attrib dict, so a member at the
# byte cap materialises several times that in live objects — on a worker shared
# by other grading sessions. Streamed with `iterparse` and cleared per object,
# this ceiling bounds what is ever live at once (~100 bytes per element shell).
MAX_XML_ELEMENTS = 500_000
MAX_TEXT_PARSE_BYTES = 128 * 1024 * 1024
# Per-object property cap in the FCStd digest. A PartDesign body carries a few
# dozen properties; hundreds means generated noise the judge should sample via
# `tree` mode instead of paging through the digest.
MAX_PROPERTIES_PER_OBJECT = 64
MAX_DIGEST_LIST_ITEMS = 200

STEP_EXTENSIONS = frozenset({".step", ".stp"})
IGES_EXTENSIONS = frozenset({".iges", ".igs"})
STL_EXTENSIONS = frozenset({".stl"})
FCSTD_EXTENSIONS = frozenset({".fcstd"})
DXF_EXTENSIONS = frozenset({".dxf"})
GERBER_EXTENSIONS = frozenset(
    {".gbr", ".ger", ".gtl", ".gbl", ".gts", ".gbs", ".gto", ".gbo"}
)
# Excellon drill programs, a distinct command language from Gerber.
DRILL_EXTENSIONS = frozenset({".drl"})

CAD_EXTENSIONS = frozenset(
    STEP_EXTENSIONS
    | IGES_EXTENSIONS
    | STL_EXTENSIONS
    | FCSTD_EXTENSIONS
    | DXF_EXTENSIONS
    | GERBER_EXTENSIONS
    | DRILL_EXTENSIONS
)

# Facts a kernel would establish that no parser here can. Stated once and
# appended to every digest, so a judge grading a criterion that needs one of
# these reports "requires kernel" instead of inferring it from the parse.
KERNEL_ONLY_NOTE = (
    "Not established by this digest (requires a CAD kernel): recompute success, "
    "Shape.isValid(), boolean solid volume/area of a B-rep, interference or "
    "clearance, and whether editing a parameter rebuilds the geometry."
)


def needs_cad_reader(file_name: str) -> bool:
    """Whether only `read_cad_document` can usefully open this file.

    Narrower than membership in `CAD_EXTENSIONS`: STEP, IGES, DXF and Gerber
    are ASCII, so a harness's native reader shows their raw text — expensive
    but not blind. FCStd is a ZIP and binary STL is a struct layout; a
    general-purpose reader returns noise for both, exactly the situation
    `needs_document_reader` names for docx.
    """
    suffix = PurePosixPath(file_name).suffix.lower()
    return suffix in FCSTD_EXTENSIONS or suffix in STL_EXTENSIONS


def is_cad_file(file_name: str) -> bool:
    return PurePosixPath(file_name).suffix.lower() in CAD_EXTENSIONS


def read_cad_bytes(data: bytes, file_name: str, mode: str) -> str:
    """Digest ``data`` as the CAD format its extension names.

    ``mode`` is one of ``digest`` (structured summary), ``tree`` (the FCStd
    ``Document.xml`` verbatim; digest for other formats, which have no tree),
    or ``raw`` (the file's own text for ASCII formats). Windowing/offset is the
    caller's job — this module returns the whole text and `read_file`'s
    machinery bounds what one tool reply carries.
    """
    if len(data) > MAX_CAD_FILE_BYTES:
        return (
            f"{file_name} is {len(data):,} bytes, over the "
            f"{MAX_CAD_FILE_BYTES:,}-byte cap for CAD parsing."
        )
    suffix = PurePosixPath(file_name).suffix.lower()
    try:
        if suffix in FCSTD_EXTENSIONS:
            return _read_fcstd(data, file_name, mode)
        if suffix in STL_EXTENSIONS:
            if mode == "raw" and _is_ascii_stl(data):
                return _decode_text(data)
            # Binary STL has no text form, so raw falls back to the digest.
            return _render(_stl_digest(data, file_name))
        if suffix in STEP_EXTENSIONS:
            return _read_ascii(data, file_name, mode, _step_digest)
        if suffix in IGES_EXTENSIONS:
            return _read_ascii(data, file_name, mode, _iges_digest)
        if suffix in DXF_EXTENSIONS:
            return _read_ascii(data, file_name, mode, _dxf_digest)
        if suffix in GERBER_EXTENSIONS:
            return _read_ascii(data, file_name, mode, _gerber_digest)
        if suffix in DRILL_EXTENSIONS:
            return _read_ascii(data, file_name, mode, _excellon_digest)
    except CadParseError as e:
        return f"Could not parse {file_name}: {e}"
    except (ValueError, zlib.error, zipfile.BadZipFile) as e:
        # Malformed numerics or corrupt archive bytes are a parse failure the
        # judge should see, never a failed tool call.
        return f"Could not parse {file_name}: {e}"
    return (
        f"{file_name} is not a CAD format this tool reads. It opens: "
        f"{', '.join(sorted(CAD_EXTENSIONS))}."
    )


class CadParseError(Exception):
    """A parse failure the judge should see verbatim."""


def _render(digest: dict[str, Any]) -> str:
    digest["kernel_only"] = KERNEL_ONLY_NOTE
    return json.dumps(digest, indent=2, ensure_ascii=False)


def _decode_text(data: bytes) -> str:
    if len(data) > MAX_TEXT_PARSE_BYTES:
        raise CadParseError(
            f"{len(data):,} bytes exceeds the {MAX_TEXT_PARSE_BYTES:,}-byte text cap"
        )
    return data.decode("utf-8", errors="replace")


def _read_ascii(data: bytes, file_name: str, mode: str, digester: Any) -> str:
    text = _decode_text(data)
    if mode == "raw":
        return text
    return _render(digester(text, file_name))


# --- FCStd -------------------------------------------------------------------


def _safe_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    """The archive's members, with the snapshot extractor's refusals applied.

    The size checks here read the header-declared sizes, which the archive's
    author controls; any member actually decompressed must also go through
    `_read_member_bytes`, which counts real bytes against the same cap.
    """
    members = zf.infolist()
    if len(members) > MAX_FCSTD_MEMBERS:
        raise CadParseError(
            f"archive lists {len(members)} members (cap {MAX_FCSTD_MEMBERS})"
        )
    total = 0
    for info in members:
        name = info.filename
        if name.startswith(("/", "\\")) or ".." in PurePosixPath(name).parts:
            raise CadParseError(f"archive member escapes the archive: {name!r}")
        if info.file_size > MAX_FCSTD_MEMBER_BYTES:
            raise CadParseError(
                f"member {name!r} decompresses to {info.file_size:,} bytes "
                f"(cap {MAX_FCSTD_MEMBER_BYTES:,})"
            )
        total += info.file_size
    if total > MAX_FCSTD_TOTAL_BYTES:
        raise CadParseError(
            f"archive decompresses to {total:,} bytes (cap {MAX_FCSTD_TOTAL_BYTES:,})"
        )
    return members


def _read_member_bytes(zf: zipfile.ZipFile, name: str) -> bytes:
    """One member's content, counted as bytes leave the decompressor.

    The header-declared size already passed `_safe_members`; counting real
    decompressed bytes here means a lying header still cannot expand past the
    member cap.
    """
    out = bytearray()
    try:
        with zf.open(name) as src:
            while True:
                chunk = src.read(1 << 20)
                if not chunk:
                    return bytes(out)
                out += chunk
                if len(out) > MAX_FCSTD_MEMBER_BYTES:
                    raise CadParseError(
                        f"member {name!r} decompresses past the "
                        f"{MAX_FCSTD_MEMBER_BYTES:,}-byte member cap"
                    )
    except (RuntimeError, NotImplementedError) as e:
        # zipfile raises RuntimeError for an encrypted member and
        # NotImplementedError for a compression method Python cannot read.
        # Uncaught, either escapes the tool and errors the whole grading run.
        raise CadParseError(f"member {name!r} is unreadable: {e}") from e


def _read_fcstd(data: bytes, file_name: str, mode: str) -> str:
    try:
        zf = zipfile.ZipFile(BytesIO(data))
    except zipfile.BadZipFile as e:
        raise CadParseError(f"not a ZIP archive (FCStd files are): {e}") from e
    with zf:
        members = _safe_members(zf)
        names = {info.filename for info in members}
        if "Document.xml" not in names:
            raise CadParseError(
                "no Document.xml member — a valid FCStd always carries one"
            )
        document_xml = _read_member_bytes(zf, "Document.xml")
        if mode == "tree" or mode == "raw":
            # The tree IS the raw content that matters; both modes return it.
            return _decode_text(document_xml)
        digest = _fcstd_digest(document_xml, file_name)
        digest["archive_members"] = [
            {"name": info.filename, "bytes": info.file_size}
            for info in members[:MAX_DIGEST_LIST_ITEMS]
        ]
        digest["brep_members"] = sorted(n for n in names if n.lower().endswith(".brp"))[
            :MAX_DIGEST_LIST_ITEMS
        ]
        return _render(digest)


def _fcstd_digest(document_xml: bytes, file_name: str) -> dict[str, Any]:
    """The feature-tree digest, streamed so the byte cap is also a memory cap.

    `iterparse` with end events: each complete ``Object`` subtree is read with
    the same lookups a full tree would allow, then cleared, so peak live memory
    is one object's subtree plus cleared shells — bounded by
    ``MAX_XML_ELEMENTS``, not by the member's size on disk.
    """
    # Before any parse: `iterparse` expands internal DTD entities ahead of the
    # first element event, so an entity bomb allocates past both caps before
    # either can fire. FreeCAD never writes a DOCTYPE, so one is refused, not
    # parsed — and the byte search only sees ASCII-compatible text, so a
    # UTF-16 document (BOM, or NULs in the prolog) is refused outright rather
    # than parsed around the check. FreeCAD always writes UTF-8.
    if document_xml[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in document_xml[:256]:
        raise CadParseError(
            "Document.xml is not UTF-8, which FreeCAD always writes; refusing "
            "to parse it (mode='tree' still returns the raw text)"
        )
    if b"<!DOCTYPE" in document_xml:
        raise CadParseError(
            "Document.xml declares a DTD, which FreeCAD never writes; refusing "
            "to parse it (mode='tree' still returns the raw text)"
        )
    declared: dict[str, str] = {}
    data_by_name: dict[str, dict[str, Any]] = {}
    dependencies: list[tuple[str, str]] = []
    elements = 0
    try:
        for event, elem in ET.iterparse(BytesIO(document_xml), events=("start", "end")):
            if event == "start":
                # Counted at start, not end: an element is live from its
                # opening tag, and a deeply nested document fires no end event
                # until the innermost closes — end-only counting would let it
                # build the whole stack before the ceiling ever moved.
                elements += 1
                if elements > MAX_XML_ELEMENTS:
                    raise CadParseError(
                        f"Document.xml exceeds {MAX_XML_ELEMENTS:,} XML "
                        "elements; use mode='tree' with offset paging to "
                        "inspect it raw"
                    )
                continue
            if elem.tag != "Object":
                continue
            name = elem.get("name")
            obj_type = elem.get("type")
            if name and obj_type:
                declared[name] = obj_type
            elif name:
                props: dict[str, Any] = {}
                for prop in elem.iter("Property"):
                    prop_name = prop.get("name")
                    if not prop_name or len(props) >= MAX_PROPERTIES_PER_OBJECT:
                        continue
                    value = _property_value(prop)
                    if value is not None:
                        props[prop_name] = value
                    # Every Link descendant, so LinkList/LinkSub nestings count.
                    for link in prop.iter("Link"):
                        if link.get("value"):
                            dependencies.append((name, str(link.get("value"))))
                data_by_name[name] = props
            elem.clear()
    except ET.ParseError as e:
        raise CadParseError(f"Document.xml does not parse: {e}") from e

    objects = [
        {"name": name, "type": obj_type, "properties": data_by_name.get(name, {})}
        for name, obj_type in declared.items()
    ]

    return {
        "format": "FCStd",
        "file": file_name,
        "object_count": len(declared),
        "objects": objects[:MAX_DIGEST_LIST_ITEMS],
        "dependencies": [
            {"object": a, "links_to": b}
            for a, b in dependencies[:MAX_DIGEST_LIST_ITEMS]
        ],
        "evidence": (
            "Parsed from the parametric feature tree (Document.xml). These are "
            "the document's declared parameters and links, not measured "
            "geometry."
        ),
    }


def _property_value(prop: ET.Element) -> Any:
    """One Property element's scalar payload, or None when it has none.

    Typed by the child tag FreeCAD wrote, not by what the text happens to
    parse as: a String like "001" stays a string, a Bool becomes a boolean.
    """
    for child in prop:
        value = child.get("value")
        if value is None:
            continue
        if child.tag == "Bool":
            return value == "true"
        if child.tag in ("Integer", "Float"):
            for cast in (int, float):
                try:
                    return cast(value)
                except ValueError:
                    continue
        return value
    return None


# --- STEP --------------------------------------------------------------------

# Anchored on the record separator, not the line: ISO-10303-21 allows several
# `#id = TYPE(...)` records per line, and a line-anchored histogram undercounts
# compact files while the unanchored surface regexes still match.
_STEP_ENTITY = re.compile(r"(?:^|;)\s*#\d+\s*=\s*([A-Z0-9_]+)\s*\(", re.MULTILINE)
# The name is optional in ISO-10303-21 and real CATIA/SolidWorks/NX exports
# write it as `$` rather than an empty quote, so the pattern accepts either.
_NAME = r"(?:'[^']*'|\$)"
_STEP_CYLINDER = re.compile(
    rf"CYLINDRICAL_SURFACE\s*\(\s*{_NAME}\s*,\s*#\d+\s*,\s*([0-9.Ee+-]+)\s*\)"
)
_STEP_TORUS = re.compile(
    rf"TOROIDAL_SURFACE\s*\(\s*{_NAME}\s*,\s*#\d+\s*,\s*([0-9.Ee+-]+)\s*,\s*([0-9.Ee+-]+)\s*\)"
)
_STEP_POINT = re.compile(
    rf"CARTESIAN_POINT\s*\(\s*{_NAME}\s*,\s*\(\s*([0-9.Ee+-]+)\s*,\s*([0-9.Ee+-]+)\s*,\s*([0-9.Ee+-]+)\s*\)\s*\)"
)
_STEP_PRODUCT = re.compile(r"\bPRODUCT\s*\(\s*'([^']*)'")


def _step_digest(text: str, file_name: str) -> dict[str, Any]:
    if "ISO-10303-21" not in text[:4096]:
        raise CadParseError("missing ISO-10303-21 header — not a STEP part-21 file")
    entity_counts = Counter(_STEP_ENTITY.findall(text))
    cylinders = sorted({round(float(r), 6) for r in _STEP_CYLINDER.findall(text)})
    tori = sorted(
        {
            (round(float(major), 6), round(float(minor), 6))
            for major, minor in _STEP_TORUS.findall(text)
        }
    )
    bbox = None
    xs: list[float] = []
    ys: list[float] = []
    zs: list[float] = []
    for x, y, z in _STEP_POINT.findall(text):
        xs.append(float(x))
        ys.append(float(y))
        zs.append(float(z))
    if xs:
        bbox = {
            "min": [min(xs), min(ys), min(zs)],
            "max": [max(xs), max(ys), max(zs)],
            "span": [max(xs) - min(xs), max(ys) - min(ys), max(zs) - min(zs)],
        }
    return {
        "format": "STEP",
        "file": file_name,
        "products": _STEP_PRODUCT.findall(text)[:MAX_DIGEST_LIST_ITEMS],
        "entity_counts": dict(entity_counts.most_common(MAX_DIGEST_LIST_ITEMS)),
        "cylindrical_surface_radii": cylinders[:MAX_DIGEST_LIST_ITEMS],
        "toroidal_surfaces_major_minor": [list(t) for t in tori][
            :MAX_DIGEST_LIST_ITEMS
        ],
        "cartesian_point_count": len(xs),
        "bbox_of_cartesian_points": bbox,
        "solid_entities": {
            name: entity_counts[name]
            for name in ("MANIFOLD_SOLID_BREP", "BREP_WITH_VOIDS", "CLOSED_SHELL")
            if name in entity_counts
        },
        "evidence": (
            "Parsed from the STEP entity records. Radii and the point bounding "
            "box are literal values from the file; the bbox spans control "
            "points, not evaluated geometry."
        ),
    }


# --- IGES --------------------------------------------------------------------

# IGES 5.x directory-entry section: entity type number in columns 1-8 of each
# odd D-section line.
_IGES_TYPE_NAMES = {
    100: "CIRCULAR_ARC",
    102: "COMPOSITE_CURVE",
    108: "PLANE",
    110: "LINE",
    112: "PARAMETRIC_SPLINE_CURVE",
    114: "PARAMETRIC_SPLINE_SURFACE",
    116: "POINT",
    120: "SURFACE_OF_REVOLUTION",
    122: "TABULATED_CYLINDER",
    124: "TRANSFORMATION_MATRIX",
    126: "RATIONAL_BSPLINE_CURVE",
    128: "RATIONAL_BSPLINE_SURFACE",
    142: "CURVE_ON_PARAMETRIC_SURFACE",
    144: "TRIMMED_PARAMETRIC_SURFACE",
    186: "MANIFOLD_SOLID_BREP",
    314: "COLOR",
    402: "ASSOCIATIVITY_INSTANCE",
    406: "PROPERTY",
    408: "SINGULAR_SUBFIGURE_INSTANCE",
    510: "FACE",
    514: "SHELL",
}


def _iges_digest(text: str, file_name: str) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    d_lines = 0
    for line in text.splitlines():
        if len(line) >= 73 and line[72] == "D":
            d_lines += 1
            if d_lines % 2 == 1:  # each entity spans two D lines
                try:
                    type_number = int(line[:8])
                except ValueError:
                    continue
                counts[_IGES_TYPE_NAMES.get(type_number, f"TYPE_{type_number}")] += 1
    if not d_lines:
        raise CadParseError("no directory-entry (D) section — not an IGES file")
    return {
        "format": "IGES",
        "file": file_name,
        "entity_counts": dict(counts.most_common(MAX_DIGEST_LIST_ITEMS)),
        "evidence": "Entity histogram from the IGES directory section.",
    }


# --- STL ---------------------------------------------------------------------


_Triangle = tuple[tuple[float, float, float], ...]


def _stl_digest(data: bytes, file_name: str) -> dict[str, Any]:
    # Single streaming pass: triangles are never retained, and half-edges are
    # packed bytes rather than tuple keys, because the edge map is what
    # dominates parse memory.
    triangle_count = 0
    inf = float("inf")
    mins = [inf, inf, inf]
    maxs = [-inf, -inf, -inf]
    volume6 = 0.0
    area2 = 0.0
    cx = cy = cz = 0.0
    edges: Counter[bytes] = Counter()
    for a, b, c in _stl_triangles(data):
        triangle_count += 1
        for v in (a, b, c):
            for axis in range(3):
                if v[axis] < mins[axis]:
                    mins[axis] = v[axis]
                if v[axis] > maxs[axis]:
                    maxs[axis] = v[axis]
        det = (
            a[0] * (b[1] * c[2] - b[2] * c[1])
            - a[1] * (b[0] * c[2] - b[2] * c[0])
            + a[2] * (b[0] * c[1] - b[1] * c[0])
        )
        volume6 += det
        cx += det * (a[0] + b[0] + c[0])
        cy += det * (a[1] + b[1] + c[1])
        cz += det * (a[2] + b[2] + c[2])
        ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
        vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
        nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
        area2 += (nx * nx + ny * ny + nz * nz) ** 0.5
        for p, q in ((a, b), (b, c), (c, a)):
            edges[struct.pack("<6d", *p, *q)] += 1
    if not triangle_count:
        raise CadParseError("no triangles")

    # Watertight: every undirected edge is shared by exactly two triangles.
    # Consistently oriented: each shared edge is traversed once per direction.
    # Both are needed before the divergence-theorem sums mean anything — a
    # flipped face keeps a mesh watertight while corrupting the sums.
    watertight = True
    oriented = True
    non_manifold = 0
    for key, count in edges.items():
        reverse = key[24:] + key[:24]
        if reverse == key:
            # A zero-length edge (both endpoints equal) is its own reverse and
            # would count itself twice; degenerate geometry is never manifold.
            watertight = False
            oriented = False
            non_manifold += 1
            continue
        reverse_count = edges.get(reverse, 0)
        if reverse_count and reverse < key:
            continue  # this undirected edge was judged from its other direction
        if count + reverse_count != 2:
            watertight = False
            non_manifold += 1
        if count != 1 or reverse_count != 1:
            oriented = False

    volume = volume6 / 6.0
    centroid = (
        [cx / (4.0 * volume6), cy / (4.0 * volume6), cz / (4.0 * volume6)]
        if abs(volume6) > 1e-12
        else None
    )
    return {
        "format": "STL",
        "file": file_name,
        "triangle_count": triangle_count,
        "bbox": {
            "min": mins,
            "max": maxs,
            "span": [maxs[0] - mins[0], maxs[1] - mins[1], maxs[2] - mins[2]],
        },
        "surface_area": area2 / 2.0,
        # Signed: negative means the winding is inverted, itself a finding.
        "signed_volume": volume,
        "center_of_mass": centroid,
        "watertight_manifold": watertight,
        "consistently_oriented": oriented,
        "non_manifold_edges": non_manifold,
        "evidence": (
            "Measured from the tessellation by the divergence theorem. Volume, "
            "area and centre of mass are exact for this mesh; they approximate "
            "the CAD solid only as well as the tessellation does. "
            "signed_volume and center_of_mass are meaningful only when "
            "watertight_manifold and consistently_oriented are both true."
        ),
    }


def _is_ascii_stl(data: bytes) -> bool:
    # A binary STL's 80-byte header is arbitrary text and may legally start
    # with "solid", so an exactly consistent binary triangle layout wins the
    # tie over the ASCII-looking prefix.
    if not (data[:5].lower() == b"solid" and b"facet" in data[:1024]):
        return False
    return not _matches_binary_stl_layout(data)


def _matches_binary_stl_layout(data: bytes) -> bool:
    if len(data) < 84:
        return False
    (count,) = struct.unpack_from("<I", data, 80)
    return 84 + count * 50 == len(data)


def _stl_triangles(data: bytes) -> Iterator[_Triangle]:
    if _is_ascii_stl(data):
        return _stl_ascii(data)
    return _stl_binary(data)


def _stl_binary(data: bytes) -> Iterator[_Triangle]:
    if len(data) < 84:
        raise CadParseError("binary STL shorter than its 84-byte header")
    (count,) = struct.unpack_from("<I", data, 80)
    if count > MAX_STL_TRIANGLES:
        raise CadParseError(f"{count:,} triangles (cap {MAX_STL_TRIANGLES:,})")
    expected = 84 + count * 50
    if len(data) < expected:
        raise CadParseError(
            f"header declares {count} triangles ({expected:,} bytes) but the "
            f"file is {len(data):,} bytes"
        )
    for values in struct.iter_unpack("<12fH", data[84:expected]):
        yield (values[3:6], values[6:9], values[9:12])


_STL_VERTEX = re.compile(rb"vertex\s+([0-9.Ee+-]+)\s+([0-9.Ee+-]+)\s+([0-9.Ee+-]+)")


def _stl_ascii(data: bytes) -> Iterator[_Triangle]:
    triangle_count = 0
    vertex_count = 0
    vertices: list[tuple[float, float, float]] = []
    for match in _STL_VERTEX.finditer(data):
        x, y, z = match.groups()
        vertex_count += 1
        vertices.append((float(x), float(y), float(z)))
        if len(vertices) == 3:
            triangle_count += 1
            if triangle_count > MAX_STL_TRIANGLES:
                raise CadParseError(
                    f"more than {MAX_STL_TRIANGLES:,} triangles "
                    f"(cap {MAX_STL_TRIANGLES:,})"
                )
            yield tuple(vertices)
            vertices.clear()
    if vertices:
        raise CadParseError(f"{vertex_count} vertices is not a multiple of 3")


# --- DXF ---------------------------------------------------------------------


def _dxf_digest(text: str, file_name: str) -> dict[str, Any]:
    lines = text.splitlines()
    entities: Counter[str] = Counter()
    layers: set[str] = set()
    section = ""
    index = 0
    while index + 1 < len(lines):
        code = lines[index].strip()
        value = lines[index + 1].strip()
        if code == "0":
            if value == "SECTION" and index + 3 < len(lines):
                # A SECTION record is followed by a (2, <name>) pair.
                section = lines[index + 3].strip()
            elif value == "ENDSEC":
                section = ""
            elif section == "ENTITIES" and value != "EOF":
                entities[value] += 1
        elif code == "8":
            layers.add(value)
        index += 2
    if not entities and "EOF" not in text:
        raise CadParseError("no group-code structure — not a DXF file")
    return {
        "format": "DXF",
        "file": file_name,
        "entity_counts": dict(entities.most_common(MAX_DIGEST_LIST_ITEMS)),
        "layers": sorted(layers)[:MAX_DIGEST_LIST_ITEMS],
        "evidence": "Entity and layer counts from the DXF ENTITIES section.",
    }


# --- Gerber ------------------------------------------------------------------


def _gerber_digest(text: str, file_name: str) -> dict[str, Any]:
    apertures = re.findall(r"%ADD(\d+)([A-Z]+)", text)
    draws = text.count("D01*")
    moves = text.count("D02*")
    flashes = text.count("D03*")
    if not apertures and not (draws or flashes):
        raise CadParseError("no aperture definitions or operations — not Gerber")
    return {
        "format": "Gerber",
        "file": file_name,
        "aperture_count": len(apertures),
        "aperture_shapes": dict(Counter(shape for _, shape in apertures)),
        "draw_operations": draws,
        "move_operations": moves,
        "flash_operations": flashes,
        "evidence": "Aperture and operation counts from the Gerber command stream.",
    }


# --- Excellon drill ------------------------------------------------------------

_EXCELLON_TOOL_DEF = re.compile(r"^T0*(\d+)C([0-9.]+)", re.MULTILINE)
_EXCELLON_HIT = re.compile(r"^X[+-]?[0-9.]+Y[+-]?[0-9.]+", re.MULTILINE)


def _excellon_digest(text: str, file_name: str) -> dict[str, Any]:
    tools = {f"T{num}": float(dia) for num, dia in _EXCELLON_TOOL_DEF.findall(text)}
    hits = len(_EXCELLON_HIT.findall(text))
    if "M48" not in text and not tools and not hits:
        raise CadParseError(
            "no M48 header, tool definitions or drill hits — not an Excellon drill file"
        )
    return {
        "format": "Excellon",
        "file": file_name,
        "tool_count": len(tools),
        "tool_diameters": tools,
        "drill_hits": hits,
        "evidence": (
            "Tool definitions and drill-hit counts from the Excellon command stream."
        ),
    }

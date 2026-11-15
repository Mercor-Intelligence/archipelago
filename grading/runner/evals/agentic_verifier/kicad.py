"""Pure-Python KiCad digests for the CAD judge's opt-in ``kicad_digest``.

Reads the KiCad MCP app's project files (`.kicad_sch`, `.kicad_pcb`,
`.kicad_pro`) and the reports its snapshot hook writes under
`exports/{reports,netlist,bom}/`. Nothing here runs `kicad-cli`: ERC, DRC and
LVS counts are read from the reports the app produced. Kept apart from
`cad.py` so `read_cad_bytes` and `CAD_EXTENSIONS` are unchanged when the field
is off.
"""

from __future__ import annotations

import csv
import io
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from io import BytesIO
from pathlib import PurePosixPath
from typing import Any

from runner.helpers.artifact_state.parsers.sexpr import SExprParser

from .cad import (
    MAX_CAD_FILE_BYTES,
    MAX_DIGEST_LIST_ITEMS,
    MAX_XML_ELEMENTS,
    CadParseError,
    _decode_text,
)

# The tokenizer materialises every token, so the s-expression cap sits well
# below the generic text cap.
MAX_KICAD_SEXPR_BYTES = 32 * 1024 * 1024
MAX_REPORT_ITEMS = 20
MAX_DESCRIPTION_CHARS = 200
# Same string rule as SExprParser's tokenizer, so quoted parens are skipped.
_PAREN_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[()]')

KICAD_SOURCE_EXTENSIONS = frozenset({".kicad_sch", ".kicad_pcb", ".kicad_pro"})
# Names written by the KiCad MCP snapshot hook (`<project>_<kind>.<ext>`).
_REPORT_KINDS = {"_erc.json": "erc", "_drc.json": "drc", "_lvs.json": "lvs"}
_NETLIST_SUFFIXES = ("_netlist.xml", "_netlist.cir", ".net")
_BOM_SUFFIX = "_bom.csv"
_DNP_FITTED = frozenset({"", "0", "false", "no", "n"})

KICAD_NOTE = (
    "Not established by this digest: a fresh ERC/DRC/LVS run (counts are read "
    "from the reports the app wrote at snapshot time; a missing report means "
    "the check did not run), circuit simulation, and copper geometry such as "
    "clearances or track widths beyond the counts shown."
)


def kicad_format(file_name: str) -> str | None:
    """The KiCad artifact kind ``file_name`` names, or None."""
    name = PurePosixPath(file_name).name.lower()
    suffix = PurePosixPath(name).suffix
    if suffix in KICAD_SOURCE_EXTENSIONS:
        return suffix.lstrip(".")
    for ending, kind in _REPORT_KINDS.items():
        if name.endswith(ending):
            return f"kicad_{kind}"
    if name.endswith(_NETLIST_SUFFIXES):
        return "kicad_netlist"
    if name.endswith(_BOM_SUFFIX):
        return "kicad_bom"
    return None


def is_kicad_file(file_name: str) -> bool:
    return kicad_format(file_name) is not None


def read_kicad_bytes(data: bytes, file_name: str) -> str:
    """A JSON digest of one KiCad artifact; parse failures start ``Could not parse``."""
    if len(data) > MAX_CAD_FILE_BYTES:
        return (
            f"{file_name} is {len(data):,} bytes, over the "
            f"{MAX_CAD_FILE_BYTES:,}-byte cap for CAD parsing."
        )
    kind = kicad_format(file_name)
    try:
        if kind == "kicad_sch":
            digest = _schematic_digest(_sexpr(data), file_name)
        elif kind == "kicad_pcb":
            digest = _board_digest(_sexpr(data), file_name)
        elif kind == "kicad_pro":
            digest = _project_digest(_json(data), file_name)
        elif kind in ("kicad_erc", "kicad_drc", "kicad_lvs"):
            digest = _report_digest(_json(data), file_name, kind)
        elif kind == "kicad_netlist":
            digest = _netlist_digest(data, file_name)
        elif kind == "kicad_bom":
            digest = _bom_digest(_decode_text(data), file_name)
        else:
            return f"{file_name} is not a KiCad file this reader opens."
    except CadParseError as e:
        return f"Could not parse {file_name}: {e}"
    except (
        ValueError,
        TypeError,
        OverflowError,
        RecursionError,
        csv.Error,
        ET.ParseError,
    ) as e:
        return f"Could not parse {file_name}: {type(e).__name__}: {e}"
    digest["not_established"] = KICAD_NOTE
    return json.dumps(digest, indent=2, ensure_ascii=False)


def _sexpr(data: bytes) -> list[Any]:
    if len(data) > MAX_KICAD_SEXPR_BYTES:
        raise CadParseError(
            f"{len(data):,} bytes exceeds the {MAX_KICAD_SEXPR_BYTES:,}-byte "
            "s-expression cap"
        )
    text = _decode_text(data)
    depth = 0
    for match in _PAREN_TOKEN.finditer(text):
        token = match.group()
        depth += 1 if token == "(" else -1 if token == ")" else 0
        if depth < 0:
            raise CadParseError("unbalanced ')'")
    if depth:
        raise CadParseError(f"{depth} unclosed '(' — the file is truncated")
    tree = SExprParser().parse(text)
    if not tree or not isinstance(tree[0], list) or not tree[0]:
        raise CadParseError("no top-level s-expression")
    return tree[0]


def _json(data: bytes) -> dict[str, Any]:
    parsed = json.loads(_decode_text(data))
    if not isinstance(parsed, dict):
        raise CadParseError("top level is not a JSON object")
    return parsed


def _children(node: list[Any], tag: str) -> list[list[Any]]:
    return [c for c in node if isinstance(c, list) and c and c[0] == tag]


def _value(node: list[Any], tag: str) -> Any | None:
    found = _children(node, tag)
    return found[0][1] if found and len(found[0]) > 1 else None


def _property(node: list[Any], name: str) -> str | None:
    for prop in _children(node, "property"):
        if len(prop) > 2 and prop[1] == name and isinstance(prop[2], str):
            return prop[2]
    return None


def _capped(items: list[Any]) -> list[Any]:
    return items[:MAX_DIGEST_LIST_ITEMS]


# --- Schematic -------------------------------------------------------------------


def _schematic_digest(root: list[Any], file_name: str) -> dict[str, Any]:
    if root[0] != "kicad_sch":
        raise CadParseError(f"top-level node is {root[0]!r}, not kicad_sch")
    symbols = [s for s in _children(root, "symbol") if _value(s, "lib_id")]
    placed = [
        {
            "reference": _property(s, "Reference"),
            "value": _property(s, "Value"),
            "footprint": _property(s, "Footprint") or None,
            "lib_id": _value(s, "lib_id"),
        }
        for s in symbols
    ]
    lib_symbols = _children(root, "lib_symbols")
    return {
        "format": "KiCad schematic",
        "file": file_name,
        "version": _value(root, "version"),
        "symbol_count": len(placed),
        "power_symbol_count": sum(
            1 for p in placed if str(p["reference"] or "").startswith("#")
        ),
        "symbols": _capped(placed),
        "library_symbol_count": len(_children(lib_symbols[0], "symbol"))
        if lib_symbols
        else 0,
        "wire_count": len(_children(root, "wire")),
        "junction_count": len(_children(root, "junction")),
        "no_connect_count": len(_children(root, "no_connect")),
        "labels": _capped(
            sorted(
                {
                    str(lbl[1])
                    for tag in ("label", "global_label", "hierarchical_label")
                    for lbl in _children(root, tag)
                    if len(lbl) > 1
                }
            )
        ),
        "sub_sheet_count": len(_children(root, "sheet")),
        "evidence": "Placed symbols, wires and labels parsed from the schematic file.",
    }


# --- Board -----------------------------------------------------------------------

_EDGE_GRAPHICS = ("gr_line", "gr_arc", "gr_circle", "gr_rect", "gr_poly", "gr_curve")


def _footprint_ref(fp: list[Any]) -> tuple[str | None, str | None]:
    ref, value = _property(fp, "Reference"), _property(fp, "Value")
    for text in _children(fp, "fp_text"):
        if len(text) > 2 and text[1] == "reference" and ref is None:
            ref = text[2]
        elif len(text) > 2 and text[1] == "value" and value is None:
            value = text[2]
    return ref, value


def _board_digest(root: list[Any], file_name: str) -> dict[str, Any]:
    if root[0] != "kicad_pcb":
        raise CadParseError(f"top-level node is {root[0]!r}, not kicad_pcb")
    footprints = _children(root, "footprint") + _children(root, "module")
    placed = []
    for fp in footprints:
        ref, value = _footprint_ref(fp)
        placed.append(
            {
                "reference": ref,
                "value": value,
                "footprint": fp[1] if len(fp) > 1 and isinstance(fp[1], str) else None,
                "layer": _value(fp, "layer"),
            }
        )
    layers = _children(root, "layers")
    layer_names = (
        [
            str(entry[1])
            for entry in layers[0][1:]
            if isinstance(entry, list) and len(entry) > 1
        ]
        if layers
        else []
    )
    nets = [n for n in _children(root, "net") if len(n) > 2 and n[2] != ""]
    general = _children(root, "general")
    return {
        "format": "KiCad board",
        "file": file_name,
        "version": _value(root, "version"),
        "thickness_mm": _value(general[0], "thickness") if general else None,
        "copper_layers": [n for n in layer_names if n.endswith(".Cu")],
        "layer_count": len(layer_names),
        "footprint_count": len(placed),
        "footprints": _capped(placed),
        "net_count": len(nets),
        "nets": _capped([str(n[2]) for n in nets]),
        "track_segment_count": len(_children(root, "segment")),
        "track_arc_count": len(_children(root, "arc")),
        "via_count": len(_children(root, "via")),
        "zone_count": len(_children(root, "zone")),
        "edge_cuts_graphic_count": sum(
            1
            for tag in _EDGE_GRAPHICS
            for g in _children(root, tag)
            if _value(g, "layer") == "Edge.Cuts"
        ),
        "evidence": "Footprints, nets, tracks and outline parsed from the board file.",
    }


# --- Project -----------------------------------------------------------------------


def _project_digest(project: dict[str, Any], file_name: str) -> dict[str, Any]:
    net_settings = project.get("net_settings")
    classes = net_settings.get("classes") if isinstance(net_settings, dict) else None
    meta = project.get("meta")
    sheets = project.get("sheets")
    variables = project.get("text_variables")
    return {
        "format": "KiCad project",
        "file": file_name,
        "project_file": meta.get("filename") if isinstance(meta, dict) else None,
        "sections": sorted(project)[:MAX_DIGEST_LIST_ITEMS],
        "sheets": _capped([s for s in sheets if isinstance(s, list)])
        if isinstance(sheets, list)
        else [],
        "net_classes": _capped([c.get("name") for c in classes if isinstance(c, dict)])
        if isinstance(classes, list)
        else [],
        "text_variables": {
            str(k)[:MAX_DESCRIPTION_CHARS]: str(v)[:MAX_DESCRIPTION_CHARS]
            for k, v in list(variables.items())[:MAX_DIGEST_LIST_ITEMS]
        }
        if isinstance(variables, dict)
        else {},
        "evidence": "Settings sections parsed from the .kicad_pro JSON.",
    }


# --- ERC / DRC / LVS reports ----------------------------------------------------------


def _items(raw: Any) -> list[dict[str, Any]]:
    return [v for v in raw if isinstance(v, dict)] if isinstance(raw, list) else []


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "count": len(rows),
        "by_severity": dict(Counter(str(r.get("severity", "unknown")) for r in rows)),
        "by_type": dict(
            Counter(str(r.get("type", "unknown")) for r in rows).most_common(
                MAX_DIGEST_LIST_ITEMS
            )
        ),
        "first": [
            {
                "severity": r.get("severity"),
                "type": r.get("type"),
                "description": str(r.get("description", ""))[:MAX_DESCRIPTION_CHARS],
            }
            for r in rows[:MAX_REPORT_ITEMS]
        ],
    }


def _report_digest(report: dict[str, Any], file_name: str, kind: str) -> dict[str, Any]:
    if not isinstance(report.get("violations"), list) and not isinstance(
        report.get("sheets"), list
    ):
        raise CadParseError("no violations or sheets array — the report is incomplete")
    violations = _items(report.get("violations"))
    # KiCad 8+ ERC nests violations per sheet; DRC keeps them top-level.
    for sheet in _items(report.get("sheets")):
        violations += _items(sheet.get("violations"))
    digest: dict[str, Any] = {
        "format": f"KiCad {kind.removeprefix('kicad_').upper()} report",
        "file": file_name,
        "source": report.get("source"),
        "kicad_version": report.get("kicad_version"),
        "date": report.get("date"),
        "violations": _summary(violations),
    }
    if kind != "kicad_erc":
        digest["unconnected_items"] = _summary(_items(report.get("unconnected_items")))
        digest["schematic_parity"] = _summary(_items(report.get("schematic_parity")))
    digest["evidence"] = (
        "Counts read from the report kicad-cli wrote at snapshot time; "
        "error_count is violations with severity 'error'."
    )
    digest["error_count"] = digest["violations"]["by_severity"].get("error", 0)
    return digest


# --- Netlist -------------------------------------------------------------------------

_SPICE_NODE_LINE = re.compile(r"^\s*([A-Za-z][^\s]*)\s+(.*)$")
# Node count by element letter; anything unlisted is two-terminal.
_SPICE_TERMINALS = {
    "K": 0,
    "J": 3,
    "Q": 3,
    "Z": 3,
    "E": 4,
    "G": 4,
    "M": 4,
    "S": 4,
    "T": 4,
}
# Elements whose model name comes right before any key=value parameters.
_SPICE_MODEL_ELEMENTS = frozenset("DJMQXZ")


def _netlist_digest(data: bytes, file_name: str) -> dict[str, Any]:
    name = file_name.lower()
    if name.endswith(".xml"):
        return _xml_netlist_digest(data, file_name)
    if name.endswith(".cir"):
        return _spice_netlist_digest(_decode_text(data), file_name)
    return _sexpr_netlist_digest(_sexpr(data), file_name)


def _netlist_result(
    file_name: str,
    fmt: str,
    comps: list[dict[str, Any]],
    nets: dict[str, int],
    component_count: int | None = None,
) -> dict[str, Any]:
    return {
        "format": fmt,
        "file": file_name,
        "component_count": len(comps) if component_count is None else component_count,
        "components": _capped(comps),
        "net_count": len(nets),
        "nets": _capped(
            [{"name": n, "node_count": c} for n, c in sorted(nets.items())]
        ),
        "single_node_nets": _capped(sorted(n for n, c in nets.items() if c == 1)),
        "evidence": "Components and net membership read from the exported netlist.",
    }


def _xml_netlist_digest(data: bytes, file_name: str) -> dict[str, Any]:
    if data[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in data[:256]:
        raise CadParseError("the netlist is not UTF-8, which KiCad always writes")
    if b"<!DOCTYPE" in data:
        raise CadParseError("the netlist declares a DTD, which KiCad never writes")
    comps: list[dict[str, Any]] = []
    comp_count = 0
    nets: dict[str, int] = {}
    elements = 0
    stack: list[ET.Element] = []
    for event, elem in ET.iterparse(BytesIO(data), events=("start", "end")):
        if event == "start":
            elements += 1
            if elements > MAX_XML_ELEMENTS:
                raise CadParseError(
                    f"netlist exceeds {MAX_XML_ELEMENTS:,} XML elements"
                )
            stack.append(elem)
            continue
        stack.pop()
        if elem.tag == "comp":
            comp_count += 1
            if len(comps) < MAX_DIGEST_LIST_ITEMS:
                comps.append(
                    {
                        "reference": elem.get("ref"),
                        "value": elem.findtext("value"),
                        "footprint": elem.findtext("footprint"),
                    }
                )
        elif elem.tag == "net":
            nets[elem.get("name") or f"code {elem.get('code')}"] = len(
                elem.findall("node")
            )
        else:
            continue
        # Detach the read subtree so memory stays flat across a large netlist.
        if stack:
            stack[-1].remove(elem)
    return _netlist_result(
        file_name, "KiCad netlist (XML)", comps, nets, component_count=comp_count
    )


def _spice_netlist_digest(text: str, file_name: str) -> dict[str, Any]:
    if not any(line.strip().lower() == ".end" for line in text.splitlines()):
        raise CadParseError("no .end line — not a SPICE netlist")
    comps: list[dict[str, Any]] = []
    nets: Counter[str] = Counter()
    for line in text.splitlines():
        if line.startswith(("*", ".", "+")) or not line.strip():
            continue
        match = _SPICE_NODE_LINE.match(line)
        if not match:
            continue
        ref, rest = match.group(1), match.group(2).split()
        letter = ref[0].upper()
        params = next(
            (i for i, tok in enumerate(rest) if "=" in tok or tok.upper() == "PARAMS:"),
            len(rest),
        )
        terminals = params - 1 if letter == "X" else _SPICE_TERMINALS.get(letter, 2)
        # The model or value always follows the nodes (`D1 __D1`, `M1 D G S NMOS W=1u`).
        last = params if letter in _SPICE_MODEL_ELEMENTS else len(rest)
        nodes = rest[: max(0, min(terminals, last - 1))]
        comps.append(
            {
                "reference": ref,
                "nodes": nodes,
                "value": " ".join(rest[len(nodes) :]) or None,
            }
        )
        nets.update(nodes)
    return _netlist_result(file_name, "SPICE netlist", comps, dict(nets))


def _sexpr_netlist_digest(root: list[Any], file_name: str) -> dict[str, Any]:
    if root[0] != "export":
        raise CadParseError(f"top-level node is {root[0]!r}, not export")
    sections = {c[0]: c for c in root if isinstance(c, list) and c}
    comps = [
        {
            "reference": _value(c, "ref"),
            "value": _value(c, "value"),
            "footprint": _value(c, "footprint"),
        }
        for c in _children(sections.get("components", []), "comp")
    ]
    nets = {
        str(_value(n, "name")): len(_children(n, "node"))
        for n in _children(sections.get("nets", []), "net")
    }
    return _netlist_result(file_name, "KiCad netlist (s-expression)", comps, nets)


# --- BOM -----------------------------------------------------------------------------


def _bom_digest(text: str, file_name: str) -> dict[str, Any]:
    reader = csv.DictReader(io.StringIO(text))
    columns = reader.fieldnames or []
    if not columns:
        raise CadParseError("no header row — not a BOM CSV")
    rows = list(reader)
    lower = {c.lower(): c for c in columns}
    qty_col = lower.get("qty") or lower.get("quantity")
    dnp_col = lower.get("dnp")
    quantity = 0
    for row in rows:
        try:
            quantity += int(float(row.get(qty_col or "", "") or 0))
        except (ValueError, OverflowError):
            continue
    return {
        "format": "KiCad BOM",
        "file": file_name,
        "columns": columns[:MAX_DIGEST_LIST_ITEMS],
        "line_count": len(rows),
        "total_quantity": quantity if qty_col else None,
        "dnp_lines": sum(
            1 for r in rows if (r.get(dnp_col) or "").strip().lower() not in _DNP_FITTED
        )
        if dnp_col
        else None,
        "lines": _capped(rows),
        "evidence": "Rows read from the BOM CSV the app exported.",
    }

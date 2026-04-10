"""KiCad Field Check Verifier - File-based verification for KiCad projects.

Unlike SQL-based verifiers, this verifier parses KiCad's text-based S-expression
files (.kicad_sch, .kicad_pcb) and JSON reports (DRC/ERC) directly from snapshots.
No KiCad installation or kicad-skip library required - pure regex parsing.
"""

import re
import zipfile
from typing import Any

from loguru import logger

from runner.evals.kicad.common import count_vias, find_project_files, find_report
from runner.evals.models import EvalImplInput
from runner.evals.snapshot_utils import (
    count_export_files,
    export_file_exists,
)
from runner.models import VerifierResult, VerifierResultStatus


def _read_text_file(snapshot_zip: zipfile.ZipFile, path: str) -> str | None:
    """Read a text file from the snapshot zip by its full internal path."""
    try:
        return snapshot_zip.read(path).decode("utf-8", errors="replace")
    except (KeyError, Exception) as e:
        logger.warning(f"Could not read {path}: {e}")
        return None


# ============================================================================
# SCHEMATIC PARSING (S-expression regex)
# ============================================================================


def _count_components(text: str) -> int:
    """Count schematic symbols (components) via regex."""
    return len(re.findall(r"\(symbol\s+\(lib_id\s+\"", text))


def _count_wires(text: str) -> int:
    """Count wire segments in a schematic."""
    return len(re.findall(r"\(wire\s+\(pts\s+", text))


def _count_net_labels(text: str) -> int:
    """Count net labels (local + global + hierarchical)."""
    local = len(re.findall(r"\(label\s+\"", text))
    global_ = len(re.findall(r"\(global_label\s+\"", text))
    hierarchical = len(re.findall(r"\(hierarchical_label\s+\"", text))
    return local + global_ + hierarchical


def _count_power_symbols(text: str) -> int:
    """Count power symbols (VCC, GND, etc.)."""
    return len(re.findall(r'\(symbol\s+\(lib_id\s+"power:', text))


def _extract_unique_nets(text: str) -> set[str]:
    """Extract all unique net names from schematic labels."""
    nets: set[str] = set()
    for pattern in [
        r'\(label\s+"([^"]+)"',
        r'\(global_label\s+"([^"]+)"',
        r'\(hierarchical_label\s+"([^"]+)"',
    ]:
        for m in re.finditer(pattern, text):
            nets.add(m.group(1))
    return nets


def _extract_components(text: str) -> list[dict[str, str]]:
    """Extract all components with reference, value, and lib_id."""
    components = []
    for m in re.finditer(r'\(symbol\s+\(lib_id\s+"([^"]+)"\)', text):
        entry: dict[str, str] = {"lib_id": m.group(1)}
        block_start = m.start()
        chunk = text[block_start : block_start + 2000]

        ref_m = re.search(r'\(property\s+"Reference"\s+"([^"]*)"', chunk)
        if ref_m:
            entry["reference"] = ref_m.group(1)

        val_m = re.search(r'\(property\s+"Value"\s+"([^"]*)"', chunk)
        if val_m:
            entry["value"] = val_m.group(1)

        fp_m = re.search(r'\(property\s+"Footprint"\s+"([^"]*)"', chunk)
        if fp_m:
            entry["footprint"] = fp_m.group(1)

        components.append(entry)
    return components


def _find_component(text: str, reference: str) -> dict[str, str] | None:
    """Find a specific component by reference designator."""
    for comp in _extract_components(text):
        if comp.get("reference") == reference:
            return comp
    return None


# ============================================================================
# PCB PARSING (S-expression regex)
# ============================================================================


def _count_footprints(text: str) -> int:
    """Count footprints in a PCB file."""
    return len(re.findall(r'\(footprint\s+"', text))


def _count_tracks(text: str) -> int:
    """Count track segments in a PCB."""
    return len(re.findall(r"\(segment\s+\(start\s+", text))


def _count_zones(text: str) -> int:
    """Count copper zones in a PCB."""
    return len(re.findall(r"\(zone\s+\(net\s+", text))


def _extract_footprint_refs(text: str) -> list[str]:
    """Extract all footprint reference designators from a PCB."""
    refs = []
    for m in re.finditer(r'\(footprint\s+"[^"]*"', text):
        block_start = m.start()
        chunk = text[block_start : block_start + 3000]
        ref_m = re.search(r'\(property\s+"Reference"\s+"([^"]*)"', chunk)
        if ref_m:
            refs.append(ref_m.group(1))
    return refs


# ============================================================================
# DRC / ERC REPORT PARSING (JSON)
# ============================================================================


def _find_drc_report(
    snapshot_zip: zipfile.ZipFile, project_name: str | None = None
) -> dict[str, Any] | None:
    return find_report(snapshot_zip, "_drc.json", project_name)


def _find_erc_report(
    snapshot_zip: zipfile.ZipFile, project_name: str | None = None
) -> dict[str, Any] | None:
    return find_report(snapshot_zip, "_erc.json", project_name)


def _count_violations(report: dict[str, Any], severity: str | None = None) -> int:
    """Count violations in a DRC/ERC report, optionally filtered by severity."""
    violations = report.get("violations", [])
    if severity:
        return sum(1 for v in violations if v.get("severity") == severity)
    return len(violations)


# ============================================================================
# FIELD MAPPINGS
# ============================================================================

FIELD_MAPPINGS: dict[str, dict[str, Any]] = {
    "Component Count": {
        "file_type": "schematic",
        "parser": "count_components",
        "required_params": [],
        "result_type": "integer",
        "description": "Total number of components in the schematic",
    },
    "Component Count by Library": {
        "file_type": "schematic",
        "parser": "count_components_by_lib_id",
        "required_params": ["lib_id"],
        "result_type": "integer",
        "description": "Count components using a specific library symbol (e.g., 'Device:R')",
    },
    "Component Exists": {
        "file_type": "schematic",
        "parser": "component_exists",
        "required_params": ["reference"],
        "result_type": "boolean",
        "description": "Check if a component with given reference exists (e.g., 'R1')",
    },
    "Component Value": {
        "file_type": "schematic",
        "parser": "get_component_value",
        "required_params": ["reference"],
        "result_type": "string",
        "description": "Get the Value property of a component (e.g., '10k' for R1)",
    },
    "Component Library ID": {
        "file_type": "schematic",
        "parser": "get_component_lib_id",
        "required_params": ["reference"],
        "result_type": "string",
        "description": "Get the library symbol ID (e.g., 'Device:R') of a component",
    },
    "Component Footprint": {
        "file_type": "schematic",
        "parser": "get_component_footprint",
        "required_params": ["reference"],
        "result_type": "string",
        "description": "Get the footprint assigned to a component",
    },
    "Wire Count": {
        "file_type": "schematic",
        "parser": "count_wires",
        "required_params": [],
        "result_type": "integer",
        "description": "Total number of wire segments in the schematic",
    },
    "Net Label Exists": {
        "file_type": "schematic",
        "parser": "net_label_exists",
        "required_params": ["net_name"],
        "result_type": "boolean",
        "description": "Check if a net label exists (e.g., 'VCC', 'GND', 'CLK')",
    },
    "Net Label Count": {
        "file_type": "schematic",
        "parser": "count_net_labels",
        "required_params": [],
        "result_type": "integer",
        "description": "Total number of net labels (local + global + hierarchical)",
    },
    "Unique Net Count": {
        "file_type": "schematic",
        "parser": "count_unique_nets",
        "required_params": [],
        "result_type": "integer",
        "description": "Number of distinct net names in the schematic",
    },
    "Power Symbol Count": {
        "file_type": "schematic",
        "parser": "count_power_symbols",
        "required_params": [],
        "result_type": "integer",
        "description": "Count of power symbols (VCC, GND, etc.)",
    },
    "Footprint Count": {
        "file_type": "pcb",
        "parser": "count_footprints",
        "required_params": [],
        "result_type": "integer",
        "description": "Total number of footprints on the PCB",
    },
    "Footprint Exists": {
        "file_type": "pcb",
        "parser": "footprint_exists",
        "required_params": ["reference"],
        "result_type": "boolean",
        "description": "Check if a footprint with given reference exists on the PCB",
    },
    "Track Count": {
        "file_type": "pcb",
        "parser": "count_tracks",
        "required_params": [],
        "result_type": "integer",
        "description": "Total number of track (trace) segments on the PCB",
    },
    "Via Count": {
        "file_type": "pcb",
        "parser": "count_vias",
        "required_params": [],
        "result_type": "integer",
        "description": "Total number of vias on the PCB",
    },
    "Zone Count": {
        "file_type": "pcb",
        "parser": "count_zones",
        "required_params": [],
        "result_type": "integer",
        "description": "Total number of copper zones (ground planes, etc.) on the PCB",
    },
    "DRC Violation Count": {
        "file_type": "drc_report",
        "parser": "count_drc_violations",
        "required_params": [],
        "result_type": "integer",
        "description": "Total DRC violation count from the most recent report",
    },
    "DRC Error Count": {
        "file_type": "drc_report",
        "parser": "count_drc_errors",
        "required_params": [],
        "result_type": "integer",
        "description": "Count of DRC errors (severity=error) only",
    },
    "DRC Warning Count": {
        "file_type": "drc_report",
        "parser": "count_drc_warnings",
        "required_params": [],
        "result_type": "integer",
        "description": "Count of DRC warnings (severity=warning) only",
    },
    "DRC Pass": {
        "file_type": "drc_report",
        "parser": "drc_passed",
        "required_params": [],
        "result_type": "boolean",
        "description": "Whether the PCB passes DRC with zero errors",
    },
    "ERC Violation Count": {
        "file_type": "erc_report",
        "parser": "count_erc_violations",
        "required_params": [],
        "result_type": "integer",
        "description": "Total ERC violation count from the most recent report",
    },
    "ERC Error Count": {
        "file_type": "erc_report",
        "parser": "count_erc_errors",
        "required_params": [],
        "result_type": "integer",
        "description": "Count of ERC errors (severity=error) only",
    },
    "ERC Warning Count": {
        "file_type": "erc_report",
        "parser": "count_erc_warnings",
        "required_params": [],
        "result_type": "integer",
        "description": "Count of ERC warnings (severity=warning) only",
    },
    "ERC Pass": {
        "file_type": "erc_report",
        "parser": "erc_passed",
        "required_params": [],
        "result_type": "boolean",
        "description": "Whether the schematic passes ERC with zero errors",
    },
    "Export File Exists": {
        "file_type": "export",
        "parser": "export_file_exists",
        "required_params": ["file_path"],
        "result_type": "boolean",
        "description": "Check if a specific export file exists (path relative to exports/, e.g. 'gerber/board.gbr')",
    },
    "Export File Count": {
        "file_type": "export",
        "parser": "count_export_files",
        "required_params": ["export_directory"],
        "result_type": "integer",
        "description": "Count files in an export directory (e.g., 'gerber', 'bom', 'pdf')",
    },
    "Project Exists": {
        "file_type": "project",
        "parser": "project_exists",
        "required_params": ["project_name"],
        "result_type": "boolean",
        "description": "Check if a KiCad project (.kicad_pro) exists in the snapshot",
    },
    "Schematic File Exists": {
        "file_type": "project",
        "parser": "schematic_file_exists",
        "required_params": [],
        "result_type": "boolean",
        "description": "Check if any .kicad_sch file exists in the project",
    },
    "PCB File Exists": {
        "file_type": "project",
        "parser": "pcb_file_exists",
        "required_params": [],
        "result_type": "boolean",
        "description": "Check if any .kicad_pcb file exists in the project",
    },
}


# ============================================================================
# FIELD EVALUATION DISPATCH
# ============================================================================


def _evaluate_field(
    snapshot_zip: zipfile.ZipFile,
    field_to_check: str,
    params: dict[str, Any],
    project_name: str | None = None,
    projects_base_path: str | None = None,
) -> tuple[Any, str | None]:
    """Evaluate a field check and return (result, error_message)."""
    config = FIELD_MAPPINGS[field_to_check]
    file_type = config["file_type"]
    parser = config["parser"]

    if file_type == "schematic":
        sch_files = find_project_files(
            snapshot_zip, ".kicad_sch", project_name, projects_base_path
        )
        if not sch_files:
            return None, "No .kicad_sch file found in snapshot"
        text = _read_text_file(snapshot_zip, sch_files[0])
        if text is None:
            return None, f"Could not read schematic file: {sch_files[0]}"

        if parser == "count_components":
            return _count_components(text), None
        elif parser == "count_components_by_lib_id":
            lib_id = params["lib_id"]
            comps = _extract_components(text)
            count = sum(1 for c in comps if c.get("lib_id") == lib_id)
            return count, None
        elif parser == "component_exists":
            comp = _find_component(text, params["reference"])
            return 1 if comp else 0, None
        elif parser == "get_component_value":
            comp = _find_component(text, params["reference"])
            if not comp:
                return None, f"Component {params['reference']} not found"
            return comp.get("value", ""), None
        elif parser == "get_component_lib_id":
            comp = _find_component(text, params["reference"])
            if not comp:
                return None, f"Component {params['reference']} not found"
            return comp.get("lib_id", ""), None
        elif parser == "get_component_footprint":
            comp = _find_component(text, params["reference"])
            if not comp:
                return None, f"Component {params['reference']} not found"
            return comp.get("footprint", ""), None
        elif parser == "count_wires":
            return _count_wires(text), None
        elif parser == "net_label_exists":
            nets = _extract_unique_nets(text)
            return 1 if params["net_name"] in nets else 0, None
        elif parser == "count_net_labels":
            return _count_net_labels(text), None
        elif parser == "count_unique_nets":
            return len(_extract_unique_nets(text)), None
        elif parser == "count_power_symbols":
            return _count_power_symbols(text), None

    elif file_type == "pcb":
        pcb_files = find_project_files(
            snapshot_zip, ".kicad_pcb", project_name, projects_base_path
        )
        if not pcb_files:
            return None, "No .kicad_pcb file found in snapshot"
        text = _read_text_file(snapshot_zip, pcb_files[0])
        if text is None:
            return None, f"Could not read PCB file: {pcb_files[0]}"

        if parser == "count_footprints":
            return _count_footprints(text), None
        elif parser == "footprint_exists":
            refs = _extract_footprint_refs(text)
            return 1 if params["reference"] in refs else 0, None
        elif parser == "count_tracks":
            return _count_tracks(text), None
        elif parser == "count_vias":
            return count_vias(text), None
        elif parser == "count_zones":
            return _count_zones(text), None

    elif file_type == "drc_report":
        report = _find_drc_report(snapshot_zip, project_name)
        if report is None:
            return None, "No DRC report found in snapshot (agent may not have run DRC)"

        if parser == "count_drc_violations":
            return _count_violations(report), None
        elif parser == "count_drc_errors":
            return _count_violations(report, severity="error"), None
        elif parser == "count_drc_warnings":
            return _count_violations(report, severity="warning"), None
        elif parser == "drc_passed":
            return 1 if _count_violations(report, severity="error") == 0 else 0, None

    elif file_type == "erc_report":
        report = _find_erc_report(snapshot_zip, project_name)
        if report is None:
            return None, "No ERC report found in snapshot (agent may not have run ERC)"

        if parser == "count_erc_violations":
            return _count_violations(report), None
        elif parser == "count_erc_errors":
            return _count_violations(report, severity="error"), None
        elif parser == "count_erc_warnings":
            return _count_violations(report, severity="warning"), None
        elif parser == "erc_passed":
            return 1 if _count_violations(report, severity="error") == 0 else 0, None

    elif file_type == "export":
        if parser == "export_file_exists":
            return 1 if export_file_exists(
                snapshot_zip, params["file_path"]
            ) else 0, None
        elif parser == "count_export_files":
            return count_export_files(snapshot_zip, params["export_directory"]), None

    elif file_type == "project":
        if parser == "project_exists":
            pro_files = find_project_files(
                snapshot_zip,
                ".kicad_pro",
                params.get("project_name"),
                projects_base_path,
            )
            return 1 if pro_files else 0, None
        elif parser == "schematic_file_exists":
            sch_files = find_project_files(
                snapshot_zip, ".kicad_sch", project_name, projects_base_path
            )
            return 1 if sch_files else 0, None
        elif parser == "pcb_file_exists":
            pcb_files = find_project_files(
                snapshot_zip, ".kicad_pcb", project_name, projects_base_path
            )
            return 1 if pcb_files else 0, None

    return None, f"Unknown parser '{parser}' for field '{field_to_check}'"


# ============================================================================
# MAIN EVAL FUNCTION
# ============================================================================


async def kicad_field_check_eval(input: EvalImplInput) -> VerifierResult:
    """Verify KiCad project state using file-based parsing.

    Parses S-expression schematic/PCB files and DRC/ERC JSON reports directly
    from the snapshot zip. No KiCad installation or external dependencies required.
    """
    verifier_id = input.verifier.verifier_id
    verifier_version = input.verifier.verifier_version

    field_to_check = input.verifier.verifier_values.get("field_to_check")
    if not field_to_check:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="field_to_check is required",
        )

    expected_value = input.verifier.verifier_values.get("expected_value")
    if expected_value is None:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="expected_value is required",
        )

    field_config = FIELD_MAPPINGS.get(field_to_check)
    if not field_config:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=f"Unknown field_to_check: {field_to_check}",
        )

    params: dict[str, Any] = {}
    for param in field_config["required_params"]:
        value = input.verifier.verifier_values.get(param)
        if value is None:
            return VerifierResult(
                verifier_id=verifier_id,
                verifier_version=verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={},
                message=f"Required parameter '{param}' is missing for '{field_to_check}'",
            )
        params[param] = value

    project_name = input.verifier.verifier_values.get("project_name")
    projects_base_path = (
        input.verifier.verifier_values.get("projects_base_path") or None
    )

    if not input.final_snapshot_bytes:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="No final snapshot available",
        )

    try:
        input.final_snapshot_bytes.seek(0)
        with zipfile.ZipFile(input.final_snapshot_bytes, "r") as snapshot_zip:
            result, error = _evaluate_field(
                snapshot_zip, field_to_check, params, project_name, projects_base_path
            )
    except zipfile.BadZipFile:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="Invalid snapshot zip file",
        )
    except Exception as e:
        logger.error(f"Error evaluating {field_to_check}: {e}")
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=f"Evaluation failed: {e!s}",
        )

    if error:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={"error": error, "field_checked": field_to_check},
            message=error,
        )

    result_type = field_config["result_type"]

    if result is None:
        matches = False
        message = f"{field_to_check}: No result found"
    elif result_type == "boolean":
        actual_bool = (
            int(result) > 0 if isinstance(result, (int, float)) else bool(result)
        )
        expected_bool = str(expected_value).lower() in ("true", "1", "yes")
        matches = actual_bool == expected_bool
        message = f"{field_to_check}: {'Yes' if actual_bool else 'No'} {'✓' if matches else '✗'}"
    elif result_type == "integer":
        try:
            actual_int = int(result)
            expected_int = int(expected_value)
            matches = actual_int == expected_int
        except (ValueError, TypeError) as e:
            return VerifierResult(
                verifier_id=verifier_id,
                verifier_version=verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={},
                message=f"Invalid integer value - result: {result}, expected: {expected_value}. Error: {e!s}",
            )
        message = f"{field_to_check}: {actual_int} {'✓' if matches else f'✗ expected {expected_int}'}"
    elif result_type == "string":
        actual_str = str(result).strip()
        expected_str = str(expected_value).strip()
        matches = actual_str.lower() == expected_str.lower()
        expected_msg = f"✗ expected '{expected_str}'"
        message = f"{field_to_check}: '{actual_str}' {'✓' if matches else expected_msg}"
    else:
        matches = str(result) == str(expected_value)
        message = f"{field_to_check}: {result} {'✓' if matches else f'✗ expected {expected_value}'}"

    logger.info(
        f"KiCad field check: {field_to_check} = {result} (expected: {expected_value}) -> {matches}"
    )

    return VerifierResult(
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        score=1.0 if matches else 0.0,
        status=VerifierResultStatus.OK,
        verifier_result_values={
            "actual_value": str(result) if result is not None else None,
            "expected_value": str(expected_value),
            "field_checked": field_to_check,
            "matches": matches,
        },
        message=message,
    )

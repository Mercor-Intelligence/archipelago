"""FreeCAD Field Check Verifier - File-based verification for FreeCAD projects.

Parses .FCStd files (ZIP archives containing Document.xml) to extract object
tree data, properties, and relationships. No FreeCAD installation required -
pure XML parsing from snapshots.

Geometry-dependent checks (volume, surface area, bounding box) are NOT possible
without a FreeCAD runtime. This verifier focuses on structural/parametric checks
that can be done from the XML alone.
"""

import io
import xml.etree.ElementTree as ET
import zipfile
from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.evals.snapshot_utils import (
    count_export_files,
    export_file_exists,
    find_files_in_snapshot,
)
from runner.models import VerifierResult, VerifierResultStatus

FREECAD_PROJECTS_BASES = [
    ".apps_data/freecad_mcp/projects",
    ".apps_data/freecad",
]


def _find_project_files(
    snapshot_zip: zipfile.ZipFile,
    extension: str,
    project_name: str | None = None,
    projects_base_path: str | None = None,
) -> list[str]:
    """Find FreeCAD project files, optionally filtered by project name.

    If projects_base_path is given, only that path is searched.
    Otherwise, all known default bases are tried in order.
    """
    bases = [projects_base_path] if projects_base_path else FREECAD_PROJECTS_BASES
    all_files: list[str] = []
    for base in bases:
        all_files = find_files_in_snapshot(snapshot_zip, extension, base)
        if all_files:
            break
    if not project_name:
        return all_files
    project_lower = project_name.lower()
    return [f for f in all_files if project_lower in f.rsplit("/", 1)[-1].lower()]


# ============================================================================
# FCSTD PARSING (ZIP -> Document.xml -> ElementTree)
# ============================================================================


def _read_document_xml(
    snapshot_zip: zipfile.ZipFile, fcstd_path: str
) -> ET.Element | None:
    """Extract and parse Document.xml from an FCStd file nested inside the snapshot zip.

    FCStd files are themselves ZIP archives, so this does a nested extraction:
    snapshot.zip -> project.FCStd (bytes) -> Document.xml (XML)
    """
    try:
        fcstd_bytes = snapshot_zip.read(fcstd_path)
    except (KeyError, Exception) as e:
        logger.warning(f"Could not read FCStd file {fcstd_path}: {e}")
        return None

    try:
        with zipfile.ZipFile(io.BytesIO(fcstd_bytes), "r") as fcstd_zip:
            if "Document.xml" not in fcstd_zip.namelist():
                logger.warning(f"No Document.xml in {fcstd_path}")
                return None
            xml_bytes = fcstd_zip.read("Document.xml")
            return ET.fromstring(xml_bytes)
    except (zipfile.BadZipFile, ET.ParseError, Exception) as e:
        logger.warning(f"Could not parse Document.xml from {fcstd_path}: {e}")
        return None


def _find_and_parse_fcstd(
    snapshot_zip: zipfile.ZipFile,
    project_name: str | None = None,
    projects_base_path: str | None = None,
) -> tuple[ET.Element | None, str | None]:
    """Find an FCStd file and return its parsed Document.xml root."""
    fcstd_files = _find_project_files(
        snapshot_zip, ".fcstd", project_name, projects_base_path
    )
    if not fcstd_files:
        return None, "No .FCStd file found in snapshot"
    root = _read_document_xml(snapshot_zip, fcstd_files[0])
    if root is None:
        return None, f"Could not parse Document.xml from {fcstd_files[0]}"
    return root, None


# ============================================================================
# OBJECT EXTRACTION FROM DOCUMENT.XML
# ============================================================================


def _get_objects(root: ET.Element) -> list[dict[str, str]]:
    """Extract all objects (name + type) from the Objects section."""
    objects_section = root.find("Objects")
    if objects_section is None:
        return []
    result = []
    for obj in objects_section.findall("Object"):
        name = obj.get("name", "")
        obj_type = obj.get("type", "")
        if name:
            result.append({"name": name, "type": obj_type})
    return result


def _get_object_data(root: ET.Element, object_name: str) -> ET.Element | None:
    """Find the ObjectData element for a specific object."""
    object_data = root.find("ObjectData")
    if object_data is None:
        return None
    for obj in object_data.findall("Object"):
        if obj.get("name") == object_name:
            return obj
    return None


def _get_object_property(root: ET.Element, object_name: str, property_name: str) -> Any:
    """Extract a property value from an object in ObjectData.

    Handles common FreeCAD property types:
    - App::PropertyString -> String/@value
    - App::PropertyLength/Float/Angle -> Float/@value
    - App::PropertyBool -> Bool/@value
    - App::PropertyInteger -> Integer/@value
    - App::PropertyLink -> Link/@value
    - App::PropertyPlacement -> PropertyPlacement attributes
    """
    obj_data = _get_object_data(root, object_name)
    if obj_data is None:
        return None

    properties = obj_data.find("Properties")
    if properties is None:
        return None

    for prop in properties.findall("Property"):
        if prop.get("name") == property_name:
            return _extract_property_value(prop)

    return None


def _extract_property_value(prop_element: ET.Element) -> Any:
    """Extract the value from a Property element based on its type."""
    string_el = prop_element.find("String")
    if string_el is not None:
        return string_el.get("value", "")

    float_el = prop_element.find("Float")
    if float_el is not None:
        try:
            return float(float_el.get("value", "0"))
        except ValueError:
            return float_el.get("value", "")

    bool_el = prop_element.find("Bool")
    if bool_el is not None:
        return bool_el.get("value", "false").lower() == "true"

    int_el = prop_element.find("Integer")
    if int_el is not None:
        try:
            return int(int_el.get("value", "0"))
        except ValueError:
            return int_el.get("value", "")

    link_el = prop_element.find("Link")
    if link_el is not None:
        return link_el.get("value", "")

    placement_el = prop_element.find("PropertyPlacement")
    if placement_el is not None:
        return {
            "x": float(placement_el.get("Px", "0")),
            "y": float(placement_el.get("Py", "0")),
            "z": float(placement_el.get("Pz", "0")),
            "rotation_angle": float(placement_el.get("A", "0")),
        }

    return None


# ============================================================================
# COUNTING AND QUERY FUNCTIONS
# ============================================================================


def _count_objects(root: ET.Element) -> int:
    """Count total objects in the document."""
    return len(_get_objects(root))


def _count_objects_by_type(root: ET.Element, type_pattern: str) -> int:
    """Count objects whose type contains the given pattern (case-insensitive)."""
    pattern_lower = type_pattern.lower()
    return sum(1 for obj in _get_objects(root) if pattern_lower in obj["type"].lower())


def _object_exists(root: ET.Element, object_name: str) -> bool:
    """Check if an object with the given name exists."""
    return any(obj["name"] == object_name for obj in _get_objects(root))


def _get_object_type(root: ET.Element, object_name: str) -> str | None:
    """Get the type of a named object."""
    for obj in _get_objects(root):
        if obj["name"] == object_name:
            return obj["type"]
    return None


def _get_object_label(root: ET.Element, object_name: str) -> str | None:
    """Get the Label property of an object."""
    val = _get_object_property(root, object_name, "Label")
    return str(val) if val is not None else None


def _count_dependencies(root: ET.Element, object_name: str) -> int:
    """Count the number of dependencies for an object."""
    objects_section = root.find("Objects")
    if objects_section is None:
        return 0
    for obj_deps in objects_section.findall("ObjectDeps"):
        if obj_deps.get("Name") == object_name:
            return int(obj_deps.get("Count", "0"))
    return 0


def _has_boolean_operation(root: ET.Element, op_type: str) -> bool:
    """Check if a boolean operation type exists (Fuse, Cut, Common)."""
    full_type = f"Part::{op_type}"
    return any(obj["type"] == full_type for obj in _get_objects(root))


def _count_boolean_operations(root: ET.Element) -> int:
    """Count total boolean operations (Fuse, Cut, Common)."""
    boolean_types = {"Part::Fuse", "Part::Cut", "Part::Common"}
    return sum(1 for obj in _get_objects(root) if obj["type"] in boolean_types)


def _has_fillet_or_chamfer(root: ET.Element) -> bool:
    """Check if any fillet or chamfer operations exist."""
    fillet_types = {
        "Part::Fillet",
        "Part::Chamfer",
        "PartDesign::Fillet",
        "PartDesign::Chamfer",
    }
    return any(obj["type"] in fillet_types for obj in _get_objects(root))


def _get_document_label(root: ET.Element) -> str | None:
    """Get the document-level Label property."""
    properties = root.find("Properties")
    if properties is None:
        return None
    for prop in properties.findall("Property"):
        if prop.get("name") == "Label":
            string_el = prop.find("String")
            if string_el is not None:
                return string_el.get("value", "")
    return None


# ============================================================================
# FIELD MAPPINGS
# ============================================================================

FIELD_MAPPINGS: dict[str, dict[str, Any]] = {
    "Project Exists": {
        "file_type": "project",
        "parser": "project_exists",
        "required_params": ["project_name"],
        "result_type": "boolean",
        "description": "Check if a FreeCAD project (.FCStd) exists in the snapshot",
    },
    "Document Label": {
        "file_type": "fcstd",
        "parser": "document_label",
        "required_params": [],
        "result_type": "string",
        "description": "Get the document-level Label (display name) from the project file",
    },
    "Object Count": {
        "file_type": "fcstd",
        "parser": "count_objects",
        "required_params": [],
        "result_type": "integer",
        "description": "Total number of objects in the document tree",
    },
    "Object Count by Type": {
        "file_type": "fcstd",
        "parser": "count_objects_by_type",
        "required_params": ["object_type"],
        "result_type": "integer",
        "description": "Count objects of a specific type (e.g., 'Part::Box', 'Part::Cylinder', 'Part::Cut')",
    },
    "Boolean Operation Count": {
        "file_type": "fcstd",
        "parser": "count_boolean_operations",
        "required_params": [],
        "result_type": "integer",
        "description": "Count of boolean operations (Fuse, Cut, Common) in the model",
    },
    "Object Exists": {
        "file_type": "fcstd",
        "parser": "object_exists",
        "required_params": ["object_name"],
        "result_type": "boolean",
        "description": "Check if an object with the given name exists in the document",
    },
    "Object Type": {
        "file_type": "fcstd",
        "parser": "object_type",
        "required_params": ["object_name"],
        "result_type": "string",
        "description": "Get the type of a named object (e.g., 'Part::Box', 'Part::Fuse')",
    },
    "Object Label": {
        "file_type": "fcstd",
        "parser": "object_label",
        "required_params": ["object_name"],
        "result_type": "string",
        "description": "Get the Label property of a named object",
    },
    "Has Boolean Operation": {
        "file_type": "fcstd",
        "parser": "has_boolean_operation",
        "required_params": ["operation_type"],
        "result_type": "boolean",
        "description": "Check if a boolean operation type exists (Fuse, Cut, or Common)",
    },
    "Has Fillet or Chamfer": {
        "file_type": "fcstd",
        "parser": "has_fillet_or_chamfer",
        "required_params": [],
        "result_type": "boolean",
        "description": "Check if any fillet or chamfer operations exist in the model",
    },
    "Object Property Value": {
        "file_type": "fcstd",
        "parser": "object_property_value",
        "required_params": ["object_name", "property_name"],
        "result_type": "string",
        "description": "Get any property value of a named object (e.g., Length, Width, Height, Radius)",
    },
    "Object Dependency Count": {
        "file_type": "fcstd",
        "parser": "object_dependency_count",
        "required_params": ["object_name"],
        "result_type": "integer",
        "description": "Number of objects that a named object depends on",
    },
    "Object Length": {
        "file_type": "fcstd",
        "parser": "object_dimension",
        "required_params": ["object_name"],
        "result_type": "decimal",
        "description": "Get the Length property of a named object (mm)",
        "dimension_name": "Length",
    },
    "Object Width": {
        "file_type": "fcstd",
        "parser": "object_dimension",
        "required_params": ["object_name"],
        "result_type": "decimal",
        "description": "Get the Width property of a named object (mm)",
        "dimension_name": "Width",
    },
    "Object Height": {
        "file_type": "fcstd",
        "parser": "object_dimension",
        "required_params": ["object_name"],
        "result_type": "decimal",
        "description": "Get the Height property of a named object (mm)",
        "dimension_name": "Height",
    },
    "Object Radius": {
        "file_type": "fcstd",
        "parser": "object_dimension",
        "required_params": ["object_name"],
        "result_type": "decimal",
        "description": "Get the Radius property of a named object (mm)",
        "dimension_name": "Radius",
    },
    "Export File Exists": {
        "file_type": "export",
        "parser": "export_file_exists",
        "required_params": ["file_path"],
        "result_type": "boolean",
        "description": "Check if a specific export file exists (path relative to exports/, e.g. 'step/model.step')",
    },
    "Export File Count": {
        "file_type": "export",
        "parser": "count_export_files",
        "required_params": ["export_directory"],
        "result_type": "integer",
        "description": "Count files in an export directory (e.g., 'step', 'stl', 'obj', 'iges')",
    },
    "STEP Export Exists": {
        "file_type": "export",
        "parser": "step_export_exists",
        "required_params": [],
        "result_type": "boolean",
        "description": "Check if any STEP export file exists",
    },
    "STL Export Exists": {
        "file_type": "export",
        "parser": "stl_export_exists",
        "required_params": [],
        "result_type": "boolean",
        "description": "Check if any STL export file exists",
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

    if file_type == "project":
        if parser == "project_exists":
            pro_files = _find_project_files(
                snapshot_zip, ".fcstd", params.get("project_name"), projects_base_path
            )
            return 1 if pro_files else 0, None

    elif file_type == "fcstd":
        root, error = _find_and_parse_fcstd(
            snapshot_zip, project_name, projects_base_path
        )
        if root is None:
            return None, error

        if parser == "count_objects":
            return _count_objects(root), None
        elif parser == "count_objects_by_type":
            return _count_objects_by_type(root, params["object_type"]), None
        elif parser == "count_boolean_operations":
            return _count_boolean_operations(root), None
        elif parser == "object_exists":
            return 1 if _object_exists(root, params["object_name"]) else 0, None
        elif parser == "object_type":
            obj_type = _get_object_type(root, params["object_name"])
            if obj_type is None:
                return None, f"Object '{params['object_name']}' not found"
            return obj_type, None
        elif parser == "object_label":
            label = _get_object_label(root, params["object_name"])
            if label is None:
                return None, f"Object '{params['object_name']}' not found"
            return label, None
        elif parser == "has_boolean_operation":
            return 1 if _has_boolean_operation(
                root, params["operation_type"]
            ) else 0, None
        elif parser == "has_fillet_or_chamfer":
            return 1 if _has_fillet_or_chamfer(root) else 0, None
        elif parser == "object_property_value":
            val = _get_object_property(
                root, params["object_name"], params["property_name"]
            )
            if val is None:
                return (
                    None,
                    f"Property '{params['property_name']}' not found on '{params['object_name']}'",
                )
            return val, None
        elif parser == "object_dependency_count":
            if not _object_exists(root, params["object_name"]):
                return None, f"Object '{params['object_name']}' not found"
            return _count_dependencies(root, params["object_name"]), None
        elif parser == "object_dimension":
            dim_name = config.get("dimension_name", "")
            val = _get_object_property(root, params["object_name"], dim_name)
            if val is None:
                return (
                    None,
                    f"Property '{dim_name}' not found on '{params['object_name']}'",
                )
            return val, None
        elif parser == "document_label":
            label = _get_document_label(root)
            if label is None:
                return None, "Document Label not found"
            return label, None

    elif file_type == "export":
        if parser == "export_file_exists":
            return 1 if export_file_exists(
                snapshot_zip, params["file_path"]
            ) else 0, None
        elif parser == "count_export_files":
            return count_export_files(snapshot_zip, params["export_directory"]), None
        elif parser == "step_export_exists":
            count = count_export_files(snapshot_zip, "step", ".step")
            return 1 if count > 0 else 0, None
        elif parser == "stl_export_exists":
            count = count_export_files(snapshot_zip, "stl", ".stl")
            return 1 if count > 0 else 0, None

    return None, f"Unknown parser '{parser}' for field '{field_to_check}'"


# ============================================================================
# MAIN EVAL FUNCTION
# ============================================================================


async def freecad_field_check_eval(input: EvalImplInput) -> VerifierResult:
    """Verify FreeCAD project state using file-based parsing.

    Parses Document.xml from FCStd ZIP archives inside the snapshot.
    No FreeCAD installation or external dependencies required.
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
        message = f"{field_to_check}: {'Yes' if actual_bool else 'No'}"
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
        message = f"{field_to_check}: {actual_int} (expected {expected_int})"
    elif result_type == "decimal":
        try:
            actual_float = float(result)
            expected_float = float(expected_value)
            raw_tolerance = input.verifier.verifier_values.get("tolerance")
            tolerance = (
                float(raw_tolerance)
                if raw_tolerance is not None and raw_tolerance != ""
                else 0.01
            )
            matches = abs(actual_float - expected_float) <= tolerance
        except (ValueError, TypeError) as e:
            return VerifierResult(
                verifier_id=verifier_id,
                verifier_version=verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={},
                message=f"Invalid decimal value - result: {result}, expected: {expected_value}. Error: {e!s}",
            )
        message = f"{field_to_check}: {actual_float} (expected {expected_float}, tolerance {tolerance})"
    elif result_type == "string":
        actual_str = str(result).strip()
        expected_str = str(expected_value).strip()
        matches = actual_str.lower() == expected_str.lower()
        message = f"{field_to_check}: '{actual_str}' (expected '{expected_str}')"
    else:
        matches = str(result) == str(expected_value)
        message = f"{field_to_check}: {result} (expected {expected_value})"

    logger.info(
        f"FreeCAD field check: {field_to_check} = {result} (expected: {expected_value}) -> {matches}"
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

"""KiCad Layout Quality Verifier.

Computes PCB layout quality metrics (trace length, via count, board area
utilization, decoupling proximity) from the .kicad_pcb file in the snapshot.
Returns continuous 0-1 scores for use as reward signal components.
"""

import math
import re
import zipfile

from runner.evals.kicad.common import count_vias, extract_balanced_block, find_pcb_file
from runner.evals.models import EvalImplInput
from runner.models import VerifierResult, VerifierResultStatus


def _compute_trace_length(pcb_text: str) -> float:
    total = 0.0
    for m in re.finditer(
        r"\(segment\s+\(start\s+(-?[\d.]+)\s+(-?[\d.]+)\)\s+\(end\s+(-?[\d.]+)\s+(-?[\d.]+)\)",
        pcb_text,
    ):
        x1, y1 = float(m.group(1)), float(m.group(2))
        x2, y2 = float(m.group(3)), float(m.group(4))
        total += math.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
    return round(total, 2)


def _compute_board_area(pcb_text: str) -> float:
    edge_points: list[tuple[float, float]] = []
    for m in re.finditer(r"\(gr_line\b", pcb_text):
        block = extract_balanced_block(pcb_text, m.start())
        if '"Edge.Cuts"' not in block:
            continue
        coord_m = re.search(
            r"\(start\s+(-?[\d.]+)\s+(-?[\d.]+)\)\s+\(end\s+(-?[\d.]+)\s+(-?[\d.]+)\)",
            block,
        )
        if coord_m:
            edge_points.append((float(coord_m.group(1)), float(coord_m.group(2))))
            edge_points.append((float(coord_m.group(3)), float(coord_m.group(4))))

    if len(edge_points) < 3:
        return 0.0

    xs = [p[0] for p in edge_points]
    ys = [p[1] for p in edge_points]
    return (max(xs) - min(xs)) * (max(ys) - min(ys))


# Average footprint area estimate (mm^2). Rough approximation assuming
# a mix of SMD passives (~2x2mm) and ICs (~7x7mm). Used only for
# board_area_utilization metric when actual courtyard data is unavailable.
DEFAULT_FOOTPRINT_AREA_MM2 = 25.0


def _compute_footprint_area(pcb_text: str) -> float:
    """Estimate total footprint area from component count (rough approximation)."""
    count = len(re.findall(r'\(footprint\s+"', pcb_text))
    return count * DEFAULT_FOOTPRINT_AREA_MM2


async def kicad_layout_quality_eval(input: EvalImplInput) -> VerifierResult:
    """Compute a layout quality metric and score it against a target.

    Supported metrics: total_trace_length, via_count, board_area_utilization,
    decoupling_proximity (future), routing_efficiency (future).

    Score is continuous (0-1). For 'lte' metrics (lower is better):
    score = max(0, 1 - (actual - target) / target). For 'gte' metrics:
    score = min(1, actual / target).
    """
    verifier_id = input.verifier.verifier_id
    verifier_version = input.verifier.verifier_version

    metric = input.verifier.verifier_values.get("metric", "total_trace_length")
    expected_value = input.verifier.verifier_values.get("expected_value")
    comparison = input.verifier.verifier_values.get("comparison_operator", "lte")
    project_name = input.verifier.verifier_values.get("project_name")

    if expected_value is None:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="expected_value is required",
        )

    try:
        target = float(expected_value)
    except (ValueError, TypeError):
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=f"expected_value must be numeric, got: {expected_value}",
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
            pcb_path = find_pcb_file(snapshot_zip, project_name)
            if not pcb_path:
                return VerifierResult(
                    verifier_id=verifier_id,
                    verifier_version=verifier_version,
                    score=0.0,
                    status=VerifierResultStatus.ERROR,
                    verifier_result_values={},
                    message="No .kicad_pcb file found in snapshot",
                )
            pcb_text = snapshot_zip.read(pcb_path).decode("utf-8", errors="replace")
    except zipfile.BadZipFile:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message="Invalid snapshot zip file",
        )

    if metric == "total_trace_length":
        actual = _compute_trace_length(pcb_text)
    elif metric == "via_count":
        actual = float(count_vias(pcb_text))
    elif metric == "board_area_utilization":
        board_area = _compute_board_area(pcb_text)
        if board_area <= 0:
            return VerifierResult(
                verifier_id=verifier_id,
                verifier_version=verifier_version,
                score=0.0,
                status=VerifierResultStatus.ERROR,
                verifier_result_values={},
                message="Could not compute board area (no Edge.Cuts gr_line elements found). "
                "Boards using gr_rect or gr_arc for outline are not yet supported.",
            )
        fp_area = _compute_footprint_area(pcb_text)
        actual = round(fp_area / board_area, 3)
    else:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={},
            message=f"Unknown metric: {metric}. Supported: total_trace_length, via_count, board_area_utilization",
        )

    if target <= 0:
        if comparison == "lte":
            score = 1.0 if actual <= 0 else 0.0
        elif comparison == "gte":
            score = 1.0 if actual >= target else 0.0
        else:
            score = 1.0 if actual == target else 0.0
    elif comparison == "lte":
        score = max(0.0, 1.0 - (actual - target) / target) if actual > target else 1.0
    elif comparison == "gte":
        score = min(1.0, actual / target)
    elif comparison == "within_range":
        # Smooth falloff: score = 1.0 at target, degrades linearly with distance
        deviation = abs(actual - target) / target
        score = max(0.0, 1.0 - deviation)
    else:
        score = 1.0 if actual == target else 0.0

    score = round(max(0.0, min(1.0, score)), 4)
    meets_target = score >= 0.9

    return VerifierResult(
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        score=score,
        status=VerifierResultStatus.OK,
        verifier_result_values={
            "metric": metric,
            "actual_value": actual,
            "target_value": target,
            "comparison": comparison,
            "meets_target": meets_target,
        },
        message=f"Layout quality ({metric}): {actual} vs target {target} ({comparison}) -> score {score:.2f}",
    )

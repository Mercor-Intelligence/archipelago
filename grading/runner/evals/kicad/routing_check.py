"""KiCad Routing Completeness Verifier.

Verifies that all (or a threshold of) nets in the PCB are routed.
Parses the DRC report for unconnected items or directly compares
net definitions against routed segments in the .kicad_pcb file.
"""

import re
import zipfile
from typing import Any

from runner.evals.kicad.common import find_pcb_file
from runner.evals.models import EvalImplInput
from runner.models import VerifierResult, VerifierResultStatus


def _compute_routing_completeness(pcb_text: str) -> dict[str, Any]:
    """Compute routing completeness from PCB S-expression text."""
    all_nets: dict[int, str] = {}
    for m in re.finditer(r'\(net\s+(\d+)\s+"([^"]*)"\)', pcb_text):
        net_id = int(m.group(1))
        net_name = m.group(2)
        if net_id > 0:
            all_nets[net_id] = net_name

    routed_net_ids: set[int] = set()
    for m in re.finditer(
        r"\((?:segment|arc|via|zone)\s.*?\(net\s+(\d+)\)", pcb_text, re.DOTALL
    ):
        routed_net_ids.add(int(m.group(1)))

    routed = [name for nid, name in all_nets.items() if nid in routed_net_ids]
    unrouted = [name for nid, name in all_nets.items() if nid not in routed_net_ids]
    total = len(all_nets)
    pct = len(routed) / total if total > 0 else 0.0

    return {
        "total_nets": total,
        "routed_count": len(routed),
        "unrouted_count": len(unrouted),
        "routing_percentage": round(pct, 4),
        "unrouted_nets": sorted(unrouted)[:20],
    }


async def kicad_routing_completeness_eval(input: EvalImplInput) -> VerifierResult:
    """Verify that all nets in the PCB are routed.

    Returns a continuous score (routed_nets / total_nets) enabling partial credit.
    Gate threshold is configurable via min_completion (default 0.95).
    """
    verifier_id = input.verifier.verifier_id
    verifier_version = input.verifier.verifier_version
    try:
        min_completion = float(
            input.verifier.verifier_values.get("min_completion", "0.95")
        )
    except (ValueError, TypeError):
        min_completion = 0.95
    project_name = input.verifier.verifier_values.get("project_name")

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

    result = _compute_routing_completeness(pcb_text)
    pct = result["routing_percentage"]
    score = pct

    passes = pct >= min_completion
    unrouted_preview = ", ".join(result["unrouted_nets"][:10])
    if result["unrouted_count"] > 10:
        unrouted_preview += f" ... and {result['unrouted_count'] - 10} more"

    return VerifierResult(
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        score=score,
        status=VerifierResultStatus.OK,
        verifier_result_values={
            "total_nets": result["total_nets"],
            "routed_count": result["routed_count"],
            "unrouted_count": result["unrouted_count"],
            "routing_percentage": round(pct * 100, 1),
            "unrouted_nets": unrouted_preview,
            "passes_threshold": passes,
        },
        message=f"Routing: {round(pct * 100, 1)}% ({result['routed_count']}/{result['total_nets']} nets) {'PASS' if passes else 'FAIL'}",
    )

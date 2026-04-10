"""Golden file match eval — compare golden response files against trajectory files.

Supports multiple golden snapshots: if ANY golden snapshot matches fully, the eval passes.
This allows tasks with multiple valid end states (per RLS-3349).

Volatile values (timestamps, UUIDs, etc.) are automatically normalized before comparison
to prevent false mismatches from dynamic content.
"""

import hashlib
import io
import os
import zipfile
from collections.abc import Callable
from typing import Any

from loguru import logger

from runner.evals.models import EvalImplInput
from runner.evals.utils.normalizer import (
    create_normalizer,
)
from runner.models import VerifierResult, VerifierResultStatus


def _index_files_by_basename(
    zip_bytes: io.BytesIO,
    normalizer: Callable[[bytes, str], bytes] | None = None,
) -> dict[str, bytes]:
    """Index all files in a zip by basename. Shallowest path wins on collision.

    Args:
        zip_bytes: BytesIO containing the zip file
        normalizer: Optional function to normalize file content before indexing

    Returns:
        Dict mapping basename to (normalized) file bytes
    """
    files: dict[str, bytes] = {}
    try:
        with zipfile.ZipFile(zip_bytes) as z:
            # Sort: shallowest path first (fewest slashes), then alphabetical
            entries = sorted(z.namelist(), key=lambda n: (n.count("/"), n))
            for name in entries:
                if name.endswith("/"):
                    continue
                basename = os.path.basename(name)
                if not basename:
                    continue
                if basename not in files:
                    content = z.read(name)
                    if normalizer is not None:
                        content = normalizer(content, basename)
                    files[basename] = content
    except zipfile.BadZipFile:
        logger.warning("[GOLDEN_FILE_MATCH] Bad zip file encountered")
    return files


def _compare_files(
    golden_files: dict[str, bytes],
    trajectory_files: dict[str, bytes],
) -> dict[str, Any]:
    """Compare golden files against trajectory files by SHA-256.

    Returns a dict with matched count, total count, score, and per-file results.
    """
    matched = 0
    results: list[dict[str, Any]] = []

    for basename in sorted(golden_files.keys()):
        golden_bytes = golden_files[basename]
        golden_hash = hashlib.sha256(golden_bytes).hexdigest()

        if basename in trajectory_files:
            traj_hash = hashlib.sha256(trajectory_files[basename]).hexdigest()
            if golden_hash == traj_hash:
                matched += 1
                results.append({"file": basename, "status": "match"})
            else:
                results.append(
                    {
                        "file": basename,
                        "status": "mismatch",
                        "golden_sha256": golden_hash[:16],
                        "trajectory_sha256": traj_hash[:16],
                    }
                )
        else:
            results.append({"file": basename, "status": "not_found"})

    total = len(golden_files)
    score = matched / total if total > 0 else 0.0

    return {
        "matched_files": matched,
        "total_golden_files": total,
        "score": score,
        "results": results,
    }


async def golden_file_match_eval(input: EvalImplInput) -> VerifierResult:
    """Compare golden response files against trajectory snapshot files by basename using SHA-256.

    Supports multiple golden snapshots: if ANY golden snapshot matches fully (score=1.0),
    the eval returns score=1.0 immediately. If none match fully, returns the best partial
    match score with details about the closest golden snapshot.

    Volatile values (timestamps, UUIDs, session tokens, etc.) are automatically normalized
    before comparison to prevent false mismatches from dynamic content.
    """
    verifier_id = input.verifier.verifier_id
    verifier_version = input.verifier.verifier_version

    if not input.golden_snapshots:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={
                "total_golden_files": 0,
                "matched_files": 0,
                "results": [],
                "golden_snapshots_checked": 0,
            },
            message="No golden response files provided.",
        )

    # Always normalize volatile values (timestamps, UUIDs, etc.) before comparison
    normalizer = create_normalizer()

    # Index trajectory files once (reused for all golden snapshot comparisons)
    input.final_snapshot_bytes.seek(0)
    trajectory_files = _index_files_by_basename(
        input.final_snapshot_bytes, normalizer=normalizer
    )

    # Try each golden snapshot - pass immediately if any matches fully
    best_result: dict[str, Any] | None = None
    best_index: int = 0

    for idx, golden_bytes in enumerate(input.golden_snapshots):
        golden_bytes.seek(0)
        golden_files = _index_files_by_basename(golden_bytes, normalizer=normalizer)

        if not golden_files:
            # Skip empty golden snapshots
            continue

        comparison = _compare_files(golden_files, trajectory_files)

        # If this golden snapshot matches fully, return immediately
        if comparison["score"] == 1.0:
            return VerifierResult(
                verifier_id=verifier_id,
                verifier_version=verifier_version,
                score=1.0,
                verifier_result_values={
                    "total_golden_files": comparison["total_golden_files"],
                    "matched_files": comparison["matched_files"],
                    "results": comparison["results"],
                    "golden_snapshots_checked": len(input.golden_snapshots),
                    "matching_snapshot_index": idx,
                },
            )

        # Track the best partial match
        if best_result is None or comparison["score"] > best_result["score"]:
            best_result = comparison
            best_index = idx

    # No full match found
    if best_result is None:
        return VerifierResult(
            verifier_id=verifier_id,
            verifier_version=verifier_version,
            score=0.0,
            status=VerifierResultStatus.ERROR,
            verifier_result_values={
                "total_golden_files": 0,
                "matched_files": 0,
                "results": [],
                "golden_snapshots_checked": len(input.golden_snapshots),
            },
            message="All golden response snapshots were empty.",
        )

    # Return best partial match
    return VerifierResult(
        verifier_id=verifier_id,
        verifier_version=verifier_version,
        score=best_result["score"],
        verifier_result_values={
            "total_golden_files": best_result["total_golden_files"],
            "matched_files": best_result["matched_files"],
            "results": best_result["results"],
            "golden_snapshots_checked": len(input.golden_snapshots),
            "best_match_snapshot_index": best_index,
        },
    )

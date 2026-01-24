"""Content-based matching utilities for snapshot diff generation.

This module provides utilities for matching sub-artifacts (slides, sheets, pages)
between original and final snapshots using content-based strategies instead of
positional index matching.

This solves the index-shifting problem where deleting/inserting a slide causes
all subsequent slides to be incorrectly marked as modified.
"""

import difflib
import hashlib
from typing import Any

from loguru import logger


def get_artifact_fingerprint(artifact: dict[str, Any]) -> str:
    """
    Generate a fingerprint hash for a sub-artifact including text and images.

    This ensures that image-only changes are detected (not just text changes).

    Args:
        artifact: Sub-artifact dict with content, images, etc.

    Returns:
        MD5 hash string representing the artifact's content + images
    """
    content = artifact.get("content", "")
    images = artifact.get("images", [])

    # Include sorted image URLs/hashes for deterministic fingerprint
    image_keys = sorted(
        [
            img.get("url", "") or img.get("hash", "") or str(img.get("caption", ""))
            for img in images
            if img  # Skip None/empty entries
        ]
    )

    # Combine text + images with separator
    if image_keys:
        combined = content + "\n---IMAGES---\n" + "\n".join(image_keys)
    else:
        combined = content

    return hashlib.md5(combined.encode()).hexdigest()


def match_sub_artifacts_by_content(
    original_sub_artifacts: list[dict[str, Any]],
    final_sub_artifacts: list[dict[str, Any]],
    similarity_threshold: float = 0.5,
    artifact_type: str | None = None,
) -> list[tuple[dict[str, Any] | None, dict[str, Any] | None, str]]:
    """
    Match sub-artifacts using file-type specific strategies.

    This solves the index-shifting problem where deleting/inserting a slide
    causes all subsequent slides to be incorrectly marked as modified.

    Matching strategies by artifact type:
    - "sheet" (Spreadsheets): Title-based matching first (sheet names are unique identifiers)
    - "slide" (Presentations): Content + image hash matching
    - Other: Content + image hash matching (default)

    Algorithm:
    1. For sheets: Title-based exact matching first
    2. Hash-based exact matching (content + images) - O(n)
    3. Similarity matching for unmatched (text only) - O(k²)
    4. Remaining unmatched originals → deleted
    5. Remaining unmatched finals → created

    Args:
        original_sub_artifacts: Sub-artifacts from original snapshot
        final_sub_artifacts: Sub-artifacts from final snapshot
        similarity_threshold: Minimum similarity ratio to consider a match (default 0.5)
        artifact_type: Type of artifact ("sheet", "slide", "page") for strategy selection

    Returns:
        List of (original, final, match_type) tuples where match_type is one of:
        - "unchanged": Exact content match (including images)
        - "modified": Similar content (above threshold) or same title with different content
        - "deleted": Original with no matching final
        - "created": Final with no matching original
    """
    matches: list[tuple[dict[str, Any] | None, dict[str, Any] | None, str]] = []
    unmatched_originals = list(original_sub_artifacts)
    unmatched_finals: list[dict[str, Any]] = []

    # Determine artifact type from first sub-artifact if not provided
    if artifact_type is None and original_sub_artifacts:
        artifact_type = original_sub_artifacts[0].get("type", "")
    if artifact_type is None and final_sub_artifacts:
        artifact_type = final_sub_artifacts[0].get("type", "")

    logger.debug(
        f"[JUDGE][DIFF] Matching {len(original_sub_artifacts)} original → "
        f"{len(final_sub_artifacts)} final sub-artifacts "
        f"(type={artifact_type}, threshold={similarity_threshold})"
    )

    is_sheet = artifact_type == "sheet"

    # Step 1: For Spreadsheets sheets - match by title first (sheet names are reliable)
    if is_sheet:
        orig_by_title: dict[str, dict[str, Any]] = {}
        for orig in original_sub_artifacts:
            title = orig.get("title", "")
            if title and title not in orig_by_title:
                orig_by_title[title] = orig

        for final in final_sub_artifacts:
            title = final.get("title", "")
            if title and title in orig_by_title:
                orig = orig_by_title[title]
                if orig in unmatched_originals:
                    # Same title - check if content changed
                    orig_fingerprint = get_artifact_fingerprint(orig)
                    final_fingerprint = get_artifact_fingerprint(final)

                    if orig_fingerprint == final_fingerprint:
                        matches.append((orig, final, "unchanged"))
                    else:
                        matches.append((orig, final, "modified"))
                    unmatched_originals.remove(orig)
                    continue
            unmatched_finals.append(final)
    else:
        # For slides/pages: go directly to content matching
        unmatched_finals = list(final_sub_artifacts)

    # Step 2: Build hash index with list for duplicates - O(n)
    # hash -> list of originals with that hash (handles duplicates)
    orig_by_hash: dict[str, list[dict[str, Any]]] = {}
    for orig in unmatched_originals:
        fingerprint = get_artifact_fingerprint(orig)
        if fingerprint not in orig_by_hash:
            orig_by_hash[fingerprint] = []
        orig_by_hash[fingerprint].append(orig)

    # Step 3: Exact hash matching with duplicate support - O(n)
    still_unmatched_finals: list[dict[str, Any]] = []
    for final in unmatched_finals:
        fingerprint = get_artifact_fingerprint(final)

        if fingerprint in orig_by_hash and orig_by_hash[fingerprint]:
            # Pop one original from the list (handles duplicates automatically)
            orig = orig_by_hash[fingerprint].pop(0)
            matches.append((orig, final, "unchanged"))
            unmatched_originals.remove(orig)
        else:
            still_unmatched_finals.append(final)

    # Step 4: Similarity matching for unmatched (text only) - O(k²)
    # TODO: Uses text-only similarity; image changes don't affect match score.
    # Result: matched slides are marked "modified", but image differences
    # aren't factored into whether two slides are considered "similar enough".
    remaining_unmatched_finals: list[dict[str, Any]] = []

    for final in still_unmatched_finals:
        final_content = final.get("content", "")
        best_match: dict[str, Any] | None = None
        best_score = 0.0

        for orig in unmatched_originals:
            orig_content = orig.get("content", "")
            score = difflib.SequenceMatcher(None, orig_content, final_content).ratio()

            if score > best_score and score >= similarity_threshold:
                best_match = orig
                best_score = score

        if best_match is not None:
            # Note: If we reached similarity matching, hash matching (Step 3) already failed.
            # The fingerprint includes text + images, so if text is identical but hashes differ,
            # the images must be different. Always mark as "modified" to preserve image changes.
            matches.append((best_match, final, "modified"))
            unmatched_originals.remove(best_match)
        else:
            remaining_unmatched_finals.append(final)

    # Step 5: Remaining originals are truly deleted
    for orig in unmatched_originals:
        matches.append((orig, None, "deleted"))

    # Step 6: Remaining finals are truly created
    for final in remaining_unmatched_finals:
        matches.append((None, final, "created"))

    # Log summary
    unchanged = sum(1 for _, _, t in matches if t == "unchanged")
    modified = sum(1 for _, _, t in matches if t == "modified")
    deleted = sum(1 for _, _, t in matches if t == "deleted")
    created = sum(1 for _, _, t in matches if t == "created")
    logger.debug(
        f"[JUDGE][DIFF] Match complete: {unchanged} unchanged, {modified} modified, "
        f"{deleted} deleted, {created} created"
    )

    return matches

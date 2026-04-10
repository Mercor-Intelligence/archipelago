from __future__ import annotations

import base64
import mimetypes
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from loguru import logger

from runner.helpers.snapshot_diff.constants import (
    PURE_IMAGE_EXTENSIONS,
    PURE_IMAGE_MIME_TYPES,
)
from runner.helpers.snapshot_diff.types import Artifact, ArtifactChange
from runner.utils.file_transformations.models import (
    ArtifactTransformationId,
    TransformationOutput,
)
from runner.utils.file_transformations.registry import (
    get_available_transformations,
    get_transformation,
)

from ..output_llm.utils.log_helpers import (
    log_reference_artifact_error,
    log_reference_artifact_result,
)
from ..output_llm.utils.services.artifact_reference import (
    MAX_REFERENCE_ARTIFACT_CHARS,
    MAX_REFERENCE_ARTIFACT_IMAGES,
    ArtifactSelection,
)
from ..output_llm.utils.snapshot_utils import read_file_from_snapshot_zip

SOURCE_TRANSFORMATION = "source"


class ArtifactSelectionWithTransformations(ArtifactSelection):
    transformations: list[ArtifactTransformationId] | None = None


def resolve_transformations(
    path: str,
    requested: Iterable[ArtifactTransformationId | str] | None,
) -> list[ArtifactTransformationId | str]:
    if not requested:
        return [SOURCE_TRANSFORMATION]

    available = {
        d.transformation_id
        for d in get_available_transformations(Path(path).suffix.lower())
    }
    valid_ids = {t.value for t in ArtifactTransformationId}
    resolved: list[ArtifactTransformationId | str] = []
    include_source = False
    seen: set[str] = set()

    for raw in requested:
        val = str(raw).strip().lower()
        if not val or val in seen:
            continue
        seen.add(val)
        if val == SOURCE_TRANSFORMATION:
            include_source = True
            continue
        if val in valid_ids:
            tid = ArtifactTransformationId(val)
            if tid in available:
                resolved.append(tid)

    if include_source:
        resolved.insert(0, SOURCE_TRANSFORMATION)

    if not resolved:
        return [SOURCE_TRANSFORMATION]

    return resolved


async def fetch_artifacts_with_transformations(
    artifacts_to_reference: list[ArtifactSelectionWithTransformations],
    initial_snapshot_zip: zipfile.ZipFile | None = None,
    task_id: str | None = None,
    criteria: str | None = None,
) -> list[Artifact]:
    _task = task_id or "unknown"

    if not artifacts_to_reference:
        logger.info(
            f"[JUDGE][GRADER][PROMPT_BUILD][REF_FETCH] task={_task} | "
            f"no reference artifacts requested, skipping fetch"
        )
        return []

    if not initial_snapshot_zip:
        logger.warning(
            f"[JUDGE][GRADER][PROMPT_BUILD][REF_FETCH] task={_task} | "
            f"no initial snapshot zip provided | cannot fetch {len(artifacts_to_reference)} reference artifacts"
        )
        return []

    artifacts: list[Artifact] = []
    fetched_names: list[str] = []
    failed_names: list[str] = []
    total_text_chars = 0
    total_images = 0

    for i, spec in enumerate(artifacts_to_reference, 1):
        name = spec.name
        resolved = resolve_transformations(name, spec.transformations)
        try:
            logger.debug(
                f"[JUDGE][GRADER][PROMPT_BUILD][REF_FETCH] task={_task} | "
                f"[{i}/{len(artifacts_to_reference)}] fetching | "
                f"file={name} | source={spec.source} | "
                f"transformations={[str(t) for t in resolved]}"
            )
            fetched = await _fetch_single_artifact_with_transformations(
                artifact_spec=spec,
                snapshot_zip=initial_snapshot_zip,
                task_id=_task,
                transformations=resolved,
            )
            if fetched:
                artifacts.extend(fetched)
                fetched_names.append(name)

                spec_text = sum(len(a.content) for a in fetched if a.content)
                spec_images = sum(
                    len(a.embedded_images) for a in fetched if a.embedded_images
                )
                total_text_chars += spec_text
                total_images += spec_images

                logger.debug(
                    f"[JUDGE][GRADER][PROMPT_BUILD][REF_FETCH] task={_task} | "
                    f"[{i}/{len(artifacts_to_reference)}] success | "
                    f"file={name} | artifacts={len(fetched)} | "
                    f"text={spec_text:,} chars | images={spec_images}"
                )
            else:
                failed_names.append(name)
                logger.warning(
                    f"[JUDGE][GRADER][PROMPT_BUILD][REF_FETCH] task={_task} | "
                    f"[{i}/{len(artifacts_to_reference)}] failed | "
                    f"file={name} | reason=no artifact returned"
                )
        except Exception as e:
            failed_names.append(name)
            log_reference_artifact_error(_task, name, e, criteria=criteria)
            continue

    logger.info(
        f"[JUDGE][GRADER][PROMPT_BUILD][REF_FETCH] task={_task} | "
        f"fetch complete | fetched_specs={len(fetched_names)}/{len(artifacts_to_reference)} | "
        f"emitted_artifacts={len(artifacts)} | "
        f"total_text={total_text_chars:,} chars | total_images={total_images}"
    )

    log_reference_artifact_result(
        _task,
        fetched=len(fetched_names),
        total=len(artifacts_to_reference),
        fetched_names=fetched_names if fetched_names else None,
        failed_names=failed_names if failed_names else None,
        criteria=criteria,
    )
    return artifacts


def _to_image_dict(img: Any) -> dict[str, Any]:
    if hasattr(img, "model_dump"):
        return img.model_dump()
    if isinstance(img, dict):
        return img
    return vars(img) if hasattr(img, "__dict__") else {}


def _transformation_title(
    name: str,
    transformation: ArtifactTransformationId | str,
    multiple: bool,
) -> str:
    if transformation == SOURCE_TRANSFORMATION and not multiple:
        return name
    return f"{name} [{transformation}]"


def _build_artifact_from_transformation_output(
    *,
    name: str,
    transformation: ArtifactTransformationId | str,
    multiple: bool,
    output: TransformationOutput,
) -> Artifact | None:
    title = _transformation_title(name, transformation, multiple)

    if output.pdf_bytes:
        pdf_b64 = base64.b64encode(output.pdf_bytes).decode("utf-8")
        return Artifact(
            path=name,
            artifact_type="file",
            change_type="unchanged",
            title=title,
            content=f"data:application/pdf;base64,{pdf_b64}",
            is_visual=False,
            visual_url=None,
            screenshot_url=None,
            embedded_images=None,
            sub_artifacts=None,
            early_truncated=False,
        )

    has_text = bool(output.text)
    has_images = bool(output.images)

    if not has_text and not has_images:
        return None

    return Artifact(
        path=name,
        artifact_type="file",
        change_type="unchanged",
        title=title,
        content=output.text,
        is_visual=has_images,
        visual_url=output.images[0].url
        if has_images and len(output.images) == 1 and output.images[0].type == "Image"
        else None,
        screenshot_url=None,
        embedded_images=[_to_image_dict(img) for img in output.images]
        if has_images
        else None,
        sub_artifacts=None,
        early_truncated=False,
    )


def _build_source_artifact(
    *,
    name: str,
    multiple: bool,
    file_bytes: bytes,
    file_ext: str,
    is_pure_visual: bool,
) -> Artifact:
    title = _transformation_title(name, SOURCE_TRANSFORMATION, multiple)

    if is_pure_visual:
        mime_type, _ = mimetypes.guess_type(name)
        if not mime_type or not mime_type.startswith("image/"):
            mime_type = PURE_IMAGE_MIME_TYPES.get(file_ext, "image/png")
        base64_data = base64.b64encode(file_bytes).decode("utf-8")
        visual_url = f"data:{mime_type};base64,{base64_data}"
        return Artifact(
            path=name,
            artifact_type="file",
            change_type="unchanged",
            title=title,
            content=None,
            is_visual=True,
            visual_url=visual_url,
            screenshot_url=None,
            embedded_images=None,
            sub_artifacts=None,
            early_truncated=False,
        )

    text = file_bytes.decode("utf-8", errors="replace")
    return Artifact(
        path=name,
        artifact_type="file",
        change_type="unchanged",
        title=title,
        content=text,
        is_visual=False,
        visual_url=None,
        screenshot_url=None,
        embedded_images=None,
        sub_artifacts=None,
        early_truncated=False,
    )


async def _fetch_single_artifact_with_transformations(
    artifact_spec: ArtifactSelectionWithTransformations,
    snapshot_zip: zipfile.ZipFile,
    task_id: str | None = None,
    transformations: list[ArtifactTransformationId | str] | None = None,
) -> list[Artifact]:
    _task = task_id or "unknown"
    name = artifact_spec.name
    source = artifact_spec.source
    file_ext = Path(name).suffix.lower()
    is_pure_visual = file_ext in PURE_IMAGE_EXTENSIONS

    normalized_name = name
    if name.startswith("filesystem/"):
        normalized_name = name[len("filesystem/") :]

    file_bytes = read_file_from_snapshot_zip(snapshot_zip, normalized_name)
    if not file_bytes:
        logger.warning(
            f"[JUDGE][GRADER][PROMPT_BUILD][REF_FETCH][ZIP_READ] task={_task} | "
            f"file not found in snapshot | file={name} | source={source}"
        )
        return []

    requested = transformations or [SOURCE_TRANSFORMATION]
    multiple = len(requested) > 1
    results: list[Artifact] = []

    for t in requested:
        if t == SOURCE_TRANSFORMATION:
            results.append(
                _build_source_artifact(
                    name=name,
                    multiple=multiple,
                    file_bytes=file_bytes,
                    file_ext=file_ext,
                    is_pure_visual=is_pure_visual,
                )
            )
            continue

        defn = (
            get_transformation(t) if isinstance(t, ArtifactTransformationId) else None
        )
        if not defn or not defn.transformation_impl:
            logger.warning(
                f"[JUDGE][GRADER][PROMPT_BUILD][REF_FETCH] task={_task} | "
                f"no transformation for {t}"
            )
            continue

        try:
            output = await defn.transformation_impl(file_bytes, name)
        except Exception as e:
            logger.warning(
                f"[JUDGE][GRADER][PROMPT_BUILD][REF_FETCH] task={_task} | "
                f"transformation {t} failed for {name}: {e}"
            )
            continue

        if output.text and len(output.text) > MAX_REFERENCE_ARTIFACT_CHARS:
            output.text = output.text[:MAX_REFERENCE_ARTIFACT_CHARS]

        if output.images and len(output.images) > MAX_REFERENCE_ARTIFACT_IMAGES:
            dropped = output.images[MAX_REFERENCE_ARTIFACT_IMAGES:]
            if output.text:
                for img in dropped:
                    if img.placeholder:
                        output.text = output.text.replace(img.placeholder, "")
            output.images = output.images[:MAX_REFERENCE_ARTIFACT_IMAGES]

        artifact = _build_artifact_from_transformation_output(
            name=name,
            transformation=t,
            multiple=multiple,
            output=output,
        )
        if artifact is not None:
            results.append(artifact)

    return results


# Map file extensions to their preferred visual transformation
_VISUAL_TRANSFORMATION_MAP: dict[str, ArtifactTransformationId] = {
    ".docx": ArtifactTransformationId.DOCX_TO_IMAGES,
    ".doc": ArtifactTransformationId.DOCX_TO_IMAGES,
    ".pdf": ArtifactTransformationId.PDF_TO_IMAGES,
    ".pptx": ArtifactTransformationId.PPTX_TO_IMAGES,
    ".ppt": ArtifactTransformationId.PPTX_TO_IMAGES,
    ".xlsx": ArtifactTransformationId.SPREADSHEET_TO_IMAGES,
    ".xls": ArtifactTransformationId.SPREADSHEET_TO_IMAGES,
    ".xlsm": ArtifactTransformationId.SPREADSHEET_TO_IMAGES,
}

# Maximum number of page images per output artifact to avoid context blowup
MAX_OUTPUT_ARTIFACT_IMAGES = 20


async def transform_output_artifacts(
    selected_artifacts: list[ArtifactChange],
    final_snapshot_zip: zipfile.ZipFile,
    task_id: str | None = None,
) -> list[Artifact]:
    """
    Apply visual transformations (to_images) to the agent's output artifacts.

    This reads document files from the final snapshot and renders them as page
    images so the LLM judge can evaluate visual properties like formatting,
    colors, page count, and layout — properties that are lost during the
    text-only SNAPSHOT_DIFF extraction.

    Only applies to document file types that support visual transformation
    (docx, pdf, pptx, xlsx, etc.). Regular text files are skipped.

    Args:
        selected_artifacts: The ArtifactChange objects selected for evaluation
        final_snapshot_zip: ZipFile of the agent's final snapshot
        task_id: Optional task ID for logging

    Returns:
        List of Artifact objects containing rendered page images
    """
    _task = task_id or "unknown"

    # Deduplicate by file path — multiple ArtifactChange entries may share
    # the same parent file (e.g. individual sheets from one xlsx)
    seen_paths: set[str] = set()
    artifacts_to_transform: list[tuple[str, ArtifactTransformationId]] = []

    for ac in selected_artifacts:
        if ac.path in seen_paths:
            continue

        ext = Path(ac.path).suffix.lower()
        transform_id = _VISUAL_TRANSFORMATION_MAP.get(ext)
        if transform_id is None:
            continue

        seen_paths.add(ac.path)
        artifacts_to_transform.append((ac.path, transform_id))

    if not artifacts_to_transform:
        return []

    logger.info(
        f"[JUDGE][GRADER][OUTPUT_TRANSFORM] task={_task} | "
        f"transforming {len(artifacts_to_transform)} output artifacts to images"
    )

    results: list[Artifact] = []

    for file_path, transform_id in artifacts_to_transform:
        normalized = file_path
        if file_path.startswith("filesystem/"):
            normalized = file_path[len("filesystem/") :]

        file_bytes = read_file_from_snapshot_zip(final_snapshot_zip, normalized)
        if not file_bytes:
            logger.warning(
                f"[JUDGE][GRADER][OUTPUT_TRANSFORM] task={_task} | "
                f"file not found in final snapshot | file={file_path}"
            )
            continue

        defn = get_transformation(transform_id)
        if not defn or not defn.transformation_impl:
            logger.warning(
                f"[JUDGE][GRADER][OUTPUT_TRANSFORM] task={_task} | "
                f"no transformation impl for {transform_id}"
            )
            continue

        try:
            output = await defn.transformation_impl(file_bytes, file_path)
        except Exception as e:
            logger.warning(
                f"[JUDGE][GRADER][OUTPUT_TRANSFORM] task={_task} | "
                f"transformation {transform_id} failed for {file_path}: {e}"
            )
            continue

        if output.images and len(output.images) > MAX_OUTPUT_ARTIFACT_IMAGES:
            output.images = output.images[:MAX_OUTPUT_ARTIFACT_IMAGES]

        artifact = _build_artifact_from_transformation_output(
            name=file_path,
            transformation=transform_id,
            multiple=False,
            output=output,
        )
        if artifact is not None:
            # Override title to clearly label this as a rendered output artifact
            artifact.title = f"{file_path} [rendered output]"
            results.append(artifact)
            image_count = len(output.images) if output.images else 0
            logger.info(
                f"[JUDGE][GRADER][OUTPUT_TRANSFORM] task={_task} | "
                f"transformed {file_path} | images={image_count}"
            )

    logger.info(
        f"[JUDGE][GRADER][OUTPUT_TRANSFORM] task={_task} | "
        f"output transformation complete | "
        f"transformed={len(results)}/{len(artifacts_to_transform)}"
    )

    return results

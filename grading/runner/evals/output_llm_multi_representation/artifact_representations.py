from __future__ import annotations

import base64
import mimetypes
import zipfile
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from loguru import logger
from pydantic import field_validator

from runner.helpers.snapshot_diff.constants import (
    PURE_IMAGE_EXTENSIONS,
    PURE_IMAGE_MIME_TYPES,
)
from runner.helpers.snapshot_diff.types import Artifact, ArtifactChange
from runner.utils.file_transformations.docx_to_style_metadata.main import (
    docx_to_style_metadata_output,
)
from runner.utils.file_transformations.models import (
    INPUT_FILE_FAMILY_DEFNS,
    ArtifactTransformationId,
    InputFileFamily,
    TransformationOutput,
)
from runner.utils.file_transformations.pptx_to_style_metadata.main import (
    pptx_to_style_metadata_output,
)
from runner.utils.file_transformations.registry import (
    get_available_transformations,
    get_transformation,
)
from runner.utils.file_transformations.spreadsheet_to_style_metadata.main import (
    spreadsheet_to_style_metadata,
)
from runner.utils.token_utils import count_tokens, truncate_markup_to_tokens

from ..output_llm.utils.log_helpers import (
    log_reference_artifact_error,
    log_reference_artifact_result,
)
from ..output_llm.utils.services.artifact_reference import (
    MAX_REFERENCE_ARTIFACT_CHARS,
    MAX_REFERENCE_ARTIFACT_IMAGES,
    ArtifactSelection,
)
from ..output_llm.utils.snapshot_utils import read_artifact_from_snapshot_zip

SOURCE_TRANSFORMATION = "source"

# Map file-extension-based prefixes (e.g. "xlsx_", "xls_", "xlsm_") to the
# canonical family prefix ("spreadsheet_") so that verifier configs authored
# with the intuitive "{ext}_native" pattern are accepted without error.
_EXTENSION_TO_FAMILY: dict[str, str] = {}
for _family, _defn in INPUT_FILE_FAMILY_DEFNS.items():
    for _ext in _defn.extensions:
        _ext_prefix = _ext.lstrip(".")
        if _ext_prefix != _family.value:
            _EXTENSION_TO_FAMILY[_ext_prefix] = _family.value


def _normalize_transformation_id(raw: str) -> str:
    """Rewrite extension-based aliases to their canonical family-based ID.

    For example ``xlsx_native`` → ``spreadsheet_native``.  Values that are
    already canonical (or unrecognised) are returned unchanged so that
    Pydantic's own enum validation can accept or reject them.
    """
    lowered = raw.strip().lower()
    for ext_prefix, family_prefix in _EXTENSION_TO_FAMILY.items():
        if lowered.startswith(ext_prefix + "_"):
            return family_prefix + lowered[len(ext_prefix) :]
    return lowered


class ArtifactSelectionWithTransformations(ArtifactSelection):
    transformations: list[ArtifactTransformationId] | None = None

    @field_validator("transformations", mode="before")
    @classmethod
    def normalize_transformation_aliases(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        return [_normalize_transformation_id(t) for t in v]


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

    # Resolve via the centralized helper that handles both new (full
    # snapshot path) and legacy (bare relative path) selection.name
    # formats — see read_artifact_from_snapshot_zip docstring.
    file_bytes = read_artifact_from_snapshot_zip(snapshot_zip, name)
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
    ".odt": ArtifactTransformationId.DOCX_TO_IMAGES,
    ".pdf": ArtifactTransformationId.PDF_TO_IMAGES,
    ".pptx": ArtifactTransformationId.PPTX_TO_IMAGES,
    ".ppt": ArtifactTransformationId.PPTX_TO_IMAGES,
    ".odp": ArtifactTransformationId.PPTX_TO_IMAGES,
    ".xlsx": ArtifactTransformationId.SPREADSHEET_TO_IMAGES,
    ".xls": ArtifactTransformationId.SPREADSHEET_TO_IMAGES,
    ".xlsm": ArtifactTransformationId.SPREADSHEET_TO_IMAGES,
    ".ods": ArtifactTransformationId.SPREADSHEET_TO_IMAGES,
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
        file_bytes = read_artifact_from_snapshot_zip(final_snapshot_zip, file_path)
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


# Which extensions get style extraction, and which extractor handles each.
# Derived from the canonical family definitions so these never drift. .xls
# (legacy binary) is supported via a LibreOffice-conversion fallback inside
# spreadsheet_to_style_metadata itself.
#
# Not covered: pdf (no style extractor exists — PDFs record glyph placement
# rather than a style model, so this needs a new dependency and is tracked
# separately).
_STYLE_METADATA_FAMILIES: dict[str, InputFileFamily] = {
    **{
        ext: InputFileFamily.SPREADSHEET
        for ext in INPUT_FILE_FAMILY_DEFNS[InputFileFamily.SPREADSHEET].extensions
    },
    # Narrower than the PPTX family on purpose: python-pptx only reads the
    # modern OOXML package, and raises BadZipFile on legacy .ppt (OLE2) and
    # .odp (OpenDocument) — confirmed empirically. Including them would cost a
    # snapshot read plus a warning log per criterion and yield no style facts.
    # Unlike .xls, which spreadsheet_to_style_metadata converts via
    # LibreOffice, there's no ppt->pptx conversion helper here yet; intersecting
    # with the family keeps this from ever claiming support beyond it.
    **{
        ext: InputFileFamily.PPTX
        for ext in INPUT_FILE_FAMILY_DEFNS[InputFileFamily.PPTX].extensions & {".pptx"}
    },
    # Same intersection reasoning as PPTX: python-docx reads only the OOXML
    # package, so legacy .doc (OLE2) and .odt (OpenDocument) would raise rather
    # than yield style facts.
    **{
        ext: InputFileFamily.DOCX
        for ext in INPUT_FILE_FAMILY_DEFNS[InputFileFamily.DOCX].extensions & {".docx"}
    },
}

# This text is appended directly to the prompt in main.py, after
# build_grading_prompt() has already run its own model-context-derived
# token budget — nothing trims it after that point, so it needs its own,
# real, token-based budget rather than a flat character guess. Reuses the
# same truncate_files_equally utility the rest of grading already relies on
# for token-aware, per-model truncation, so the cap actually reflects what
# the specific judge model can hold rather than an arbitrary constant.
#
# Sized for a task emitting several style-eligible outputs at once, since the
# budget is split evenly across them: each extractor already bounds its own
# output (~21k chars for an 8-sheet workbook, ~2.5k for a 7-slide deck), so
# the aggregate is predictable. At 15k a workbook sharing the budget with a
# deck lost half its worksheets to truncation, which breaks criteria asking
# about "every worksheet". Note the XML runs ~2.7 chars/token and Gemini
# judges apply a 1.9x conservative multiplier, so this is ~9k real tokens.
STYLE_METADATA_TOKEN_BUDGET = 24_000


def has_style_metadata_artifacts(selected_artifacts: list[ArtifactChange]) -> bool:
    """Whether any selected artifact is eligible for style extraction.

    Lets callers skip opening the snapshot zip entirely when there's nothing
    to inspect, without duplicating the extension set.
    """
    return any(
        Path(ac.path).suffix.lower() in _STYLE_METADATA_FAMILIES
        for ac in selected_artifacts
    )


async def fetch_style_metadata_artifacts(
    selected_artifacts: list[ArtifactChange],
    final_snapshot_zip: zipfile.ZipFile,
    model: str,
    task_id: str | None = None,
) -> list[Artifact]:
    """
    Extract exact style facts (bold, colors, borders, gridlines, frozen
    panes, print settings) from spreadsheet output artifacts.

    Spreadsheet formatting criteria are otherwise judged either from the
    plain-value text extraction (no style info at all) or a rendered
    screenshot (forces the judge to eyeball colors/bold from pixels, and
    loses print-only settings and any sheet the renderer didn't reach). This
    gives the judge the exact underlying facts instead.

    Applies to the whole spreadsheet family. .xlsx/.xlsm are read directly
    by openpyxl; .xls (legacy binary) falls back to a LibreOffice conversion
    inside spreadsheet_to_style_metadata, which costs a subprocess round
    trip (120s timeout) for those files only. Agent-produced workbooks are
    almost always .xlsx, so that path is rare in practice.
    """
    _task = task_id or "unknown"

    seen_paths: set[str] = set()
    extracted: list[dict[str, str]] = []

    for ac in selected_artifacts:
        if ac.path in seen_paths:
            continue
        family = _STYLE_METADATA_FAMILIES.get(Path(ac.path).suffix.lower())
        if family is None:
            continue
        seen_paths.add(ac.path)

        file_bytes = read_artifact_from_snapshot_zip(final_snapshot_zip, ac.path)
        if not file_bytes:
            logger.warning(
                f"[JUDGE][GRADER][STYLE_METADATA] task={_task} | "
                f"file not found in final snapshot | file={ac.path}"
            )
            continue

        try:
            # Dispatched by name (not a prebuilt table) so the functions
            # resolve at call time. Every family is matched explicitly: an
            # `else` fallthrough here silently handed .docx to the pptx parser,
            # which failed inside the except below and dropped the artifact.
            if family is InputFileFamily.SPREADSHEET:
                output = await spreadsheet_to_style_metadata(file_bytes, ac.path)
            elif family is InputFileFamily.PPTX:
                output = await pptx_to_style_metadata_output(file_bytes, ac.path)
            elif family is InputFileFamily.DOCX:
                output = await docx_to_style_metadata_output(file_bytes, ac.path)
            else:
                logger.warning(
                    f"[JUDGE][GRADER][STYLE_METADATA] task={_task} | "
                    f"{family} is style-eligible but has no extractor wired "
                    f"here | file={ac.path}"
                )
                continue
        except Exception as e:
            logger.warning(
                f"[JUDGE][GRADER][STYLE_METADATA] task={_task} | "
                f"style metadata extraction failed for {ac.path}: {e}"
            )
            # Say so rather than dropping the file silently. Skipping it left the
            # judge with no mention of a file it was grading, and absence of
            # evidence reads as "nothing notable found" rather than "could not be
            # read" — the same distinction the budget-omission stub and the
            # unsupported-format note already preserve.
            extracted.append(
                {
                    "path": ac.path,
                    "content": (
                        '<style_metadata unreadable="true">Style facts could not '
                        "be extracted from this file; draw no conclusion from "
                        "their absence.</style_metadata>"
                    ),
                }
            )
            continue

        if not output.text:
            # "Inspected, nothing notable" is a real answer and for a formatting
            # criterion it is evidence of absence rather than absence of
            # evidence — a workbook with no styled cells and no native tables is
            # exactly what a criterion about bold headers should fail on. Left
            # unsaid, the judge could not tell it from a file that was never
            # examined, which is the ambiguity every other branch here avoids.
            logger.info(
                f"[JUDGE][GRADER][STYLE_METADATA] task={_task} | "
                f"no notable styling found in {ac.path}"
            )
            extracted.append(
                {
                    "path": ac.path,
                    "content": (
                        '<style_metadata none="true">This file was inspected and '
                        "contains no notable styling.</style_metadata>"
                    ),
                }
            )
            continue

        extracted.append({"path": ac.path, "content": output.text})

    if not extracted:
        return []

    # Not truncate_files_equally: it delegates to truncate_text_to_tokens,
    # which keeps a head AND a tail joined by a sentinel. That is right for a
    # log and wrong for markup built of sibling blocks — on a five-sheet sample
    # it left Sheet5's cells inside <template sheet="Sheet1">, so the judge was
    # told one sheet's formatting belonged to another. The prompt-level fitter
    # cannot repair that, because the splice has already happened here. Same
    # even split of the budget, but each share is cut head-only and re-closed.
    available = max(0, STYLE_METADATA_TOKEN_BUDGET - 500)
    sizes = {
        e["path"]: count_tokens(e.get("content", ""), model, conservative_estimate=True)
        for e in extracted
    }
    total_before = sum(sizes.values())

    # Only cut when the aggregate actually overflows. Capping every file at 1/N
    # unconditionally shrank a workbook that fit comfortably beside a small deck
    # — losing the later worksheets this feature exists to surface, and the
    # prompt fitter cannot restore what never arrived.
    if total_before <= available:
        shares = dict(sizes)
    else:
        # Water-filling: a file under its equal share keeps only what it needs
        # and the surplus goes to the files that are over, so one small deck
        # does not cost a large workbook half the budget.
        shares = {}
        remaining_budget = available
        pending = sorted(sizes, key=lambda p: sizes[p])
        for index, path in enumerate(pending):
            share = remaining_budget // (len(pending) - index)
            granted = min(sizes[path], share)
            shares[path] = granted
            remaining_budget -= granted

    truncated_files: list[dict[str, str]] = []
    budget_metadata: dict[str, Any] = {"files": [], "was_truncated": False}
    total_after = 0
    for entry in extracted:
        original = entry.get("content", "")
        path = entry["path"]
        fitted = (
            original
            if shares[path] >= sizes[path]
            else truncate_markup_to_tokens(original, shares[path], model)
        )
        after = count_tokens(fitted, model, conservative_estimate=True)
        total_after += after
        # Compare exactly, not by length: truncate_markup_to_tokens returns the
        # head plus the closing tags needed to re-balance it, so when the dropped
        # tail is shorter than those closers the result can be the same size or
        # larger. A length test then reports "complete", the artifact loses its
        # truncated="true" marker, and the judge treats partial evidence as the
        # whole picture.
        cut = fitted != original
        # final_tokens is read by the per-file log below. truncate_files_equally
        # used to supply it; the hand-rolled replacement dropped the field while
        # keeping the log line, so every entry reported tokens=0.
        budget_metadata["files"].append(
            {"path": path, "was_truncated": cut, "final_tokens": after}
        )
        budget_metadata["was_truncated"] = budget_metadata["was_truncated"] or cut
        truncated_files.append({"path": path, "content": fitted})
    budget_metadata["total_original_tokens"] = total_before
    budget_metadata["total_final_tokens"] = total_after

    if budget_metadata.get("was_truncated"):
        logger.warning(
            f"[JUDGE][GRADER][STYLE_METADATA] task={_task} | "
            f"style metadata exceeded budget: "
            f"{budget_metadata['total_original_tokens']:,} -> "
            f"{budget_metadata['total_final_tokens']:,} tokens across "
            f"{len(extracted)} artifact(s)"
        )

    results: list[Artifact] = []
    for file_dict, file_meta in zip(
        truncated_files, budget_metadata["files"], strict=False
    ):
        path = file_dict["path"]
        content = file_dict.get("content", "")
        was_truncated = bool(file_meta.get("was_truncated"))

        if not content:
            # Empty here always means the fitter cut it to nothing: all three
            # branches that populate `extracted` write non-empty content (the
            # unreadable stub, the none stub, or a text guarded by `if not
            # output.text`), so an empty `fitted` differs from its original and
            # was_truncated is necessarily True. The `if not was_truncated:
            # continue` that used to guard this could never fire.
            #
            # The aggregate budget was already spent before this file got
            # any share — say so explicitly rather than silently providing
            # zero evidence, which the judge could otherwise mistake for
            # "nothing notable found" rather than "not evaluated".
            content = "<!-- STYLE METADATA OMITTED: token budget exhausted -->"

        # No "(TRUNCATED)" suffix here. That marker is a prompt_builder
        # convention for artifacts whose title is rendered; style metadata is
        # rendered by multi_representation_eval as <STYLE_METADATA file="...">
        # built from `path`, so this title never reaches the judge and the
        # suffix only ever misled readers of this code into documenting a marker
        # the prompt cannot contain. early_truncated below is the real signal,
        # surfaced as truncated="true" on the block.
        title = f"{path} [style metadata]"

        results.append(
            Artifact(
                path=path,
                artifact_type="file",
                change_type="unchanged",
                title=title,
                content=content,
                is_visual=False,
                visual_url=None,
                screenshot_url=None,
                embedded_images=None,
                sub_artifacts=None,
                early_truncated=was_truncated,
            )
        )
        logger.info(
            f"[JUDGE][GRADER][STYLE_METADATA] task={_task} | "
            f"extracted style metadata for {path} | "
            f"tokens={file_meta.get('final_tokens', 0)}"
        )

    return results

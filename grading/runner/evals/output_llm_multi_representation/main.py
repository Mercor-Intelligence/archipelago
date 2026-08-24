import json
import zipfile

from litellm import Choices
from loguru import logger
from pydantic import ValidationError

from runner.evals.db_diff_llm_tools import db_diff_llm_tools_eval
from runner.evals.models import EvalImplInput
from runner.helpers.models import HelperIds
from runner.helpers.snapshot_diff import extract_artifact_changes_from_diff
from runner.helpers.snapshot_diff.types import Artifact
from runner.models import VerifierResult
from runner.utils.file_transformations.xml_utils import xml_escape
from runner.utils.llm import build_messages, call_llm
from runner.utils.token_utils import (
    count_tokens,
    count_tokens_uncached,
    get_model_context_limit,
    truncate_markup_to_tokens,
)

from ..output_llm.artifact_filters import FileTypeCategory, is_valid_file_type
from ..output_llm.utils.context_allocation import estimate_image_tokens
from ..output_llm.utils.log_helpers import (
    get_artifact_identity,
    log_artifact_selector_result,
    log_diff_extraction,
    log_grader_final_prompt,
    log_grader_result,
    log_grader_start,
    log_grader_truncation,
)
from ..output_llm.utils.prompts import (
    GRADING_REMINDER_MARKER,
    GRADING_SYSTEM_PROMPT,
    GRADING_SYSTEM_PROMPT_NO_REFERENCE,
    STYLE_METADATA_TRUNCATION_NOTE,
    GradingResponseSchema,
)
from ..output_llm.utils.services.artifact_evaluate import (
    prepare_images_for_llm,
    select_artifacts_to_evaluate,
)
from ..output_llm.utils.services.prompt_builder import build_grading_prompt
from ..output_llm.utils.shared import (
    LLM_JUDGE_TIMEOUT,
    MAX_JSON_RETRIES,
    estimate_artifact_tokens,
    extract_task_prompt,
    filter_artifacts_programmatically,
    get_artifact_display_names,
    should_auto_fail_missing_file_type,
)
from .artifact_representations import (
    ArtifactSelectionWithTransformations,
    fetch_artifacts_with_transformations,
    fetch_style_metadata_artifacts,
    has_style_metadata_artifacts,
    transform_output_artifacts,
)

# Headroom reserved for the model's own JSON grading response when checking
# whether there's real room left to append style metadata post-budget.
RESPONSE_RESERVE_TOKENS = 2000


# Rough size of the omitted="true" stub, held in reserve per pending file so a
# large earlier file cannot trim itself over every later file's mention.
_STUB_TOKENS = 48


def _omission_notice(paths: list[str]) -> str:
    """Name files whose style metadata is absent, and why.

    The judge is told the style block is ground truth for formatting, so a file
    that simply never appears reads as "nothing notable found" rather than "not
    evaluated" — turning a budget shortfall into a fabricated pass. Naming the
    file costs a few tokens and removes the inference.
    """
    return (
        "<!-- style metadata not included for: "
        + xml_escape(", ".join(paths))
        + " (prompt budget exhausted); these files were NOT evaluated for "
        + "formatting, so draw no conclusion from their absence -->"
    )


def _truncation_notice(paths: list[str]) -> str:
    """Name files trimmed while fitting, which the preamble cannot know about.

    STYLE_METADATA_TRUNCATION_NOTE is chosen before fitting runs, from the
    extractor's own early_truncated flag. A block the fitter trims afterwards
    gets truncated="true" with nothing in the preamble explaining it, so the
    marker arrives unexplained. This says which files, at the point it is known.
    """
    return (
        "<!-- style metadata truncated to fit for: "
        + xml_escape(", ".join(paths))
        + '; those blocks are marked truncated="true" and list only part of the '
        + "file, so the absence of a font, color, fill or table inside them is "
        + "not evidence that it is missing from the file -->"
    )


def _fit_style_addition(
    preamble: str,
    blocks: list[tuple[str, str]],
    budget: int,
    model: str,
    task_id: str,
) -> str:
    """Fit <STYLE_METADATA> blocks into `budget` without corrupting any of them.

    Three guarantees, each earned from a real defect:

    - No block is ever split. Truncating the assembled string kept a head and a
      tail, leaving the first file's opening tag paired with the second's
      closing tag, so the judge read one file's formatting under another's name.
    - A block too large to include whole is trimmed from the head only and
      re-closed, and says truncated="true". Trimming head-and-tail spliced
      Sheet2's cells into <template sheet="Sheet1">.
    - Every file is named even when nothing of it fits. Silently omitting one
      let the judge treat unevaluated formatting as absent formatting, which is
      the exact failure this feature exists to remove.
    """
    if not blocks:
        return ""

    def tokens(text: str) -> int:
        return count_tokens(text, model, conservative_estimate=True)

    emitted: list[str] = []
    # Held back so the closing notices can always be written. At very tight
    # budgets even a per-file stub would not fit, and a file that goes entirely
    # unmentioned is the one outcome this must never produce.
    #
    # Measured against this call's actual paths, not a fixed constant: the
    # notices name every file they cover, so a run with many files or long paths
    # needs a proportionally larger reserve, and a constant tuned for two short
    # names would under-reserve and let the assembled block overshoot `budget`.
    # Both notices are charged even though a file cannot be both trimmed and
    # omitted — erring toward reserving slightly too much, which costs a little
    # evidence, over too little, which costs correctness.
    all_paths = [path for path, _ in blocks]
    budget -= tokens(_omission_notice(all_paths)) + tokens(
        _truncation_notice(all_paths)
    )
    spent = tokens(preamble)
    trimmed: list[str] = []
    omitted: list[str] = []

    for index, (path, block) in enumerate(blocks):
        # "\n\n" between blocks; charge it so the total stays honest.
        separator = 2 if emitted else 0
        remaining = budget - spent - separator
        # Hold back enough for every later file to at least name itself.
        # Without this a single large workbook trimmed to the whole budget and
        # the small deck after it disappeared with no mention — reintroducing
        # the silent-omission bug one layer up.
        allowance = remaining - _STUB_TOKENS * (len(blocks) - index - 1)
        if allowance <= 0:
            omitted.append(path)
            continue

        if tokens(block) <= allowance:
            chosen = block
        else:
            remaining = allowance
            opening, _, rest = block.partition("\n")
            body = rest.rsplit("</STYLE_METADATA>", 1)[0].rstrip("\n")
            if 'truncated="true"' not in opening:
                opening = opening.replace(">", ' truncated="true">', 1)
            scaffold = f"{opening}\n\n</STYLE_METADATA>"
            head = truncate_markup_to_tokens(body, remaining - tokens(scaffold), model)
            if not head.strip():
                # Not even one element fits: still tell the judge the file
                # exists and was not shown, so absence is not read as evidence.
                stub = (
                    f'<STYLE_METADATA file="{xml_escape(path)}" omitted="true">'
                    f"Style metadata for this file did not fit the prompt budget "
                    f"and was not included; draw no conclusion from its absence."
                    f"</STYLE_METADATA>"
                )
                if tokens(stub) <= remaining:
                    emitted.append(stub)
                    spent += tokens(stub) + separator
                omitted.append(path)
                continue
            chosen = f"{opening}\n{head}\n</STYLE_METADATA>"
            trimmed.append(path)

        emitted.append(chosen)
        spent += tokens(chosen) + separator

    if trimmed:
        logger.warning(
            f"[JUDGE][GRADER] task={task_id} | trimmed style metadata for "
            f"{', '.join(trimmed)} to fit the remaining {budget:,} tokens; later "
            f"sheets/slides of those files are not represented"
        )
        # In the prompt too, not only the log. Trimming here happens after the
        # preamble's truncation note was already decided, so without this the
        # judge sees truncated="true" with nothing telling it what that implies.
        emitted.append(_truncation_notice(trimmed))
    if omitted:
        logger.warning(
            f"[JUDGE][GRADER] task={task_id} | no style metadata room for "
            f"{len(omitted)} file(s): {', '.join(omitted)} — criteria about those "
            f"files have no formatting evidence"
        )
        # Stated in the prompt, not only logged. The sibling path in
        # fetch_style_metadata_artifacts already emits an explicit "OMITTED"
        # marker for the same reason: the judge is told this block is ground
        # truth, so unmentioned evidence reads as "nothing notable found"
        # rather than "not evaluated".
        emitted.append(_omission_notice(omitted))

    return preamble + "\n\n".join(emitted)


async def multi_representation_eval(input: EvalImplInput) -> VerifierResult:
    verifier_values = input.verifier.verifier_values or {}
    task_id = input.verifier.task_id or "unknown"
    criteria = verifier_values.get("criteria", "")

    log_grader_start(task_id, criteria, is_negative=False)

    if not criteria:
        raise ValueError("Missing required field: criteria")

    # Database grading target: a .db file is a binary SQLite database, so grading
    # it as a text artifact is meaningless. Instead delegate to the EXACT same
    # logic the db_diff_llm_tools verifier uses (shared DB_DIFF helper + the
    # tool-augmented diff exploration loop), so improvements to either verifier
    # benefit both. Requires HelperIds.DB_DIFF in this eval's helper_dependencies.
    if (
        verifier_values.get("expected_file_type")
        == FileTypeCategory.DATABASE_FILES.value
    ):
        logger.info(
            f"[JUDGE][GRADER] task={task_id} | Grading Target is "
            f"'{FileTypeCategory.DATABASE_FILES.value}' | delegating to db_diff_llm_tools"
        )
        return await db_diff_llm_tools_eval(input)

    try:
        if not input.helper_results:
            raise ValueError("Missing helper results")

        final_answer = input.helper_results[HelperIds.FINAL_ANSWER]
        diff_result = input.helper_results[HelperIds.SNAPSHOT_DIFF]

        model = input.grading_settings.llm_judge_model
        extra_args = input.grading_settings.llm_judge_extra_args

        task_prompt = extract_task_prompt(input)

        all_artifacts = extract_artifact_changes_from_diff(diff_result)
        log_diff_extraction(task_id, diff_result, all_artifacts, criteria=criteria)

        expected_file_type = verifier_values.get("expected_file_type")
        if not expected_file_type:
            expected_file_type = (
                "All output (modified files and final message in console)"
            )
        elif not is_valid_file_type(expected_file_type):
            expected_file_type = (
                "All output (modified files and final message in console)"
            )

        filtered_artifacts = filter_artifacts_programmatically(
            all_artifacts,
            expected_file_type,
            task_id=task_id,
            criteria=criteria,
        )

        if should_auto_fail_missing_file_type(expected_file_type, filtered_artifacts):
            return VerifierResult(
                verifier_id=input.verifier.verifier_id,
                verifier_version=input.verifier.verifier_version,
                score=0.0,
                verifier_result_values={
                    "judge_grade": "fail",
                    "grade_rationale": (
                        f"No files matching the expected type ({expected_file_type}) were found. "
                        f"The agent did not produce any artifacts of the required type."
                    ),
                    "evaluated_artifacts": "",
                    "auto_failed": True,
                    "auto_fail_reason": "no_matching_file_type",
                },
            )

        total_artifact_tokens = sum(
            estimate_artifact_tokens(a, model) for a in filtered_artifacts
        )
        context_limit = get_model_context_limit(model)
        artifact_budget_threshold = int(context_limit * 0.50)

        if total_artifact_tokens <= artifact_budget_threshold:
            selected_artifacts = filtered_artifacts
            selection_metadata = None
        else:
            selected_artifacts, selection_metadata = await select_artifacts_to_evaluate(
                filtered_artifacts,
                criteria,
                model=model,
                extra_args=extra_args,
                task_id=task_id,
                task_prompt=task_prompt,
            )

        selected_identities = {get_artifact_identity(a) for a in selected_artifacts}
        rejected_artifacts = [
            a
            for a in filtered_artifacts
            if get_artifact_identity(a) not in selected_identities
        ]

        log_artifact_selector_result(
            task_id,
            input_count=len(filtered_artifacts),
            selected_count=len(selected_artifacts),
            selected_artifacts=selected_artifacts,
            criteria=criteria,
            rejected_artifacts=rejected_artifacts if rejected_artifacts else None,
        )

        # When enable_visual_grading is set, render output document files
        # (docx, pdf, pptx, xlsx) from the final snapshot as page images
        # so the LLM judge can evaluate visual properties like formatting,
        # colors, page count, and layout that are lost in text extraction.
        # Spreadsheet style metadata is gathered regardless — see below.
        enable_visual_grading = verifier_values.get("enable_visual_grading", False)
        visual_output_artifacts: list[Artifact] = []
        style_metadata_artifacts: list[Artifact] = []
        wants_style_metadata = has_style_metadata_artifacts(selected_artifacts)
        # Only touch the snapshot zip if something will actually read from it —
        # opening it for every criterion that has neither images nor a
        # style-eligible file is pure overhead.
        if selected_artifacts and (enable_visual_grading or wants_style_metadata):
            try:
                # All verifiers for a trajectory are gathered concurrently and
                # share this one BytesIO, and widening the condition above means
                # far more of them now open it. That is safe, and deliberately
                # not "fixed" by copying: zipfile wraps the stream in a
                # _SharedFile that keeps its own logical offset and re-seeks to
                # it under a lock before every read, so another coroutine's
                # seek(0) cannot move the cursor out from under a read in
                # progress — and the seek+read pair has no await inside it, so
                # on a single-threaded loop it cannot interleave at all.
                # Verified empirically: 24 concurrent verifiers x 4 reads over
                # one shared BytesIO, zero corrupt or short reads. Handing each
                # verifier its own io.BytesIO(getvalue()) would copy the whole
                # snapshot per verifier — tens of MB x ~80 verifiers — which is
                # a far worse problem than the one it would be guarding against.
                input.final_snapshot_bytes.seek(0)
                with zipfile.ZipFile(input.final_snapshot_bytes, "r") as final_zip:
                    if enable_visual_grading:
                        visual_output_artifacts = await transform_output_artifacts(
                            selected_artifacts=selected_artifacts,
                            final_snapshot_zip=final_zip,
                            task_id=task_id,
                        )
                    # Deliberately NOT gated on enable_visual_grading. Style
                    # metadata is plain text, not a rendered image, so it has no
                    # dependency on the image pipeline — and the criteria that
                    # need it most are exactly the ones failing on runs that never
                    # opted into images. The trajectory behind criteria 56 was one
                    # of those: its judge saw only text artifacts and failed the
                    # criterion for "no color information", which gating this
                    # behind the image flag would not have fixed.
                    #
                    # Nor is it gated on the criterion's wording. Eligibility is
                    # purely by file extension, so a content-only criterion on a
                    # style-eligible file also receives this. That is deliberate:
                    # any content-based gate is the same brittle heuristic that
                    # produced the bug in the first place, and a verifier opt-in
                    # flag would default off and so miss the criteria that need
                    # it. Measured cost of carrying it everywhere instead:
                    # ~14.5k tokens for the 17MB 8-sheet workbook and ~600 for a
                    # one-page docx, i.e. 1.4% and 0.05% of a 1M window. It is
                    # appended from leftover headroom after the prompt budget has
                    # already been applied, and skipped when there is none, so it
                    # never displaces artifact text. Extraction is cached per
                    # grading run, so the parse cost is paid once per file.
                    if wants_style_metadata:
                        # Best-effort: a workbook that won't parse must never
                        # fail a criterion that would otherwise have graded
                        # fine from the text representation alone.
                        try:
                            style_metadata_artifacts = (
                                await fetch_style_metadata_artifacts(
                                    selected_artifacts=selected_artifacts,
                                    final_snapshot_zip=final_zip,
                                    model=model,
                                    task_id=task_id,
                                )
                            )
                        except Exception as exc:
                            logger.warning(
                                f"[JUDGE][GRADER] task={task_id} | style metadata "
                                f"extraction failed ({type(exc).__name__}: {exc}); "
                                f"grading without it"
                            )
            except Exception as exc:
                # Opening the snapshot used to happen only under
                # enable_visual_grading; it now also happens for any
                # style-eligible artifact. Keep that widening additive — a
                # snapshot that can't be opened must not newly break criteria
                # that never asked for images. Visual grading keeps its
                # pre-existing behaviour of surfacing the failure.
                if enable_visual_grading:
                    raise
                logger.warning(
                    f"[JUDGE][GRADER] task={task_id} | could not open the final "
                    f"snapshot for style metadata ({type(exc).__name__}: {exc}); "
                    f"grading without it"
                )
            finally:
                # Rewind for later readers, but never let the reset itself be
                # the thing that fails the criterion — final_snapshot_bytes is
                # untyped (Any) and is None in some callers, so the seek can
                # raise even once the error above has been handled.
                try:
                    input.final_snapshot_bytes.seek(0)
                except Exception:
                    pass

            if visual_output_artifacts:
                logger.info(
                    f"[JUDGE][GRADER] task={task_id} | generated {len(visual_output_artifacts)} "
                    f"visual representations of output artifacts"
                )
            if style_metadata_artifacts:
                logger.info(
                    f"[JUDGE][GRADER] task={task_id} | extracted style metadata for "
                    f"{len(style_metadata_artifacts)} output artifact(s)"
                )

        # Fetch reference artifacts with representation expansion
        artifacts_to_reference_specs = verifier_values.get("artifacts_to_reference", [])
        artifacts_to_reference = None

        if artifacts_to_reference_specs:
            parsed_specs = [
                ArtifactSelectionWithTransformations(**spec)
                if isinstance(spec, dict)
                else spec
                for spec in artifacts_to_reference_specs
            ]

            input.initial_snapshot_bytes.seek(0)
            with zipfile.ZipFile(input.initial_snapshot_bytes, "r") as initial_zip:
                artifacts_to_reference = await fetch_artifacts_with_transformations(
                    artifacts_to_reference=parsed_specs,
                    initial_snapshot_zip=initial_zip,
                    task_id=task_id,
                    criteria=criteria,
                )
            input.initial_snapshot_bytes.seek(0)

            logger.info(
                f"[JUDGE][GRADER] task={task_id} | fetched {len(artifacts_to_reference)} "
                f"transformed artifacts from {len(artifacts_to_reference_specs)} specs"
            )

        constructed_prompt = build_grading_prompt(
            criteria=criteria,
            final_answer=final_answer,
            model=model,
            artifacts_to_evaluate=selected_artifacts if selected_artifacts else None,
            artifacts_to_reference=artifacts_to_reference,
            include_full_content=True,
            task_id=task_id,
            expected_file_type=expected_file_type,
            task_prompt=task_prompt,
        )

        system_prompt = (
            GRADING_SYSTEM_PROMPT
            if artifacts_to_reference
            else GRADING_SYSTEM_PROMPT_NO_REFERENCE
        )

        # Merge rendered output artifact images into the prompt so the LLM
        # judge can see visual properties alongside the text extraction.
        if visual_output_artifacts:
            output_images = prepare_images_for_llm(visual_output_artifacts)
            if output_images:
                existing = constructed_prompt.visual_artifacts_to_evaluate or []
                constructed_prompt.visual_artifacts_to_evaluate = (
                    list(existing) + output_images
                )
                logger.info(
                    f"[JUDGE][GRADER] task={task_id} | added {len(output_images)} "
                    f"rendered output images to prompt "
                    f"(total images: {len(constructed_prompt.visual_artifacts_to_evaluate)})"
                )

        # Best-effort, like the extraction itself. Style metadata is additive
        # evidence, so a failure assembling or fitting it must not fail a
        # criterion that would otherwise have graded from the text alone. The
        # snapshot-open path above already works this way; this is the same rule
        # applied to the append, which was only covered by the eval-wide handler
        # that re-raises as "LLM grading failed".
        try:
            if style_metadata_artifacts:
                # `file` carries the real path, escaped like every other attribute
                # in this feature — a name containing a quote or ampersand would
                # otherwise produce a malformed block. Truncation is a separate
                # attribute rather than decoration appended to the filename, so
                # the judge isn't told the file is called
                # "report.xlsx [style metadata] (TRUNCATED)".
                style_block_list = [
                    (
                        a.path,
                        f'<STYLE_METADATA file="{xml_escape(a.path)}"'
                        + (' truncated="true"' if a.early_truncated else "")
                        + f">\n{a.content}\n</STYLE_METADATA>",
                    )
                    for a in style_metadata_artifacts
                    if a.content
                ]
                style_blocks = "\n\n".join(b for _, b in style_block_list)
                if style_blocks:
                    style_was_truncated = any(
                        a.early_truncated for a in style_metadata_artifacts
                    )
                    # Style-specific note, not the shared TRUNCATION_NOTE: that
                    # one names a "(TRUNCATED)" title marker this block never
                    # renders, and tells the judge to assess the visible content
                    # — which for a coverage criterion turns cut sheets into a
                    # pass. See STYLE_METADATA_TRUNCATION_NOTE.
                    truncation_note = (
                        f"\n{STYLE_METADATA_TRUNCATION_NOTE}\n"
                        if style_was_truncated
                        else ""
                    )
                    # Wording stays format-neutral: this block carries workbook,
                    # slide-deck and document metadata alike, and each block's own
                    # markup (<template sheet=...> vs <presentation><slide> vs
                    # <style_metadata>) says which is which. Describing it as
                    # spreadsheet data told the judge a deck was a spreadsheet and
                    # promised properties like frozen panes that a deck never has.
                    # Each format's facts are named so the judge doesn't assume a
                    # missing property was checked and found absent.
                    style_preamble = (
                        "\n\nThe following is exact style metadata extracted directly "
                        "from the underlying structure of the agent's output files "
                        "(fonts, sizes, bold/italic, colors, borders, and — where the "
                        "format has them — number formats, gridlines, frozen panes, "
                        "autofilter, native table objects and print settings for "
                        "worksheets; per-slide text styles for presentations; or page "
                        "count, page background, per-paragraph run styles, and table "
                        "and chart fills for documents). Treat it as ground truth for "
                        "formatting criteria — it is more reliable than what is "
                        f"visible in a rendered screenshot:\n{truncation_note}\n"
                    )
                    # build_grading_prompt's own budgeting (TOTAL_CONTENT_BUDGET_RATIO,
                    # 90%) only bounds base_prompt_tokens (criteria + final_answer)
                    # plus artifact content and images — it deliberately excludes
                    # the system prompt, which is sent as a separate message and
                    # meant to fit in the remaining 10% alongside the response.
                    # Re-applying that same 90% ratio here and ALSO subtracting
                    # system_prompt would double-count that reserve and could
                    # zero out headroom even when real room remains. Measure
                    # against the full context window instead, with the model's
                    # own response as the only additional reserve.
                    context_limit = get_model_context_limit(model)
                    image_tokens = estimate_image_tokens(
                        constructed_prompt.visual_artifacts_to_evaluate
                    )
                    # Uncached: these two strings are unique per verifier and
                    # counted once, so memoizing them would pin a whole prompt body
                    # per verifier for the life of the process without ever hitting.
                    used_tokens = (
                        count_tokens_uncached(
                            system_prompt, model, conservative_estimate=True
                        )
                        + count_tokens_uncached(
                            constructed_prompt.user_prompt,
                            model,
                            conservative_estimate=True,
                        )
                        + image_tokens
                    )
                    remaining_headroom = (
                        context_limit - used_tokens - RESPONSE_RESERVE_TOKENS
                    )

                    if remaining_headroom <= 0:
                        # Every file still gets named. Dropping the block outright
                        # was the one outcome _fit_style_addition exists to
                        # prevent: the judge is told style metadata is ground
                        # truth for formatting, so files that go unmentioned read
                        # as "nothing notable found" rather than "not evaluated",
                        # and a criterion about them fails for no stated reason.
                        #
                        # There is no free room by definition here, so the notice
                        # is paid for out of RESPONSE_RESERVE_TOKENS: ~85 tokens
                        # of a 2,000-token reserve for one file, ~460 for twelve.
                        # Reserving for it before this comparison instead (the
                        # review's suggestion) moves the same cost rather than
                        # removing it — once headroom is exhausted, any notice
                        # comes out of the response reserve either way. Chosen
                        # deliberately: the reserve is a conservative floor for a
                        # short JSON verdict, and a shrunken reserve degrades one
                        # response, while a silently missing file is the exact
                        # false positive this feature exists to remove.
                        logger.warning(
                            f"[JUDGE][GRADER] task={task_id} | no headroom left for "
                            f"style metadata ({used_tokens:,} tokens already used "
                            f"of {context_limit:,} context) — naming the "
                            f"{len(style_block_list)} affected file(s) without their "
                            f"metadata rather than dropping them silently"
                        )
                        style_addition = "\n\n" + _omission_notice(
                            [path for path, _ in style_block_list]
                        )
                    else:
                        style_addition = _fit_style_addition(
                            preamble=style_preamble,
                            blocks=style_block_list,
                            budget=remaining_headroom,
                            model=model,
                            task_id=task_id,
                        )
                        logger.info(
                            f"[JUDGE][GRADER] task={task_id} | added style "
                            f"metadata for {len(style_metadata_artifacts)} "
                            f"artifact(s) to prompt ({remaining_headroom:,} tokens "
                            f"headroom available)"
                        )

                    # Insert BEFORE the trailing REMINDER block, which restates
                    # the response-format requirement and is meant to be the
                    # last thing the model sees. Appending after it buried the
                    # instructions behind a large evidence block, inviting
                    # malformed JSON and wasted retries.
                    prompt = constructed_prompt.user_prompt
                    marker_at = prompt.rfind(GRADING_REMINDER_MARKER)
                    if marker_at == -1:
                        constructed_prompt.user_prompt = prompt + style_addition
                    else:
                        constructed_prompt.user_prompt = (
                            prompt[:marker_at]
                            + style_addition.strip("\n")
                            + "\n\n"
                            + prompt[marker_at:]
                        )
        except Exception as exc:
            logger.warning(
                f"[JUDGE][GRADER] task={task_id} | could not append style "
                f"metadata ({type(exc).__name__}: {exc}); grading without it"
            )

        if constructed_prompt.token_metadata:
            log_grader_truncation(
                task_id,
                was_truncated=constructed_prompt.token_metadata.get(
                    "was_truncated", False
                ),
                original_tokens=constructed_prompt.token_metadata.get(
                    "total_original_tokens", 0
                ),
                final_tokens=constructed_prompt.token_metadata.get(
                    "total_final_tokens", 0
                ),
                files_metadata=constructed_prompt.token_metadata.get("files"),
                criteria=criteria,
            )

        log_grader_final_prompt(
            task_id=task_id,
            criteria=criteria,
            is_negative=False,
            model=model,
            system_prompt_chars=len(system_prompt),
            user_prompt_chars=len(constructed_prompt.user_prompt),
            artifacts_to_evaluate=selected_artifacts if selected_artifacts else None,
            artifacts_to_reference=artifacts_to_reference,
            image_count=len(constructed_prompt.visual_artifacts_to_evaluate or []),
        )

        messages = build_messages(
            system_prompt=system_prompt,
            user_prompt=constructed_prompt.user_prompt,
            images=constructed_prompt.visual_artifacts_to_evaluate,
        )

        parsed = None
        raw_content = None
        for _attempt in range(MAX_JSON_RETRIES):
            response = await call_llm(
                model=model,
                messages=messages,
                timeout=LLM_JUDGE_TIMEOUT,
                extra_args=extra_args,
                response_format={"type": "json_object"},
            )

            choices = response.choices
            if not choices or not isinstance(choices[0], Choices):
                continue

            raw_content = choices[0].message.content
            if not raw_content:
                continue

            try:
                try:
                    raw_json = json.loads(raw_content)
                    if isinstance(raw_json, dict) and isinstance(
                        raw_json.get("rationale"), dict
                    ):
                        raw_json["rationale"] = json.dumps(raw_json["rationale"])
                        raw_content = json.dumps(raw_json)
                except json.JSONDecodeError:
                    pass

                parsed = GradingResponseSchema.model_validate_json(raw_content)
                break
            except ValidationError:
                continue

        if parsed is None:
            raise ValueError(f"Invalid JSON after {MAX_JSON_RETRIES} attempts")

        is_criteria_true = parsed.is_criteria_true
        rationale = parsed.rationale
        judge_grade = "pass" if is_criteria_true else "fail"

        evaluated_artifact_names = get_artifact_display_names(selected_artifacts)

        result_values = {
            "judge_grade": judge_grade,
            "grade_rationale": rationale,
            "evaluated_artifacts": evaluated_artifact_names,
        }

        log_grader_result(
            task_id,
            is_negative=False,
            passed=is_criteria_true,
            score=1.0 if is_criteria_true else 0.0,
            criteria=criteria,
        )

        score = 1.0 if is_criteria_true else 0.0

        return VerifierResult(
            verifier_id=input.verifier.verifier_id,
            verifier_version=input.verifier.verifier_version,
            score=score,
            verifier_result_values=result_values,
        )

    except Exception as e:
        error_msg = f"LLM grading failed: {str(e)}"
        raise ValueError(error_msg) from e

# File transformations

Each transformation turns an artifact into something an LLM judge can read.
The `*_to_style_metadata` modules are the formatting ones: they answer criteria
about fonts, sizes, colours, links and layout for docx, pptx and spreadsheets.

## Absence of a fact reads as a violated criterion

This block is presented to the judge as ground truth about the file. When a
fact is missing the judge does not abstain — it infers, usually from a rendered
screenshot, and fails the criterion. So a style extractor has three jobs:

1. **Report the value that actually renders, not the reference to it.** OOXML
   stores formatting indirectly all over the place: a colour as a theme slot
   plus a tint, a size on a slide layout rather than the run, a font as a
   scheme name whose stored spelling has gone stale. Emitting `theme:6` or
   `inherited` is honest but unverifiable, and a criterion naming an exact hex
   or point size is failed against a file that satisfies it.

2. **Say where a resolved value came from.** Every resolution is an inference,
   so resolved values carry a `*_source` attribute (`layout_placeholder`,
   `paragraph_style`, `auto`). A reader can then tell a size the run states
   from one this code walked a chain to find.

3. **Never invent a value.** Where nothing readable declares one, the output
   says `inherited` or `auto` rather than guessing. A confidently wrong number
   is worse than an absent one, because the judge has no way to discount it.
   Several tests exist only to pin this boundary.

## Counts are load-bearing

A criterion like "all cross-references navigate correctly" is vacuously true
at zero, and without a count the judge cannot tell *none* from *unreported*.
So `<links>`, `<tables>`, `<charts>` and `<headers_footers>` emit their counts
even at zero.

Two places do not, and both are deliberate: `<fonts>` is omitted entirely when
a sheet has no fonts, and `<run_styles />` self-closes without a count — a
sheet or document with no text has nothing for a font or style criterion to be
about. A spreadsheet with no styled cells at all produces empty output for the
same reason. If you add a section, prefer the zero-count form.

## Everything is capped, and the cap is disclosed

These summaries share one token budget with the rest of the judge prompt, and
nothing downstream truncates them — `style_metadata_cache` bounds how many
entries it keeps, not how large they are. An unbounded dump once cost 45MB and
lost most of a workbook to truncation.

So each section lists at most `_MAX_*` entries and says so when it truncates
(`shapes_listed="3 of 9"`, `<!-- 12 more omitted -->`), while the count
attribute keeps the true total. A silent cap is the same failure as an omission:
it reads as "this is everything".

## Degrade per item, not per file

python-pptx and python-docx raise on shapes and parts they cannot model. One
odd shape must not discard every other shape's fonts and colours, so failures
are caught at the item and recorded (`unreadable`, `partial_shapes`) rather
than dropped — an unreported gap would again read as absence.

## Adding to one of these modules

- Put the value and its source in the output; do not emit a reference.
- Give the new section a count and a cap.
- Add a test for the case where nothing declares the value, asserting the
  output still says `inherited`/`auto` rather than a number.
- Existing evals consume these dicts directly (`pptx_style_verifier`,
  `docx_style_verifier_apex_v2`, `pptx_style_verifier_apex_v2`) as well as the
  generic `output_llm_multi_representation` judge, so a changed value reaches
  more than one grader.

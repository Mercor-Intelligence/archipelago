"""Prompt + answer-set helpers for the browsecomp_judge_2 eval.

Grading rules:
  - the ground-truth answer is the task's ``expected_answer`` custom field ONLY,
  - multi-part answers are serialized with ``;`` and EVERY part must be present,
  - the judge applies name-equivalence / formatting tolerance rules.
"""

from __future__ import annotations

import re


def normalize_answer_part(answer: str | None) -> str:
    text = (answer or "").casefold()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def split_target_answers(answer: str | None) -> list[str]:
    """Split the canonical answer string into deduped answer parts.

    Multi-answer targets are stored as ``"answer 1; answer 2"``.
    """
    text = (answer or "").strip()
    if not text:
        return []
    parts = [part.strip() for part in text.split(";") if part.strip()]
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        key = normalize_answer_part(part)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(part)
    return out


def normalize_expected_answer(raw: str | None) -> str:
    """Ground-truth normalization.

    A newline-delimited list with no ``;`` is rewritten to the canonical
    ``"; "`` form so the multi-answer logic fires.
    """
    text = (raw or "").strip()
    if text and ";" not in text and "\n" in text:
        text = "; ".join(p.strip() for p in text.split("\n") if p.strip())
    return text


def answer_set_grading_instructions(answer: str) -> str:
    parts = split_target_answers(answer)
    if len(parts) <= 1:
        return ""
    return (
        "\nMULTI-ANSWER GRADING NOTE\n"
        "=========================\n"
        "The ground-truth answer is an answer SET serialized with semicolons. "
        f"It has {len(parts)} required parts: {parts!r}. Grade a model as "
        "correct only if its final answer clearly includes every required "
        "part and no incompatible extra answer. A required part counts as "
        "present if it is stated anywhere in the response, including inside a "
        "parenthetical, an appositive, or a longer phrase; it need not appear "
        "as a standalone item or as the final line. Ignore ordering unless the "
        "question explicitly asks for chronological/source order.\n"
    )


def build_grader_prompt(
    *,
    question: str,
    target_answer: str,
    response: str,
) -> str:
    """Single-response BrowseComp grader prompt."""
    multi_answer_note = answer_set_grading_instructions(target_answer)
    return (
        "A frontier LLM was asked the question below (no system prompt, web search "
        "enabled). Grade the response against the ground-truth answer.\n\n"
        f"QUESTION\n========\n{question}\n\n"
        "GROUND-TRUTH ANSWER\n===================\n"
        f"{target_answer}\n\n"
        "RESPONSE\n========\n"
        f"{response}\n\n"
        f"{multi_answer_note}"
        "EVALUATION RULES\n================\n"
        "- Allow minor formatting differences ('25' vs '25 years' vs 'twenty-five').\n"
        "- The model may discuss alternatives or hedge — what matters is whether "
        "the correct answer is clearly stated as its FINAL answer.\n"
        "- A response that gives a well-known alias, full legal name, or alternate name of "
        "the ground-truth entity is correct even if it never states the ground-truth string "
        'verbatim (e.g. "Rebecca Naa Dedei Aryeetey" and "Dedei Ashikishan" are the same '
        "person).\n"
        "- For PERSON NAMES specifically: judge by real-world identity, not surface string "
        "match. Honorifics, abbreviations, full name vs. surname-only, spelling/transliteration "
        'variants, diacritics (e.g. "Muller" for "Müller"), name order, and common nicknames '
        "are all the same person if the question context makes the identity unambiguous. "
        "STRICT STRING MATCHING IS WRONG for names — the benchmark tests whether the model "
        "found the answer, not whether it formatted the name identically to the ground-truth.\n"
        "- Do NOT mark a response correct merely because it contains the ground-truth "
        "string somewhere in passing if its FINAL stated answer is a different entity.\n"
        "- If the response is empty, an error, or a timeout, mark correct=no.\n"
        "- `reasoning` must be ONE short sentence (under 30 words). State your conclusion "
        "once and stop — do not restate it, list alternatives, or repeat any phrase or "
        "character.\n"
        "- Emit three fields IN THIS ORDER: `reasoning`, `extracted_final_answer` (the "
        "specific value the model stated as its final answer; 'None' if there is no final "
        "answer), `correct` ('yes' or 'no').\n"
    )

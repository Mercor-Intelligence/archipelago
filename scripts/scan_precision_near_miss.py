#!/usr/bin/env python3
"""Scan grades.json files for strict numeric precision near-misses."""
from __future__ import annotations

import argparse
import collections
import glob
import json
import os
import re
from pathlib import Path
from typing import Any


NUM_RE = re.compile(
    r"(?P<prefix>[$€£~≈]?\s*)"
    r"(?P<num>-?\d+(?:,\d{3})*(?:\.\d+)?)"
    r"(?P<suffix>\s*(?:%|x|MM|million|billion|m|bps|Euros?|EUR)?)",
    re.IGNORECASE,
)


def clean_num(raw: str) -> float:
    return float(raw.replace(",", ""))


def decimals(raw: str) -> int:
    raw = raw.replace(",", "")
    return len(raw.split(".", 1)[1]) if "." in raw else 0


def is_year(text: str, start: int, end: int, value: float) -> bool:
    if not 1900 <= abs(value) <= 2100:
        return False
    ctx = text[max(0, start - 8) : min(len(text), end + 8)]
    return bool(re.search(r"(20\d\d\s*[EA]?|FY\s*20\d\d|20\d\dE|20\d\dA|year|\b20\d\d\b)", ctx, re.I))


def extract_numbers(text: str, section: str) -> list[dict[str, Any]]:
    numbers: list[dict[str, Any]] = []
    for match in NUM_RE.finditer(text):
        raw = match.group("num")
        value = clean_num(raw)
        if is_year(text, match.start(), match.end(), value):
            continue
        numbers.append(
            {
                "val": value,
                "raw": raw,
                "dp": decimals(raw),
                "unit": (match.group("prefix") + match.group("suffix")).strip(),
                "section": section,
                "ctx": text[max(0, match.start() - 55) : min(len(text), match.end() + 55)],
                "pos": match.start(),
            }
        )
    return numbers


def criterion_requirement(rationale: str) -> str:
    match = re.search(r"criterion requirement:\s*(.*)", rationale, re.IGNORECASE | re.DOTALL)
    if not match:
        return ""
    text = match.group(1).strip()
    # Stop before conclusion text even when the judge emits everything on one line.
    text = re.split(r"(?:\n|\s)-?\s*Conclusion:|\s+Conclusion:", text, maxsplit=1, flags=re.IGNORECASE)[0]
    text = re.split(r"\n\s*-?\s*(?:Assessment|Evidence):", text, maxsplit=1, flags=re.IGNORECASE)[0]
    return text.strip()


def target_part(requirement: str) -> str:
    matches = list(re.finditer(r"\b(?:is|are|equals?|equal to|be)\b", requirement, re.IGNORECASE))
    return requirement[matches[-1].end() :] if matches else requirement


def evidence_and_assessment(rationale: str) -> tuple[str, str]:
    parts = re.split(r"## Assessment|Assessment:", rationale, flags=re.IGNORECASE)
    evidence = parts[0]
    assessment = parts[1] if len(parts) > 1 else ""
    return evidence, assessment


def unit_class(unit: str) -> str:
    unit = unit.lower()
    if "%" in unit or "bps" in unit:
        return "pct"
    if "x" in unit:
        return "x"
    if any(token in unit for token in ("$", "€", "£", "mm", "million", "billion", "eur", "euro")):
        return "money"
    return "plain"


def compatible(a: dict[str, Any], b: dict[str, Any]) -> bool:
    ca = unit_class(str(a["unit"]))
    cb = unit_class(str(b["unit"]))
    return ca == "plain" or cb == "plain" or ca == cb


def precision_unit(number: dict[str, Any]) -> float:
    return 10 ** (-int(number["dp"]))


def scan(grades_dir: Path, expected_attempts: int) -> dict[str, Any]:
    files = [path for path in sorted(grades_dir.glob("task_*__attempt*.json")) if path.stat().st_size > 0]
    rows: list[dict[str, Any]] = []
    total = 0
    failed = 0
    skipped_no_req = 0
    for path in files:
        task_id, attempt_part = path.stem.split("__attempt", 1)
        attempt = int(attempt_part)
        payload = json.loads(path.read_text())
        for index, result in enumerate(payload.get("verifier_results", []) or []):
            total += 1
            values = result.get("verifier_result_values", {}) or {}
            rationale = str(values.get("grade_rationale", ""))
            score = float(result.get("score") or 0.0)
            judge_grade = str(values.get("judge_grade", "")).lower()
            if not (score == 0.0 or judge_grade == "fail"):
                continue
            failed += 1
            requirement = criterion_requirement(rationale)
            if not requirement:
                skipped_no_req += 1
                continue
            required_numbers = extract_numbers(target_part(requirement), "requirement")
            if not required_numbers:
                continue
            evidence, assessment = evidence_and_assessment(rationale)
            candidates = []
            for number in extract_numbers(evidence, "evidence") + extract_numbers(assessment, "assessment"):
                ctx = str(number["ctx"]).lower()
                if any(
                    marker in ctx
                    for marker in (
                        "criterion requirement",
                        "required value",
                        "required $",
                        "instead of the required",
                        "does not match the required",
                    )
                ):
                    continue
                candidates.append(number)
            best: dict[str, Any] | None = None
            for required in required_numbers:
                for candidate in candidates:
                    if not compatible(required, candidate):
                        continue
                    diff = abs(float(required["val"]) - float(candidate["val"]))
                    if diff == 0:
                        continue
                    rel = diff / (abs(float(required["val"])) if required["val"] else 1.0)
                    unit = max(precision_unit(required), precision_unit(candidate))
                    strict = diff <= unit + 1e-9 and rel <= 0.001
                    if max(int(required["dp"]), int(candidate["dp"])) == 0 and diff <= 1 + 1e-9 and rel <= 0.001:
                        strict = True
                    small = (not strict) and rel <= 0.001 and diff <= max(unit * 5, 0.5)
                    if strict or small:
                        rank = (0 if strict else 1, rel, diff)
                        if best is None or rank < best["rank"]:
                            best = {
                                "task_id": task_id,
                                "attempt": attempt,
                                "verifier_index": index,
                                "required": required,
                                "provided": candidate,
                                "diff": diff,
                                "rel": rel,
                                "unit_step": unit,
                                "strict": strict,
                                "small": small,
                                "req_line": requirement,
                                "rationale": rationale[:1200],
                                "rank": rank,
                            }
            if best is not None:
                rows.append(best)

    strict_rows = [row for row in rows if row["strict"]]
    small_rows = [row for row in rows if row["small"]]
    summary = {
        "valid_grade_files": len(files),
        "missing_or_empty_grade_files": expected_attempts - len(files),
        "verifier_total": total,
        "failed_verifiers": failed,
        "skipped_failed_without_req_line": skipped_no_req,
        "strict_precision_near_miss": len(strict_rows),
        "strict_share_failed": len(strict_rows) / failed if failed else None,
        "strict_share_total": len(strict_rows) / total if total else None,
        "small_numeric_delta_non_strict": len(small_rows),
        "small_share_failed": len(small_rows) / failed if failed else None,
        "combined_strict_plus_small": len(rows),
        "combined_share_failed": len(rows) / failed if failed else None,
        "strict_unique_tasks": len({row["task_id"] for row in strict_rows}),
        "strict_unique_task_attempts": len({(row["task_id"], row["attempt"]) for row in strict_rows}),
        "small_unique_tasks": len({row["task_id"] for row in small_rows}),
    }
    return {
        "summary": summary,
        "top_strict_tasks": [
            {"task_id": task_id, "count": count}
            for task_id, count in collections.Counter(row["task_id"] for row in strict_rows).most_common()
        ],
        "strict_diff_buckets": [
            {"diff": diff, "count": count}
            for diff, count in collections.Counter(round(float(row["diff"]), 6) for row in strict_rows).most_common()
        ],
        "rows": [{key: value for key, value in row.items() if key != "rank"} for row in rows],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--grades-dir", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--expected-attempts", type=int, default=1280)
    args = parser.parse_args()
    result = scan(Path(args.grades_dir), args.expected_attempts)
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Add numeric precision near-miss diagnostics to a pass@k report."""
from __future__ import annotations

import argparse
import collections
import html
import json
import os
from pathlib import Path
from typing import Any


def pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value * 100:.2f}%"


def load_json(path: Path) -> Any:
    return json.loads(path.read_text())


def attempt_scores(grades_dir: Path, overrides: set[tuple[str, int, int]]) -> dict[tuple[str, int], float]:
    scores: dict[tuple[str, int], float] = {}
    for path in sorted(grades_dir.glob("task_*__attempt*.json")):
        if path.stat().st_size == 0:
            continue
        task_id, attempt_part = path.stem.split("__attempt", 1)
        attempt = int(attempt_part)
        payload = load_json(path)
        verifier_scores = []
        for index, result in enumerate(payload.get("verifier_results", []) or []):
            score = float(result.get("score") or 0.0)
            if (task_id, attempt, index) in overrides:
                score = 1.0
            verifier_scores.append(score)
        if verifier_scores:
            scores[(task_id, attempt)] = sum(verifier_scores) / len(verifier_scores)
    return scores


def pass_metrics(score_map: dict[tuple[str, int], float], total_tasks: int) -> dict[str, Any]:
    tasks = sorted({task_id for task_id, _ in score_map})

    def success(score: float | None) -> bool:
        return score is not None and score >= 0.999999

    pass1 = sum(1 for task_id in tasks if success(score_map.get((task_id, 1))))
    pass8 = sum(
        1
        for task_id in tasks
        if any(success(score_map.get((task_id, attempt))) for attempt in range(1, 9))
    )
    return {
        "pass1_successes": pass1,
        "pass1_rate": pass1 / total_tasks if total_tasks else None,
        "pass8_successes": pass8,
        "pass8_rate": pass8 / total_tasks if total_tasks else None,
    }


def precision_analysis(
    precision_payload: dict[str, Any],
    grades_dir: Path,
    total_tasks: int,
) -> dict[str, Any]:
    globals()["_grades_dir_for_classification"] = grades_dir
    rows = precision_payload.get("rows", []) or []
    strict_rows = [row for row in rows if row.get("strict")]
    small_rows = [row for row in rows if row.get("small")]
    strict_overrides = {
        (row["task_id"], int(row["attempt"]), int(row["verifier_index"]))
        for row in strict_rows
    }
    combined_overrides = {
        (row["task_id"], int(row["attempt"]), int(row["verifier_index"]))
        for row in rows
    }
    original = attempt_scores(grades_dir, set())
    strict_adjusted = attempt_scores(grades_dir, strict_overrides)
    combined_adjusted = attempt_scores(grades_dir, combined_overrides)

    original_metrics = pass_metrics(original, total_tasks)
    strict_metrics = pass_metrics(strict_adjusted, total_tasks)
    combined_metrics = pass_metrics(combined_adjusted, total_tasks)

    def converted(score_map: dict[tuple[str, int], float]) -> tuple[list[tuple[str, int]], list[str]]:
        attempts = [
            key
            for key, score in original.items()
            if score < 0.999999 and score_map.get(key, 0.0) >= 0.999999
        ]
        original_pass8 = {
            task_id: any(original.get((task_id, attempt), 0.0) >= 0.999999 for attempt in range(1, 9))
            for task_id in {task_id for task_id, _ in original}
        }
        adjusted_pass8 = {
            task_id: any(score_map.get((task_id, attempt), 0.0) >= 0.999999 for attempt in range(1, 9))
            for task_id in {task_id for task_id, _ in original}
        }
        newly_passed = sorted(
            task_id
            for task_id, passed in original_pass8.items()
            if not passed and adjusted_pass8.get(task_id)
        )
        return attempts, newly_passed

    strict_attempts, strict_new_tasks = converted(strict_adjusted)
    combined_attempts, combined_new_tasks = converted(combined_adjusted)
    issue_classes = classify_issue_impact(strict_rows, original, total_tasks)

    top_tasks = [
        {"task_id": task_id, "count": count}
        for task_id, count in collections.Counter(row["task_id"] for row in strict_rows).most_common(12)
    ]
    diff_buckets = [
        {"diff": diff, "count": count}
        for diff, count in collections.Counter(round(float(row["diff"]), 6) for row in strict_rows).most_common()
    ]
    examples = [
        {
            "task_id": row["task_id"],
            "attempt": row["attempt"],
            "required": row["required"]["raw"],
            "provided": row["provided"]["raw"],
            "diff": row["diff"],
            "rationale": row.get("rationale", ""),
        }
        for row in strict_rows[:12]
    ]

    summary = dict(precision_payload.get("summary", {}) or {})
    summary.update(
        {
            "original_metrics": original_metrics,
            "strict_adjusted_metrics": strict_metrics,
            "combined_upper_bound_metrics": combined_metrics,
            "strict_converted_attempts": len(strict_attempts),
            "strict_converted_tasks": len({task_id for task_id, _ in strict_attempts}),
            "strict_new_pass8_tasks": strict_new_tasks,
            "combined_converted_attempts": len(combined_attempts),
            "combined_converted_tasks": len({task_id for task_id, _ in combined_attempts}),
            "combined_new_pass8_tasks": combined_new_tasks,
        }
    )
    return {
        "method": (
            "Heuristic extraction from grades.json rationale. Strict near-miss means the provided "
            "number differs from the criterion value by no more than one displayed precision unit "
            "and relative error <= 0.1%. Small numeric delta is a wider upper-bound bucket."
        ),
        "summary": summary,
        "top_strict_tasks": top_tasks,
        "strict_diff_buckets": diff_buckets,
        "strict_examples": examples,
        "sampled_audit": sampled_audit(),
        "issue_classification": issue_classes,
    }


def issue_class_by_task() -> dict[str, dict[str, str]]:
    return {
        "task_0896a8bf7ee3473d81baa594c05814b3": {
            "class": "hidden_golden_assumption",
            "note": "Rubric/gold rounding is inconsistent with audited raw terminal value and revised stake calculation.",
        },
        "task_260818eebc2a4366af65fe8f3f17910f": {
            "class": "hidden_golden_assumption",
            "note": "Prompt does not specify Open/Close or whether the as-of date is included; gold implies an unstated FDUS median price/window.",
        },
        "task_00b30f56f39a4a9891d9503443bafb27": {
            "class": "hidden_golden_assumption",
            "note": "Workbook cached values support 2767.4 for Comparables while rubric requires 2767.5.",
        },
        "task_883f8bcbf38148648037f16db02a9754": {
            "class": "hidden_golden_assumption",
            "note": "Risk-free-rate date/source and exact workbook recalculation path are hidden; all four DCF sensitivities miss by 0.01.",
        },
        "task_2802d722ce6d40279fd0931576d2ed88": {
            "class": "hidden_golden_assumption",
            "note": "Runner artifacts compute 1843.498, which rounds to 1843; rubric requires 1842.",
        },
        "task_a7c1e23437d1451ca11f4ff27105fa40": {
            "class": "runner_precision",
            "note": "Prompt specifies VWAP formula; runner/model output lands 0.01 below gold, likely due to workbook/input precision.",
        },
        "task_00db129e3bd9497da0acf6470a1d33d2": {
            "class": "format_rounding_tolerance",
            "note": "Output includes unrounded and rounded employee metric; this is mostly strict formatting tolerance.",
        },
        "task_2b2666310e7e4712be0f2c0e4240d5a2": {
            "class": "hidden_golden_assumption",
            "note": "Peer-multiple and actual-equity calculations are exact-value sensitive; prompt does not expose intermediate golden values.",
        },
        "task_915931c8aa7840ef9359ce9a50583e3d": {
            "class": "hidden_golden_assumption",
            "note": "Target share price differs by one cent after a multi-step REIT valuation with hidden intermediate exact values.",
        },
        "task_ac9acf55ae54420fba1675a2985c519e": {
            "class": "runner_precision",
            "note": "Gold is supportable using 914.900mm denominator; runner used 914.88mm.",
        },
        "task_b8270cca4f7c455791d7b9807ed34295": {
            "class": "hidden_golden_assumption",
            "note": "Runner raw breakeven price is 127.345805 -> 127.35; gold likely depends on exact average close/model inputs.",
        },
        "task_340d128cb49e4df5952885b707a1cddd": {
            "class": "ordinary_or_real_error",
            "note": "Near number appears inside a substantially wrong PF net income result; not a hidden-golden precision issue.",
        },
        "task_6137ed8e71c541119bfc2b842364beea": {
            "class": "hidden_golden_assumption",
            "note": "Model IRR cell shown in artifacts rounds to 20.43 while rubric requires 20.44.",
        },
        "task_658948aa7e6a4ad8af4a73bd76df287c": {
            "class": "format_rounding_tolerance",
            "note": "Agent displayed rounded table value while rubric expects a more precise percentage.",
        },
        "task_6caba0e23298489cbfc7732bf26ff1e3": {
            "class": "hidden_golden_assumption",
            "note": "P/E differs by 0.01 after multiple hidden model inputs and intermediate values.",
        },
        "task_7d11f0f8a4ac415599f715647d2a09e4": {
            "class": "hidden_golden_assumption",
            "note": "Treasury-rate date/source and workbook recalculation path are hidden; implied share price differs by 0.01.",
        },
        "task_a9ce195e45104521ac830136c86d0f69": {
            "class": "hidden_golden_assumption",
            "note": "UFCF differs by 0.01 with hidden exact formula/intermediate values.",
        },
    }


def classify_issue_impact(
    strict_rows: list[dict[str, Any]],
    original: dict[tuple[str, int], float],
    total_tasks: int,
) -> dict[str, Any]:
    by_task = issue_class_by_task()
    by_class: dict[str, dict[str, Any]] = {}
    for row in strict_rows:
        task_id = row["task_id"]
        issue = by_task.get(task_id, {"class": "unclassified", "note": ""})
        bucket = by_class.setdefault(
            issue["class"],
            {
                "strict_rows": 0,
                "tasks": set(),
                "task_attempts": set(),
                "task_notes": {},
            },
        )
        bucket["strict_rows"] += 1
        bucket["tasks"].add(task_id)
        bucket["task_attempts"].add((task_id, int(row["attempt"])))
        bucket["task_notes"][task_id] = issue["note"]

    class_summaries: dict[str, Any] = {}
    for issue_class, bucket in by_class.items():
        overrides = {
            (row["task_id"], int(row["attempt"]), int(row["verifier_index"]))
            for row in strict_rows
            if by_task.get(row["task_id"], {"class": "unclassified"})["class"] == issue_class
        }
        adjusted = attempt_scores_from_original(original, overrides)
        original_metrics = pass_metrics(original, total_tasks)
        adjusted_metrics = pass_metrics(adjusted, total_tasks)
        class_summaries[issue_class] = {
            "strict_rows": bucket["strict_rows"],
            "tasks": len(bucket["tasks"]),
            "task_attempts": len(bucket["task_attempts"]),
            "share_of_strict_rows": bucket["strict_rows"] / len(strict_rows) if strict_rows else None,
            "pass1_delta": adjusted_metrics["pass1_successes"] - original_metrics["pass1_successes"],
            "pass8_delta": adjusted_metrics["pass8_successes"] - original_metrics["pass8_successes"],
            "pass1_adjusted_rate": adjusted_metrics["pass1_rate"],
            "pass8_adjusted_rate": adjusted_metrics["pass8_rate"],
            "tasks_detail": [
                {"task_id": task_id, "note": bucket["task_notes"][task_id]}
                for task_id in sorted(bucket["tasks"])
            ],
        }
    return {
        "method": (
            "Manual conservative classification of strict numeric near-miss tasks based on prompt, "
            "gold/rubric, grader rationale, and sampled runner artifacts. hidden_golden_assumption "
            "means the prompt/gold omits a source, date-window, input precision, or intermediate "
            "rounding rule needed to reproduce the exact value."
        ),
        "by_class": class_summaries,
    }


def attempt_scores_from_original(
    original: dict[tuple[str, int], float],
    overrides: set[tuple[str, int, int]],
) -> dict[tuple[str, int], float]:
    # Recompute from grade files because original stores aggregate attempt scores.
    # This helper is patched in main by assigning _grades_dir_for_classification.
    grades_dir = globals().get("_grades_dir_for_classification")
    if not isinstance(grades_dir, Path):
        return original
    return attempt_scores(grades_dir, overrides)


def sampled_audit() -> list[dict[str, str]]:
    return [
        {
            "task_id": "task_00b30f56f39a4a9891d9503443bafb27",
            "classification": "gold / rubric questionable",
            "evidence": (
                "Workbook cached values support raw Comparables 2767.4469, which rounds to 2767.4. "
                "The rubric's 2767.5 is not supported by the audited calculation; the related mean "
                "3030.8 is supportable if computed from raw values."
            ),
        },
        {
            "task_id": "task_0896a8bf7ee3473d81baa594c05814b3",
            "classification": "likely gold rounding inconsistency",
            "evidence": (
                "Run log computes discounted terminal value 19959.097, which rounds to 19959.1, while "
                "the rubric requires 19959.0. The same calculation still supports the rubric's revised "
                "stake value of 4949.8."
            ),
        },
        {
            "task_id": "task_b8270cca4f7c455791d7b9807ed34295",
            "classification": "likely input-source / golden ambiguity",
            "evidence": (
                "The selected verification calculation gives raw 127.345805, which rounds to 127.35; "
                "the rubric requires 127.36. This likely depends on exact share-price source or cached "
                "model inputs, not a clear model reasoning failure."
            ),
        },
        {
            "task_id": "task_ac9acf55ae54420fba1675a2985c519e",
            "classification": "runner intermediate precision issue",
            "evidence": (
                "Workbook denominator is 914900 thousand. Using it gives Base 55.91% and Bull 91.61%, "
                "matching gold. The runner appears to use 914.88 million, producing 55.92% and 91.62%."
            ),
        },
        {
            "task_id": "task_260818eebc2a4366af65fe8f3f17910f",
            "classification": "runner rounded inputs before division",
            "evidence": (
                "The run log divides displayed prices 20.57 / 8.74 = 2.3535469, rounding to 2.354. "
                "Gold's 2.355 likely uses more precise underlying share prices, so this is an "
                "intermediate precision or input extraction issue."
            ),
        },
        {
            "task_id": "task_a006f24d413c4dc99dd644d5e0dc12f7",
            "classification": "scanner false positive / real runner miss",
            "evidence": (
                "This case was removed by the corrected scanner. The runner calculated 22.02 while the "
                "rubric required 22.41, so it is not a precision near-miss."
            ),
        },
    ]


def render_section(analysis: dict[str, Any]) -> str:
    summary = analysis["summary"]
    original = summary["original_metrics"]
    strict = summary["strict_adjusted_metrics"]
    combined = summary["combined_upper_bound_metrics"]
    top_rows = "\n".join(
        "<tr>"
        f"<td><code>{html.escape(row['task_id'])}</code></td>"
        f"<td>{row['count']}</td>"
        "</tr>"
        for row in analysis["top_strict_tasks"]
    )
    bucket_rows = "\n".join(
        "<tr>"
        f"<td>{row['diff']}</td>"
        f"<td>{row['count']}</td>"
        "</tr>"
        for row in analysis["strict_diff_buckets"]
    )
    example_rows = "\n".join(
        "<tr>"
        f"<td><code>{html.escape(row['task_id'])}</code></td>"
        f"<td>{html.escape(str(row['attempt']))}</td>"
        f"<td>{html.escape(str(row['provided']))}</td>"
        f"<td>{html.escape(str(row['required']))}</td>"
        f"<td>{html.escape(str(round(float(row['diff']), 6)))}</td>"
        f"<td>{html.escape(row['rationale'])}</td>"
        "</tr>"
        for row in analysis["strict_examples"]
    )
    audit_rows = "\n".join(
        "<tr>"
        f"<td><code>{html.escape(row['task_id'])}</code></td>"
        f"<td>{html.escape(row['classification'])}</td>"
        f"<td>{html.escape(row['evidence'])}</td>"
        "</tr>"
        for row in analysis.get("sampled_audit", [])
    )
    issue_classes = analysis.get("issue_classification", {}).get("by_class", {})
    issue_order = [
        "hidden_golden_assumption",
        "runner_precision",
        "format_rounding_tolerance",
        "ordinary_or_real_error",
        "unclassified",
    ]
    issue_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(issue_class)}</td>"
        f"<td>{bucket['strict_rows']}</td>"
        f"<td>{bucket['tasks']}</td>"
        f"<td>{pct(bucket.get('share_of_strict_rows'))}</td>"
        f"<td>+{bucket['pass1_delta']}</td>"
        f"<td>+{bucket['pass8_delta']}</td>"
        f"<td>{pct(bucket.get('pass8_adjusted_rate'))}</td>"
        "</tr>"
        for issue_class in issue_order
        if (bucket := issue_classes.get(issue_class))
    )
    hidden_details = ""
    hidden_bucket = issue_classes.get("hidden_golden_assumption", {})
    if hidden_bucket:
        hidden_details = "\n".join(
            "<tr>"
            f"<td><code>{html.escape(row['task_id'])}</code></td>"
            f"<td>{html.escape(row['note'])}</td>"
            "</tr>"
            for row in hidden_bucket.get("tasks_detail", [])
        )
    strict_delta_pass1 = strict["pass1_successes"] - original["pass1_successes"]
    strict_delta_pass8 = strict["pass8_successes"] - original["pass8_successes"]
    combined_delta_pass1 = combined["pass1_successes"] - original["pass1_successes"]
    combined_delta_pass8 = combined["pass8_successes"] - original["pass8_successes"]
    return f"""
<!-- precision-near-miss:start -->
<section class="panel" style="margin-top:16px"><h2>Precision Near-Miss 影响范围</h2>
<p class="note">{html.escape(analysis["method"])}</p>
<div class="grid metrics">
<div class="panel"><div class="label">strict near-miss</div><div class="value info">{summary['strict_precision_near_miss']}</div><div class="sub">failed verifier 占比 {pct(summary['strict_share_failed'])}；涉及 {summary['strict_unique_tasks']} 个 task</div></div>
<div class="panel"><div class="label">small delta 上界</div><div class="value info">{summary['combined_strict_plus_small']}</div><div class="sub">failed verifier 占比 {pct(summary['combined_share_failed'])}</div></div>
<div class="panel"><div class="label">pass@1 调整</div><div class="value ok">{pct(strict['pass1_rate'])}</div><div class="sub">原始 {pct(original['pass1_rate'])}；strict +{strict_delta_pass1} tasks，上界 +{combined_delta_pass1}</div></div>
<div class="panel"><div class="label">pass@8 调整</div><div class="value ok">{pct(strict['pass8_rate'])}</div><div class="sub">原始 {pct(original['pass8_rate'])}；strict +{strict_delta_pass8} tasks，上界 +{combined_delta_pass8}</div></div>
<div class="panel"><div class="label">新通过 task</div><div class="value">{len(summary['strict_new_pass8_tasks'])}</div><div class="sub">strict 口径；上界 {len(summary['combined_new_pass8_tasks'])}</div></div>
</div>
<div class="callout">这些调整是诊断口径，不替代正式 pass@k。它用于衡量 exact numeric rubric 对边界 rounding / golden ambiguity 的影响。</div>
<div class="callout">抽查结论：Precision Near-Miss 不是全部由 golden 不合理导致。样本中同时存在 gold / rubric rounding inconsistency、runner 中间精度或输入源问题，以及扫描误报。</div>
<h2 style="margin-top:16px">Hidden Golden Assumption 影响</h2>
<p class="note">{html.escape(analysis.get("issue_classification", {}).get("method", ""))}</p>
<table class="compact"><thead><tr><th>class</th><th>strict rows</th><th>tasks</th><th>占 strict</th><th>pass@1 delta</th><th>pass@8 delta</th><th>class-only pass@8</th></tr></thead><tbody>{issue_rows}</tbody></table>
<h2 style="margin-top:16px">hidden_golden_assumption task 明细</h2>
<table class="compact"><thead><tr><th>task</th><th>evidence / note</th></tr></thead><tbody>{hidden_details}</tbody></table>
<section class="grid two" style="margin-top:16px">
<div><h2>高频 strict near-miss task</h2><table><thead><tr><th>task</th><th>failed verifiers</th></tr></thead><tbody>{top_rows}</tbody></table></div>
<div><h2>误差分布</h2><table><thead><tr><th>abs diff</th><th>failed verifiers</th></tr></thead><tbody>{bucket_rows}</tbody></table></div>
</section>
<h2 style="margin-top:16px">抽查分类</h2>
<table class="compact"><thead><tr><th>task</th><th>classification</th><th>evidence</th></tr></thead><tbody>{audit_rows}</tbody></table>
<h2 style="margin-top:16px">strict near-miss 样例</h2>
<table class="compact"><thead><tr><th>task</th><th>attempt</th><th>provided</th><th>required</th><th>diff</th><th>evidence snippet</th></tr></thead><tbody>{example_rows}</tbody></table>
</section>
<!-- precision-near-miss:end -->
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report-json", required=True)
    parser.add_argument("--report-html", required=True)
    parser.add_argument("--precision-json", required=True)
    parser.add_argument("--grades-dir", required=True)
    parser.add_argument("--output-json", required=True)
    parser.add_argument("--output-html", required=True)
    args = parser.parse_args()

    report_json = Path(args.report_json)
    report_html = Path(args.report_html)
    precision_json = Path(args.precision_json)
    grades_dir = Path(args.grades_dir)
    output_json = Path(args.output_json)
    output_html = Path(args.output_html)

    report = load_json(report_json)
    precision_payload = load_json(precision_json)
    analysis = precision_analysis(precision_payload, grades_dir, int(report["total_tasks"]))
    report["precision_near_miss_analysis"] = analysis
    output_json.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n")

    html_text = report_html.read_text()
    section = render_section(analysis)
    marker = '<section class="panel" style="margin-top:16px"><h2>Trajectory 样本验证</h2>'
    while "<!-- precision-near-miss:start -->" in html_text and "<!-- precision-near-miss:end -->" in html_text:
        start = html_text.index("<!-- precision-near-miss:start -->")
        end = html_text.index("<!-- precision-near-miss:end -->", start) + len("<!-- precision-near-miss:end -->")
        html_text = html_text[:start] + html_text[end:]
    legacy_marker = '<section class="panel" style="margin-top:16px"><h2>Precision Near-Miss 影响范围</h2>'
    while legacy_marker in html_text and marker in html_text:
        start = html_text.index(legacy_marker)
        end = html_text.index(marker, start)
        html_text = html_text[:start] + html_text[end:]
    if marker in html_text:
        html_text = html_text.replace(marker, section + "\n" + marker, 1)
    else:
        html_text = html_text.replace("</main></body></html>", section + "\n</main></body></html>")
    output_html.write_text(html_text)
    print(json.dumps(analysis["summary"], ensure_ascii=False, indent=2))
    print(f"html={output_html}")
    print(f"json={output_json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

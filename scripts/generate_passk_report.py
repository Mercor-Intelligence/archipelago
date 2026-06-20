#!/usr/bin/env python3
"""
Generate a staged pass@k report from GCS result artifacts.

The report intentionally separates:
  - pass@1 over completed attempt1 tasks
  - any-attempt pass rate over tasks with any completed attempt
  - prefix pass@k over tasks that have attempts 1..k completed

Some failed runs upload status.tsv and trajectory.json but intentionally skip
grades.json. Those attempts are real completed attempts and must count as 0,
otherwise pass@k is biased upward by silently dropping failures.
"""
from __future__ import annotations

import argparse
import collections
import concurrent.futures
import html
import json
import math
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from google.cloud import storage
except ModuleNotFoundError:  # pragma: no cover - user-facing dependency hint
    storage = None


ARTIFACT_RE = re.compile(r"/results/(task_[^/]+)/attempt(\d+)/(status\.tsv|grades\.json|trajectory\.json)$")
GRADE_RE = re.compile(r"/results/(task_[^/]+)/attempt(\d+)/grades\.json$")
PRICE_VALUE_RE = re.compile(
    r"\b("
    r"price|share price|per share|offer price|purchase price|trading price|target price|"
    r"valuation|value|enterprise value|equity value|market cap|ev|dcf|lbo|"
    r"multiple|ev/ebitda|irr|moic|accretion|dilution"
    r")\b",
    re.IGNORECASE,
)


FAILURE_CATEGORY_LABELS = {
    "artifact_output_incomplete": "Artifact / final output 不完整",
    "numeric_finance_mismatch": "财务数值不匹配",
    "spreadsheet_formula_state_error": "Spreadsheet 公式 / 状态错误",
    "cannot_complete_or_data_not_found": "数据发现失败 / cannot complete",
    "missing_required_detail": "漏掉 rubric 要求细节",
    "wrong_conclusion": "结论方向错误",
    "other": "其他 judge failure",
}


def classify_failure_rationale(rationale: str) -> str:
    low = rationale.lower()
    if (
        "cannot complete" in low
        or "no access" in low
        or "not available in the sandbox" in low
        or "failed to complete" in low
    ):
        return "cannot_complete_or_data_not_found"
    if "#value!" in low or "formula error" in low:
        return "spreadsheet_formula_state_error"
    if (
        any(
            marker in low
            for marker in (
                "instead of the required",
                "does not match",
                "rather than the required",
                "provided a value",
                "reported",
            )
        )
        and any(
            term in low
            for term in (
                "irr",
                "cagr",
                "equity value",
                "enterprise value",
                "share price",
                "ebitda",
                "revenue",
                "price",
                "moic",
                "capex",
                "wacc",
                "dcf",
                "lbo",
            )
        )
    ):
        return "numeric_finance_mismatch"
    if any(marker in low for marker in ("does not mention", "omits", "not explicitly", "fails to state", "does not state")):
        return "missing_required_detail"
    if (
        any(term in low for term in ("created file", "artifact", "document", "spreadsheet", "sheet"))
        and any(marker in low for marker in ("not met", "does not contain", "missing"))
    ):
        return "artifact_output_incomplete"
    if any(marker in low for marker in ("contradicts", "incorrectly concludes", "concluded that")):
        return "wrong_conclusion"
    return "other"


def pct(value: float | None) -> str:
    return "N/A" if value is None else f"{value * 100:.2f}%"


def num(value: float | None) -> str:
    return "N/A" if value is None else f"{value:.3f}"


def require_storage() -> Any:
    if storage is None:
        raise SystemExit(
            "Missing dependency google-cloud-storage. Install with:\n"
            "  python3 -m venv /tmp/gcs-report-venv\n"
            "  /tmp/gcs-report-venv/bin/pip install google-cloud-storage\n"
            "  /tmp/gcs-report-venv/bin/python scripts/generate_passk_report.py ..."
        )
    return storage


def list_result_blobs(client: Any, bucket_name: str, project_dir: str) -> list[str]:
    prefix = f"{project_dir}/results/"
    names = []
    for blob in client.list_blobs(bucket_name, prefix=prefix):
        if ARTIFACT_RE.search("/" + blob.name):
            names.append(blob.name)
    return names


def read_grade(
    bucket: Any,
    blob_name: str,
) -> tuple[str, int, float, float | None, int, dict[str, int], int, int, dict[str, str]] | None:
    match = GRADE_RE.search("/" + blob_name)
    if not match:
        return None
    task_id = match.group(1)
    attempt = int(match.group(2))
    try:
        payload = json.loads(bucket.blob(blob_name).download_as_text())
        score = payload.get("scoring_results", {}).get("final_score")
        price_scores = []
        failure_categories: collections.Counter[str] = collections.Counter()
        failure_examples: dict[str, str] = {}
        verifier_total = 0
        verifier_failed = 0
        for result in payload.get("verifier_results", []) or []:
            verifier_total += 1
            values = result.get("verifier_result_values", {}) or {}
            haystack = "\n".join(
                str(values.get(key, ""))
                for key in ("grade_rationale", "evaluated_artifacts", "judge_grade")
            )
            if PRICE_VALUE_RE.search(haystack):
                result_score = result.get("score")
                if result_score is not None:
                    price_scores.append(float(result_score))
            result_score = result.get("score")
            if result_score is not None and float(result_score) < 1.0:
                verifier_failed += 1
                category = classify_failure_rationale(str(values.get("grade_rationale", "")))
                failure_categories[category] += 1
                failure_examples.setdefault(
                    category,
                    re.sub(r"\s+", " ", str(values.get("grade_rationale", ""))).strip()[:320],
                )
    except Exception:
        return None
    if score is None:
        return None
    price_score = sum(price_scores) / len(price_scores) if price_scores else None
    return (
        task_id,
        attempt,
        float(score),
        price_score,
        len(price_scores),
        dict(failure_categories),
        verifier_total,
        verifier_failed,
        failure_examples,
    )


def aggregate(
    rows: list[tuple[str, int, float, float | None, int, dict[str, int], int, int, dict[str, str]]],
    total_tasks: int,
    completed_without_grades: list[tuple[str, int]],
) -> dict[str, Any]:
    by_task: dict[str, dict[int, float]] = collections.defaultdict(dict)
    price_by_task: dict[str, dict[int, dict[str, float | int]]] = collections.defaultdict(dict)
    verifier_failure_categories: collections.Counter[str] = collections.Counter()
    verifier_failure_examples: dict[str, dict[str, str]] = {}
    attempt_primary_categories: collections.Counter[str] = collections.Counter()
    attempt_primary_scores: dict[str, list[float]] = collections.defaultdict(list)
    verifier_total_count = 0
    verifier_failed_count = 0
    for task_id, attempt, score, price_score, price_count, failure_categories, verifier_total, verifier_failed, failure_examples in rows:
        by_task[task_id][attempt] = max(score, by_task[task_id].get(attempt, -1.0))
        if price_score is not None:
            existing = price_by_task[task_id].get(attempt)
            if existing is None or price_score > float(existing["score"]):
                price_by_task[task_id][attempt] = {"score": price_score, "n": price_count}
        verifier_total_count += verifier_total
        verifier_failed_count += verifier_failed
        verifier_failure_categories.update(failure_categories)
        for category, example in failure_examples.items():
            verifier_failure_examples.setdefault(
                category,
                {
                    "task_id": task_id,
                    "attempt": attempt,
                    "example": example,
                },
            )
        if failure_categories:
            primary_category = collections.Counter(failure_categories).most_common(1)[0][0]
        else:
            primary_category = "pass"
        attempt_primary_categories[primary_category] += 1
        attempt_primary_scores[primary_category].append(score)

    by_attempt: collections.Counter[int] = collections.Counter()
    success_by_attempt: collections.Counter[int] = collections.Counter()
    score_bins: collections.Counter[str] = collections.Counter()
    for attempts in by_task.values():
        for attempt, score in attempts.items():
            by_attempt[attempt] += 1
            if score >= 1.0:
                success_by_attempt[attempt] += 1
            if score == 0:
                score_bins["0"] += 1
            elif score < 0.5:
                score_bins["0-<0.5"] += 1
            elif score < 0.8:
                score_bins["0.5-<0.8"] += 1
            elif score < 1.0:
                score_bins["0.8-<1"] += 1
            else:
                score_bins["1.0"] += 1

    scores = [score for attempts in by_task.values() for score in attempts.values()]
    success_tasks = {
        task_id for task_id, attempts in by_task.items() if any(score >= 1.0 for score in attempts.values())
    }

    pass1_denom = by_attempt[1]
    pass1_successes = success_by_attempt[1]
    attempt1_scores = [attempts[1] for attempts in by_task.values() if 1 in attempts]

    per_k = []
    max_attempt = max(max((attempts.keys()), default=0) for attempts in by_task.values()) if by_task else 0
    for k in range(1, max(8, max_attempt) + 1):
        eligible = [
            (task_id, attempts)
            for task_id, attempts in by_task.items()
            if all(i in attempts for i in range(1, k + 1))
        ]
        successes = sum(
            1 for _, attempts in eligible if any(attempts[i] >= 1.0 for i in range(1, k + 1))
        )
        best_scores = [max(attempts[i] for i in range(1, k + 1)) for _, attempts in eligible]
        per_k.append(
            {
                "k": k,
                "n_tasks_with_1_to_k": len(eligible),
                "successes": successes,
                "pass_at_k": successes / len(eligible) if eligible else None,
                "lower_bound_all_tasks": successes / total_tasks,
                "mean_best_score": sum(best_scores) / len(best_scores) if best_scores else None,
            }
        )

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total_tasks": total_tasks,
        "scored_tasks": len(by_task),
        "scored_attempts": len(scores),
        "attempt_counts": {str(i): by_attempt[i] for i in range(1, max(8, max_attempt) + 1)},
        "success_by_attempt": {str(i): success_by_attempt[i] for i in range(1, max(8, max_attempt) + 1)},
        "pass_at_1_attempt1_only": {
            "tasks_with_attempt1_grade": pass1_denom,
            "successes": pass1_successes,
            "observed_on_attempt1_graded_tasks": pass1_successes / pass1_denom if pass1_denom else None,
            "lower_bound_all_tasks": pass1_successes / total_tasks,
            "mean_attempt1_score": sum(attempt1_scores) / len(attempt1_scores) if attempt1_scores else None,
        },
        "current_any_attempt_pass_rate": {
            "scored_task_denom": len(by_task),
            "success_any": len(success_tasks),
            "observed_on_scored_tasks": len(success_tasks) / len(by_task) if by_task else None,
            "lower_bound_all_tasks": len(success_tasks) / total_tasks,
        },
        "per_k_complete_prefix": per_k,
        "mean_score_all_scored_attempts": sum(scores) / len(scores) if scores else None,
        "completed_attempts_without_grades": [
            {"task_id": task_id, "attempt": attempt}
            for task_id, attempt in sorted(completed_without_grades)
        ],
        "n_scored_attempts_per_task_distribution": dict(
            sorted(collections.Counter(len(attempts) for attempts in by_task.values()).items())
        ),
        "score_bins": dict(score_bins),
        "failure_analysis": {
            "method": (
                "Heuristic parsing of verifier grade_rationale for current scored attempts. "
                "Trajectory samples were used to validate the major categories; exact category labels are directional."
            ),
            "verifier_total": verifier_total_count,
            "verifier_failed": verifier_failed_count,
            "verifier_failure_categories": [
                {
                    "category": category,
                    "label": FAILURE_CATEGORY_LABELS.get(category, category),
                    "count": count,
                    "share_of_failed_verifiers": count / verifier_failed_count if verifier_failed_count else None,
                    "example": verifier_failure_examples.get(category),
                }
                for category, count in verifier_failure_categories.most_common()
            ],
            "attempt_primary_categories": [
                {
                    "category": category,
                    "label": "Pass" if category == "pass" else FAILURE_CATEGORY_LABELS.get(category, category),
                    "attempts": count,
                    "mean_score": (
                        sum(attempt_primary_scores[category]) / len(attempt_primary_scores[category])
                        if attempt_primary_scores[category]
                        else None
                    ),
                }
                for category, count in attempt_primary_categories.most_common()
            ],
            "trajectory_sample_findings": [
                {
                    "task_id": "task_00b30f56f39a4a9891d9503443bafb27",
                    "attempt": "attempt1",
                    "score": 0.4,
                    "finding": "CNS valuation path mostly worked, but failed exact numeric checks by 0.1-0.2mm and mean rounding.",
                },
                {
                    "task_id": "task_0de09be0daf242208f7a60ee83bf8717",
                    "attempt": "attempt2",
                    "score": 0.42857142857142855,
                    "finding": "Same task has passing attempts, but this rollout chose the wrong filtered EV/EBITDA and made $48.35 the primary answer instead of $51.03.",
                },
                {
                    "task_id": "task_a757e127fe3a4b148dadeb34ef3540f7",
                    "attempt": "attempt5",
                    "score": 0.14285714285714285,
                    "finding": "Elastic LBO workbook was modified and answered, but assumption propagation produced systematically wrong exit metrics.",
                },
                {
                    "task_id": "task_db355b58e80749a1879a9d79681d05cc",
                    "attempt": "attempt2",
                    "score": 0.0,
                    "finding": "Agent read the spreadsheet and hand-computed CAGRs, but used the wrong financial calculation basis.",
                },
            ],
            "rl_data_recommendation": [
                "Prioritize finance workbook mutation, recalculation, and exact output-cell extraction over generic retrieval tasks.",
                "Create same-task winner/loser preference pairs from pass vs partial rollouts.",
                "Add numeric-distance, formula-health, artifact-state, and primary-answer-selection reward signals.",
                "Keep retrieval worlds, but use them mainly as support for spreadsheet and document-output workflows.",
            ],
        },
        "task_scores": {
            task_id: {str(attempt): score for attempt, score in sorted(attempts.items())}
            for task_id, attempts in sorted(by_task.items())
        },
        "price_value_verifier_scores": {
            task_id: {
                str(attempt): {"score": values["score"], "n": values["n"]}
                for attempt, values in sorted(attempts.items())
            }
            for task_id, attempts in sorted(price_by_task.items())
        },
    }


def bar(label: str, value: int, max_value: int, klass: str = "") -> str:
    width = (value / max_value * 100) if max_value else 0
    class_attr = f' class="{klass}"' if klass else ""
    return f'<div class="bar"><span>{html.escape(label)}</span><b{class_attr} style="width:{width:.1f}%"></b><em>{value}</em></div>'


def render_html(report: dict[str, Any], project_dir: str, bucket: str) -> str:
    attempts = {int(k): v for k, v in report["attempt_counts"].items()}
    successes = {int(k): v for k, v in report["success_by_attempt"].items()}
    max_attempt_count = max(attempts.values() or [1])
    attempt_bars = "\n".join(
        bar(f"attempt{i}", attempts.get(i, 0), max_attempt_count) for i in sorted(attempts)
    )
    attempt_rows = "\n".join(
        "<tr>"
        f"<td>attempt{i}</td><td>{attempts.get(i, 0)}</td><td>{successes.get(i, 0)}</td>"
        f"<td>{(successes.get(i, 0) / attempts.get(i, 1) * 100 if attempts.get(i, 0) else 0):.2f}%</td>"
        "</tr>"
        for i in sorted(attempts)
    )

    dist = {int(k): v for k, v in report["n_scored_attempts_per_task_distribution"].items()}
    max_dist = max(dist.values() or [1])
    dist_bars = "\n".join(bar(f"{k} attempts", v, max_dist, "green") for k, v in sorted(dist.items()))

    score_order = ["0", "0-<0.5", "0.5-<0.8", "0.8-<1", "1.0"]
    bins = report["score_bins"]
    max_bin = max((bins.get(k, 0) for k in score_order), default=1)
    bin_bars = "\n".join(
        bar(
            k,
            bins.get(k, 0),
            max_bin,
            "green" if k == "1.0" else "red" if k == "0" else "amber",
        )
        for k in score_order
    )

    prefix_rows = "\n".join(
        "<tr>"
        f"<td>{row['k']}</td><td>{row['n_tasks_with_1_to_k']}</td><td>{row['successes']}</td>"
        f"<td>{pct(row['pass_at_k'])}</td><td>{pct(row['lower_bound_all_tasks'])}</td>"
        f"<td>{num(row['mean_best_score'])}</td>"
        "</tr>"
        for row in report["per_k_complete_prefix"]
    )
    passk_rows = [
        row for row in report["per_k_complete_prefix"]
        if row["pass_at_k"] is not None and 1 <= int(row["k"]) <= 8
    ]
    passk_by_k = {int(row["k"]): row for row in passk_rows}
    prediction_rows: list[dict[str, Any]] = []
    prediction_note = ""
    if all(k in passk_by_k for k in range(1, 9)):
        observed = [(k, float(passk_by_k[k]["successes"])) for k in range(1, 9)]
        total_tasks = int(report["total_tasks"])
        s1 = observed[0][1]
        max_seen = max(successes for _, successes in observed)
        best_fit: tuple[float, float, float] | None = None
        # Saturating exponential: S_k = L - (L - S_1) * exp(-b * (k - 1)).
        # Grid search is stable enough here and avoids adding scipy as a report dependency.
        for l_i in range(int(math.ceil(max_seen * 100)), total_tasks * 100 + 1):
            asymptote = l_i / 100
            if asymptote <= s1:
                continue
            for b_i in range(1, 1001):
                decay = b_i / 1000
                sse = 0.0
                for k, actual in observed:
                    predicted = asymptote - (asymptote - s1) * math.exp(-decay * (k - 1))
                    sse += (predicted - actual) ** 2
                if best_fit is None or sse < best_fit[0]:
                    best_fit = (sse, asymptote, decay)
        asymptote = best_fit[1] if best_fit else float(total_tasks)
        decay = best_fit[2] if best_fit else 0.0
        for k in (9, 10):
            predicted_successes = min(
                float(total_tasks),
                asymptote - (asymptote - s1) * math.exp(-decay * (k - 1)),
            )
            prediction_rows.append(
                {
                    "k": k,
                    "predicted_successes": predicted_successes,
                    "pass_at_k": predicted_successes / total_tasks if total_tasks else None,
                }
            )
        prediction_note = (
            "预测方法：对 pass@1-8 的累计成功 task 数拟合饱和指数曲线 "
            f"S_k = L - (L - S_1) * exp(-b*(k-1))，估计上限 L={asymptote:.1f} tasks，"
            f"收敛速度 b={decay:.3f}。预测段仅用于趋势参考。"
        )
    chart_w, chart_h = 760, 300
    left, right, top, bottom = 54, 22, 18, 42
    plot_w = chart_w - left - right
    plot_h = chart_h - top - bottom
    max_rate = max(
        [
            0.4,
            *[float(row["pass_at_k"]) for row in passk_rows],
            *[float(row["pass_at_k"]) for row in prediction_rows if row["pass_at_k"] is not None],
        ],
        default=0.4,
    )
    y_max = min(1.0, max(0.4, ((int(max_rate * 10 + 0.999) or 1) / 10)))

    def chart_x(k: int) -> float:
        return left + ((k - 1) / 9) * plot_w

    def chart_y(rate: float) -> float:
        return top + (1 - rate / y_max) * plot_h

    polyline_points = " ".join(
        f"{chart_x(int(row['k'])):.1f},{chart_y(float(row['pass_at_k'])):.1f}"
        for row in passk_rows
    )
    projected_points = " ".join(
        [
            f"{chart_x(8):.1f},{chart_y(float(passk_by_k[8]['pass_at_k'])):.1f}"
            if 8 in passk_by_k
            else "",
            *[
                f"{chart_x(int(row['k'])):.1f},{chart_y(float(row['pass_at_k'])):.1f}"
                for row in prediction_rows
                if row["pass_at_k"] is not None
            ],
        ]
    ).strip()
    y_ticks = [0, y_max / 4, y_max / 2, y_max * 3 / 4, y_max]
    y_grid = "\n".join(
        f'<line x1="{left}" y1="{chart_y(t):.1f}" x2="{chart_w - right}" y2="{chart_y(t):.1f}" class="gridline"/>'
        f'<text x="{left - 10}" y="{chart_y(t) + 4:.1f}" class="axislabel" text-anchor="end">{t * 100:.0f}%</text>'
        for t in y_ticks
    )
    x_ticks = "\n".join(
        f'<line x1="{chart_x(k):.1f}" y1="{chart_h - bottom}" x2="{chart_x(k):.1f}" y2="{chart_h - bottom + 5}" class="tick"/>'
        f'<text x="{chart_x(k):.1f}" y="{chart_h - 16}" class="axislabel" text-anchor="middle">pass@{k}</text>'
        for k in range(1, 11)
    )
    passk_points = "\n".join(
        f'<g><circle cx="{chart_x(int(row["k"])):.1f}" cy="{chart_y(float(row["pass_at_k"])):.1f}" r="4.5" class="point"/>'
        f'<text x="{chart_x(int(row["k"])):.1f}" y="{chart_y(float(row["pass_at_k"])) - 10:.1f}" class="pointlabel" text-anchor="middle">{pct(float(row["pass_at_k"]))}</text></g>'
        for row in passk_rows
    )
    predicted_points = "\n".join(
        f'<g><circle cx="{chart_x(int(row["k"])):.1f}" cy="{chart_y(float(row["pass_at_k"])):.1f}" r="4.5" class="predpoint"/>'
        f'<text x="{chart_x(int(row["k"])):.1f}" y="{chart_y(float(row["pass_at_k"])) - 10:.1f}" class="pointlabel" text-anchor="middle">{pct(float(row["pass_at_k"]))}</text></g>'
        for row in prediction_rows
        if row["pass_at_k"] is not None
    )
    passk_chart = f"""
<svg class="linechart" viewBox="0 0 {chart_w} {chart_h}" role="img" aria-label="pass@1 到 pass@8 通过率折线图">
  {y_grid}
  <line x1="{left}" y1="{top}" x2="{left}" y2="{chart_h - bottom}" class="axis"/>
  <line x1="{left}" y1="{chart_h - bottom}" x2="{chart_w - right}" y2="{chart_h - bottom}" class="axis"/>
  {x_ticks}
  <polyline points="{polyline_points}" class="passline"/>
  <polyline points="{projected_points}" class="predline"/>
  {passk_points}
  {predicted_points}
</svg>
"""

    task_scores: dict[str, dict[str, float]] = report.get("task_scores", {})
    heatmap_attempts = list(range(1, max([8, *[int(a) for scores in task_scores.values() for a in scores.keys()]], default=8) + 1))

    def score_class(score: float | None) -> str:
        if score is None:
            return "missing"
        if score >= 1.0:
            return "pass"
        if score >= 0.8:
            return "near"
        if score >= 0.5:
            return "mid"
        if score > 0:
            return "low"
        return "zero"

    heatmap_head = "".join(f"<th>a{i}</th>" for i in heatmap_attempts)

    def render_score_heatmap_rows(score_map: dict[str, dict[str, float]]) -> str:
        return "\n".join(
            "<tr>"
            f"<td class=\"task\"><code>{html.escape(task_id)}</code></td>"
            + "".join(
                (
                    f"<td class=\"cell {score_class(scores.get(str(i)))}\" "
                    f"title=\"{html.escape(task_id)} attempt{i}: "
                    f"{'missing' if scores.get(str(i)) is None else num(scores.get(str(i)))}\">"
                    f"{'' if scores.get(str(i)) is None else num(scores.get(str(i)))}"
                    "</td>"
                )
                for i in heatmap_attempts
            )
            + "</tr>"
            for task_id, scores in sorted(score_map.items())
        )

    heatmap_rows = render_score_heatmap_rows(task_scores)

    pass1 = report["pass_at_1_attempt1_only"]
    any_pass = report["current_any_attempt_pass_rate"]
    per_k_by_k = {row["k"]: row for row in report["per_k_complete_prefix"]}
    pass5 = per_k_by_k.get(5)
    pass8 = per_k_by_k.get(8)
    json_uri = f"gs://{bucket}/{project_dir}/state/stage-report-passk-current.json"
    missing_grade_count = len(report.get("completed_attempts_without_grades", []))
    failure_analysis = report.get("failure_analysis", {})
    verifier_failed = int(failure_analysis.get("verifier_failed", 0) or 0)
    verifier_total = int(failure_analysis.get("verifier_total", 0) or 0)
    failure_category_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(row.get('label', row.get('category', '')))}</td>"
        f"<td>{row.get('count', 0)}</td>"
        f"<td>{pct(row.get('share_of_failed_verifiers'))}</td>"
        f"<td><code>{html.escape((row.get('example') or {}).get('task_id', ''))}</code> "
        f"{html.escape(str((row.get('example') or {}).get('attempt', '')))}</td>"
        f"<td>{html.escape((row.get('example') or {}).get('example', ''))}</td>"
        "</tr>"
        for row in failure_analysis.get("verifier_failure_categories", [])
    )
    attempt_primary_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(row.get('label', row.get('category', '')))}</td>"
        f"<td>{row.get('attempts', 0)}</td>"
        f"<td>{num(row.get('mean_score'))}</td>"
        "</tr>"
        for row in failure_analysis.get("attempt_primary_categories", [])
    )
    sample_finding_rows = "\n".join(
        "<tr>"
        f"<td><code>{html.escape(row.get('task_id', ''))}</code></td>"
        f"<td>{html.escape(str(row.get('attempt', '')))}</td>"
        f"<td>{num(row.get('score'))}</td>"
        f"<td>{html.escape(row.get('finding', ''))}</td>"
        "</tr>"
        for row in failure_analysis.get("trajectory_sample_findings", [])
    )
    rl_recommendations = "\n".join(
        f"<li>{html.escape(item)}</li>"
        for item in failure_analysis.get("rl_data_recommendation", [])
    )

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Seed Pro IB 当前阶段报告</title>
<style>
body{{margin:0;background:#f6f7f9;color:#1f2933;font-family:ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;line-height:1.5}}
main{{max-width:1180px;margin:auto;padding:28px 24px 48px}}h1{{margin:0 0 8px;font-size:30px;letter-spacing:0}}h2{{font-size:18px;margin:0 0 12px}}
.meta{{color:#697586;font-size:14px}}.grid{{display:grid;gap:16px}}.metrics{{grid-template-columns:repeat(5,minmax(0,1fr));margin:20px 0}}.two{{grid-template-columns:1fr 1fr;margin-top:16px}}
.panel{{background:white;border:1px solid #d9dee7;border-radius:8px;padding:18px;box-shadow:0 1px 2px rgba(16,24,40,.06)}}.label{{font-size:13px;color:#697586;margin-bottom:8px}}.value{{font-size:32px;font-weight:700;line-height:1}}
.sub,.note{{font-size:13px;color:#697586;margin-top:8px}}.ok{{color:#16865a}}.info{{color:#2866c7}}table{{width:100%;border-collapse:collapse;font-size:14px}}th,td{{padding:9px 8px;border-bottom:1px solid #d9dee7;text-align:left}}th{{font-size:12px;color:#697586;text-transform:uppercase}}.compact td{{vertical-align:top;font-size:13px}}.compact td:last-child{{color:#4b5565}}ul{{margin:8px 0 0;padding-left:20px}}li{{margin:5px 0}}
.bar{{display:grid;grid-template-columns:92px 1fr 52px;gap:10px;align-items:center;margin:9px 0;font-size:14px}}.bar b{{display:block;height:12px;border-radius:4px;background:#2866c7}}.bar b.green{{background:#16865a}}.bar b.amber{{background:#a66500}}.bar b.red{{background:#b42318}}
.linechart{{width:100%;height:auto;display:block}}.linechart .gridline{{stroke:#e6ebf2;stroke-width:1}}.linechart .axis,.linechart .tick{{stroke:#98a2b3;stroke-width:1.2}}.linechart .passline{{fill:none;stroke:#2866c7;stroke-width:3;stroke-linecap:round;stroke-linejoin:round}}.linechart .predline{{fill:none;stroke:#a66500;stroke-width:3;stroke-linecap:round;stroke-linejoin:round;stroke-dasharray:7 6}}.linechart .point{{fill:#16865a;stroke:white;stroke-width:2}}.linechart .predpoint{{fill:#a66500;stroke:white;stroke-width:2}}.linechart .axislabel{{fill:#697586;font-size:12px}}.linechart .pointlabel{{fill:#1f2933;font-size:12px;font-weight:700}}
.heatwrap{{overflow:auto;max-height:720px;border:1px solid #d9dee7;border-radius:8px}}.heatmap{{border-collapse:separate;border-spacing:0;font-size:12px;min-width:760px}}.heatmap th{{position:sticky;top:0;background:#f8fafc;z-index:2}}.heatmap .task{{position:sticky;left:0;background:#fff;z-index:1;min-width:260px}}.heatmap th,.heatmap td{{border-bottom:1px solid #e6ebf2;border-right:1px solid #eef2f7;padding:6px 7px;text-align:center;white-space:nowrap}}.heatmap .cell{{font-variant-numeric:tabular-nums;min-width:54px}}.heatmap .missing{{background:#f2f4f7;color:#98a2b3}}.heatmap .zero{{background:#f8d7da;color:#7a271a}}.heatmap .low{{background:#fff1c2;color:#7a4b00}}.heatmap .mid{{background:#cfe8ff;color:#164c8a}}.heatmap .near{{background:#b7e4c7;color:#0b5d3b}}.heatmap .pass{{background:#16865a;color:white;font-weight:700}}.legend{{display:flex;flex-wrap:wrap;gap:10px;margin:10px 0 0;font-size:12px;color:#697586}}.legend span{{display:inline-flex;align-items:center;gap:5px}}.swatch{{width:14px;height:14px;border-radius:3px;display:inline-block;border:1px solid #d9dee7}}.heat-missing{{background:#f2f4f7}}.heat-zero{{background:#f8d7da}}.heat-low{{background:#fff1c2}}.heat-mid{{background:#cfe8ff}}.heat-near{{background:#b7e4c7}}.heat-pass{{background:#16865a}}
code{{background:#eef2f7;padding:2px 5px;border-radius:4px;font-size:12px}}.callout{{border-left:4px solid #a66500;background:#fff8ec;padding:12px 14px;border-radius:6px;color:#614000;font-size:14px}}
@media(max-width:900px){{.metrics,.two{{grid-template-columns:1fr 1fr}}}}@media(max-width:620px){{main{{padding:20px 14px}}.metrics,.two{{grid-template-columns:1fr}}}}
</style></head><body><main>
<h1>APEX Agents IB 当前阶段报告：pass@1 / pass@k</h1>
<p class="meta">项目：<code>{html.escape(project_dir)}</code></p>
<p class="meta">生成时间：{html.escape(report["generated_at"])}</p>
<section class="grid metrics">
<div class="panel"><div class="label">pass@1 观测值</div><div class="value ok">{pct(pass1["observed_on_attempt1_graded_tasks"])}</div><div class="sub">{pass1["successes"]} / {pass1["tasks_with_attempt1_grade"]} 个 attempt1 已打分 task 成功</div></div>
<div class="panel"><div class="label">pass@5</div><div class="value ok">{pct(pass5["pass_at_k"] if pass5 else None)}</div><div class="sub">{pass5["successes"] if pass5 else 0} / {pass5["n_tasks_with_1_to_k"] if pass5 else 0} 个完整前缀 task 成功</div></div>
<div class="panel"><div class="label">pass@8</div><div class="value ok">{pct(pass8["pass_at_k"] if pass8 else None)}</div><div class="sub">{pass8["successes"] if pass8 else 0} / {pass8["n_tasks_with_1_to_k"] if pass8 else 0} 个完整前缀 task 成功</div></div>
<div class="panel"><div class="label">已计入 attempts</div><div class="value info">{report["scored_attempts"]}</div><div class="sub">覆盖 {report["scored_tasks"]} / {report["total_tasks"]} 个 task；无 grades 记 0：{missing_grade_count}</div></div>
<div class="panel"><div class="label">平均分</div><div class="value">{num(report["mean_score_all_scored_attempts"])}</div><div class="sub">所有已打分 attempts final_score 均值</div></div>
</section>
<section class="panel"><h2>当前结论</h2><div class="callout">这是当前阶段报告。Prefix pass@k 要求同一 task 具备 attempt1..k 的完成结果；完整执行但没有 grades.json 的 attempt 按 0 分计入。</div></section>
<section class="panel" style="margin-top:16px"><h2>pass@1 到 pass@10 通过率趋势</h2>{passk_chart}<p class="note">{html.escape(prediction_note)}</p></section>
<section class="grid two"><div class="panel"><h2>Attempt 覆盖</h2>{attempt_bars}<table><thead><tr><th>attempt</th><th>已计入</th><th>成功 attempt</th><th>attempt 成功率</th></tr></thead><tbody>{attempt_rows}</tbody></table></div><div class="panel"><h2>每个 task 已计入 attempts 数</h2>{dist_bars}</div></section>
<section class="grid two"><div class="panel"><h2>Prefix pass@k</h2><table><thead><tr><th>k</th><th>完整前缀 task</th><th>成功 task</th><th>pass@k</th><th>全量下界</th><th>best score 均值</th></tr></thead><tbody>{prefix_rows}</tbody></table></div><div class="panel"><h2>分数分布</h2>{bin_bars}<p class="note">成功阈值：<code>final_score &gt;= 1.0</code>。</p></div></section>
<section class="panel" style="margin-top:16px"><h2>RL 数据诊断：Verifier 丢分归因</h2><p class="note">当前 scored attempts 的 verifier checks：{verifier_failed} / {verifier_total} failed。分类来自 <code>grades.json</code> 的 judge rationale 规则解析，类别用于定位 RL 数据方向，不作为精确人工标签。</p><table class="compact"><thead><tr><th>failure category</th><th>failed verifiers</th><th>占 failed verifier</th><th>example</th><th>evidence snippet</th></tr></thead><tbody>{failure_category_rows}</tbody></table></section>
<section class="grid two"><div class="panel"><h2>Attempt 主失败类型</h2><table><thead><tr><th>primary category</th><th>attempts</th><th>mean score</th></tr></thead><tbody>{attempt_primary_rows}</tbody></table></div><div class="panel"><h2>RL 数据建议</h2><ul>{rl_recommendations}</ul><p class="note">结论：当前 IB benchmark 主要瓶颈不是泛化检索，而是 finance workbook mutation、recalculation、exact numeric extraction、artifact/final answer 对齐。</p></div></section>
<section class="panel" style="margin-top:16px"><h2>Trajectory 样本验证</h2><table class="compact"><thead><tr><th>task</th><th>attempt</th><th>score</th><th>finding</th></tr></thead><tbody>{sample_finding_rows}</tbody></table></section>
<section class="panel" style="margin-top:16px"><h2>Task × Attempt Score 热力图</h2><div class="heatwrap"><table class="heatmap"><thead><tr><th class="task">task</th>{heatmap_head}</tr></thead><tbody>{heatmap_rows}</tbody></table></div><div class="legend"><span><i class="swatch heat-missing"></i>missing</span><span><i class="swatch heat-zero"></i>0</span><span><i class="swatch heat-low"></i>0-0.5</span><span><i class="swatch heat-mid"></i>0.5-0.8</span><span><i class="swatch heat-near"></i>0.8-1</span><span><i class="swatch heat-pass"></i>1.0 pass</span></div></section>
<section class="panel" style="margin-top:16px"><h2>报告来源</h2><p class="meta">JSON：<code>{html.escape(json_uri)}</code></p></section>
</main></body></html>"""


def upload(path: Path, uri: str) -> None:
    subprocess.run(["gsutil", "cp", str(path), uri], check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate staged pass@k report from GCS result artifacts")
    parser.add_argument("--bucket", default="sotalab-archipelago-eval")
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--total-tasks", type=int, default=160)
    parser.add_argument("--output-html", default=None)
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--upload", action="store_true")
    parser.add_argument("--jobs", type=int, default=64)
    args = parser.parse_args()

    storage_mod = require_storage()
    client = storage_mod.Client()
    bucket = client.bucket(args.bucket)
    blob_names = list_result_blobs(client, args.bucket, args.project_dir)
    print(f"result_artifacts={len(blob_names)}")

    artifacts: dict[tuple[str, int], set[str]] = collections.defaultdict(set)
    grade_blob_names: list[str] = []
    for name in blob_names:
        match = ARTIFACT_RE.search("/" + name)
        if not match:
            continue
        key = (match.group(1), int(match.group(2)))
        artifact = match.group(3)
        artifacts[key].add(artifact)
        if artifact == "grades.json":
            grade_blob_names.append(name)
    print(f"grades={len(grade_blob_names)}")

    rows: list[tuple[str, int, float, float | None, int, dict[str, int], int, int, dict[str, str]]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        for row in executor.map(lambda name: read_grade(bucket, name), grade_blob_names):
            if row is not None:
                rows.append(row)
    grade_keys = {(task_id, attempt) for task_id, attempt, *_ in rows}
    completed_without_grades = [
        key
        for key, names in artifacts.items()
        if {"status.tsv", "trajectory.json"} <= names and key not in grade_keys
    ]
    for task_id, attempt in completed_without_grades:
        rows.append((task_id, attempt, 0.0, None, 0, {}, 0, 0, {}))
    print(f"completed_without_grades={len(completed_without_grades)}")

    report = aggregate(rows, args.total_tasks, completed_without_grades)
    report["project_dir"] = args.project_dir
    report["bucket"] = args.bucket

    output_json = Path(args.output_json or "/tmp/seed-pro-ib-pass8-stage-report-current.json")
    output_html = Path(
        args.output_html
        or "/Users/lumin/sotalab/reports/seed-pro-ib-pass8-stage-report-current.html"
    )
    output_json.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    output_html.parent.mkdir(parents=True, exist_ok=True)
    output_html.write_text(render_html(report, args.project_dir, args.bucket))

    if args.upload:
        upload(output_json, f"gs://{args.bucket}/{args.project_dir}/state/stage-report-passk-current.json")
        upload(output_html, f"gs://{args.bucket}/{args.project_dir}/state/stage-report-passk-current.html")

    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"html={output_html}")
    print(f"json={output_json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

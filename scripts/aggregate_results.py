#!/usr/bin/env python3
"""
One-shot aggregator: download all grades.json from GCS, compute
pass@1 / pass@k / mean score, write summary.json.

Usage:
    python scripts/aggregate_results.py [--project-dir ...] [--bucket ...]

Reads jobs from local ~/.archipelago-eval/<project>/jobs.json (written by
local_scheduler.py publish) and pulls grades.json from GCS results/ tree.
"""
from __future__ import annotations

import argparse
import collections
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from google.cloud import storage

PROJECT_DIR_DEFAULT = "eval-projects/seed-pro-pass3-20260616"
BUCKET_DEFAULT = "sotalab-archipelago-eval"
PROJECT_DEFAULT = "sotalab-prod"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_state_dir(project_dir: str) -> Path:
    return Path.home() / ".archipelago-eval" / project_dir.replace("/", "__")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project-dir", default=PROJECT_DIR_DEFAULT)
    ap.add_argument("--bucket", default=BUCKET_DEFAULT)
    ap.add_argument("--project", default=PROJECT_DEFAULT)
    ap.add_argument("--k", type=int, default=None,
                    help="Total attempts per task (overrides jobs.json)")
    args = ap.parse_args()

    state_dir = local_state_dir(args.project_dir)
    jobs_path = state_dir / "jobs.json"
    if not jobs_path.exists():
        print(f"missing {jobs_path}; run local_scheduler.py publish first")
        return 1

    jobs = json.loads(jobs_path.read_text())
    if args.k is None:
        args.k = max(
            (j["attempt"] for j in jobs),
            default=0,
        )

    client = storage.Client(project=args.project)
    bucket = client.bucket(args.bucket)

    by_task: dict[str, list[dict]] = collections.defaultdict(list)
    for j in jobs:
        gp = f"{args.project_dir}/results/{j['task_id']}/attempt{j['attempt']}/grades.json"
        blob = bucket.blob(gp)
        if blob.exists():
            g = json.loads(blob.download_as_text())
            score = g.get("scoring_results", {}).get("final_score")
        else:
            score = None
        by_task[j["task_id"]].append({"attempt": j["attempt"], "score": score})

    rows = []
    for tid, runs in sorted(by_task.items()):
        scores = [r["score"] for r in runs if isinstance(r["score"], (int, float))]
        n = len(scores)
        if n == 0:
            rows.append({
                "task_id": tid, "n_scored": 0,
                "pass_at_1": None, "pass_at_k": 0, "mean_score": None,
                "scores": [],
            })
            continue
        pass_1 = sum(1 for s in scores if s >= 1.0) / n
        pass_k = 1 if any(s >= 1.0 for s in scores) else 0
        mean = sum(scores) / n
        rows.append({
            "task_id": tid, "n_scored": n,
            "pass_at_1": pass_1, "pass_at_k": pass_k,
            "mean_score": mean, "scores": scores,
        })

    n_scored = sum(r["n_scored"] for r in rows)
    overall_pass1 = (sum(r["pass_at_1"] * r["n_scored"]
                         for r in rows) / n_scored) if n_scored else 0
    overall_passk = (sum(r["pass_at_k"] for r in rows) / len(rows)) if rows else 0
    overall_mean = (sum(r["mean_score"] * r["n_scored"]
                        for r in rows) / n_scored) if n_scored else 0

    out = {
        "generated_at": now_iso(),
        "project_dir": args.project_dir,
        "model": jobs[0]["model"] if jobs else None,
        "k": args.k,
        "per_task": rows,
        "overall": {
            "pass_at_1": overall_pass1,
            "pass_at_k": overall_passk,
            "mean_score": overall_mean,
            "n_attempts_scored": n_scored,
        },
    }

    out_local = state_dir / "summary.json"
    out_local.write_text(json.dumps(out, indent=2))

    # CSV sibling (paste-into-spreadsheet friendly)
    csv_path = state_dir / "summary.csv"
    with csv_path.open("w") as f:
        f.write("task_id,n_scored,pass_at_1,pass_at_k,mean_score,scores\n")
        for r in rows:
            p1 = "" if r["pass_at_1"] is None else f"{r['pass_at_1']:.4f}"
            mn = "" if r["mean_score"] is None else f"{r['mean_score']:.4f}"
            f.write(
                f"{r['task_id']},{r['n_scored']},{p1},{r['pass_at_k']},{mn},"
                f"\"{';'.join(f'{s:.2f}' for s in r['scores'])}\"\n"
            )
        f.write(
            f"OVERALL,{n_scored},{overall_pass1:.4f},"
            f"{overall_passk:.4f},{overall_mean:.4f},\"\n"
        )

    out_gcs = bucket.blob(f"{args.project_dir}/state/summary.json")
    out_gcs.upload_from_string(
        json.dumps(out, indent=2), content_type="application/json"
    )
    bucket.blob(f"{args.project_dir}/state/summary.csv").upload_from_string(
        csv_path.read_text(), content_type="text/csv"
    )

    print(f"\n=== AGGREGATE  ({now_iso()}) ===")
    print(f"  {'task_id':<50} {'pass@1':<8} {'pass@k':<8} {'mean':<6}")
    print("  " + "-" * 80)
    for r in rows:
        p1 = f"{r['pass_at_1']:.2f}" if r["pass_at_1"] is not None else "—"
        pk = f"{r['pass_at_k']:.2f}" if r["pass_at_k"] is not None else "—"
        mn = f"{r['mean_score']:.2f}" if r["mean_score"] is not None else "—"
        print(f"  {r['task_id']:<50} {p1:<8} {pk:<8} {mn:<6}")
    print(
        f"\n  OVERALL  pass@1={overall_pass1:.2f}  "
        f"pass@k={overall_passk:.2f}  mean={overall_mean:.2f}  "
        f"(n_scored={n_scored})"
    )
    print(f"\n  summary.json -> {out_local}")
    print(f"  gs://{args.bucket}/{args.project_dir}/state/summary.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())

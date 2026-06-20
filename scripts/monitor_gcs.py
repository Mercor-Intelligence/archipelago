#!/usr/bin/env python3
"""
GCS-backed status monitor for archipelago eval jobs.

Polls gs://<bucket>/<project>/results/*/attempt*/status.tsv every N seconds.
No SSH, no Pub/Sub — just GCS.

Usage:
    python scripts/monitor_gcs.py \
        --bucket sotalab-archipelago-eval \
        --project-dir eval-projects/seed-pro-pass3-20260616 \
        [--interval 30]

Prints a live table showing each (task, attempt):
  - status (pending / running / completed / error / failed)
  - final_score (if grades.json available)
  - worker_id (from status.tsv)
  - duration (s)
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

from google.cloud import storage


def list_jobs_from_manifest(bucket, project_dir: str) -> list[dict[str, Any]]:
    """Read jobs list from state/manifest.json if present, else infer from GCS results/."""
    blob = bucket.blob(f"{project_dir}/state/manifest.json")
    if blob.exists():
        try:
            data = json.loads(blob.download_as_text())
            return data.get("jobs", [])
        except Exception:
            pass
    # Fallback: scan GCS results/ tree
    jobs = []
    blobs = client.list_blobs(bucket.name, prefix=f"{project_dir}/results/")
    seen = set()
    for b in blobs:
        parts = b.name.split("/")
        # results/<task_id>/attempt<N>/<file>
        if len(parts) >= 4 and parts[0] == "results":
            task_id, attempt_dir = parts[1], parts[2]
            if attempt_dir.startswith("attempt"):
                key = (task_id, attempt_dir)
                if key not in seen:
                    seen.add(key)
                    attempt = int(attempt_dir.replace("attempt", ""))
                    jobs.append({"task_id": task_id, "attempt": attempt, "model": "?"})
    return jobs


def read_status_tsv(bucket, project_dir: str, task_id: str, attempt: int) -> dict[str, str]:
    """Read status.tsv from GCS, return {field: value} dict."""
    blob = bucket.blob(f"{project_dir}/results/{task_id}/attempt{attempt}/status.tsv")
    if not blob.exists():
        return {}
    try:
        text = blob.download_as_text()
    except Exception:
        return {}
    result = {}
    for line in text.splitlines():
        if "\t" in line:
            k, v = line.split("\t", 1)
            result[k] = v
    return result


def read_grades(bucket, project_dir: str, task_id: str, attempt: int) -> float | None:
    blob = bucket.blob(f"{project_dir}/results/{task_id}/attempt{attempt}/grades.json")
    if not blob.exists():
        return None
    try:
        g = json.loads(blob.download_as_text())
        return g.get("scoring_results", {}).get("final_score")
    except Exception:
        return None


def render_row(job: dict, status: dict, score: float | None) -> str:
    task_id = job["task_id"]
    attempt = job["attempt"]
    model = job.get("model", "?")
    if score is not None:
        run_status = "completed"
        score_str = f"{score:.2f}"
    else:
        run_status = status.get("run_status", "pending")
        score_str = "—"
    worker = status.get("worker_id", "—")[:18]
    dur = status.get("agent_exit", "—")
    return f"  {task_id[:48]:<48} a{attempt:<2} {model[:24]:<24} {run_status:<10} {score_str:<6} {worker:<18} {dur}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", default="sotalab-archipelago-eval")
    ap.add_argument("--project-dir", default="eval-projects/seed-pro-pass3-20260616")
    ap.add_argument("--project", default="sotalab-prod")
    ap.add_argument("--interval", type=int, default=30, help="poll interval seconds")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    client = storage.Client(project=args.project)
    bucket = client.bucket(args.bucket)

    while True:
        jobs = list_jobs_from_manifest(bucket, args.project_dir)
        if not jobs:
            print(f"  (no jobs found under {args.bucket}/{args.project_dir}/)")
            if args.once:
                return 0
            time.sleep(args.interval)
            continue
        print("\n" + "=" * 130)
        print(f"  archipelago eval  {args.bucket}/{args.project_dir}  ({len(jobs)} jobs)")
        print(f"  {'task_id':<48} {'at':<3} {'model':<24} {'status':<10} {'score':<6} {'worker':<18} {'exit'}")
        print("  " + "-" * 128)
        for j in jobs:
            status = read_status_tsv(bucket, args.project_dir, j["task_id"], j["attempt"])
            score = read_grades(bucket, args.project_dir, j["task_id"], j["attempt"])
            print(render_row(j, status, score))
        n_completed = sum(
            1 for j in jobs
            if read_grades(bucket, args.project_dir, j["task_id"], j["attempt"]) is not None
        )
        print(f"\n  completed: {n_completed}/{len(jobs)}")
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    sys.exit(main())
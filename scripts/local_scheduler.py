#!/usr/bin/env python3
"""
Local (Mac) scheduler for the archipelago GCS-backed eval.

Subcommands:
  publish    Sample tasks + write jobs/task ids to local state and GCS
  status     Print current progress from GCS results/state
  aggregate  Download all grades.json from GCS, compute pass@1/pass@3/mean score
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import random
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_DIR_DEFAULT = "eval-projects/seed-pro-pass3-20260616"
BUCKET_DEFAULT = "sotalab-archipelago-eval"
GCP_PROJECT_DEFAULT = "sotalab-prod"
MODEL_DEFAULT = "doubao-seed-2-0-pro-260215"
K_DEFAULT = 3
N_TASKS_DEFAULT = 5


def gcs_path(bucket: str, *parts: str) -> str:
    return f"gs://{bucket}/" + "/".join(p.strip("/") for p in parts)


def local_state_dir(project_dir: str) -> Path:
    name = project_dir.replace("/", "__")
    return Path.home() / ".archipelago-eval" / name


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def require_storage():
    try:
        from google.cloud import storage
    except ImportError as exc:
        raise SystemExit(
            "google-cloud-storage is required for this command. "
            "Run through `uv run python scripts/local_scheduler.py ...` "
            "or install `google-cloud-storage` in the active Python environment."
        ) from exc
    return storage


def require_hf_hub_download():
    try:
        from huggingface_hub import hf_hub_download
    except ImportError as exc:
        raise SystemExit(
            "huggingface_hub is required for `publish`. "
            "Run through `uv run python scripts/local_scheduler.py publish ...` "
            "or install `huggingface_hub` in the active Python environment."
        ) from exc
    return hf_hub_download


def cmd_publish(args: argparse.Namespace) -> int:
    """Sample N tasks, generate K*N jobs, and write scheduler state to GCS."""
    project_dir = args.project_dir
    bucket = args.bucket
    project = args.gcp_project
    model = args.model
    k = args.k
    n_tasks = args.n_tasks

    eval_id = f"eval-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
    state_dir = local_state_dir(project_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    hf_hub_download = require_hf_hub_download()
    storage = require_storage()

    # Download tasks
    print(f"[publish] downloading tasks_and_rubrics.json from HF...")
    tasks_path = hf_hub_download(
        "mercor/apex-agents", "tasks_and_rubrics.json", repo_type="dataset"
    )
    tasks = json.load(open(tasks_path))
    print(f"[publish] total tasks available: {len(tasks)}")

    rng = random.Random(args.seed)
    sampled = rng.sample(tasks, n_tasks)
    sampled_path = state_dir / "sampled_tasks.json"
    sampled_path.write_text(json.dumps([t["task_id"] for t in sampled], indent=2))
    print(f"[publish] sampled {n_tasks} tasks (seed={args.seed}): "
          f"{[t['task_id'] for t in sampled]}")

    # Build jobs
    jobs = []
    for t in sampled:
        for attempt in range(1, k + 1):
            jobs.append({
                "eval_id": eval_id,
                "task_id": t["task_id"],
                "task_name": t.get("task_name"),
                "model": model,
                "attempt": attempt,
                "force_rerun": bool(args.force_rerun),
                "job_id": f"{t['task_id']}-a{attempt}",
                "published_at": now_iso(),
            })

    print(f"[publish] prepared {len(jobs)} jobs for GCS queue workers")

    # Save scheduler state locally.
    (state_dir / "jobs.json").write_text(json.dumps(jobs, indent=2))
    task_ids = [{"task_id": t["task_id"], "model": model} for t in sampled]
    (state_dir / "task_ids.json").write_text(json.dumps(task_ids, indent=2))

    # Update GCS state used by dynamic queue workers and status/aggregate.
    client = storage.Client(project=project)
    bucket_obj = client.bucket(bucket)
    bucket_obj.blob(f"{project_dir}/state/jobs.json").upload_from_string(
        json.dumps(jobs, indent=2),
        content_type="application/json",
    )
    bucket_obj.blob(f"{project_dir}/state/task_ids.json").upload_from_string(
        json.dumps(task_ids, indent=2),
        content_type="application/json",
    )
    manifest_blob = bucket_obj.blob(f"{project_dir}/state/manifest.json")
    manifest_blob.upload_from_string(
        json.dumps({
            "eval_id": eval_id,
            "project_dir": project_dir,
            "model": model,
            "k": k,
            "n_tasks": n_tasks,
            "sampled_tasks": [t["task_id"] for t in sampled],
            "jobs": jobs,
            "prepared_at": now_iso(),
            "status": "prepared",
            "queue": "gcs",
        }, indent=2),
        content_type="application/json",
    )
    print(f"[publish] manifest written to gs://{bucket}/{project_dir}/state/manifest.json")
    print(f"[publish] task ids written to gs://{bucket}/{project_dir}/state/task_ids.json")
    print("[publish] DONE. Start workers with 'make dynamic-workers' and monitor with 'make status'.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """Print progress: published, pending, done (with scores)."""
    state_dir = local_state_dir(args.project_dir)
    jobs_path = state_dir / "jobs.json"
    if not jobs_path.exists():
        print(f"[status] no jobs.json at {jobs_path}. Run 'publish' first.")
        return 1
    jobs = json.loads(jobs_path.read_text())
    storage = require_storage()
    client = storage.Client(project=args.gcp_project)
    bucket_obj = client.bucket(args.bucket)
    status_by_job: dict[str, dict] = {}
    for j in jobs:
        path = (
            f"{args.project_dir}/results/{j['task_id']}/attempt{j['attempt']}/status.tsv"
        )
        blob = bucket_obj.blob(path)
        if blob.exists():
            data = blob.download_as_text()
            entry = {"gcs": True}
            for line in data.splitlines():
                if "\t" in line:
                    k, v = line.split("\t", 1)
                    entry[k] = v
            status_by_job[j["job_id"]] = entry
        else:
            status_by_job[j["job_id"]] = {"gcs": False}

    n_pub = len(jobs)
    n_done = sum(1 for s in status_by_job.values()
                 if s.get("run_status") == "completed")
    n_failed = sum(1 for s in status_by_job.values()
                   if s.get("run_status") == "error")
    n_pending = n_pub - n_done - n_failed

    print(f"\n=== STATUS  ({now_iso()}) ===")
    print(f"  published : {n_pub}")
    print(f"  completed : {n_done}")
    print(f"  failed    : {n_failed}")
    print(f"  pending   : {n_pending}")
    print()
    print(f"  {'job_id':<60} {'status':<12} {'score':<6} {'worker':<25}")
    print("  " + "-" * 105)
    for j in jobs:
        s = status_by_job.get(j["job_id"], {})
        status = s.get("run_status", "pending")
        score = "—"
        if status == "completed":
            # try to read grades.json
            gp = (
                f"{args.project_dir}/results/{j['task_id']}/attempt{j['attempt']}/grades.json"
            )
            gblob = bucket_obj.blob(gp)
            if gblob.exists():
                g = json.loads(gblob.download_as_text())
                score = f"{g.get('scoring_results',{}).get('final_score',0):.2f}"
        worker = s.get("worker_id", "—")
        print(f"  {j['job_id']:<60} {status:<12} {score:<6} {worker:<25}")
    return 0


def cmd_aggregate(args: argparse.Namespace) -> int:
    """Download all grades.json, compute per-task pass@1/pass@3/mean score."""
    storage = require_storage()
    client = storage.Client(project=args.gcp_project)
    bucket_obj = client.bucket(args.bucket)
    state_dir = local_state_dir(args.project_dir)
    jobs_path = state_dir / "jobs.json"
    if not jobs_path.exists():
        print(f"[aggregate] no jobs.json; run 'publish' first")
        return 1
    jobs = json.loads(jobs_path.read_text())
    by_task: dict[str, list[dict]] = collections.defaultdict(list)
    for j in jobs:
        gp = (
            f"{args.project_dir}/results/{j['task_id']}/attempt{j['attempt']}/grades.json"
        )
        gblob = bucket_obj.blob(gp)
        if gblob.exists():
            g = json.loads(gblob.download_as_text())
            score = g.get("scoring_results", {}).get("final_score")
        else:
            score = None
        by_task[j["task_id"]].append({
            "attempt": j["attempt"],
            "model": j["model"],
            "score": score,
        })

    print(f"\n=== AGGREGATE  ({now_iso()}) ===")
    print(f"  {'task_id':<50} {'name':<40} {'pass@1':<8} {'pass@3':<8} {'mean':<6}")
    print("  " + "-" * 120)
    summary_rows = []
    for tid, runs in by_task.items():
        scores = [r["score"] for r in runs if isinstance(r["score"], (int, float))]
        n = len(scores)
        pass_1 = sum(1 for s in scores if s >= 1.0) / n if n else 0
        # pass@k: any run scored 1.0 among the k attempts
        pass_k = 1 if any(s >= 1.0 for s in scores) else 0
        mean = sum(scores) / n if n else 0
        name = runs[0].get("task_name") or tid
        summary_rows.append({
            "task_id": tid,
            "task_name": name,
            "n_attempts_scored": n,
            "pass_at_1": pass_1,
            "pass_at_k": pass_k,
            "mean_score": mean,
            "scores": scores,
        })
        print(f"  {tid:<50} {name[:38]:<40} {pass_1:.2f}     {pass_k:.2f}     {mean:.2f}")

    overall_n = sum(r["n_attempts_scored"] for r in summary_rows)
    overall_pass1 = (sum(r["pass_at_1"] * r["n_attempts_scored"]
                         for r in summary_rows) / overall_n) if overall_n else 0
    overall_passk = (sum(r["pass_at_k"] for r in summary_rows)
                     / len(summary_rows)) if summary_rows else 0
    overall_mean = (sum(r["mean_score"] * r["n_attempts_scored"]
                        for r in summary_rows) / overall_n) if overall_n else 0
    print(f"\n  OVERALL   pass@1={overall_pass1:.2f}  pass@k={overall_passk:.2f}  mean={overall_mean:.2f}  (n_scored={overall_n})")

    out = {
        "generated_at": now_iso(),
        "project_dir": args.project_dir,
        "model": jobs[0]["model"] if jobs else None,
        "k": max(len(runs) for runs in by_task.values()) if by_task else 0,
        "per_task": summary_rows,
        "overall": {
            "pass_at_1": overall_pass1,
            "pass_at_k": overall_passk,
            "mean_score": overall_mean,
            "n_attempts_scored": overall_n,
        },
    }
    out_path = state_dir / "summary.json"
    out_path.write_text(json.dumps(out, indent=2))
    print(f"\n  summary.json -> {out_path}")

    # Upload to GCS too
    bucket_obj.blob(f"{args.project_dir}/state/summary.json").upload_from_string(
        json.dumps(out, indent=2),
        content_type="application/json",
    )
    print(f"  gs://{args.bucket}/{args.project_dir}/state/summary.json")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-dir", default=PROJECT_DIR_DEFAULT)
    parser.add_argument("--bucket", default=BUCKET_DEFAULT)
    parser.add_argument("--gcp-project", default=GCP_PROJECT_DEFAULT)
    parser.add_argument(
        "--pubsub-project",
        dest="gcp_project",
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("publish")
    p.add_argument("--n-tasks", type=int, default=N_TASKS_DEFAULT)
    p.add_argument("--k", type=int, default=K_DEFAULT)
    p.add_argument("--model", default=MODEL_DEFAULT)
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--force-rerun", action="store_true")
    p.set_defaults(func=cmd_publish)

    s = sub.add_parser("status")
    s.set_defaults(func=cmd_status)

    a = sub.add_parser("aggregate")
    a.set_defaults(func=cmd_aggregate)

    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

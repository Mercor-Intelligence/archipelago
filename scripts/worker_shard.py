#!/usr/bin/env python3
"""
Run a static shard of HuggingFace APEX tasks on one worker VM.

Each worker gets a deterministic modulo shard of the full task list. Tasks run
serially inside one VM so port 8080, Docker container state, and local output do
not overlap between tasks. Different VMs write to different task prefixes in
GCS.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import subprocess
import sys
import time
from pathlib import Path


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(f"worker_shard.{socket.gethostname()}")

ARCHIPELAGO_DIR = Path(os.environ.get("ARCHIPELAGO_DIR", "/opt/archipelago"))
DEFAULT_BUCKET = os.environ.get("EVAL_BUCKET", "sotalab-archipelago-eval")
DEFAULT_PROJECT_DIR = os.environ.get(
    "EVAL_PROJECT_DIR", "eval-projects/seed-pro-full-20260618"
)
WORKER_ID = socket.gethostname()


def load_task_ids(path: Path) -> list[str]:
    data = json.loads(path.read_text())
    task_ids: list[str] = []
    for item in data:
        if isinstance(item, str):
            task_ids.append(item)
        elif isinstance(item, dict) and item.get("task_id"):
            task_ids.append(str(item["task_id"]))
        else:
            raise ValueError(f"Unsupported task entry: {item!r}")
    if len(task_ids) != len(set(task_ids)):
        raise ValueError("Task list contains duplicate task_id values")
    return task_ids


def gcs_has_completed_result(bucket: str, project_dir: str, task_id: str, attempt: int) -> bool:
    prefix = f"gs://{bucket}/{project_dir}/results/{task_id}/attempt{attempt}"
    status = subprocess.run(
        ["gsutil", "cat", f"{prefix}/status.tsv"],
        text=True,
        capture_output=True,
    )
    if status.returncode != 0 or "run_status\tcompleted" not in status.stdout:
        return False

    listing = subprocess.run(
        ["gsutil", "ls", "-r", f"{prefix}/**"],
        text=True,
        capture_output=True,
    )
    if listing.returncode != 0:
        return False
    files = listing.stdout.splitlines()
    return any(p.endswith("/trajectory.json") for p in files) and any(
        p.endswith("/grades.json") for p in files
    )


def run_task(
    task_id: str,
    model: str,
    attempt: int,
    bucket: str,
    project_dir: str,
    max_steps: int,
    temperature: float | None,
    rerun: bool,
) -> int:
    cmd = [
        "python3",
        str(ARCHIPELAGO_DIR / "scripts" / "run_single_task.py"),
        "--task-id",
        task_id,
        "--model",
        model,
        "--attempt",
        str(attempt),
        "--worker-id",
        WORKER_ID,
        "--eval-project",
        project_dir,
        "--bucket",
        bucket,
        "--project-dir",
        project_dir,
        "--max-steps",
        str(max_steps),
    ]
    if temperature is not None:
        cmd.extend(["--temperature", str(temperature)])
    if rerun:
        cmd.append("--rerun")
    log.info("running task=%s attempt=%d", task_id, attempt)
    return subprocess.run(cmd, cwd=ARCHIPELAGO_DIR).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Run one static shard of APEX tasks")
    parser.add_argument("--tasks-file", required=True)
    parser.add_argument("--shard-index", type=int, required=True, help="0-based shard index")
    parser.add_argument("--num-shards", type=int, default=5)
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument(
        "--attempt-start",
        type=int,
        default=None,
        help="First attempt to run when --k is set; defaults to --attempt",
    )
    parser.add_argument(
        "--k",
        type=int,
        default=1,
        help="Number of sequential attempts to run per task",
    )
    parser.add_argument("--model", default="doubao-seed-2-0-pro-260215")
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--project-dir", default=DEFAULT_PROJECT_DIR)
    parser.add_argument("--max-steps", type=int, default=200)
    parser.add_argument("--temperature", type=float, default=None)
    parser.add_argument("--force", action="store_true", help="Rerun even if GCS already has completed output")
    parser.add_argument("--sleep-between", type=float, default=2.0)
    args = parser.parse_args()

    if args.shard_index < 0 or args.shard_index >= args.num_shards:
        raise SystemExit("--shard-index must be in [0, num-shards)")

    task_ids = load_task_ids(Path(args.tasks_file))
    shard_tasks = [
        task_id for i, task_id in enumerate(task_ids) if i % args.num_shards == args.shard_index
    ]
    if args.k < 1:
        raise SystemExit("--k must be >= 1")
    attempt_start = args.attempt if args.attempt_start is None else args.attempt_start
    if attempt_start < 1:
        raise SystemExit("--attempt-start/--attempt must be >= 1")
    attempts = list(range(attempt_start, attempt_start + args.k))

    log.info(
        "shard starting: worker_id=%s shard=%d/%d tasks=%d total=%d attempts=%s model=%s temperature=%s project_dir=%s",
        WORKER_ID,
        args.shard_index,
        args.num_shards,
        len(shard_tasks),
        len(task_ids),
        ",".join(str(a) for a in attempts),
        args.model,
        args.temperature,
        args.project_dir,
    )

    failures = 0
    skipped = 0
    started_at = time.time()
    for ordinal, task_id in enumerate(shard_tasks, start=1):
        log.info("task %d/%d: %s", ordinal, len(shard_tasks), task_id)
        for attempt in attempts:
            if not args.force and gcs_has_completed_result(
                args.bucket, args.project_dir, task_id, attempt
            ):
                skipped += 1
                log.info("skip completed task=%s attempt=%d", task_id, attempt)
                continue

            rc = run_task(
                task_id=task_id,
                model=args.model,
                attempt=attempt,
                bucket=args.bucket,
                project_dir=args.project_dir,
                max_steps=args.max_steps,
                temperature=args.temperature,
                rerun=args.force,
            )
            if rc != 0:
                failures += 1
                log.warning("task failed: task=%s attempt=%d rc=%d", task_id, attempt, rc)
            time.sleep(args.sleep_between)

    elapsed = time.time() - started_at
    log.info(
        "shard done: worker_id=%s shard=%d/%d tasks=%d attempts=%d skipped=%d failures=%d elapsed_sec=%.1f",
        WORKER_ID,
        args.shard_index,
        args.num_shards,
        len(shard_tasks),
        len(attempts),
        skipped,
        failures,
        elapsed,
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

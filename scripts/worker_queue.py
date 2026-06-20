#!/usr/bin/env python3
"""
Dynamic GCS-backed queue worker for APEX evaluations.

Each job is a (task_id, attempt) pair. Workers atomically claim jobs by creating
state/claims/<queue>/<job_id>.json with if_generation_match=0, then run
run_single_task.py. Completed GCS results are always skipped first, so the queue
can be restarted or expanded safely.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import random
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.api_core.exceptions import NotFound, PreconditionFailed
from google.cloud import storage


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(f"worker_queue.{socket.gethostname()}")

ARCHIPELAGO_DIR = Path(os.environ.get("ARCHIPELAGO_DIR", "/opt/archipelago"))
WORKER_ID = socket.gethostname()


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_jobs(path: Path, attempt_start: int, attempt_end: int) -> list[dict[str, Any]]:
    data = json.loads(path.read_text())
    jobs: list[dict[str, Any]] = []
    for item in data:
        task_id = item["task_id"] if isinstance(item, dict) else str(item)
        model = item.get("model") if isinstance(item, dict) else None
        for attempt in range(attempt_start, attempt_end + 1):
            jobs.append(
                {
                    "task_id": task_id,
                    "attempt": attempt,
                    "model": model,
                    "job_id": f"{task_id}-a{attempt}",
                }
            )
    return jobs


def result_prefix(project_dir: str, task_id: str, attempt: int) -> str:
    return f"{project_dir}/results/{task_id}/attempt{attempt}"


def has_completed_result(bucket: storage.Bucket, project_dir: str, task_id: str, attempt: int) -> bool:
    prefix = result_prefix(project_dir, task_id, attempt)
    status_blob = bucket.blob(f"{prefix}/status.tsv")
    try:
        status = status_blob.download_as_text()
    except Exception:
        return False
    if "run_status\tcompleted" not in status:
        return False
    return bucket.blob(f"{prefix}/grades.json").exists() and bucket.blob(
        f"{prefix}/trajectory.json"
    ).exists()


def claim_job(
    bucket: storage.Bucket,
    project_dir: str,
    queue_name: str,
    job: dict[str, Any],
) -> bool:
    blob = bucket.blob(f"{project_dir}/state/claims/{queue_name}/{job['job_id']}.json")
    payload = {
        **job,
        "worker_id": WORKER_ID,
        "claimed_at": now_iso(),
    }
    try:
        blob.upload_from_string(
            json.dumps(payload, indent=2) + "\n",
            content_type="application/json",
            if_generation_match=0,
        )
        return True
    except PreconditionFailed:
        return False


def mark_done(
    bucket: storage.Bucket,
    project_dir: str,
    queue_name: str,
    job: dict[str, Any],
    rc: int,
) -> None:
    blob = bucket.blob(f"{project_dir}/state/done/{queue_name}/{job['job_id']}.json")
    payload = {
        **job,
        "worker_id": WORKER_ID,
        "finished_at": now_iso(),
        "returncode": rc,
    }
    blob.upload_from_string(json.dumps(payload, indent=2) + "\n", content_type="application/json")


def mark_failure(
    bucket: storage.Bucket,
    project_dir: str,
    queue_name: str,
    job: dict[str, Any],
    rc: int,
) -> None:
    blob = bucket.blob(
        f"{project_dir}/state/failures/{queue_name}/{job['job_id']}-{WORKER_ID}-{int(time.time())}.json"
    )
    payload = {
        **job,
        "worker_id": WORKER_ID,
        "finished_at": now_iso(),
        "returncode": rc,
    }
    blob.upload_from_string(json.dumps(payload, indent=2) + "\n", content_type="application/json")


def release_claim(
    bucket: storage.Bucket,
    project_dir: str,
    queue_name: str,
    job: dict[str, Any],
) -> None:
    blob = bucket.blob(f"{project_dir}/state/claims/{queue_name}/{job['job_id']}.json")
    try:
        blob.delete()
    except NotFound:
        return


def run_job(
    job: dict[str, Any],
    model: str,
    bucket_name: str,
    project_dir: str,
    max_steps: int,
    temperature: float | None,
) -> int:
    cmd = [
        "python3",
        str(ARCHIPELAGO_DIR / "scripts" / "run_single_task.py"),
        "--task-id",
        job["task_id"],
        "--model",
        model,
        "--attempt",
        str(job["attempt"]),
        "--worker-id",
        WORKER_ID,
        "--eval-project",
        project_dir,
        "--bucket",
        bucket_name,
        "--project-dir",
        project_dir,
        "--max-steps",
        str(max_steps),
    ]
    if temperature is not None:
        cmd.extend(["--temperature", str(temperature)])
    log.info("running job_id=%s task=%s attempt=%s", job["job_id"], job["task_id"], job["attempt"])
    return subprocess.run(cmd, cwd=ARCHIPELAGO_DIR).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Run dynamic GCS queue jobs")
    parser.add_argument("--jobs-file", required=True)
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--bucket", default=os.environ.get("EVAL_BUCKET", "sotalab-archipelago-eval"))
    parser.add_argument("--queue-name", default="pass5")
    parser.add_argument("--attempt-start", type=int, default=1)
    parser.add_argument("--attempt-end", type=int, default=5)
    parser.add_argument("--model", default="doubao-seed-2-0-pro-260215")
    parser.add_argument("--max-steps", type=int, default=200)
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--idle-sleep", type=float, default=10.0)
    parser.add_argument("--max-consecutive-failures", type=int, default=2)
    parser.add_argument("--shuffle", action="store_true")
    args = parser.parse_args()

    client = storage.Client()
    bucket = client.bucket(args.bucket)
    jobs = load_jobs(Path(args.jobs_file), args.attempt_start, args.attempt_end)
    if args.shuffle:
        random.shuffle(jobs)

    log.info(
        "queue starting: worker_id=%s queue=%s jobs=%d attempts=%d..%d model=%s temperature=%s project_dir=%s",
        WORKER_ID,
        args.queue_name,
        len(jobs),
        args.attempt_start,
        args.attempt_end,
        args.model,
        args.temperature,
        args.project_dir,
    )

    completed = skipped_claimed = claimed = failures = consecutive_failures = 0
    for job in jobs:
        if has_completed_result(bucket, args.project_dir, job["task_id"], job["attempt"]):
            completed += 1
            continue
        if not claim_job(bucket, args.project_dir, args.queue_name, job):
            skipped_claimed += 1
            continue
        claimed += 1
        rc = run_job(
            job=job,
            model=args.model,
            bucket_name=args.bucket,
            project_dir=args.project_dir,
            max_steps=args.max_steps,
            temperature=args.temperature,
        )
        if rc != 0:
            failures += 1
            consecutive_failures += 1
            mark_failure(bucket, args.project_dir, args.queue_name, job, rc)
            release_claim(bucket, args.project_dir, args.queue_name, job)
            log.warning("job failed: job_id=%s rc=%d", job["job_id"], rc)
            if consecutive_failures >= args.max_consecutive_failures:
                log.error(
                    "stopping after %d consecutive failures; last job_id=%s",
                    consecutive_failures,
                    job["job_id"],
                )
                return 1
        else:
            consecutive_failures = 0
            mark_done(bucket, args.project_dir, args.queue_name, job, rc)
            release_claim(bucket, args.project_dir, args.queue_name, job)
        time.sleep(args.idle_sleep)

    log.info(
        "queue done: completed=%d skipped_claimed=%d claimed=%d failures=%d",
        completed,
        skipped_claimed,
        claimed,
        failures,
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

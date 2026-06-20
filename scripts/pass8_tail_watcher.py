#!/usr/bin/env python3
"""
Wait for pass@5 artifacts, then run the pass@8 tail attempts.

This is meant to run as a lightweight systemd service on each worker VM. All
workers can run the watcher concurrently; once pass@5 is complete they all enter
the same GCS-backed pass8 queue, and claim creation keeps work distributed.
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from pathlib import Path

from google.cloud import storage


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger("pass8_tail_watcher")


def count_complete_attempts(
    client: storage.Client,
    bucket_name: str,
    project_dir: str,
    attempt_start: int,
    attempt_end: int,
) -> tuple[int, int]:
    required = {"status.tsv", "grades.json", "trajectory.json"}
    seen: dict[tuple[str, int], set[str]] = {}
    prefix = f"{project_dir}/results/"
    for blob in client.list_blobs(bucket_name, prefix=prefix):
        parts = blob.name.split("/")
        if len(parts) < 5:
            continue
        try:
            result_idx = parts.index("results")
        except ValueError:
            continue
        if result_idx + 3 >= len(parts):
            continue
        task_id = parts[result_idx + 1]
        attempt_part = parts[result_idx + 2]
        filename = parts[result_idx + 3]
        if not task_id.startswith("task_") or not attempt_part.startswith("attempt"):
            continue
        try:
            attempt = int(attempt_part.removeprefix("attempt"))
        except ValueError:
            continue
        if attempt_start <= attempt <= attempt_end and filename in required:
            seen.setdefault((task_id, attempt), set()).add(filename)
    complete = sum(1 for files in seen.values() if required <= files)
    target = 160 * (attempt_end - attempt_start + 1)
    return complete, target


def main() -> int:
    parser = argparse.ArgumentParser(description="Start pass8 tail queue after pass5 completes")
    parser.add_argument("--jobs-file", required=True)
    parser.add_argument("--project-dir", required=True)
    parser.add_argument("--bucket", default="sotalab-archipelago-eval")
    parser.add_argument("--wait-attempt-start", type=int, default=1)
    parser.add_argument("--wait-attempt-end", type=int, default=5)
    parser.add_argument("--queue-name", default="pass8")
    parser.add_argument("--attempt-start", type=int, default=6)
    parser.add_argument("--attempt-end", type=int, default=8)
    parser.add_argument("--model", default="doubao-seed-2-0-pro-260215")
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--max-steps", type=int, default=200)
    parser.add_argument("--max-consecutive-failures", type=int, default=2)
    parser.add_argument("--sleep-seconds", type=int, default=300)
    parser.add_argument("--archipelago-dir", default="/opt/archipelago")
    args = parser.parse_args()

    client = storage.Client()
    while True:
        complete, target = count_complete_attempts(
            client,
            args.bucket,
            args.project_dir,
            args.wait_attempt_start,
            args.wait_attempt_end,
        )
        log.info(
            "pass5 gate: complete=%d target=%d missing=%d",
            complete,
            target,
            target - complete,
        )
        if complete >= target:
            break
        time.sleep(args.sleep_seconds)

    archipelago_dir = Path(args.archipelago_dir)
    cmd = [
        sys.executable,
        str(archipelago_dir / "scripts" / "worker_queue.py"),
        "--jobs-file",
        args.jobs_file,
        "--project-dir",
        args.project_dir,
        "--bucket",
        args.bucket,
        "--queue-name",
        args.queue_name,
        "--attempt-start",
        str(args.attempt_start),
        "--attempt-end",
        str(args.attempt_end),
        "--model",
        args.model,
        "--temperature",
        str(args.temperature),
        "--max-steps",
        str(args.max_steps),
        "--shuffle",
        "--max-consecutive-failures",
        str(args.max_consecutive_failures),
    ]
    log.info("starting pass8 tail queue: %s", " ".join(cmd))
    return subprocess.run(cmd, cwd=archipelago_dir).returncode


if __name__ == "__main__":
    sys.exit(main())

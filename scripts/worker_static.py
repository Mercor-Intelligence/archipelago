#!/usr/bin/env python3
"""
Static-assignment eval worker — alternative to Pub/Sub.

Each VM runs this script with --task-id T --k K. It:
  1. Reads /etc/archipelago-eval.env for config
  2. For each attempt 1..K, runs scripts/run_single_task.py
  3. Uploads results to GCS (handled by run_single_task.py)

No Pub/Sub, no flow control — each worker is assigned exactly one task.

Usage:
  python3 scripts/worker_static.py --task-id <task_id> --k 3 [--model doubao-seed-2-0-pro-260215]

This is meant to run via nohup inside each VM, after the VM's setup_worker.sh
has provisioned the environment image + tokens.
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
log = logging.getLogger(f"worker_static.{socket.gethostname()}")

ARCHIPELAGO_DIR = Path(os.environ.get("ARCHIPELAGO_DIR", "/opt/archipelago"))
EVAL_PROJECT_DIR = os.environ.get("EVAL_PROJECT_DIR", "eval-projects/seed-pro-pass3-20260616")
EVAL_BUCKET = os.environ.get("EVAL_BUCKET", "sotalab-archipelago-eval")

WORKER_ID = socket.gethostname()


def run_one_attempt(task_id: str, model: str, attempt: int, force: bool = False) -> int:
    cmd = [
        "python3",
        str(ARCHIPELAGO_DIR / "scripts" / "run_single_task.py"),
        "--task-id", task_id,
        "--model", model,
        "--attempt", str(attempt),
        "--worker-id", WORKER_ID,
        "--eval-project", EVAL_PROJECT_DIR,
        "--bucket", EVAL_BUCKET,
        "--project-dir", EVAL_PROJECT_DIR,
    ]
    if force:
        cmd.append("--rerun")
    log.info("running: %s", " ".join(cmd))
    return subprocess.run(cmd, cwd=ARCHIPELAGO_DIR).returncode


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task-id", required=True)
    ap.add_argument("--k", type=int, default=3)
    ap.add_argument("--model", default="doubao-seed-2-0-pro-260215")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    log.info(
        "static worker starting: task_id=%s k=%d model=%s force=%s worker_id=%s",
        args.task_id, args.k, args.model, args.force, WORKER_ID,
    )

    for attempt in range(1, args.k + 1):
        log.info("attempt %d / %d", attempt, args.k)
        rc = run_one_attempt(args.task_id, args.model, attempt, force=args.force)
        log.info("attempt %d done rc=%d", attempt, rc)
        if rc != 0:
            log.warning("attempt %d failed rc=%d, continuing", attempt, rc)

    log.info("static worker done: task_id=%s", args.task_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
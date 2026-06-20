#!/usr/bin/env python3
"""
Archipelago eval worker — runs on each GCE VM.

Subscribes to PUBSUB_SUBSCRIPTION, processes one job at a time:
  1. Idempotency check: skip if results/{task}/attempt{N}/grades.json exists
  2. Force rerun: if force_rerun=true, rm -r the result dir before running
  3. Run scripts/run_single_task.py
  4. Upload results to GCS (handled by run_single_task.py)
  5. Ack the Pub/Sub message

Required env vars (set by setup_worker.sh via /etc/archipelago-eval.env):
  ARCHIPELAGO_DIR, EVAL_PROJECT_DIR, EVAL_BUCKET, PUBSUB_SUBSCRIPTION,
  PUBSUB_PROJECT
"""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stdout,
)
log = logging.getLogger(f"eval_worker.{socket.gethostname()}")

ARCHIPELAGO_DIR = Path(os.environ.get("ARCHIPELAGO_DIR", "/opt/archipelago"))
EVAL_PROJECT_DIR = os.environ.get("EVAL_PROJECT_DIR", "eval-projects/seed-pro-pass3-20260616")
EVAL_BUCKET = os.environ.get("EVAL_BUCKET", "sotalab-archipelago-eval")
PUBSUB_SUBSCRIPTION = os.environ.get("PUBSUB_SUBSCRIPTION", "archipelago-eval-workers")
PUBSUB_PROJECT = os.environ.get("PUBSUB_PROJECT", "sotalab-staging")

WORKER_ID = socket.gethostname()
SUB_NAME = (
    f"projects/{PUBSUB_PROJECT}/subscriptions/{PUBSUB_SUBSCRIPTION}"
)

# In-memory ack deadline extender state
ack_state: dict[str, dict[str, Any]] = {}


def gcs_results_path(task_id: str, attempt: int) -> str:
    return (
        f"gs://{EVAL_BUCKET}/{EVAL_PROJECT_DIR}/results/{task_id}/attempt{attempt}"
    )


def gcs_results_url(task_id: str, attempt: int) -> str:
    return gcs_results_path(task_id, attempt) + "/"


def gcs_exists(gs_url: str) -> bool:
    res = subprocess.run(
        ["gsutil", "-q", "stat", gs_url],
        capture_output=True,
    )
    return res.returncode == 0


def gcs_gsutil(*args: str) -> int:
    return subprocess.run(["gsutil", *args]).returncode


def is_already_done(task_id: str, attempt: int) -> bool:
    """Skip job if grades.json + status.tsv already exist in GCS."""
    grades = gcs_results_path(task_id, attempt) + "/grades.json"
    status = gcs_results_path(task_id, attempt) + "/status.tsv"
    if not (gcs_exists(grades) and gcs_exists(status)):
        return False
    # Quick check: status.tsv should report run_status=completed
    try:
        res = subprocess.run(
            ["gsutil", "cat", status], capture_output=True, text=True
        )
        for line in res.stdout.splitlines():
            if line.startswith("run_status\t"):
                return line.split("\t", 1)[1].strip() == "completed"
    except Exception as e:
        log.warning("status.tsv read failed: %s", e)
    return False


def clear_history(task_id: str, attempt: int) -> None:
    """gsutil rm -r the result dir, also wipe local output."""
    log.info("force_rerun: clearing %s", gcs_results_url(task_id, attempt))
    subprocess.run(
        ["gsutil", "-m", "-q", "rm", "-r", gcs_results_url(task_id, attempt)],
        capture_output=True,
    )
    local = (
        ARCHIPELAGO_DIR
        / "examples"
        / "hugging_face_task"
        / "output"
        / task_id
        / f"attempt{attempt}"
    )
    if local.exists():
        subprocess.run(["rm", "-rf", str(local)], check=False)


def run_single_task(job: dict[str, Any]) -> int:
    cmd = [
        "python3",
        str(ARCHIPELAGO_DIR / "scripts" / "run_single_task.py"),
        "--task-id", job["task_id"],
        "--model", job["model"],
        "--attempt", str(job["attempt"]),
        "--worker-id", WORKER_ID,
        "--eval-project", EVAL_PROJECT_DIR,
        "--bucket", EVAL_BUCKET,
        "--project-dir", EVAL_PROJECT_DIR,
    ]
    if job.get("force_rerun") or job.get("rerun"):
        cmd.append("--rerun")
    log.info("running: %s", " ".join(cmd))
    return subprocess.run(cmd, cwd=ARCHIPELAGO_DIR).returncode


def start_ack_extender(subscriber, subscription_path, ack_id, deadline_sec=300):
    """Periodically extend ack deadline while a job is running."""
    stop = threading.Event()

    def loop():
        while not stop.wait(60):
            try:
                subscriber.modify_ack_deadline(
                    subscription=subscription_path,
                    ack_ids=[ack_id],
                    ack_deadline_seconds=deadline_sec,
                )
            except Exception as e:
                log.warning("extend ack failed: %s", e)

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return stop


def vm_is_busy() -> bool:
    """Return True if a main.py agent run is already in progress on this VM.

    Used to avoid clobbering an in-flight SSH-dispatched agent run when a
    Pub/Sub job arrives simultaneously. We check by looking for an active
    main.py process under the examples/hugging_face_task directory.
    """
    try:
        res = subprocess.run(
            ["pgrep", "-f", "examples/hugging_face_task/main.py"],
            capture_output=True, text=True,
        )
        return bool(res.stdout.strip())
    except Exception:
        return False


def process_message(subscriber, message) -> None:
    payload = message.data.decode("utf-8")
    try:
        job = json.loads(payload)
    except json.JSONDecodeError:
        log.error("invalid JSON, dropping: %s", payload[:200])
        message.ack()
        return

    task_id = job["task_id"]
    attempt = int(job["attempt"])
    model = job.get("model", "?")
    force = bool(job.get("force_rerun"))

    log.info(
        "JOB received: task=%s model=%s attempt=%d force_rerun=%s",
        task_id, model, attempt, force,
    )

    # If another agent is already running on this VM (e.g. SSH-dispatched),
    # nack with a small delay so another worker can pick it up.
    if vm_is_busy() and not force:
        log.warning(
            "VM busy with another agent run; nacking %s attempt %d for re-delivery",
            task_id, attempt,
        )
        message.nack()
        return

    if force:
        clear_history(task_id, attempt)
    elif is_already_done(task_id, attempt):
        log.info(
            "SKIP: results exist for %s attempt %d (idempotent)",
            task_id, attempt,
        )
        message.ack()
        return

    extender = start_ack_extender(
        subscriber, SUB_NAME, message.ack_id, deadline_sec=600
    )
    try:
        rc = run_single_task(job)
        log.info("JOB finished: task=%s attempt=%d rc=%d", task_id, attempt, rc)
    except Exception as e:
        log.exception("JOB crashed: %s", e)
    finally:
        extender.set()

    message.ack()


def main() -> int:
    try:
        from google.cloud import pubsub_v1
    except ImportError:
        log.error("google-cloud-pubsub not installed; aborting")
        return 1

    log.info("worker starting: worker_id=%s subscription=%s", WORKER_ID, SUB_NAME)

    # Periodically tail GCS log file as well
    client = pubsub_v1.SubscriberClient()
    flow_control = pubsub_v1.types.FlowControl(max_messages=1)

    while True:
        try:
            future = client.subscribe(
                SUB_NAME,
                callback=lambda msg: process_message(client, msg),
                flow_control=flow_control,
            )
            log.info("subscribed; waiting for messages")
            future.result()  # blocks; raises on error
        except KeyboardInterrupt:
            log.info("interrupt received, shutting down")
            future.cancel()
            return 0
        except Exception as e:
            log.exception("subscribe loop error: %s; retrying in 10s", e)
            time.sleep(10)


if __name__ == "__main__":
    sys.exit(main())
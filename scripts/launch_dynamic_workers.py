#!/usr/bin/env python3
"""
Scale dynamic GCS-queue workers on GCE.

This script packages the current archipelago checkout, uploads it to GCS, then
creates enough worker VMs to reach --target-running. It is intentionally
idempotent for expansion: existing active VMs with the same prefix are counted
first, and --max-running is enforced before any VM is created.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


ACTIVE_STATUSES = {"PROVISIONING", "STAGING", "RUNNING", "REPAIRING"}
DEFAULT_ENV_IMAGE = (
    "asia-east1-docker.pkg.dev/sotalab-prod/docker-repo/"
    "sotalab-apex-archipelago-environment-prod"
)


def run(cmd: list[str], *, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess[str]:
    print("+ " + " ".join(cmd), flush=True)
    return subprocess.run(
        cmd,
        check=check,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def default_project_dir(eval_project: str | None, project_dir: str | None) -> str:
    if project_dir:
        return project_dir
    if not eval_project:
        raise SystemExit("Either --project-dir or --eval-project is required")
    return f"eval-projects/{eval_project}"


def list_instances(project: str, prefix: str) -> list[dict[str, Any]]:
    proc = run(
        [
            "gcloud",
            "compute",
            "instances",
            "list",
            f"--project={project}",
            f"--filter=name~^{re.escape(prefix)}-",
            "--format=json(name,zone,status)",
        ],
        capture=True,
    )
    return json.loads(proc.stdout or "[]")


def zone_name(zone_url: str) -> str:
    return zone_url.rsplit("/", 1)[-1]


def active_instances(instances: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [item for item in instances if item.get("status") in ACTIVE_STATUSES]


def next_worker_names(prefix: str, existing_names: set[str], count: int) -> list[str]:
    names: list[str] = []
    index = 1
    while len(names) < count:
        name = f"{prefix}-{index:03d}"
        if name not in existing_names:
            names.append(name)
        index += 1
    return names


def upload_bundle(bucket: str, project_dir: str, bundle_uri: str | None) -> str:
    if bundle_uri:
        return bundle_uri

    root = repo_root()
    parent = root.parent
    target_uri = f"gs://{bucket}/{project_dir}/setup/archipelago.tgz"
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "archipelago.tgz"
        excludes = [
            "--exclude=archipelago/.git",
            "--exclude=archipelago/.venv",
            "--exclude=archipelago/agents/.venv",
            "--exclude=archipelago/grading/.venv",
            "--exclude=archipelago/environment/.venv",
            "--exclude=archipelago/.apex_local",
            "--exclude=archipelago/apex-samples.zip",
            "--exclude=archipelago/examples/hugging_face_task/output",
            "--exclude=archipelago/agents/output",
            "--exclude=archipelago/grading/output",
            "--exclude=archipelago/environment/output",
            "--exclude=__pycache__",
            "--exclude=.pytest_cache",
            "--exclude=node_modules",
        ]
        run(["env", "COPYFILE_DISABLE=1", "tar", *excludes, "-czf", str(archive), "-C", str(parent), root.name])
        run(["gsutil", "cp", str(archive), target_uri])
    return target_uri


def upload_task_ids(bucket: str, project_dir: str, task_ids_file: str | None, task_ids_uri: str | None) -> str:
    if task_ids_file:
        target_uri = task_ids_uri or f"gs://{bucket}/{project_dir}/state/task_ids.json"
        run(["gsutil", "cp", task_ids_file, target_uri])
        return target_uri
    if task_ids_uri:
        return task_ids_uri
    return f"gs://{bucket}/{project_dir}/state/task_ids.json"


def create_instance(
    *,
    project: str,
    zone: str,
    name: str,
    machine_type: str,
    boot_disk_size: str,
    image_family: str,
    image_project: str,
    service_account: str | None,
    metadata: dict[str, str],
) -> None:
    metadata_arg = ",".join(f"{key}={value}" for key, value in metadata.items())
    cmd = [
        "gcloud",
        "compute",
        "instances",
        "create",
        name,
        f"--project={project}",
        f"--zone={zone}",
        f"--machine-type={machine_type}",
        f"--boot-disk-size={boot_disk_size}",
        f"--image-family={image_family}",
        f"--image-project={image_project}",
        "--scopes=https://www.googleapis.com/auth/cloud-platform",
        f"--metadata-from-file=startup-script={repo_root() / 'scripts' / 'setup_worker.sh'}",
        f"--metadata={metadata_arg}",
        "--quiet",
    ]
    if service_account:
        cmd.append(f"--service-account={service_account}")
    run(cmd)


def main() -> int:
    parser = argparse.ArgumentParser(description="Scale dynamic archipelago GCE workers")
    parser.add_argument("--project", default="sotalab-prod")
    parser.add_argument("--eval-project", default=None)
    parser.add_argument("--project-dir", default=None)
    parser.add_argument("--bucket", default="sotalab-archipelago-eval")
    parser.add_argument("--worker-prefix", default="archipelago-eval-auto")
    parser.add_argument("--target-running", type=int, required=True)
    parser.add_argument("--max-running", type=int, default=None)
    parser.add_argument("--zones", default="us-central1-a,us-west1-a,asia-east1-b")
    parser.add_argument("--machine-type", default="e2-standard-4")
    parser.add_argument("--boot-disk-size", default="150GB")
    parser.add_argument("--image-family", default="debian-12")
    parser.add_argument("--image-project", default="debian-cloud")
    parser.add_argument("--service-account", default=None)
    parser.add_argument("--queue-name", default="pass5")
    parser.add_argument("--attempt-start", type=int, default=1)
    parser.add_argument("--attempt-end", type=int, default=5)
    parser.add_argument("--model", default="doubao-seed-2-0-pro-260215")
    parser.add_argument("--temperature", type=float, default=0.7)
    parser.add_argument("--max-steps", type=int, default=200)
    parser.add_argument("--max-consecutive-failures", type=int, default=2)
    parser.add_argument("--task-ids-file", default=None)
    parser.add_argument("--task-ids-gcs-uri", default=None)
    parser.add_argument("--bundle-gcs-uri", default=None)
    parser.add_argument("--env-image", default=DEFAULT_ENV_IMAGE)
    parser.add_argument("--env-image-tag", default="latest")
    parser.add_argument("--new-api-secret-name", default="NEW_API_TOKEN-staging")
    parser.add_argument("--hf-token-secret-name", default="HF_TOKEN")
    parser.add_argument("--new-api-base", default="https://new-api-staging.sotalab.ai/v1")
    args = parser.parse_args()

    if args.target_running < 0:
        raise SystemExit("--target-running must be >= 0")
    max_running = args.max_running if args.max_running is not None else args.target_running
    if args.target_running > max_running:
        raise SystemExit("--target-running cannot exceed --max-running")

    project_dir = default_project_dir(args.eval_project, args.project_dir)
    zones = [zone.strip() for zone in args.zones.split(",") if zone.strip()]
    if not zones:
        raise SystemExit("--zones must contain at least one zone")

    instances = list_instances(args.project, args.worker_prefix)
    active = active_instances(instances)
    print(f"[scale] active={len(active)} target={args.target_running} max={max_running}")
    if len(active) >= args.target_running:
        print("[scale] nothing to create")
        return 0
    to_create = args.target_running - len(active)
    if len(active) + to_create > max_running:
        raise SystemExit("refusing to exceed --max-running")

    bundle_uri = upload_bundle(args.bucket, project_dir, args.bundle_gcs_uri)
    task_ids_uri = upload_task_ids(
        args.bucket,
        project_dir,
        args.task_ids_file,
        args.task_ids_gcs_uri,
    )
    run(["gsutil", "cp", "/dev/null", f"gs://{args.bucket}/{project_dir}/state/.keep"], check=False)
    run(["gsutil", "cp", "/dev/null", f"gs://{args.bucket}/{project_dir}/results/.keep"], check=False)

    existing_names = {item["name"] for item in instances}
    names = next_worker_names(args.worker_prefix, existing_names, to_create)
    metadata = {
        "ARCHIPELAGO_TGZ_GCS_URI": bundle_uri,
        "EVAL_PROJECT_DIR": project_dir,
        "EVAL_BUCKET": args.bucket,
        "RUN_DYNAMIC_QUEUE": "1",
        "QUEUE_NAME": args.queue_name,
        "ATTEMPT_START": str(args.attempt_start),
        "ATTEMPT_END": str(args.attempt_end),
        "EVAL_MODEL": args.model,
        "TEMPERATURE": str(args.temperature),
        "MAX_STEPS": str(args.max_steps),
        "MAX_CONSECUTIVE_FAILURES": str(args.max_consecutive_failures),
        "TASK_IDS_GCS_URI": task_ids_uri,
        "JOBS_FILE": "/opt/archipelago/state/task_ids.json",
        "ENV_IMAGE": args.env_image,
        "ENV_IMAGE_TAG": args.env_image_tag,
        "NEW_API_SECRET_NAME": args.new_api_secret_name,
        "HF_TOKEN_SECRET_NAME": args.hf_token_secret_name,
        "NEW_API_BASE": args.new_api_base,
    }

    created: list[tuple[str, str]] = []
    for offset, name in enumerate(names):
        zone = zones[(len(active) + offset) % len(zones)]
        print(f"[scale] creating {name} in {zone}")
        create_instance(
            project=args.project,
            zone=zone,
            name=name,
            machine_type=args.machine_type,
            boot_disk_size=args.boot_disk_size,
            image_family=args.image_family,
            image_project=args.image_project,
            service_account=args.service_account,
            metadata=metadata,
        )
        created.append((name, zone))
        time.sleep(2)

    print("[scale] created:")
    for name, zone in created:
        print(f"  {name}\t{zone}")
    print("[scale] check health with:")
    print(
        "  python scripts/worker_healthcheck.py "
        f"--project {args.project} --worker-prefix {args.worker_prefix}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

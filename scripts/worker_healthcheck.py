#!/usr/bin/env python3
"""
Batch healthcheck for dynamic archipelago workers.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import subprocess
import sys
from typing import Any


def run(cmd: list[str], *, timeout: int | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)


def list_instances(project: str, prefix: str) -> list[dict[str, Any]]:
    proc = run(
        [
            "gcloud",
            "compute",
            "instances",
            "list",
            f"--project={project}",
            f"--filter=name~^{prefix}-",
            "--format=json(name,zone,status)",
        ]
    )
    if proc.returncode != 0:
        raise SystemExit(proc.stderr.strip())
    return json.loads(proc.stdout or "[]")


def zone_name(zone_url: str) -> str:
    return zone_url.rsplit("/", 1)[-1]


def check_one(project: str, instance: dict[str, Any], timeout: int) -> dict[str, str]:
    name = instance["name"]
    zone = zone_name(instance["zone"])
    status = instance.get("status", "")
    if status != "RUNNING":
        return {
            "name": name,
            "zone": zone,
            "vm": status,
            "service": "-",
            "pid": "-",
            "last": "-",
        }

    remote = (
        "svc=$(systemctl is-active archipelago-queue-worker.service 2>/dev/null || true); "
        "pid=$(pgrep -af 'worker_queue.py' | head -1 | awk '{print $1}' || true); "
        "last=$(tail -n 1 /var/log/archipelago-queue-worker.log 2>/dev/null | tr '\\t' ' ' || true); "
        "printf 'service=%s\\npid=%s\\nlast=%s\\n' \"$svc\" \"$pid\" \"$last\""
    )
    proc = run(
        [
            "gcloud",
            "compute",
            "ssh",
            name,
            f"--project={project}",
            f"--zone={zone}",
            "--command",
            remote,
            "--quiet",
        ],
        timeout=timeout,
    )
    if proc.returncode != 0:
        return {
            "name": name,
            "zone": zone,
            "vm": status,
            "service": "ssh-failed",
            "pid": "-",
            "last": proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "-",
        }

    fields = {}
    for line in proc.stdout.splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            fields[key] = value
    return {
        "name": name,
        "zone": zone,
        "vm": status,
        "service": fields.get("service", "-") or "-",
        "pid": fields.get("pid", "-") or "-",
        "last": fields.get("last", "-") or "-",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Check dynamic worker health")
    parser.add_argument("--project", default="sotalab-prod")
    parser.add_argument("--worker-prefix", default="archipelago-eval-auto")
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--jobs", type=int, default=8)
    args = parser.parse_args()

    instances = list_instances(args.project, args.worker_prefix)
    instances.sort(key=lambda item: item["name"])
    if not instances:
        print("no instances found")
        return 1

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as executor:
        rows = list(
            executor.map(
                lambda item: check_one(args.project, item, args.timeout),
                instances,
            )
        )

    print(f"{'name':32} {'zone':15} {'vm':12} {'service':12} {'pid':8} last")
    bad = 0
    for row in rows:
        if row["vm"] != "RUNNING" or row["service"] != "active" or row["pid"] == "-":
            bad += 1
        last = row["last"]
        if len(last) > 100:
            last = last[-100:]
        print(
            f"{row['name'][:32]:32} {row['zone'][:15]:15} {row['vm'][:12]:12} "
            f"{row['service'][:12]:12} {row['pid'][:8]:8} {last}"
        )
    return 0 if bad == 0 else 2


if __name__ == "__main__":
    sys.exit(main())

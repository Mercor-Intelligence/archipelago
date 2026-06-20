#!/usr/bin/env python3
"""
Run a single (task, model, attempt) Archipelago evaluation.

Wraps examples/hugging_face_task/main.py with:
  - per-worker compose project name isolation
  - per-attempt output directory
  - GCS upload of trajectory/snapshot/grades to eval-projects/<project_dir>/results/<task_id>/attempt<N>/
  - GCS upload of worker's run.log

Usage:
    python scripts/run_single_task.py \
        --task-id task_abc123 \
        --model doubao-seed-2-0-pro-260215 \
        --attempt 1 \
        --worker-id worker-1 \
        --eval-project seed-pro-pass3-20260616 \
        --bucket sotalab-archipelago-eval \
        --project-dir eval-projects/seed-pro-pass3-20260616

Environment overrides recognized from main.py (forwarded via env):
  ENV_URL=http://localhost:8080  (default)
  COMPOSE_PROJECT_NAME=archipelago-w<worker_id>
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EXAMPLE_DIR = REPO_ROOT / "examples" / "hugging_face_task"
ENVIRONMENT_DIR = REPO_ROOT / "environment"
AGENTS_DIR = REPO_ROOT / "agents"
GRADING_DIR = REPO_ROOT / "grading"


def run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> int:
    """Run a shell command, return returncode. Stream output if not captured."""
    print(f"  $ {' '.join(cmd)}", flush=True)
    res = subprocess.run(cmd, cwd=cwd)
    return res.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a single Archipelago task")
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--attempt", required=True, type=int)
    parser.add_argument("--worker-id", required=True)
    parser.add_argument("--eval-project", required=True,
                        help="Eval project name (subdir under eval-projects/)")
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--project-dir", required=True,
                        help="Full GCS prefix, e.g. eval-projects/seed-pro-pass3-20260616")
    parser.add_argument("--max-steps", type=int, default=200,
                        help="Agent max_steps (default 200)")
    parser.add_argument("--temperature", type=float, default=None,
                        help="Optional orchestrator temperature; defaults to ORCHESTRATOR_TEMPERATURE if set")
    parser.add_argument("--rerun", action="store_true",
                        help="Wipe local output + GCS result dir before running")
    args = parser.parse_args()

    compose_project = f"archipelago-w{args.worker_id}"
    gcs_results = (
        f"gs://{args.bucket}/{args.project_dir}/results/{args.task_id}/attempt{args.attempt}"
    )

    print("=" * 60, flush=True)
    print(f"Worker: {args.worker_id}  Compose: {compose_project}", flush=True)
    print(f"Task: {args.task_id}  Model: {args.model}  Attempt: {args.attempt}", flush=True)
    print(f"GCS results: {gcs_results}", flush=True)
    print("=" * 60, flush=True)

    # Per-attempt output dir under example dir
    output_dir = EXAMPLE_DIR / "output" / args.task_id / f"attempt{args.attempt}"
    if args.rerun and output_dir.exists():
        print(f"  --rerun: wiping {output_dir}", flush=True)
        subprocess.run(["rm", "-rf", str(output_dir)], check=False)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Build per-run orchestrator config (model string for LiteLLM proxy)
    orchestrator_config_path = output_dir / "orchestrator_config.json"
    orchestrator_config = {"model": args.model}
    temperature = args.temperature
    if temperature is None and os.environ.get("ORCHESTRATOR_TEMPERATURE"):
        temperature = float(os.environ["ORCHESTRATOR_TEMPERATURE"])
    if temperature is not None:
        orchestrator_config["extra_args"] = {"temperature": temperature}
    orchestrator_config_path.write_text(json.dumps(orchestrator_config, indent=2) + "\n")

    # Build a worker_id-tagged agent_config.json so the trajectory logs the worker
    agent_config_path = output_dir / "agent_config.json"
    agent_config_path.write_text(
        '{\n'
        '  "agent_config_id": "react_toolbelt_agent",\n'
        '  "agent_name": "React Toolbelt Agent",\n'
        f'  "agent_config_values": {{"timeout": 3600, "max_steps": {args.max_steps}, "worker_id": "{args.worker_id}"}}\n'
        '}\n'
    )

    # Environment for the main.py subprocess: compose project + agent config
    env = os.environ.copy()
    env["COMPOSE_PROJECT_NAME"] = compose_project
    env["AGENT_CONFIG"] = str(agent_config_path)
    env["ORCHESTRATOR_CONFIG"] = str(orchestrator_config_path)
    env["ARCHIPELAGO_OUTPUT_DIR"] = str(output_dir)
    # HuggingFace token: if file exists at $HOME/.cache/huggingface/token,
    # read it into env so uv-run subprocess inherits it.
    hf_token_path = Path.home() / ".cache" / "huggingface" / "token"
    if hf_token_path.exists():
        token_val = hf_token_path.read_text().strip()
        env["HF_TOKEN"] = token_val
        print(f"  HF_TOKEN read from {hf_token_path} (len={len(token_val)})", flush=True)

    # Forward LITELLM_PROXY_API_BASE / KEY from current env so the agent
    # runner (which reads agents/.env via pydantic_settings) actually sees
    # the New API proxy instead of falling back to default Vertex AI.
    for k in ("LITELLM_PROXY_API_BASE", "LITELLM_PROXY_API_KEY", "AGENT_TIMEOUT_SECONDS"):
        if k in os.environ:
            env[k] = os.environ[k]

    # The main.py script writes to a hardcoded output/<task_id>/ — we work
    # around by passing the task_id, then rsyncing output/<task_id>/<attempt_dir>
    # afterwards. Easiest: just run main.py and move the per-task output into our
    # attempt dir after.
    log_path = output_dir / "run.log"
    with open(log_path, "w") as logf:
        cmd = [
            "uv", "run", "python", str(EXAMPLE_DIR / "main.py"),
            args.task_id,
        ]
        print(f"  $ {' '.join(cmd)}  (log -> {log_path})", flush=True)
        res = subprocess.run(
            cmd,
            cwd=AGENTS_DIR,
            env=env,
            stdout=logf,
            stderr=subprocess.STDOUT,
        )

    run_status = "error"
    if res.returncode == 0:
        run_status = "completed"
    print(f"  agent exit={res.returncode} status={run_status}", flush=True)

    # Move everything main.py wrote under output/<task_id>/ into our attempt dir.
    main_output = EXAMPLE_DIR / "output" / args.task_id
    if main_output.exists():
        for f in main_output.iterdir():
            if f.name == f"attempt{args.attempt}":
                continue
            target = output_dir / f.name
            if target.exists():
                continue
            shutil.move(str(f), str(target))
        try:
            main_output.rmdir()
        except OSError:
            pass

    # Upload to GCS
    upload_prefix = gcs_results
    print(f"  uploading {output_dir} -> {upload_prefix}/ ...", flush=True)
    rc = subprocess.run(
        ["gsutil", "-m", "cp", "-r", str(output_dir) + "/", upload_prefix + "/"],
    ).returncode
    if rc != 0:
        print(f"  WARNING: gsutil cp returned {rc}", flush=True)

    # Write status sentinel into GCS
    status_blob = (
        f"attempt\t{args.attempt}\n"
        f"task_id\t{args.task_id}\n"
        f"model\t{args.model}\n"
        f"worker_id\t{args.worker_id}\n"
        f"agent_exit\t{res.returncode}\n"
        f"run_status\t{run_status}\n"
    )
    status_path = output_dir / "status.tsv"
    status_path.write_text(status_blob)
    subprocess.run(
        ["gsutil", "cp", str(status_path), f"{upload_prefix}/status.tsv"],
    )

    # Keep key artifacts flat under attempt<N>/ as well as in the recursive
    # attempt<N>/attempt<N>/ copy. worker_shard.py uses these flat paths for
    # cheap GCS resume checks.
    for artifact in (
        "trajectory.json",
        "grades.json",
        "final_snapshot.zip",
        "verifiers.json",
        "run.log",
    ):
        artifact_path = output_dir / artifact
        if artifact_path.exists():
            subprocess.run(
                ["gsutil", "cp", str(artifact_path), f"{upload_prefix}/{artifact}"],
            )

    print("=" * 60, flush=True)
    print(f"DONE  status={run_status}  gcs={upload_prefix}/", flush=True)
    print("=" * 60, flush=True)

    return 0 if run_status == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())

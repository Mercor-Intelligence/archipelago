#!/usr/bin/env python3
"""
Run the simple_task example end-to-end.

Usage:
    cd archipelago/examples/simple_task
    ./run.sh

Prerequisites:
    - Docker running
    - ANTHROPIC_API_KEY or other LLM key set in agents/.env and grading/.env
"""

import io
import json
import os
import subprocess
import sys
import tarfile
import time
import uuid
import zipfile
from pathlib import Path

import requests
import shutil

# Paths - can be overridden via environment variables
EXAMPLE_DIR = Path(os.environ.get("EXAMPLE_DIR", Path(__file__).parent))
ARCHIPELAGO_DIR = Path(os.environ.get("ARCHIPELAGO_DIR", EXAMPLE_DIR.parent.parent))
ENVIRONMENT_DIR = Path(
    os.environ.get("ENVIRONMENT_DIR", ARCHIPELAGO_DIR / "environment")
)
AGENTS_DIR = Path(os.environ.get("AGENTS_DIR", ARCHIPELAGO_DIR / "agents"))
GRADING_DIR = Path(os.environ.get("GRADING_DIR", ARCHIPELAGO_DIR / "grading"))

ENV_URL = os.environ.get("ENV_URL", "http://localhost:8080")


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def wait_for_health(url: str, timeout: int = 120) -> bool:
    """Wait for environment to be healthy."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = requests.get(f"{url}/health", timeout=5)
            if resp.status_code == 200:
                return True
        except requests.RequestException:
            pass
        time.sleep(1)
    return False


def start_environment():
    """Start a fresh environment container (always restarts)."""
    # Ensure .env file exists (copy from .env.example if needed)
    env_file = ENVIRONMENT_DIR / ".env"
    env_example = ENVIRONMENT_DIR / ".env.example"
    if not env_file.exists() and env_example.exists():
        log("Creating .env from .env.example...")
        shutil.copy(env_example, env_file)
    elif not env_file.exists():
        log("Creating empty .env file...")
        env_file.touch()

    # Always stop and remove existing containers for a fresh start
    log("Stopping any existing environment containers...")
    subprocess.run(
        ["docker", "compose", "down", "-v"],
        cwd=ENVIRONMENT_DIR,
        capture_output=True,
    )

    log("Building and starting fresh environment container...")
    result = subprocess.run(
        ["docker", "compose", "up", "-d", "--build"],
        cwd=ENVIRONMENT_DIR,
    )
    if result.returncode != 0:
        log("ERROR: Failed to start environment")
        sys.exit(1)

    log("Waiting for environment to be healthy...")
    if not wait_for_health(ENV_URL):
        # Show logs on failure to help debug
        subprocess.run(["docker", "compose", "logs"], cwd=ENVIRONMENT_DIR)
        log("ERROR: Environment failed to start")
        sys.exit(1)

    log("Environment started")


def zip_to_tar_gz(zip_path: Path, strip_prefix: str = "filesystem/") -> Path:
    """Convert zip to tar.gz for environment population.

    Strips the prefix (e.g., 'filesystem/') since the subsystem parameter
    already specifies the target directory.
    """
    tar_gz_path = zip_path.with_suffix(".tar.gz")
    with zipfile.ZipFile(zip_path, "r") as zf:
        with tarfile.open(tar_gz_path, "w:gz") as tar:
            for name in zf.namelist():
                # Strip the prefix if present
                new_name = name
                if strip_prefix and name.startswith(strip_prefix):
                    new_name = name[len(strip_prefix) :]

                # Skip empty names (the prefix directory itself)
                if not new_name:
                    continue

                info = tarfile.TarInfo(name=new_name)

                # Check if it's a directory (ends with /)
                if name.endswith("/"):
                    info.type = tarfile.DIRTYPE
                    info.mode = 0o755
                    tar.addfile(info)
                else:
                    # It's a file
                    data = zf.read(name)
                    info.size = len(data)
                    info.mode = 0o644
                    tar.addfile(info, io.BytesIO(data))
    return tar_gz_path


def tar_gz_to_zip(tar_gz_path: Path) -> Path:
    """Convert tar.gz to zip for grading."""
    # Handle .tar.gz double suffix: strip both before adding .zip
    stem = tar_gz_path.stem  # "file.tar" from "file.tar.gz"
    if stem.endswith(".tar"):
        stem = stem[:-4]  # "file" from "file.tar"
    zip_path = tar_gz_path.parent / f"{stem}.zip"
    with tarfile.open(tar_gz_path, "r:gz") as tar:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for member in tar.getmembers():
                if member.isfile():
                    f = tar.extractfile(member)
                    if f is not None:
                        zf.writestr(member.name, f.read())
    return zip_path


def main():
    trajectory_id = f"example_{uuid.uuid4().hex[:8]}"
    grading_run_id = f"gr_{uuid.uuid4().hex[:8]}"

    log("=" * 60)
    log("SIMPLE TASK EXAMPLE")
    log("=" * 60)
    log(f"Trajectory ID: {trajectory_id}")

    # Start environment if needed
    start_environment()

    # Load and populate world snapshot
    log("Populating environment with world snapshot...")
    original_zip = EXAMPLE_DIR / "original_snapshot.zip"
    world_tar_gz = zip_to_tar_gz(original_zip)

    with open(world_tar_gz, "rb") as f:
        resp = requests.post(
            f"{ENV_URL}/data/populate",
            files={"archive": ("world.tar.gz", f, "application/gzip")},
            params={"subsystem": "filesystem"},
        )
        if resp.status_code != 200:
            log(f"ERROR: Failed to populate environment: {resp.status_code}")
            log(f"Response: {resp.text}")
            sys.exit(1)
        log(f"Populated: {resp.json()}")

    # Configure MCP servers
    log("Configuring MCP servers...")
    with open(EXAMPLE_DIR / "mcp_config.json") as f:
        mcp_config = json.load(f)

    resp = requests.post(f"{ENV_URL}/apps", json=mcp_config)
    resp.raise_for_status()
    log("MCP servers configured")

    # Run agent
    log("Running agent...")

    # Load orchestrator config
    with open(EXAMPLE_DIR / "orchestrator_config.json") as f:
        orchestrator_config = json.load(f)

    agent_cmd = [
        "uv",
        "run",
        "python",
        "-m",
        "runner.main",
        "--trajectory-id",
        trajectory_id,
        "--initial-messages",
        str(EXAMPLE_DIR / "initial_messages.json"),
        "--mcp-gateway-url",
        f"{ENV_URL}/mcp/",
        "--agent-config",
        str(EXAMPLE_DIR / "agent_config.json"),
        "--orchestrator-model",
        orchestrator_config["model"],
        "--output",
        str(EXAMPLE_DIR / "trajectory.json"),
    ]

    # Add extra args if present
    if orchestrator_config.get("extra_args"):
        extra_args_file = EXAMPLE_DIR / "orchestrator_extra_args.json"
        with open(extra_args_file, "w") as f:
            json.dump(orchestrator_config["extra_args"], f)
        agent_cmd.extend(["--orchestrator-extra-args", str(extra_args_file)])

    result = subprocess.run(agent_cmd, cwd=AGENTS_DIR)
    if result.returncode != 0:
        log(f"WARNING: Agent exited with code {result.returncode}")

    # Check agent status
    trajectory_file = EXAMPLE_DIR / "trajectory.json"
    agent_status = None
    if trajectory_file.exists():
        with open(trajectory_file) as f:
            trajectory = json.load(f)
            agent_status = trajectory.get("status")
            log(f"Agent status: {agent_status}")

    # Save final snapshot
    log("Saving final snapshot...")
    resp = requests.post(f"{ENV_URL}/data/snapshot", stream=True)
    resp.raise_for_status()

    final_tar_gz = EXAMPLE_DIR / "final_snapshot.tar.gz"
    with open(final_tar_gz, "wb") as f:
        for chunk in resp.iter_content(chunk_size=65536):
            f.write(chunk)

    final_zip = tar_gz_to_zip(final_tar_gz)
    log(f"Saved: {final_zip}")

    # Run grading if agent completed
    if agent_status != "completed":
        log(f"Skipping grading (agent status: {agent_status})")
    else:
        log("Running grading...")
        grading_cmd = [
            "uv",
            "run",
            "python",
            "-m",
            "runner.main",
            "--grading-run-id",
            grading_run_id,
            "--trajectory-id",
            trajectory_id,
            "--initial-snapshot",
            str(EXAMPLE_DIR / "original_snapshot.zip"),
            "--final-snapshot",
            str(final_zip),
            "--trajectory",
            str(trajectory_file),
            "--grading-settings",
            str(EXAMPLE_DIR / "grading_settings.json"),
            "--verifiers",
            str(EXAMPLE_DIR / "verifiers.json"),
            "--eval-configs",
            str(EXAMPLE_DIR / "eval_configs.json"),
            "--scoring-config",
            str(EXAMPLE_DIR / "scoring_config.json"),
            "--output",
            str(EXAMPLE_DIR / "grades.json"),
        ]

        result = subprocess.run(grading_cmd, cwd=GRADING_DIR)
        if result.returncode != 0:
            log(f"WARNING: Grading exited with code {result.returncode}")

        # Display results
        grades_file = EXAMPLE_DIR / "grades.json"
        if grades_file.exists():
            with open(grades_file) as f:
                grades = json.load(f)
            log("=" * 60)
            log("GRADING RESULTS")
            log("=" * 60)
            log(f"Status: {grades.get('grading_run_status')}")
            log(f"Final Score: {grades.get('scoring_results', {}).get('final_score')}")
            for vr in grades.get("verifier_results", []):
                log(f"  - {vr.get('verifier_id')}: {vr.get('score')}")

    log("=" * 60)
    log("DONE")
    log(f"Output: {EXAMPLE_DIR}")
    log("=" * 60)


if __name__ == "__main__":
    main()

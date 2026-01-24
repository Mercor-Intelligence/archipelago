#!/usr/bin/env python3
"""
Run a task from the mercor/apex-agents HuggingFace dataset.

Usage:
    ./run.sh              # Run task index 0
    ./run.sh 42           # Run task index 42
    ./run.sh task_abc123  # Run task by ID
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import uuid
import zipfile
from pathlib import Path

import httpx
from huggingface_hub import hf_hub_download

EXAMPLE_DIR = Path(os.environ.get("EXAMPLE_DIR", Path(__file__).parent))
ARCHIPELAGO_DIR = Path(os.environ.get("ARCHIPELAGO_DIR", EXAMPLE_DIR.parent.parent))
ENVIRONMENT_DIR = Path(os.environ.get("ENVIRONMENT_DIR", ARCHIPELAGO_DIR / "environment"))
AGENTS_DIR = Path(os.environ.get("AGENTS_DIR", ARCHIPELAGO_DIR / "agents"))
GRADING_DIR = Path(os.environ.get("GRADING_DIR", ARCHIPELAGO_DIR / "grading"))

ENV_URL = os.environ.get("ENV_URL", "http://localhost:8080")
HF_DATASET = "mercor/apex-agents"

# Default task: Investment Banking World 221 - BBDC/TVPG accretion/dilution sensitivity analysis
DEFAULT_TASK = "task_9ba58a6197114140877a1df1754d2993"


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def wait_for_health(url: str, timeout: int = 120) -> bool:
    """Wait for environment to be healthy."""
    start = time.time()
    while time.time() - start < timeout:
        try:
            resp = httpx.get(f"{url}/health", timeout=5)
            if resp.status_code == 200:
                return True
        except httpx.RequestError:
            pass
        time.sleep(1)
    return False


def start_environment():
    """Start a fresh environment container (always restarts)."""
    env_file = ENVIRONMENT_DIR / ".env"
    env_example = ENVIRONMENT_DIR / ".env.example"
    if not env_file.exists() and env_example.exists():
        log("Creating .env from .env.example...")
        shutil.copy(env_example, env_file)
    elif not env_file.exists():
        log("Creating empty .env file...")
        env_file.touch()

    log("Stopping any existing environment containers...")
    subprocess.run(["docker", "compose", "down", "-v"], cwd=ENVIRONMENT_DIR, capture_output=True)

    log("Building and starting environment container...")
    result = subprocess.run(["docker", "compose", "up", "-d", "--build"], cwd=ENVIRONMENT_DIR)
    if result.returncode != 0:
        log("ERROR: Failed to start environment")
        sys.exit(1)

    log("Waiting for environment to be healthy...")
    if not wait_for_health(ENV_URL):
        subprocess.run(["docker", "compose", "logs"], cwd=ENVIRONMENT_DIR)
        log("ERROR: Environment failed to start")
        sys.exit(1)

    log("Environment started")


def tar_gz_to_zip(tar_gz_path: Path) -> Path:
    """Convert tar.gz to zip for grading."""
    stem = tar_gz_path.stem
    if stem.endswith(".tar"):
        stem = stem[:-4]
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
    # Parse task selector from command line (index, task ID, or use default)
    task_selector = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_TASK

    # Load task and world data from HuggingFace
    log("Downloading task data from HuggingFace...")
    tasks_path = hf_hub_download(HF_DATASET, "tasks_and_rubrics.json", repo_type="dataset")
    worlds_path = hf_hub_download(HF_DATASET, "world_descriptions.json", repo_type="dataset")

    with open(tasks_path) as f:
        tasks = json.load(f)
    with open(worlds_path) as f:
        worlds = {w["world_id"]: w for w in json.load(f)}

    # Find the task
    if task_selector.isdigit():
        task_index = int(task_selector)
        if task_index < 0 or task_index >= len(tasks):
            log(f"ERROR: Task index out of range (0-{len(tasks) - 1})")
            sys.exit(1)
        task = tasks[task_index]
    else:
        task = next((t for t in tasks if t["task_id"] == task_selector), None)
        if not task:
            log(f"ERROR: Task not found: {task_selector}")
            sys.exit(1)

    world_id = task["world_id"]
    world = worlds.get(world_id)
    if not world:
        log(f"ERROR: World not found: {world_id}")
        sys.exit(1)

    trajectory_id = f"hf_{task['task_id']}_{uuid.uuid4().hex[:8]}"
    grading_run_id = f"gr_{uuid.uuid4().hex[:8]}"
    output_dir = EXAMPLE_DIR / "output" / task["task_id"]
    output_dir.mkdir(parents=True, exist_ok=True)

    log("=" * 60)
    log(f"Task: {task['task_name']}")
    log(f"Domain: {task['domain']}")
    log(f"World: {world['world_name']}")
    log(f"Prompt: {task['prompt'][:100]}...")
    log("=" * 60)

    start_environment()

    # Download and extract world snapshot
    log(f"Downloading world snapshot: {world_id}")
    zip_path = hf_hub_download(HF_DATASET, f"world_files_zipped/{world_id}.zip", repo_type="dataset")
    world_zip = output_dir / f"{world_id}.zip"
    shutil.copy(zip_path, world_zip)

    log("Populating environment with world snapshot...")
    with zipfile.ZipFile(world_zip, "r") as zf:
        names = zf.namelist()

        for subsystem in ["filesystem", ".apps_data"]:
            subsystem_files = [n for n in names if n.startswith(f"{subsystem}/")]
            if not subsystem_files:
                continue

            log(f"  Populating {subsystem} ({len(subsystem_files)} files)...")
            subsystem_tar = output_dir / f"{subsystem}.tar.gz"

            with tarfile.open(subsystem_tar, "w:gz") as tar:
                for name in subsystem_files:
                    new_name = name[len(f"{subsystem}/"):]
                    if not new_name:
                        continue
                    info = tarfile.TarInfo(name=new_name)
                    if name.endswith("/"):
                        info.type = tarfile.DIRTYPE
                        info.mode = 0o755
                        tar.addfile(info)
                    else:
                        data = zf.read(name)
                        info.size = len(data)
                        info.mode = 0o644
                        tar.addfile(info, io.BytesIO(data))

            with open(subsystem_tar, "rb") as f:
                resp = httpx.post(
                    f"{ENV_URL}/data/populate",
                    files={"archive": (f"{subsystem}.tar.gz", f.read(), "application/gzip")},
                    params={"subsystem": subsystem},
                    timeout=600.0,
                )
                if resp.status_code != 200:
                    log(f"ERROR: Failed to populate {subsystem}: {resp.text}")
                    sys.exit(1)
                log(f"  {subsystem}: {resp.json()}")

    # Configure MCP servers using the all-servers config
    log("Configuring MCP servers...")
    with open(EXAMPLE_DIR / "mcp_config_all_oss_servers.json") as f:
        mcp_config = json.load(f)
    log(f"  Servers: {list(mcp_config['mcpServers'].keys())}")

    resp = httpx.post(f"{ENV_URL}/apps", json=mcp_config, timeout=600.0)
    resp.raise_for_status()
    log("MCP servers configured")

    # Generate initial messages from HuggingFace task prompt
    # System prompt from agents/runner/agents/react_toolbelt_agent/README.md
    system_prompt = """You are an AI assistant that completes tasks by reasoning and using tools.

## Think Before Acting

Before making tool calls, briefly explain your reasoning in 1-3 sentences:
- What you learned from the previous step
- What you're doing next and why

Don't over-explain. Be concise but show your thinking.

## Tools

**Always Available (Meta-Tools):**
- `todo_write` - Task planning: create/update todos. Takes `todos` array [{id, content, status}] and `merge` boolean.
- `toolbelt_list_tools` / `toolbelt_inspect_tool` / `toolbelt_add_tool` / `toolbelt_remove_tool` - Tool management
- `final_answer` - Submit your answer (status: completed/blocked/failed)

**Domain Tools:** Use `toolbelt_list_tools` to discover, then `toolbelt_add_tool` to add them.

## Workflow

1. Plan: Use `todo_write` to create todos for complex tasks
2. Discover: Use `toolbelt_list_tools` to find relevant tools
3. Execute: Work through todos, use `todo_write` with `merge=true` to update status
4. Complete: Call `final_answer` (all todos must be completed/cancelled first)

## Rules

- Update todo status with `todo_write`: set `in_progress` when starting, `completed` when done
- Show your work for calculations
- `final_answer` is rejected if todos are incomplete
"""
    initial_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task["prompt"]},
    ]
    with open(output_dir / "initial_messages.json", "w") as f:
        json.dump(initial_messages, f, indent=2)

    # Load orchestrator config
    with open(EXAMPLE_DIR / "orchestrator_config.json") as f:
        orchestrator_config = json.load(f)

    trajectory_file = output_dir / "trajectory.json"

    # Run agent
    log("Running agent...")
    agent_cmd = [
        "uv", "run", "python", "-m", "runner.main",
        "--trajectory-id", trajectory_id,
        "--initial-messages", str(output_dir / "initial_messages.json"),
        "--mcp-gateway-url", f"{ENV_URL}/mcp/",
        "--agent-config", str(EXAMPLE_DIR / "agent_config.json"),
        "--orchestrator-model", orchestrator_config["model"],
        "--output", str(trajectory_file),
    ]

    # Add extra args if present
    if orchestrator_config.get("extra_args"):
        extra_args_file = output_dir / "orchestrator_extra_args.json"
        with open(extra_args_file, "w") as f:
            json.dump(orchestrator_config["extra_args"], f)
        agent_cmd.extend(["--orchestrator-extra-args", str(extra_args_file)])

    result = subprocess.run(agent_cmd, cwd=AGENTS_DIR)
    if result.returncode != 0:
        log(f"WARNING: Agent exited with code {result.returncode}")

    agent_status = None
    if trajectory_file.exists():
        with open(trajectory_file) as f:
            trajectory = json.load(f)
            agent_status = trajectory.get("status")
            log(f"Agent status: {agent_status}")

    # Save final snapshot
    log("Saving final snapshot...")
    with httpx.stream("POST", f"{ENV_URL}/data/snapshot") as resp:
        resp.raise_for_status()
        final_tar_gz = output_dir / "final_snapshot.tar.gz"
        with open(final_tar_gz, "wb") as f:
            for chunk in resp.iter_bytes(chunk_size=65536):
                f.write(chunk)

    final_zip = tar_gz_to_zip(final_tar_gz)
    log(f"Saved: {final_zip}")

    # Run grading if agent completed
    if agent_status != "completed":
        log(f"Skipping grading (agent status: {agent_status})")
    else:
        log("Running grading...")

        # Generate verifiers from HuggingFace rubric
        verifiers = [
            {
                "verifier_id": c["verifier_id"],
                "verifier_version": 1,
                "world_id": world_id,
                "task_id": task["task_id"],
                "eval_config_id": "ec_output_llm",
                "verifier_values": {"criteria": c["criteria"], "is_primary_objective": i == 0},
                "verifier_index": i,
                "verifier_dependencies": None,
            }
            for i, c in enumerate(task.get("rubric", []))
        ]
        with open(output_dir / "verifiers.json", "w") as f:
            json.dump(verifiers, f, indent=2)

        grades_file = output_dir / "grades.json"

        grading_cmd = [
            "uv", "run", "python", "-m", "runner.main",
            "--grading-run-id", grading_run_id,
            "--trajectory-id", trajectory_id,
            "--initial-snapshot", str(world_zip),
            "--final-snapshot", str(final_zip),
            "--trajectory", str(trajectory_file),
            "--grading-settings", str(EXAMPLE_DIR / "grading_settings.json"),
            "--verifiers", str(output_dir / "verifiers.json"),
            "--eval-configs", str(EXAMPLE_DIR / "eval_configs.json"),
            "--scoring-config", str(EXAMPLE_DIR / "scoring_config.json"),
            "--output", str(grades_file),
        ]

        result = subprocess.run(grading_cmd, cwd=GRADING_DIR)
        if result.returncode != 0:
            log(f"WARNING: Grading exited with code {result.returncode}")

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
    log(f"Output: {output_dir}")
    log("=" * 60)


if __name__ == "__main__":
    main()

# Simple Task Example

A minimal end-to-end example that demonstrates running an agent task and grading the result.

## Task

The agent is asked to explore a filesystem containing several subdirectories, each with an animal image file (with random names), and find the path to the gorilla image.

Directory structure:
```
animals/
  xk92m/qz7fw.png  (gorilla - the target)
  ab47z/rt3ky.png  (cat)
  mn83p/jw9vb.png  (elephant)
  jh21q/pl4nc.png  (penguin)
```

## Quick Start

```bash
cd archipelago/examples/simple_task
GOOGLE_API_KEY=your_key_here ./run.sh
```

The script will:
1. Start the environment container (if not already running)
2. Populate the environment with the world snapshot
3. Configure MCP servers
4. Run the agent
5. Save the final snapshot
6. Run grading and display results

## Files

| File | Description |
|------|-------------|
| `initial_messages.json` | Task prompt for the agent |
| `agent_config.json` | Agent configuration (loop_agent with defaults) |
| `orchestrator_config.json` | Model and extra args for the agent |
| `mcp_config.json` | MCP server configuration (filesystem only) |
| `original_snapshot.zip` | World snapshot with animal image directories |
| `verifiers.json` | Grading criteria |
| `eval_configs.json` | Eval definitions |
| `grading_settings.json` | LLM judge settings |
| `scoring_config.json` | Scoring method configuration |

---

## Step-by-Step Walkthrough

This walkthrough shows exactly how to run 1 task from start to finish, explaining each step.

### Overview

```
┌──────────────────┐    ┌──────────────────┐    ┌──────────────────┐
│   1. Environment │───▶│     2. Agent     │───▶│    3. Grading    │
│   (Docker)       │    │   (Python/uv)    │    │   (Python/uv)    │
└──────────────────┘    └──────────────────┘    └──────────────────┘
        │                       │                       │
   Load snapshot          Run LLM loop            Compare snapshots
   Configure MCP          Call tools              Evaluate criteria
   Expose /mcp/           Save trajectory         Calculate score
```

### Step 1: Prepare Your Task

Create a task file (`task.json`) with your task definition:

```json
{
  "trajectory_id": "task_001",
  "model": "openai/gpt-5",
  "initial_messages": [
    {
      "role": "user",
      "content": "List all files in the filesystem and create a summary.txt file."
    }
  ],
  "mcp_client_config": {
    "filesystem-server": {
      "transport": "stdio",
      "command": "python",
      "args": ["main.py"],
      "cwd": "./mcp_servers/filesystem_server",
      "env": {
        "APP_FS_ROOT": "/filesystem"
      }
    }
  }
}
```

**Key fields:**
- `trajectory_id`: Unique identifier for this run
- `model`: LLM to use (format: `provider/model-name`)
- `initial_messages`: The task prompt
- `mcp_client_config`: MCP server configuration
  - **Must include `"transport": "stdio"`**
  - `cwd` must be a valid path inside the container

### Step 2: Start the Environment

```bash
cd archipelago/environment

# Copy and configure .env
cp .env.example .env

# Start the container
docker-compose up --build
```

Wait for `Application startup complete` message.

### Step 3: Load World Snapshot

The environment expects `.tar.gz` archives. If you have a `.zip` file, convert it:

```python
import tarfile
import zipfile
import io

def zip_to_tar_gz(zip_path, tar_gz_path):
    with zipfile.ZipFile(zip_path, "r") as zf:
        with tarfile.open(tar_gz_path, "w:gz") as tar:
            for name in zf.namelist():
                data = zf.read(name)
                info = tarfile.TarInfo(name=name)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))

zip_to_tar_gz("world.zip", "world.tar.gz")
```

Then populate the environment:

```bash
curl -X POST "http://localhost:8080/data/populate?subsystem=filesystem" \
  -F "archive=@world.tar.gz"
```

### Step 4: Configure MCP Servers

```bash
curl -X POST http://localhost:8080/apps \
  -H "Content-Type: application/json" \
  -d @- << 'EOF'
{
  "mcpServers": {
    "filesystem-server": {
      "transport": "stdio",
      "command": "python",
      "args": ["main.py"],
      "cwd": "./mcp_servers/filesystem_server",
      "env": {"APP_FS_ROOT": "/filesystem"}
    }
  }
}
EOF
```

### Step 5: Create Agent Config Files

The agent runner needs separate config files:

**`agent_config.json`:**
```json
{
  "agent_config_id": "loop_agent",
  "agent_name": "Loop Agent",
  "agent_config_values": {
    "timeout": 3600,
    "max_steps": 50,
    "tool_call_timeout": 60,
    "llm_response_timeout": 300
  }
}
```

**`initial_messages.json`:**
```json
[
  {
    "role": "user",
    "content": "List all files in the filesystem and create a summary.txt file."
  }
]
```

### Step 6: Run the Agent

```bash
cd archipelago/agents

# Copy and configure .env
cp .env.example .env
# Edit .env with your LLM API key

# Install dependencies
uv sync

# Run the agent
uv run python -m runner.main \
  --trajectory-id "task_001" \
  --initial-messages ./initial_messages.json \
  --mcp-gateway-url "http://localhost:8080/mcp/" \
  --agent-config ./agent_config.json \
  --orchestrator-model "openai/gpt-5" \
  --output ./trajectory.json
```

### Step 7: Save the Final Snapshot

```bash
# Download as tar.gz
curl -X POST http://localhost:8080/data/snapshot -o final_snapshot.tar.gz

# Convert to zip for grading
python -c "
import tarfile, zipfile, io
with tarfile.open('final_snapshot.tar.gz', 'r:gz') as tar:
    with zipfile.ZipFile('final_snapshot.zip', 'w') as zf:
        for m in tar.getmembers():
            if m.isfile():
                f = tar.extractfile(m)
                if f: zf.writestr(m.name, f.read())
"
```

### Step 8: Create Grading Config Files

**`verifiers.json`:**
```json
[
  {
    "verifier_id": "ver_001",
    "verifier_version": 1,
    "world_id": null,
    "task_id": "task_001",
    "eval_config_id": "ec_output_llm",
    "verifier_values": {
      "criteria": "The agent listed all files and created a summary.txt file",
      "is_primary_objective": true
    },
    "verifier_index": 0,
    "verifier_dependencies": null
  }
]
```

**`eval_configs.json`:**
```json
[
  {
    "eval_config_id": "ec_output_llm",
    "eval_config_name": "Output LLM Verifier",
    "eval_defn_id": "output_llm",
    "eval_config_values": {}
  }
]
```

**`grading_settings.json`:**
```json
{
  "llm_judge_model": "openai/gpt-5",
  "llm_judge_extra_args": null
}
```

**`scoring_config.json`:**
```json
{
  "scoring_config_id": "sc_default",
  "scoring_config_name": "Default Scoring",
  "scoring_defn_id": "task_score_unweighted_and_universal_penalty",
  "scoring_config_values": {
    "task_primary_objective_scaling_factor": 2.0,
    "task_non_primary_objective_scaling_factor": 1.0,
    "task_negative_scaling_factor": 2.0,
    "universal_penalty_cap": 0.2,
    "final_score_ceiling": 1.0,
    "final_score_floor": 0.0
  }
}
```

### Step 9: Run Grading

```bash
cd archipelago/grading

# Copy and configure .env
cp .env.example .env
# Edit .env with your LLM API key

# Install dependencies
uv sync

# Run grading
uv run python -m runner.main \
  --grading-run-id "gr_001" \
  --trajectory-id "task_001" \
  --initial-snapshot ./original_snapshot.zip \
  --final-snapshot ./final_snapshot.zip \
  --trajectory ./trajectory.json \
  --grading-settings ./grading_settings.json \
  --verifiers ./verifiers.json \
  --eval-configs ./eval_configs.json \
  --scoring-config ./scoring_config.json \
  --output ./grades.json
```

### Step 10: View Results

```bash
cat grades.json | jq '.'
```

---

## Expected Output

After a successful run:
- `trajectory.json` - Agent's conversation history and tool calls
- `final_snapshot.zip` - Final state of the filesystem
- `grades.json` - Grading results with score and rationale

The agent should find and report the path `/animals/xk92m/qz7fw.png` as the gorilla image.

## Troubleshooting & Notes

### Scoring Configuration

This example uses the simplest scoring method (`template`) which requires no configuration:
```json
{
  "scoring_defn_id": "template",
  "scoring_config_values": {}
}
```

For production use, you may want `task_score_unweighted_and_universal_penalty` which supports weighted scoring, negative criteria penalties, and score clamping. See `archipelago/grading/runner/scoring_methods/` for available methods.

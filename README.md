# Archipelago

<a href="https://arxiv.org/abs/2601.14242"><img src="https://img.shields.io/badge/📝-Paper-b31b1b"></a>
<a href="https://huggingface.co/datasets/mercor/apex-agents"><img src="https://img.shields.io/badge/🤗-Data-yellow"></a>
<a href="http://mercor.com/blog/introducing-apex-agents"><img src="https://img.shields.io/badge/📰-Blog-0ea5e9"></a>
<a href="mailto:apex@mercor.com"><img src="https://img.shields.io/badge/✉️-Contact-green"></a>

Archipelago is a system for running and evaluating AI agents against MCP applications. It consists of three main components:

1. **Environment**: Headless environment that exposes an MCP gateway
2. **Agents**: Extensible agent runner with a registry of configurable agent implementations
3. **Grading**: Grades agent performance by comparing before/after snapshots (formerly "Verifier")

All components run in Docker containers.

The environment is meant to be run independently as a sandbox, and then an LLM agent connects to the exposed MCP server. The agents runner spawns and manages environment sandboxes automatically.

## Table of Contents

- [Quick Start: Run Your First Task](#quick-start-run-your-first-task)
- [Components](#components)
  - [Environment](#environment)
  - [Agents](#agents)
  - [Grading](#grading)
- [Local Development](#local-development)
  - [Running the Environment](#running-the-environment)
  - [Running Agents](#running-agents)
  - [Running the Grading](#running-the-grading)
- [Citation](#citation)

---

## Quick Start: Run Your First Task

**Estimated time: 30-60 minutes for first run**

This quick start walks you through running a single task end-to-end using the provided example.

### Prerequisites

- Docker Desktop
- Python 3.13
- UV
- LLM API key (Anthropic, OpenAI, or Gemini)

### 1. Set Up Environment Variables

```bash
cd archipelago

# Copy example env files
cp environment/.env.example environment/.env
cp agents/.env.example agents/.env
cp grading/.env.example grading/.env

# Edit agents/.env and grading/.env with your LLM API key (at least one required):
# ANTHROPIC_API_KEY=sk-ant-...
# or OPENAI_API_KEY=sk-...
# or GOOGLE_API_KEY=...

# The environment/.env can be left as-is for local development
```

### 2. Run an Example

We provide two examples:

**Option A: HuggingFace Benchmark Task (Recommended)**

Run tasks from the [mercor/apex-agents](https://huggingface.co/datasets/mercor/apex-agents) benchmark dataset with 480 professional services tasks.

```bash
cd examples/hugging_face_task
./run.sh
```

See [examples/hugging_face_task/README.md](./examples/hugging_face_task/README.md) for details.

**Option B: Simple Task**

A minimal example with a pre-defined task (find a gorilla image in a filesystem).

```bash
cd examples/simple_task
./run.sh
```

See [examples/simple_task/README.md](./examples/simple_task/README.md) for a detailed step-by-step walkthrough.

Both scripts will:
1. Start the environment container
2. Populate the environment with the world snapshot
3. Configure MCP servers
4. Run the agent
5. Save the final snapshot
6. Run grading and display results

### 3. Check Results

```bash
# View grading results
cat ./grades.json | jq '.scoring_results.final_score'

# View agent trajectory
cat ./trajectory.json | jq '.status'
```

---

## Components

### Environment

The Environment is a headless gateway designed to run in a Docker container. It serves as a management layer for LLM agents, providing MCP server orchestration, data population from S3, and state snapshotting.

#### Features

- **MCP Gateway**: Hot-swappable gateway that routes requests to configured MCP servers. Supports dynamic reconfiguration of tools and resources.
- **Data Management**:
  - **Population**: Download data from S3-compatible storage into local subsystems (`/filesystem`, `/.apps_data`).
  - **Snapshots**: Create `tar.gz` archives of the environment state and stream them back to the client or upload directly to S3.
- **Docker-First**: Designed to run as a containerized service with health checks and lifecycle management.

#### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check - returns 200 OK if running |
| `/docs` | GET | FastAPI generated API documentation |
| `/apps` | POST | Hot-swap MCP gateway configuration |
| `/mcp/` | - | MCP server endpoint (after configuration) |
| `/data/populate` | POST | Download data from S3 into subsystems |
| `/data/snapshot` | POST | Stream a tar.gz snapshot of environment state |
| `/data/snapshot/s3` | POST | Upload snapshot to S3, returns pre-signed URL |

#### Configuration

The environment is configured via environment variables:

| Variable | Description | Default |
|----------|-------------|---------|
| `S3_SNAPSHOTS_BUCKET` | S3 bucket for storing snapshots | `snapshots` |
| `S3_SNAPSHOTS_PREFIX` | Prefix for snapshot objects in S3 | `""` |
| `S3_DEFAULT_REGION` | AWS region for S3 operations | `us-west-2` |
| `S3_ACCESS_KEY_ID` | AWS access key ID | `None` |
| `S3_SECRET_ACCESS_KEY` | AWS secret access key | `None` |

#### Example: Configuring MCP Servers

```python
import requests

config = {
    "mcpServers": {
        "filesystem_server": {
            "transport": "stdio",
            "command": "python",
            "args": ["main.py"],
            "cwd": "./mcp_servers/filesystem_server"  # Must be a valid path in the container
        }
    }
}
requests.post("http://localhost:8080/apps", json=config)
```

After configuration, `http://localhost:8080/mcp/` exposes an MCP server that agents can connect to.

> For more details, see the [Environment README](./environment/README.md).

### Agents

The Agents component provides an extensible framework for running AI agents against environment sandboxes. It uses a registry-based architecture that allows multiple agent implementations with configurable parameters.

#### Features

- **Agent Registry**: Pluggable agent implementations (e.g., `react_toolbelt_agent`) that can be extended with custom agents
- **Configurable Parameters**: Each agent type defines its own configuration schema (max steps, timeouts, system prompts, etc.)
- **Environment Integration**: Spawns and manages environment sandboxes, handling data population, MCP configuration, and snapshotting
- **Observability**: Built-in logging to multiple backends (Datadog, PostgreSQL, Redis, file)

#### Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Agents Runner                            │
├─────────────────────────────────────────────────────────────────┤
│  runner/                                                        │
│  ├── main.py            Main orchestrator                       │
│  ├── models.py          Data models                             │
│  ├── agents/                                                    │
│  │   ├── models.py      AgentIds, AgentDefn, AgentRunInput      │
│  │   ├── registry.py    AGENT_REGISTRY mapping                  │
│  │   └── react_toolbelt_agent/  Default agent implementation    │
│  └── utils/             Settings, logging, redis                │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ HTTP API (spawned sandbox)
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Environment (Sandbox)                        │
│  POST /data/populate  · POST /apps  · /mcp/  · POST /snapshot   │
└─────────────────────────────────────────────────────────────────┘
```

#### Agent Registry

Agents are registered in `runner/agents/registry.py`. Each agent definition includes:

- `agent_id`: Unique identifier (e.g., `react_toolbelt_agent`)
- `agent_impl`: The async function that runs the agent
- `agent_config_fields`: Schema for configurable parameters

**Example: Loop Agent Configuration**

```python
AgentDefn(
    agent_id=AgentIds.LOOP_AGENT,
    agent_impl=loop_agent_run,
    agent_config_fields=[
        TaskFieldSchema(field_id="max_steps", field_type=TaskFieldType.NUMBER, default_value=100),
        TaskFieldSchema(field_id="timeout", field_type=TaskFieldType.NUMBER, default_value=10800),
        TaskFieldSchema(field_id="tool_call_timeout", field_type=TaskFieldType.NUMBER, default_value=60),
        TaskFieldSchema(field_id="system_prompt", field_type=TaskFieldType.TEXTAREA, required=False),
    ],
)
```

#### Execution Flow

1. Receive trajectory ID and fetch agent configuration
2. Spawn environment sandbox and wait for health check
3. Populate environment with world snapshot and task data
4. Configure MCP servers on the environment
5. Run agent (connects to environment's `/mcp/` endpoint)
6. Create snapshot and upload to S3
7. Report results via webhook

> For more details, see the [Agents README](./agents/README.md).

### Grading

The Grading system evaluates completed agent trajectories by analyzing what changed and checking performance against criteria.

The system automatically:

1. Computes snapshot diffs to identify file changes
2. Extracts embedded images (charts, diagrams) from visual artifacts (docs, PDFs, sheets, slides)
3. Selects relevant artifacts for each verifier
4. Grades against task-specific criteria
5. Calculates a final score

#### Verifier Types

**Task-Specific Verifiers**: Custom criteria defined per task

- `output`: Grades based on file changes (requires snapshot diff)
- `trajectory`: Grades based on agent's message history _[COMING SOON]_
- `value`: Grades based on extracted values _[COMING SOON]_

#### Inputs

**`trajectory_id`**: Trajectory identifier for saving grades

**`trajectory`**: Complete trajectory from the agent runner (AgentTrajectoryOutput)

**`grading_config`**: Grading configuration

```json
{
  "grading_run_id": "gr_abc123",
  "model": "anthropic/claude-3-5-sonnet-20241022",
  "extra_args": { "temperature": 0.7 },
  "verifiers": [
    {
      "verifier_id": "ver_001",
      "type": "output",
      "criteria": "The agent created a report.pdf file with sales analysis",
      "weight": 1.0,
      "is_primary_objective": true,
      "negative_criteria": "The agent deleted or corrupted existing data files",
      "index": 0,
      "universal": false,
      "tags": null,
      "dependencies": null,
      "criteria_explanation": null,
      "artifacts_to_reference": null,
      "modality": null
    },
    {
      "verifier_id": "ver_002",
      "type": "trajectory",
      "criteria": "The agent read the data.csv file before creating the report",
      "weight": 0.5,
      "is_primary_objective": false,
      "index": 1,
      "universal": false
    }
  ],
  "settings": {
    "scoring_method": "task_score_unweighted_and_universal_penalty",
    "universal_penalty_cap": 0.2,
    "final_score_ceiling": 1.0,
    "final_score_floor": 0.0
  }
}
```

**`task_config`**: Task configuration

**`initial_snapshot_bytes`**: Initial filesystem state before agent execution (BytesIO)

**`final_snapshot_bytes`**: Final filesystem state after agent execution (BytesIO)

#### Outputs

**Tuple of `(task_grades, final_score)`**

- `task_grades`: List of `GradeResult` for task-specific verifiers
- `final_score`: Weighted final score (float)

**`GradeResult`**: Individual grade for a single verifier

```json
{
  "trajectory_id": "traj_123abc",
  "verifier_id": "ver_001",
  "judge_grade": "pass",
  "score": 1.0,
  "grade_rationale": "The agent successfully created report.pdf containing sales analysis with charts and insights",
  "negative_grade": "pass",
  "negative_grade_rationale": "No data files were deleted or corrupted",
  "grading_prompts": {
    "grading": {
      "system_prompt": "...",
      "user_prompt": "...",
      "raw_llm_response": "...",
      "parsed_result": {"grade": "pass", "rationale": "..."},
      "visual_artifacts": [{"url": "https://...", "path": "report.pdf"}],
      "prompt_tokens": 1500,
      "completion_tokens": 200,
      "total_tokens": 1700
    },
    "negative_grading": {...}
  },
  "artifacts_to_evaluate_metadata": {
    "artifacts_to_evaluate_count": 3,
    "visual_artifacts_to_evaluate_count": 1,
    "artifacts_to_evaluate": [
      {"path": "report.pdf", "artifact_type": "file", "change_type": "created"}
    ]
  },
  "success": true,
  "error": null
}
```

> For more details on the grading pipeline, helpers, verifiers, and scoring methods, see the [Grading README](./grading/README.md).

---

## Local Development

The easiest way to run Archipelago locally is using Docker.

### Prerequisites

- Docker Desktop
- LLM API key(s)
- UV

### Running the Environment

1. **Navigate to environment directory:**

   ```bash
   cd environment
   ```

2. **Set Up Environment Variables:**

   ```bash
   cp .env.example .env
   # Edit if needed (can be left as-is for local development)
   ```

3. **Run with Docker Compose:**

   ```bash
   docker-compose --ansi always --env-file .env up --build
   ```

   The server will be available at `http://localhost:8080`.

4. **Configure MCP Servers:**

   Once running, configure your MCP servers via the `/apps` endpoint:

   ```bash
   curl -X POST http://localhost:8080/apps \
     -H "Content-Type: application/json" \
     -d '{"mcpServers": {"my-server": {"transport": "stdio", "command": "python", "args": ["main.py"], "cwd": "./mcp_servers/my-server"}}}'
   ```

5. **Connect an Agent:**

   Your LLM agent can now connect to the MCP gateway at `http://localhost:8080/mcp/`.

### Running Agents

1. **Navigate to agents directory:**

   ```bash
   cd agents
   ```

2. **Set Up Environment Variables:**

   Create your `.env` file with required credentials:

   ```bash
   cp .env.example .env
   ```

   Required variables:
   - LLM API keys (at least one): `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `GOOGLE_API_KEY`
   
   Optional:
   - Redis connection (for logging)

3. **Install Dependencies:**

   ```bash
   uv sync
   ```

4. **Run Locally:**

   For local development/testing, you can run the agent runner directly:

   ```bash
   uv run python -m runner.main --help
   ```

### Running the Grading

1. **Navigate to grading directory:**

   ```bash
   cd grading
   ```

2. **Set Up Environment Variables:**

   Create your `.env` file with LLM provider credentials:

   ```bash
   cp .env.example .env
   ```

   Then edit `.env` with your actual API keys:

   - **Required**: At least one LLM provider (Anthropic, OpenAI, Gemini, etc.)
   - **Recommended**: `REDUCTO_API_KEY` for document extraction (PDFs, Documents docs, Spreadsheets, Presentations)

3. **Prepare Configuration:**

   The grading runner expects these files in `.config/`:

   - `task.json` - Task configuration with initial messages
   - `data.zip` - Original snapshot (trajectory input)
   - `final_snapshot.zip` - Final snapshot (trajectory output)
   - `grading_config.json` - Grading configuration with verifiers

4. **Run the Grading:**

   Run this [mise](https://mise.jdx.dev/getting-started.html) task, or manually run each command:

   ```bash
   mise run start
   ```

   *Note: This runs `python validate_config.py` and `python test_local.py`*

The grading system will automatically validate configuration, compute snapshot diffs, generate screenshots for visual artifacts, grade against all verifiers, and output detailed results.

**Output**: When running locally (without webhook configured), results are saved to `.output/grades_<grading_run_id>.json` with:

- Individual grades for each verifier
- Final score
- Rationales and prompts used
- Artifacts evaluated

---

## Legal disclaimer on the content of worlds

This material is provided for research, educational, and informational purposes only. It consists of hypothetical, simulated financial and legal and regulatory analyses and illustrative scenarios (including, without limitation, simulated leverage buyout structures, capital structures, financing terms, valuation ranges, projected returns, and potential mergers, acquisitions, divestitures, or other strategic transactions, legal memoranda, hypothetical legal advice to a company, hypothetical correspondences to regulatory agencies, etc.). No representation is made that any scenario described herein is likely to occur, is being contemplated by any person, or reflects an actual proposed or pending transaction or any legal, regulatory, or compliance risk.

This material does not constitute (and should not be construed as) financial, investment, legal, tax, accounting, or other professional advice, and is not intended to form the basis of any investment decision or any contract. The analyses and outputs in this material are based on assumptions, estimates, modeling methodologies, and hypothetical legal scenarios, that may prove incorrect. The financial and legal information is derived from publicly available information and third‑party sources that have not been independently verified. Any projections, forward‑looking statements, scenario outputs, similar financial information, and any legal documents, memoranda, and correspondence, are hypothetical and thus inherently uncertain and are provided solely to illustrate how results might change under different assumptions. No representation or warranty (express or implied) is made regarding this material, and it is provided on an “as‑is” and “as‑available” basis.

To the maximum extent permitted by applicable law, Mercor disclaims any liability for any direct or indirect losses or damages arising from or related to the use of (or reliance on) this material, including without limitation any loss of profits, loss of business, loss of goodwill, or consequential, incidental, special, punitive, or exemplary damages, even if advised of the possibility of such damages. Nothing in this disclaimer limits or excludes liability that cannot be limited or excluded under applicable law.

## Intended Use & Restrictions

APEX-Agents is intended exclusively for model evaluation. Any use of this dataset for training, fine-tuning, or parameter fitting is forbidden. Crawling or scraping the dataset is also forbidden.

## Citation

If you use Archipelago in your research, please cite our paper:

```bibtex
@misc{vidgen2026apexagents,
  title        = {APEX--Agents},
  author       = {Vidgen, Bertie and Mann, Austin and Fennelly, Abby and Stanly, John Wright and Rothman, Lucas and Burstein, Marco and Benchek, Julien and Ostrofsky, David and Ravichandran, Anirudh and Sur, Debnil and Venugopal, Neel and Hsia, Alannah and Robinson, Isaac and Huang, Calix and Varones, Olivia and Khan, Daniyal and Haines, Michael and Richards, Zach and Mahapatra, Chirag and Foody, Brendan and Nitski, Osvald},
  year         = {2026},
  howpublished = {arXiv},
  url          = {https://arxiv.org/abs/2601.14242}
}
```

Paper: https://arxiv.org/abs/2601.14242

---
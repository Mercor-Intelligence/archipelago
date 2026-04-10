# Standalone Grader

A self-contained CLI tool for evaluating agent trajectories against verifiers and scoring configs. Ships with complete Docker build files so you can build and adapt the grader to your needs.

## Quick Start

### Build the Docker image

```bash
cd grader/
docker build -f Dockerfile -t grader .
```

### Run grading

```bash
docker run --rm \
  -v $(pwd)/data:/data \
  -e OPENAI_API_KEY=sk-... \
  grader \
    --initial-snapshot /data/snapshot_before.zip \
    --final-snapshot /data/snapshot_after.zip \
    --trajectory /data/trajectory.json \
    --grading-config /data/grading_config.json \
    --output /data/results.json
```

## CLI Arguments

| Argument | Required | Description |
|----------|----------|-------------|
| `--initial-snapshot` | Yes | Path to the pre-agent-run filesystem snapshot ZIP |
| `--final-snapshot` | Yes | Path to the post-agent-run filesystem snapshot ZIP |
| `--trajectory` | Yes | Path to the agent trajectory JSON file |
| `--grading-config` | Yes | Path to the unified grading config JSON |
| `--output` | No | Path to write results JSON (default: stdout) |
| `--golden-snapshot` | No | Path to a golden response snapshot ZIP (can be repeated) |
| `--grading-run-id` | No | Custom grading run ID (auto-generated if omitted) |

## Input Formats

### Filesystem Snapshots

ZIP archives of the filesystem state before and after the agent run. Should include:
- Application files (e.g., `/app/files/`)
- Application data (e.g., `/.apps_data/`)

### Trajectory JSON

The agent's conversation trajectory in LiteLLM message format:

```json
{
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."},
    {"role": "assistant", "content": "..."}
  ],
  "output": {},
  "status": "completed",
  "time_elapsed": 120.5
}
```

### Grading Config JSON

A unified config combining verifiers, eval definitions, and scoring. Pre-generated configs for each task are provided in `configs/`.

```json
{
  "grading_settings": {
    "llm_judge_model": "openai/gpt-4o",
    "llm_judge_extra_args": null
  },
  "verifiers": [
    {
      "verifier_id": "ver_abc123",
      "verifier_version": 1,
      "world_id": "world_xyz",
      "task_id": null,
      "eval_config_id": "ec_001",
      "verifier_values": { "criteria": "..." },
      "verifier_index": 0,
      "verifier_dependencies": null
    }
  ],
  "eval_configs": [
    {
      "eval_config_id": "ec_001",
      "eval_config_name": "LLM Judge",
      "eval_defn_id": "output_llm",
      "eval_config_values": {}
    }
  ],
  "scoring_config": {
    "scoring_config_id": "sc_001",
    "scoring_config_name": "Weighted Sum",
    "scoring_defn_id": "task_score_and_universal_penalty_weighted_sum",
    "scoring_config_values": {}
  }
}
```

## Output Format

```json
{
  "grading_run_id": "gr_abc123",
  "grading_run_status": "completed",
  "verifier_results": [
    {
      "verifier_id": "ver_abc123",
      "verifier_version": 1,
      "score": 1.0,
      "verifier_result_values": { "reasoning": "..." },
      "status": "ok",
      "message": ""
    }
  ],
  "scoring_results": {
    "final_score": 0.85,
    "scoring_method_result_values": {}
  }
}
```

## Environment Variables

LLM-based verifiers require API access. Set the appropriate key for your configured `llm_judge_model`:

| Provider | Environment Variable |
|----------|---------------------|
| OpenAI | `OPENAI_API_KEY` |
| Anthropic | `ANTHROPIC_API_KEY` |
| Google | `GOOGLE_API_KEY` |
| Azure | `AZURE_API_KEY`, `AZURE_API_BASE` |

The `llm_judge_model` field in `grading_config.json` uses [LiteLLM model naming](https://docs.litellm.ai/docs/providers) (e.g., `openai/gpt-4o`, `anthropic/claude-sonnet-4-20250514`).

## System Requirements

The Docker image includes all necessary dependencies:

- **Python 3.13**
- **LibreOffice** — Office document to PDF conversion (used by multi-representation verifiers)
- **poppler-utils** — PDF rendering to images
- **Data science packages** — pandas, numpy, scikit-learn, xgboost (used by programmatic verifiers)

Minimum resources: **4 GB RAM**, **2 CPU cores** recommended.

## Running Without Docker

```bash
cd grader/

# Install uv (Python package manager)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Install dependencies
uv sync --locked --all-groups

# Run
uv run python -m runner.cli \
  --initial-snapshot snapshot_before.zip \
  --final-snapshot snapshot_after.zip \
  --trajectory trajectory.json \
  --grading-config grading_config.json \
  --output results.json
```

Note: Running without Docker requires LibreOffice and poppler-utils installed on your system if you use verifiers that need document conversion.

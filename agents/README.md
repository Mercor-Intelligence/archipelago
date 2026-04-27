# Archipelago Agents

An extensible framework for running AI agents against environment sandboxes. Uses a registry-based architecture that allows multiple agent implementations with configurable parameters.

## Features

- **Agent Registry**: Pluggable agent implementations that can be extended with custom agents
- **Configurable Parameters**: Each agent type defines its own configuration schema (max steps, timeouts, etc.)
- **Environment Integration**: Spawns and manages environment sandboxes, handling data population, MCP configuration, and snapshotting
- **Observability**: Built-in logging to multiple backends (Datadog, PostgreSQL, Redis, file)

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         Agents Runner                           │
├─────────────────────────────────────────────────────────────────┤
│  runner/                                                        │
│  ├── main.py            Main orchestrator                       │
│  ├── models.py          Data models                             │
│  ├── agents/                                                    │
│  │   ├── models.py      AgentConfigIds, AgentDefn, AgentRunInput│
│  │   ├── registry.py    AGENT_REGISTRY mapping                  │
│  │   └── <agent_name>/  Agent implementations                   │
│  └── utils/             Settings, logging, redis                │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ HTTP API (spawned sandbox)
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                     Environment (Sandbox)                       │
│  POST /data/populate  · POST /apps  · /mcp/  · POST /snapshot   │
└─────────────────────────────────────────────────────────────────┘
```

## Execution Flow

1. Receive trajectory ID and fetch agent configuration
2. Spawn environment sandbox and wait for health check
3. Populate environment with world snapshot and task data
4. Configure MCP servers on the environment
5. Run agent (connects to environment's `/mcp/` endpoint)
6. Create snapshot and upload to S3
7. Report results via webhook

## Agent Contract

Every registered agent must satisfy three guarantees:

1. **Implement the run signature**: `async def run(run_input: AgentRunInput) -> AgentTrajectoryOutput`
2. **Emit a final_answer log on completion**: `logger.bind(message_type="final_answer").info(answer)`
3. **Be registered in AGENT_REGISTRY** with a matching `AgentConfigIds` enum entry

These guarantees are enforced by `tests/test_final_answer_log.py`.

## Agent Registry

Agents are registered in `runner/agents/registry.py`. Each agent definition includes:

- `agent_config_id`: Unique identifier from the `AgentConfigIds` enum (e.g., `AgentConfigIds.LOOP_AGENT`)
- `agent_impl`: The async function that runs the agent
- `agent_config_fields`: Schema for configurable parameters

### Available Agents

| ID | Description |
|----|-------------|
| `loop_agent` | Basic tool-calling loop. Calls the LLM repeatedly, executing any tool calls, until the LLM returns a response without tool calls. |
| `react_toolbelt_agent` | ReAct agent with dynamic tool selection (toolbelt), ReSum context summarization, and an explicit `final_answer` tool. This is the agent used in the APEX-Agents benchmark. |
| `echo_agent` | Reference implementation. Does not call any LLM or connect to MCP. Echoes back the last user message. Useful as a smoke test and copy-paste starting point. |

### Reference Implementation

The `echo_agent` at `runner/agents/echo_agent/` is the simplest possible agent that satisfies the full contract. It runs in O(1) wall time and is the only agent that can be exercised end-to-end in tests without mocking LiteLLM. Start here when building a new agent.

### Creating a New Agent

1. Add a new ID to `AgentConfigIds` in `runner/agents/models.py`:

```python
class AgentConfigIds(StrEnum):
    LOOP_AGENT = "loop_agent"
    REACT_TOOLBELT_AGENT = "react_toolbelt_agent"
    ECHO_AGENT = "echo_agent"
    MY_AGENT = "my_agent"  # Add your agent
```

2. Create your agent implementation in `runner/agents/my_agent/main.py`:

```python
import time

from loguru import logger

from runner.agents.models import (
    AgentRunInput,
    AgentStatus,
    AgentTrajectoryOutput,
)


async def run(run_input: AgentRunInput) -> AgentTrajectoryOutput:
    """Your custom agent implementation."""
    start = time.time()

    # Access configuration via run_input.agent_config_values
    max_steps = run_input.agent_config_values.get("max_steps", 100)

    # Connect to MCP server at run_input.mcp_gateway_url
    # Run your agent loop
    # ...

    answer = "your final answer"

    # Required: emit the final_answer log
    logger.bind(message_type="final_answer").info(answer)

    return AgentTrajectoryOutput(
        messages=list(run_input.initial_messages),
        status=AgentStatus.COMPLETED,
        time_elapsed=time.time() - start,
    )
```

3. Register your agent in `runner/agents/registry.py`:

```python
from runner.agents.my_agent.main import run as my_agent_run

# Add to the existing AGENT_REGISTRY dict:
AGENT_REGISTRY[AgentConfigIds.MY_AGENT] = AgentDefn(
    agent_config_id=AgentConfigIds.MY_AGENT,
    agent_impl=my_agent_run,
    agent_config_fields=[
        TaskFieldSchema(
            field_id="max_steps",
            field_type=TaskFieldType.NUMBER,
            label="Max Steps",
            default_value=100,
        ),
    ],
)
```

4. Verify the contract holds:

```bash
cd agents
uv run pytest tests/test_final_answer_log.py -v
```

See also `CONTRIBUTING-AGENTS.md` for a compact checklist.

## Local Development

1. **Navigate to agents directory:**

   ```bash
   cd archipelago/agents
   ```

2. **Set up environment variables:**

   ```bash
   cp .env.example .env
   ```

   Required variables:
   - LLM API keys (at least one): `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, or `GOOGLE_API_KEY`
   - AWS credentials for S3 operations (optional)
   - Redis connection (optional, for logging)

3. **Install dependencies:**

   ```bash
   uv sync
   ```

4. **Run locally:**

   ```bash
   uv run python -m runner.main --help
   ```

### Running an Agent Manually

The agent runner requires several configuration files.

**1. Create `initial_messages.json`:**

```json
[
  {
    "role": "user",
    "content": "Your task prompt goes here..."
  }
]
```

**2. Create `agent_config.json`:**

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

**3. Run the agent:**

```bash
uv run python -m runner.main \
  --trajectory-id "my_task_001" \
  --initial-messages ./initial_messages.json \
  --mcp-gateway-url "http://localhost:8080/mcp/" \
  --agent-config ./agent_config.json \
  --orchestrator-model "anthropic/claude-opus-4-5" \
  --output ./trajectory.json
```

## Data Models

### AgentRunInput

The input passed to every agent implementation:

- `trajectory_id`: Unique identifier for this run
- `initial_messages`: Initial system + user messages (LiteLLM format)
- `mcp_gateway_url`: URL to the environment's MCP gateway (None for agents that do not use MCP)
- `mcp_gateway_auth_token`: Auth token for MCP gateway (None for local/unauthenticated)
- `orchestrator_model`: LLM model to use (e.g., `anthropic/claude-opus-4-5`)
- `orchestrator_extra_args`: Additional LLM arguments (temperature, etc.)
- `agent_config_values`: Configuration values for this agent type
- `parent_trajectory_output`: Output from a previous trajectory (for multi-turn continuations, None otherwise)
- `custom_args`: Arbitrary per-trajectory metadata (None by default)

### AgentTrajectoryOutput

The output returned by agent implementations:

- `messages`: Complete message history (input + generated messages)
- `status`: Final status (`completed`, `failed`, `cancelled`, `error`)
- `time_elapsed`: Total execution time in seconds
- `output`: Structured output dict (optional)
- `usage`: Token usage dict (optional)

## Logging

The agents framework supports multiple logging backends configured via environment variables:

- **File**: Local JSON file logging
- **PostgreSQL**: Database logging for persistence
- **Redis**: Real-time streaming logs
- **Datadog**: APM and metrics

Configure in `runner/utils/logging/main.py`.

### Required: Final Answer Log

**Every agent must emit a `final_answer` log when completing.**

```python
from loguru import logger

# When your agent completes, emit:
logger.bind(message_type="final_answer").info(answer)
```

This is used to denote the final response to display to end users. The contract is enforced by `tests/test_final_answer_log.py`.

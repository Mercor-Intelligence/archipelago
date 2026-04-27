# Adding a New Agent to Archipelago

Compact checklist for registering a new agent implementation. See `README.md` for full details and the `runner/agents/echo_agent/` directory for a minimal reference implementation.

## Checklist

### 1. Add enum entry

In `runner/agents/models.py`, add your agent ID to the `AgentConfigIds` enum:

```python
class AgentConfigIds(StrEnum):
    LOOP_AGENT = "loop_agent"
    REACT_TOOLBELT_AGENT = "react_toolbelt_agent"
    ECHO_AGENT = "echo_agent"
    MY_AGENT = "my_agent"
```

### 2. Create implementation

Create `runner/agents/my_agent/__init__.py` (empty) and `runner/agents/my_agent/main.py` with this signature:

```python
async def run(run_input: AgentRunInput) -> AgentTrajectoryOutput:
```

The function must:
- Accept a single `AgentRunInput` argument
- Return an `AgentTrajectoryOutput` with a valid `AgentStatus`
- Emit `logger.bind(message_type="final_answer").info(answer)` before returning

### 3. Register in AGENT_REGISTRY

In `runner/agents/registry.py`:

```python
from runner.agents.my_agent.main import run as my_agent_run

AGENT_REGISTRY[AgentConfigIds.MY_AGENT] = AgentDefn(
    agent_config_id=AgentConfigIds.MY_AGENT,
    agent_impl=my_agent_run,
    agent_config_fields=[],  # Add TaskFieldSchema entries as needed
)
```

### 4. Verify

```bash
cd agents
uv run pytest tests/test_final_answer_log.py -v
```

All three tests must pass:
- `test_every_agent_id_has_registry_entry` -- your enum value has a registry entry
- `test_every_registered_agent_has_callable_run` -- your `agent_impl` is async and callable
- `test_echo_agent_emits_final_answer_log` -- baseline contract check (does not run your agent)

### 5. Update documentation

Add your agent to the "Available Agents" table in `README.md`.

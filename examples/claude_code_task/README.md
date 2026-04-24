# Claude Code Task Example

An end-to-end example that runs `claude_code_agent` in Archipelago using a simple Claude harness test:

1. Create and run a hello-world Python script.

## Quick Start

1. Set `ANTHROPIC_API_KEY` in:
- `agents/.env`
- `grading/.env`

2. Run:

```bash
cd archipelago/examples/claude_code_task
./run.sh
```

The script will:
1. Start/restart the environment container.
2. Populate the environment filesystem from `original_snapshot.zip`.
3. Configure MCP servers (`claude_code_filesystem`).
4. Run `claude_code_agent`.
5. Save the final snapshot.
6. Run grading and print results.

## Config Files

- `agent_config.json`: selects `claude_code_agent`.
- `orchestrator_config.json`: model + extra args for the agent.
- `initial_messages.json`: single easy test prompt.
- `mcp_config.json`: MCP servers exposed by the environment.
- `grading_settings.json`: model used by the grading runner.

## Outputs

After running:
- `trajectory.json`: full agent trajectory.
- `final_snapshot.tar.gz`: environment snapshot after run.
- `grades.json`: grading results (if agent completed).

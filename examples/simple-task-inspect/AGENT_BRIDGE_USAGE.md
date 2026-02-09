# Using Claude Code with Inspect AI

This document explains how to run the simple_task evaluation with Claude Code as the agent.

## Overview

We've implemented **two agent types** for this evaluation:

1. **React Agent** (default) - Uses Inspect's native tools
2. **Claude Code Agent** - Uses Claude Code CLI via Agent Bridge

## Agent Bridge Architecture

```
┌─────────────────────────────────────────┐
│   Inspect Evaluation                    │
│   ├─ Model: anthropic/claude-sonnet-4-0│
│   ├─ Solver: claude_code_agent()       │
│   └─ Sandbox: Docker                   │
└─────────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────┐
│   sandbox_agent_bridge()                │
│   ├─ Proxy Server: localhost:13131     │
│   └─ Intercepts API calls               │
└─────────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────────┐
│   Docker Sandbox                        │
│   ├─ Claude Code CLI                    │
│   │  └─ ANTHROPIC_BASE_URL=:13131      │
│   └─ Files: /tmp/sandbox/animals/      │
└─────────────────────────────────────────┘
```

## How It Works

### 1. Agent Bridge Setup

```python
@agent
def claude_code_agent() -> Agent:
    async def execute(state: AgentState) -> AgentState:
        # Start bridge proxy server
        async with sandbox_agent_bridge(state) as bridge:
            
            # Run Claude Code with API redirection
            result = await sandbox().exec(
                cmd=["claudecode", "--prompt", prompt.text],
                env={"ANTHROPIC_BASE_URL": f"http://localhost:{bridge.port}"}
            )
            
            # Bridge tracks state changes automatically
            return bridge.state
    
    return execute
```

### 2. API Call Flow

1. Claude Code makes API call to `http://localhost:13131`
2. Bridge proxy intercepts the request
3. Proxy forwards to Inspect's model provider
4. Response flows back through proxy to Claude Code
5. Bridge updates `state.messages` and `state.output`

### 3. Key Benefits

- **Model Flexibility**: Use any Inspect-supported model with Claude Code
- **Automatic Tracking**: Bridge tracks all messages and outputs
- **Full Shell Access**: Claude Code has complete container access
- **Transparent Integration**: Claude Code thinks it's talking to real Anthropic API

## Usage

### Running with React Agent (Default)

```bash
# Uses Inspect tools (list_files, read_image_file, get_directory_tree)
inspect eval simple_task.py --model openai/gpt-4o
```

**Agent Behavior:**
- Calls structured tools with specific parameters
- Tools execute `sandbox()` commands (ls, cat, etc.)
- Returns structured responses

### Running with Claude Code Agent

```bash
# Uses Claude Code CLI with shell access
inspect eval simple_task.py -T agent_type=claude_code --model anthropic/claude-sonnet-4-0
```

**Agent Behavior:**
- Has full shell access (`ls`, `file`, `python`, etc.)
- Can use any command-line tools in container
- More flexible but less structured

### Using Different Models

The beauty of the agent bridge is you can use **any model** with Claude Code:

```bash
# Use OpenAI with Claude Code
inspect eval simple_task.py -T agent_type=claude_code --model openai/gpt-4o

# Use Google with Claude Code
inspect eval simple_task.py -T agent_type=claude_code --model google/gemini-1.5-pro

# Use Anthropic (designed for this)
inspect eval simple_task.py -T agent_type=claude_code --model anthropic/claude-sonnet-4-0
```

## Implementation Details

### Dockerfile Configuration

```dockerfile
# Install Claude Code
RUN curl -fsSL https://static.claudeusercontent.com/install-claudecode.sh | bash || true

# Set default API endpoint (overridden by bridge)
ENV ANTHROPIC_BASE_URL=http://localhost:13131
```

### Agent Implementation

```python
from inspect_ai.agent import sandbox_agent_bridge
from inspect_ai.model import user_prompt
from inspect_ai.util import sandbox

@agent
def claude_code_agent() -> Agent:
    async def execute(state: AgentState) -> AgentState:
        async with sandbox_agent_bridge(state) as bridge:
            prompt = user_prompt(state.messages)
            
            result = await sandbox().exec(
                cmd=["claudecode", "--prompt", prompt.text, "--non-interactive"],
                env={
                    "ANTHROPIC_BASE_URL": f"http://localhost:{bridge.port}",
                    "ANTHROPIC_API_KEY": "bridge"
                }
            )
            
            if not result.success:
                raise RuntimeError(f"Claude Code error: {result.stderr}")
            
            return bridge.state
    
    return execute
```

## Comparison: React vs Claude Code

| Feature | React Agent | Claude Code Agent |
|---------|-------------|-------------------|
| **Tool Access** | Predefined tools only | Full shell access |
| **Flexibility** | Structured, limited | Very flexible |
| **Model Support** | Any vision model | Any model (via bridge) |
| **Setup** | No extra dependencies | Requires Claude Code |
| **Debugging** | Tool call logs | Shell command logs |
| **Use Case** | Controlled evaluation | Exploratory tasks |

## Troubleshooting

### Claude Code Not Installed

If Claude Code installation fails during Docker build:

```bash
# Check if Claude Code is available
docker run --rm simple-task-inspect which claudecode

# If not found, React agent still works
inspect eval simple_task.py --model openai/gpt-4o
```

### Bridge Connection Issues

```bash
# Enable debug logging
INSPECT_LOG_LEVEL=debug inspect eval simple_task.py -T agent_type=claude_code --model anthropic/claude-sonnet-4-0

# Check sandbox is running
docker ps | grep inspect
```

### API Key Issues

The bridge uses Inspect's model provider, so you need the API key for your chosen model:

```bash
# For Anthropic models
export ANTHROPIC_API_KEY=your-key

# For OpenAI models
export OPENAI_API_KEY=your-key

# For Google models
export GOOGLE_API_KEY=your-key
```

## Next Steps

1. **Bridged Tools**: Expose host-side Inspect tools to Claude Code via MCP
2. **Custom Agents**: Implement other CLI agents (Codex, custom scripts)
3. **Multi-Agent**: Combine React and Claude Code agents in one evaluation

## References

- [Inspect Agent Bridge Docs](https://inspect.aisi.org.uk/agent-bridge.html)
- [Claude Code Example](https://github.com/UKGovernmentBEIS/inspect_ai/tree/main/examples/bridge/claude)
- [Sandbox Bridge API](https://inspect.aisi.org.uk/reference/inspect_ai.agent.html#sandbox_agent_bridge)

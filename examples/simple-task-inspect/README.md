# Simple Task - Inspect AI

Inspect AI implementation of Archipelago's `simple_task` example.

## Overview

**Task**: Find a gorilla image hidden in a filesystem with multiple subdirectories containing different animal images.

**Expected Answer**: `/animals/xk92m/qz7fw.png`

## Prerequisites

- Docker Desktop (running)
- Python 3.12+
- Inspect AI: `pip install inspect-ai`
- API key for your chosen model:
  - OpenAI: `export OPENAI_API_KEY=your-key-here`
  - Anthropic: `export ANTHROPIC_API_KEY=your-key-here`

## Quick Start

### Option 1: React Agent (Default)

Uses Inspect's native tools with vision model:

```bash
# Navigate to this directory
cd examples/simple_task_inspect

# Run with React agent (uses Inspect tools)
inspect eval simple_task.py --model openai/gpt-4o

# View results in browser
inspect view
```

### Option 2: Claude Code Agent

Uses Claude Code CLI running in the sandbox:

```bash
# Run with Claude Code agent (uses shell commands)
inspect eval simple_task.py -T agent_type=claude_code --model anthropic/claude-sonnet-4-0

# View results in browser
inspect view
```

## What This Demonstrates

This implementation shows how to port Archipelago tasks to Inspect AI:

1. **Multiple Agent Types**: Support for both React agent (with tools) and Claude Code agent (CLI-based)
2. **Native Inspect Tools**: Implements filesystem tools as native Inspect `@tool` functions
3. **Agent Bridge**: Uses `sandbox_agent_bridge()` to run Claude Code in sandbox with API redirection
4. **Vision Model Support**: `read_image_file()` returns `ContentImage` for vision models
5. **Sandbox Environment**: Docker container with file population
6. **Scoring**: Pattern matching to verify correct answer

## Architecture

```
┌─────────────────────────────────────┐
│   Inspect Evaluation Framework      │
├─────────────────────────────────────┤
│  simple_task.py                     │
│  └─ Task                            │
│     ├─ Dataset (Sample w/ files)    │
│     ├─ Solver (react agent)         │
│     ├─ Tools (@tool decorators):    │
│     │  ├─ list_files()              │
│     │  ├─ read_image_file()         │
│     │  └─ get_directory_tree()      │
│     └─ Scorer (pattern matching)    │
└─────────────────────────────────────┘
           │
           ▼
┌─────────────────────────────────────┐
│   Docker Sandbox                    │
├─────────────────────────────────────┤
│  /tmp/sandbox/animals/              │
│    ├─ xk92m/qz7fw.png (gorilla)    │
│    ├─ jh21q/pl4nc.png              │
│    ├─ ab47z/rt3ky.png              │
│    └─ mn83p/jw9vb.png              │
│                                     │
│  Tools run in sandbox context       │
│  accessing files via Python stdlib  │
└─────────────────────────────────────┘
```

## Agent Types

This evaluation supports two different agent approaches:

### 1. React Agent (Default)

- Uses Inspect's built-in `react()` agent
- Accesses filesystem via custom Inspect tools (`list_files`, `read_image_file`, `get_directory_tree`)
- Tools use `sandbox()` API to execute commands in Docker container
- Works with any vision-capable model (OpenAI, Anthropic, Google, etc.)

**Pros**: Tool-based, structured, works with any model provider  
**Cons**: Limited to predefined tools

### 2. Claude Code Agent

- Uses Claude Code CLI running in the sandbox via `sandbox_agent_bridge()`
- Has full shell access to the container (can use `ls`, `file`, `python`, etc.)
- API calls are intercepted and routed through Inspect's model provider
- Requires Claude Code to be installed in Docker container

**Pros**: Full shell access, more flexible exploration  
**Cons**: Requires Claude Code installation, primarily designed for Anthropic models

## Comparison: Archipelago vs Inspect

| Aspect | Archipelago | Inspect AI (React) | Inspect AI (Claude Code) |
|--------|-------------|-------------------|-------------------------|
| **Environment** | HTTP service | Docker sandbox | Docker sandbox |
| **File Setup** | POST to `/data/populate` | `Sample.files` dict | `Sample.files` dict |
| **Tools** | MCP filesystem server | Native Inspect `@tool` | Full shell access |
| **Vision** | read_image_file returns Image | read_image_file returns ContentImage | Uses CLI tools |
| **Agent** | `react_toolbelt_agent` | `react()` agent | Claude Code CLI |
| **Grading** | LLM judge | Pattern matching | Pattern matching |
| **Orchestration** | Custom `main.py` | `inspect eval` CLI | `inspect eval` CLI |
| **Results** | JSON files | Inspect View + logs | Inspect View + logs |

## File Structure

```
simple_task_inspect/
├── simple_task.py        # Task definition with native tools
├── Dockerfile            # Minimal Python sandbox
├── compose.yaml         # Docker resource limits
├── data/animals/        # Initial images (4 PNG files)
└── README.md            # This file
```

## How Vision Works

The native `read_image_file()` tool:
1. Reads image bytes from the sandbox filesystem at `/tmp/sandbox`
2. Encodes as base64 and returns a `ContentImage` object
3. Inspect AI automatically formats this for vision models
4. GPT-4o receives the image and can identify the content

When the agent calls `read_image_file("/animals/xk92m/qz7fw.png")`, the vision model sees the actual gorilla image and can identify it.

## Expected Agent Behavior

1. **Explore**: `list_files("/animals")` → discover 4 subdirectories
2. **Investigate**: `get_directory_tree("/animals")` → see full structure
3. **View**: `read_image_file("/animals/xk92m/qz7fw.png")` → vision model sees gorilla
4. **Report**: Return answer with full path to gorilla image

**Note**: The vision model (gpt-4o) can actually "see" the images through the native `read_image_file()` tool, which returns a `ContentImage` that Inspect automatically formats for vision models.

## Troubleshooting

### Docker Build Fails

```bash
# Check Docker is running
docker ps

# Rebuild image
docker build -t simple-task-inspect .
```

### Files Not Found in Sandbox

```bash
# Verify data directory exists
ls data/animals/

# Check Inspect copied files
docker run -it simple-task-inspect bash
ls /tmp/sandbox/animals/
```

### Evaluation Hangs

- Check Docker container logs: `docker logs <container-id>`
- Verify API key is set: `echo $OPENAI_API_KEY`
- Try with `--max-connections 1` to debug

## Advanced Usage

### Use Different Model

```bash
# React agent with Anthropic
inspect eval simple_task.py --model anthropic/claude-sonnet-4-0

# React agent with Google
inspect eval simple_task.py --model google/gemini-1.5-pro

# Claude Code agent (note: primarily designed for Anthropic models)
inspect eval simple_task.py -T agent_type=claude_code --model anthropic/claude-sonnet-4-0
```

### Using Claude Code Agent

The Claude Code agent uses Inspect's **Agent Bridge** to run Claude Code in the sandbox:

```bash
# Run with Claude Code (uses shell instead of tools)
inspect eval simple_task.py -T agent_type=claude_code --model anthropic/claude-sonnet-4-0
```

**How it works:**

1. `sandbox_agent_bridge()` starts a proxy server on port 13131 in the sandbox
2. Claude Code is configured to make API calls to `http://localhost:13131`
3. The proxy intercepts calls and routes them through Inspect's model provider
4. This allows you to use any Inspect-supported model with Claude Code!

**Note**: Claude Code installation is included in the Dockerfile but may fail if the installation script is unavailable. The React agent will still work if Claude Code installation fails.

### Debug Mode

```bash
# Keep sandbox running after eval
inspect eval simple_task.py --no-sandbox-cleanup

# Get shell in sandbox
docker exec -it <container-id> bash
ls /tmp/sandbox/animals/

# Test Claude Code manually
claudecode --prompt "List files in /tmp/sandbox/animals"
```

### View Detailed Logs

```bash
# Open log viewer
inspect view

# Or view specific log file
cat logs/<timestamp>_simple_task.eval | jq '.samples[0]'
```

## Success Criteria

- ✅ Agent discovers 4 subdirectories
- ✅ Agent reads all image files
- ✅ Agent correctly identifies gorilla
- ✅ Final answer contains `/animals/xk92m/qz7fw.png`
- ✅ Accuracy score: 1.0

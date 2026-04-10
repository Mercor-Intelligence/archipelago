# Cohere Docker World Export

This directory is a runnable Docker world export.

You can build the world image and run tasks inside the container.

## Table of contents

- [Layout](#layout)
- [Run](#run)
- [Tool usage](#tool-usage)
- [Data model](#data-model-world-vs-task-public-vs-private)
- [MCP Lifecycle and Performance](#mcp-lifecycle-and-performance)
- [Planned Improvements](#planned-improvements)

## Layout

This export ships 4 core deliverables:

- `tasks/`: Set of JSON files + artifacts, each JSON implements a different task.
- `files/`: Copied as-is to docker, to be used directly with bash / UI Control.
- `tools/`: Tool code + related files (databases, JSONs, CSVs, etc)
- `Dockerfile`: Used to setup the docker image.

Also included are helper files for local setup:

- `.env.template`: All the secrets required to run the tools. Cohere is responsible for providing these secrets.
- `network_policy.json`: Specifies if the MCP apps require internet access or not.
- `docker-compose.yml`: Optional Docker compose wrapper

```text
.
├── Dockerfile
├── README.md
├── .env.template
├── docker-compose.yml
├── network_policy.json
├── tasks/
│   ├── <task_slug>.json
│   └── ...
├── files/
│   └── ...
└── tools/
    ├── start.sh
    ├── mcp.json
    ├── pyproject.toml
    ├── uv.lock
    ├── runner/
    ├── mcp_servers/
    ├── files/
    │   └── <task_slug>/...
    └── .apps_data/
        ├── <world_baseline>/...
        └── <task_slug>/...
```

## Run

You'll need provide all your secrets in `.env` and pick a task to run. Then you'll build the image, start the world, and select a task at runtime.

You can run either using Docker or Docker Compose.

Check `network_policy.json` to see whether the MCP servers require internet access (`requires_network: true`) or can run offline. We do not enforce network isolation in Docker commands so that the MCP client can communicate with the container. Apply your own network policies as needed.

Run (docker):

```bash
# Provide secrets
cp .env.template .env
# edit .env and set required values, e.g.:
# sed -i 's/MY_SECRET=.*/MY_SECRET=super_secret_value/' .env

# Pick a task, e.g.:
# TASK_SLUG="lawworld401-cbl-05-natural-prompt-6fa6d54b"
TASK_JSON="$(ls tasks/*.json | head -n 1)"
TASK_SLUG="$(basename "$TASK_JSON" .json)"

# Build the world image (one image per world)
docker build -t cohere-world:local .

# Run container (explicit startup command)
docker run --rm --env-file .env -p 8000:8000 cohere-world:local /app/tools/start.sh "$TASK_SLUG"

# Sanity check
curl -fsS http://localhost:8000/health
```

Run (docker compose):

```bash
# Provide secrets
cp .env.template .env
# edit .env and set required values, e.g.:
# sed -i 's/MY_SECRET=.*/MY_SECRET=super_secret_value/' .env

# Pick a task, e.g.:
# TASK_SLUG="lawworld401-cbl-05-natural-prompt-6fa6d54b"
TASK_JSON="$(ls tasks/*.json | head -n 1)"
TASK_SLUG="$(basename "$TASK_JSON" .json)"

# Build + run (same image, explicit startup command)
docker compose build
docker compose run --rm --service-ports world /app/tools/start.sh "$TASK_SLUG"

# Sanity check
curl -fsS http://localhost:8000/health
```

## Tool usage

MCP endpoint:

- `http://localhost:8000/mcp/` (mounted after `POST /apps` succeeds)

Ready signals:

- `GET /health` returning `OK` means the gateway server is up.
- Tools are ready when `tools/start.sh` finishes successfully (it waits for `/health`, then `POST`s `tools/mcp.json` to `/apps`).

Tool discovery:

- Use the ad hoc MCP client below to `list_tools()` and print tool names.
- Tool names are prefixed by server name (e.g. `word_*`, `code_execution_*`).

Exporting state (snapshots):

- `POST /data/snapshot` streams a `tar.gz` of the current state (both `filesystem/` and `.apps_data/`).
  - Example `curl -fsS -X POST http://localhost:8000/data/snapshot -o snapshot.tar.gz && tar -tzf snapshot.tar.gz`
- This is useful for debugging RL rollouts with their end of episode state.
- Heads up exports can be over 1 GB.

Ad hoc MCP client (prints all tool names):

```bash
# Assumes you have uv installed

uv run --with fastmcp --python 3.13 - <<'PY'
import asyncio
import os

from fastmcp import Client as FastMCPClient

port = 8000
mcp_url = f"http://localhost:{port}/mcp/"
config = {"mcpServers": {"gateway": {"transport": "http", "url": mcp_url}}}

async def main() -> None:
    client = FastMCPClient(config)
    async with client:
        tools_result = await client.session.list_tools()
        for tool in tools_result.tools:
            print(tool.name)

asyncio.run(main())
PY
```

## Data model (world vs task, public vs private)

This export builds one Docker image per world. At runtime, you select a **task** by running the provided startup command.

The start script resets the container to the world files, then loads in the specific task files. This means switching tasks does not leak state.

| scope | visibility | in zip                            | in container (baked)                   | in container (after `start.sh <task>`) |
| ----- | ---------- | --------------------------------- | -------------------------------------- | -------------------------------------- |
| world | public     | `files/**`                        | `/app/files/**`                        | `/app/files/**` (world baseline)       |
| world | private    | `tools/.apps_data/**`             | `/app/tools/.apps_data/**`             | `/.apps_data/**` (world baseline copy) |
| task  | public     | `tools/files/<task_slug>/**`      | `/app/tools/files/<task_slug>/**`      | `/app/files/**` (overlay)              |
| task  | private    | `tools/.apps_data/<task_slug>/**` | `/app/tools/.apps_data/<task_slug>/**` | `/.apps_data/**` (overlay)             |

`start.sh` lifecycle (high level):

- Backup world baselines once (`/app/_world_files_base`, `/app/_world_apps_data_base`)
- Reset runtime dirs to the world baseline (`/app/files`, `/.apps_data`)
- Overlay task-specific public/private data for the selected task slug

## MCP Lifecycle and Performance

This export runs each MCP server as an isolated OS process. Servers are started in parallel and exposed over HTTP on localhost. Once they are listening, the start script configures the MCP gateway via `POST /apps`, which warms connections and makes tools available immediately.

### What happens when you run a task

- `docker run <image> /app/tools/start.sh <task_slug>` runs the startup script directly.
- `start.sh` sets up the task filesystem overlay, then starts the runner and all MCP servers in parallel (background processes).
- The script waits for health + ports, then `POST /apps` with `tools/mcp.json`.
- After startup completes, the script supervises child processes; if any process exits, the container terminates (fail-fast semantics for orchestration).

### Benchmarks

Previously, MCP startup and `/apps` configuration could take **15–35s** due to sequential cold-start work being done at container runtime. In recent exports, build steps are moved to export/build time and MCP servers start in parallel.

On a typical developer machine, we observe:

- **`POST /apps`**: ~1-2s
- **`docker run` to `Startup complete!`**: ~4-6s

## Planned Improvements

Memory profile: Today RSS scales with the number of MCP OS processes. We're exploring two approaches:

- **Unifying the Python environment**: Reduce duplicated Python interpreters and OS overhead across MCP servers. This would shift RSS growth from \(O(N)\) toward \(O(1)\).
- **Lazy-loading MCP servers**: Start servers on demand at tool call time. This would shift RSS growth from \(O(N)\) toward \(O(K)\) for \(K\) servers running in parallel, which is likely only ever 1.

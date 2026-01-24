# Archipelago Environment

A headless RL environment gateway designed to run in a Docker container. It serves as a management layer for LLM agents, providing capabilities for Model Context Protocol (MCP) server orchestration, data population from S3, and state snapshotting.

## Features

- **MCP Gateway**: Hot-swappable gateway that routes requests to configured MCP servers. Supports dynamic reconfiguration of tools and resources.
- **Data Management**: 
  - **Population**: Download data from S3-compatible storage into local subsystems (`/filesystem`, `/.apps_data`).
  - **Snapshots**: Create `tar.gz` archives of the environment state and stream them back to the client or upload directly to S3.
- **Docker-First**: Designed to run as a containerized service with health checks and lifecycle management.

## Getting Started

### Prerequisites

- [Docker](https://www.docker.com/)
- [uv](https://github.com/astral-sh/uv) (for local development)

### Running with Docker Compose

1. **Set up environment variables:**

   ```bash
   cp .env.example .env
   # Can be left as-is for local development
   ```

2. **Start the environment:**

   ```bash
   docker-compose --ansi always --env-file .env up --build
   ```

   The server will be available at `http://localhost:8080`.

3. **Verify it's running:**

   ```bash
   curl http://localhost:8080/health
   # Should return: {"status": "ok"}
   ```

## Configuration

The application is configured via environment variables. You can set these in a `.env` file or pass them to the Docker container at runtime.

| Variable | Description | Default |
|----------|-------------|---------|
| `S3_SNAPSHOTS_BUCKET` | S3 bucket name for storing snapshots | `snapshots` |
| `S3_SNAPSHOTS_PREFIX` | Optional prefix for snapshot objects in S3 | `""` |
| `S3_DEFAULT_REGION` | Default AWS region for S3 operations | `us-west-2` |
| `S3_ACCESS_KEY_ID` | AWS access key ID | `None` |
| `S3_SECRET_ACCESS_KEY` | AWS secret access key | `None` |
| `S3_SESSION_TOKEN` | AWS session token (optional) | `None` |
| `FILESYSTEM_SUBSYSTEM_NAME`| Name of the filesystem subsystem root | `filesystem` |
| `APPS_DATA_SUBSYSTEM_NAME` | Name of the apps data subsystem root | `.apps_data` |

## API Reference

### Health Check

**GET** `/health`

Returns `200 OK` if the server is running.

**GET** `/docs`

Returns an HTML page of FastAPI generated API documentation

### Configure MCP Servers

**POST** `/apps`

Hot-swaps the MCP gateway with a new configuration. This mounts the resulting MCP gateway at `/mcp/`.

There are two ways to get MCP server code into the container:

1. **Git clone at runtime (easiest)** - Clone the repo and run the entrypoint in the command. This is the most flexible approach since you don't need to rebuild the image.

2. **Bake into the Docker image** - Add MCP server code to `./mcp_servers/` and rebuild the image. Better for production or when you need consistent versions.

> **Important Configuration Notes:**
> 1. **Always include `"transport": "stdio"`** - Required for stdio-based MCP servers
> 2. **The `cwd` path must exist inside the container** - Use paths relative to the container's working directory
> 3. **Environment variables** can be passed via the `env` field

**Option 1: Git Clone at Runtime (Recommended for Development)**

Clone the MCP server repo and run it in a single command:

```python
import requests

config = {
    "mcpServers": {
        "filesystem_server": {
            "transport": "stdio",
            "command": "sh",
            "args": ["-c", "git clone --depth 1 https://github.com/your-org/mcp-server.git /tmp/mcp && cd /tmp/mcp && python main.py"],
            "env": {
                "APP_FS_ROOT": "/filesystem"
            }
        }
    }
}
requests.post("http://localhost:8080/apps", json=config)
```

**Option 2: Pre-baked MCP Servers**

If you've added MCP server code to the image (in `./mcp_servers/`), reference it directly:

```python
config = {
    "mcpServers": {
        "filesystem_server": {
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
requests.post("http://localhost:8080/apps", json=config)
```

Now `http://localhost:8080/mcp/` will expose an MCP server an agent can connect to.

**Common Issues:**
- If MCP servers fail to start, check that `transport: "stdio"` is included
- If you get "command not found", verify the `cwd` path exists in the container
- Use `docker exec -it <container> ls ./mcp_servers/` to verify paths

### Populate Data

**POST** `/data/populate`

Upload a tar.gz archive directly to populate a subsystem. Streams to disk and extracts incrementally (constant memory usage).

**Request:** `multipart/form-data`

| Field | Type | Description |
|-------|------|-------------|
| `archive` | file | tar.gz archive to extract |
| `subsystem` | query param | Target: `filesystem`, `.apps_data`, or nested path (default: `filesystem`) |

**Example:**

```python
import requests

with open("data.tar.gz", "rb") as f:
    response = requests.post(
        "http://localhost:8080/data/populate",
        files={"archive": ("data.tar.gz", f, "application/gzip")},
        params={"subsystem": "filesystem"}
    )
print(response.json())  # {"objects_added": 42, "subsystem": "filesystem", "extracted_bytes": 1048576}
```

```bash
curl -X POST "http://localhost:8080/data/populate?subsystem=filesystem" \
  -F "archive=@data.tar.gz"
```

**POST** `/data/populate/s3`

Downloads data from S3 sources into subsystems.

**Payload:**

```json
{
  "sources": [
    {
      "url": "s3://bucket/path/to/data/",
      "subsystem": "filesystem"
    }
  ]
}
```

**Response:**

```json
{
  "objects_added": 42
}
```

### Snapshot Data

> **Note**: The environment outputs `.tar.gz` snapshots, but the grading system expects `.zip` files.

**POST** `/data/snapshot`

Streams a `tar.gz` snapshot of the environment state (filesystem and apps data) directly to the client.

**Example:**

```python
import requests

response = requests.post("http://localhost:8080/data/snapshot", stream=True)
with open("snapshot.tar.gz", "wb") as f:
    for chunk in response.iter_content(chunk_size=65536):
        f.write(chunk)
```

```bash
curl -X POST "http://localhost:8080/data/snapshot" -o snapshot.tar.gz
```

**POST** `/data/snapshot/s3`

Creates a snapshot and uploads it directly to the configured S3 bucket. Returns metadata including a pre-signed URL for downloading.

**Response:**

```json
{
  "snapshot_id": "snap_abc123",
  "s3_uri": "s3://bucket/prefix/snap_abc123.tar.gz",
  "presigned_url": "https://...",
  "size_bytes": 10485760
}
```

## MCP Servers

The project includes dependencies for several specialized MCP servers. These are defined in `pyproject.toml` dependency groups:

- `code-execution-server`: Pandas, NumPy, SciPy, etc.
- `websearch-server`: BeautifulSoup4, HTTPX
- `docs-server`: python-docx
- `pdf-server`: pypdf
- `sheets-server`: openpyxl
- `slides-server`: python-pptx
- `sql-execution-server`: pysqlite3, pandas
- `calendar-server`: icalendar

## Directory Structure

```
archipelago/environment/
├── runner/                 # Main application source code
│   ├── data/               # Data management (populate/snapshot)
│   ├── gateway/            # MCP gateway logic
│   └── utils/              # Utilities (S3, settings)
├── mcp_servers/            # (Expected location for MCP server scripts)
├── example.py              # Example client script
├── docker-compose.yml      # Docker composition
├── Dockerfile              # Docker build definition
└── pyproject.toml          # Dependencies and metadata
```

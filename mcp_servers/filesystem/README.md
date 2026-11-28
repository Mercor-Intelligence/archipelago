# Rls Filesystem MCP Server

Mercor RLS Filesystem MCP

## ArCo — Configuring Your App for Archipelago and RL Studio

### What is Archipelago?

RL Studio uses **[Archipelago](https://github.com/Mercor-Intelligence/archipelago)**, Mercor's open-source harness for running and evaluating AI agents against RL environments

Your MCP server runs inside an Archipelago environment, where AI agents connect to it via the MCP protocol to complete tasks.

### What is ArCo?

**ArCo** (short for **Archipelago Config**) is the configuration system for deploying your MCP server to Archipelago. It consists of two files that tell Archipelago how to build and run your application.

### Configuration Files

| File | Purpose |
|------|---------|
| `mise.toml` | **How to build and run your app** — lifecycle tasks (install, build, start, test) |
| `arco.toml` | **What infrastructure your app needs** — environment variables, secrets, runtime settings |

### Why ArCo?

Archipelago is deployed to multiple environments with different infrastructure requirements (Docker, Kubernetes, custom orchestrators). Rather than writing Dockerfiles or K8s manifests directly, you declare *what your app needs* in these config files, and RL Studio generates the appropriate deployment artifacts for each proprietary customer "target consumer".

You as a Mercor expert only need to write `mise.toml` and `arco.toml`, we write Dockerfiles, K8s manifests, etc. for you. 

### Mise: The Task Runner

**[Mise](https://mise.jdx.dev/)** is required for development. Install it first:

```bash
curl https://mise.run | sh
```

Mise is a polyglot tool manager -- it reads `mise.toml` and automatically installs the correct versions of Python, uv, and any other tools your project needs. You don't need to install Python or uv yourself.

**Run tasks with mise instead of calling tools directly:**

| Instead of... | Run... |
|---------------|--------|
| `uv sync --all-extras` | `mise run install` |
| `pytest` | `mise run test` |
| `uv run python main.py` | `mise run start` |
| `ruff check .` | `mise run lint` |

### Lifecycle Tasks (`mise.toml`)

The `mise.toml` file defines how to build and run your application:

```toml
[tools]
python = "3.13"
uv = "0.6.10"

[env]
_.python.venv = { path = ".venv", create = true }

[tasks.install]
description = "Install dependencies"
run = "uv sync --all-extras"

[tasks.build]
description = "Build the project"
run = "echo 'No build step required'"

[tasks.start]
description = "Start the MCP server"
run = "uv run python main.py"
depends = ["install"]

[tasks.test]
run = "pytest"

[tasks.lint]
run = "ruff check ."

[tasks.format]
run = "ruff format ."

[tasks.typecheck]
run = "basedpyright"
```

### Infrastructure Config (`arco.toml`)

The `arco.toml` file declares what infrastructure your app needs:

```toml
[arco]
source = "foundry_app"
name = "my-server"
version = "0.1.0"
env_base = "standard"

# Runtime environment: baked into container
[arco.env.runtime]
APP_FS_ROOT = "/filesystem"
INTERNET_ENABLED = "false"

# User-configurable parameters (shown in RL Studio UI)
[arco.env.runtime.schema.INTERNET_ENABLED]
type = "bool"
label = "Internet access"
description = "Allow the MCP server to make outbound network requests"

# Secrets: injected at runtime, never baked
[arco.secrets.host]
GITHUB_TOKEN = "RLS_GITHUB_READ_TOKEN"
```

### Environment Variable Matrix

ArCo uses a 2x3 matrix for environment variables:

| | Host (build orchestration) | Build (container build) | Runtime (container execution) |
|---|---|---|---|
| **Config** | `[arco.env.host]` | `[arco.env.build]` | `[arco.env.runtime]` |
| **Secret** | `[arco.secrets.host]` | `[arco.secrets.build]` | `[arco.secrets.runtime]` |

- **Config** values can be baked into containers
- **Secret** values are always injected at runtime, never baked into images

### Environment Variables: Local vs Production

**Important:** Environment variables must be set in two places — one for local development, one for production. This is current tech debt we're working to simplify.

| File | Purpose | When it's used |
|------|---------|----------------|
| `mise.toml` `[env]` | Local development | When you run `mise run start` locally |
| `arco.toml` `[arco.env.*]` | Production | When RL Studio deploys your container |

**How mise works:** Mise functions like [direnv](https://direnv.net/) — when you `cd` into a directory with a `mise.toml`, it automatically loads environment variables and activates the correct tool versions (Python, uv, etc.). You don't need to manually source anything.

**The rule:** If you add an environment variable, add it to **both files**:

```toml
# mise.toml — for local development
[env]
MY_NEW_VAR = "local_value"
```

```toml
# arco.toml — for production
[arco.env.runtime]
MY_NEW_VAR = "production_value"
```

**Do NOT use `.env` files.** The `mise.toml` + `arco.toml` system replaces `.env` entirely. These are the only two files you need for environment variable management.

### ArCo Environment Stages: host, build, runtime

Unlike `mise.toml` which has a single flat `[env]` section, ArCo separates environment variables into three stages based on *when* they're needed in the deployment pipeline. You must specify the correct stage for each variable.

| Stage | When Used | How It's Consumed | Example Variables |
|-------|-----------|-------------------|-------------------|
| `[arco.env.host]` | Before container build | Read by RL Studio orchestration layer | `REPO_URL`, `REPO_BRANCH`, `REPO_PATH` |
| `[arco.env.build]` | During `docker build` | Exported before install/build commands | `UV_COMPILE_BYTECODE`, `CFLAGS` |
| `[arco.env.runtime]` | When container runs | Baked into Dockerfile as `ENV` | `APP_FS_ROOT`, `INTERNET_ENABLED` |

**Stage Details:**

**Host Stage** (`[arco.env.host]`) — Used by RL Studio's build orchestrator (the "Report Engine") before any Docker commands. These variables tell RL Studio *how to fetch your code*:
- `REPO_URL` — Git repository to clone
- `REPO_BRANCH` — Branch to checkout (optional)
- `REPO_PATH` — Subdirectory containing your app (optional)

These are **never** injected into your container — they're consumed by infrastructure.

**Build Stage** (`[arco.env.build]`) — Available during `docker build` when running your `install` and `build` tasks. Exported as shell variables (via `export VAR=value`) before each command. Use for:
- Compiler flags (`CFLAGS`, `LDFLAGS`)
- Build-time feature toggles (`INSTALL_MEDICINE=true`)
- Package manager configuration (`UV_COMPILE_BYTECODE=1`)

These are **not** baked into the final image as `ENV` — they only exist during build.

**Runtime Stage** (`[arco.env.runtime]`) — Baked into the Dockerfile as `ENV` directives and available when your container runs. This is where most of your app configuration goes:
- `APP_FS_ROOT` — Filesystem root for your app
- `INTERNET_ENABLED` — Network policy flag
- `HAS_STATE` / `STATE_LOCATION` — Stateful app configuration
- Any custom app configuration

**Why the separation matters:** 
- Security: Host/build secrets don't leak into the final container image
- Performance: Build-time vars don't bloat the runtime environment
- Clarity: RL Studio knows exactly which vars to use at each pipeline stage

**Mapping mise.toml to arco.toml:** In local development, `mise.toml` simulates all three stages at once. When adding a new variable, consider which stage it belongs to:

```toml
# mise.toml — flat, everything available locally
[env]
APP_FS_ROOT = "/filesystem"
MY_API_URL = "http://localhost:8000"
```

```toml
# arco.toml — staged for production
[arco.env.runtime]
APP_FS_ROOT = "/filesystem"
MY_API_URL = "https://api.production.com"
```

### Secrets

Use `[arco.secrets.*]` for sensitive values like API keys, tokens, and passwords. Secrets are:
- **Never baked** into Docker images (excluded from Dockerfiles)
- **Masked** in logs and UI
- **Resolved at runtime** from AWS Secrets Manager by the MCP Core team's infrastructure

```toml
# arco.toml
[arco.secrets.runtime]
API_KEY = true              # Secret name matches env var name
DATABASE_URL = "db_password" # Custom secret name in AWS
```

**For local development:** Create a `mise.local.toml` file (gitignored) to set secret values:

```toml
# mise.local.toml — gitignored, never committed
[env]
API_KEY = "your-dev-api-key"
DATABASE_URL = "postgresql://localhost/devdb"
```

**To add a new secret:** Contact the MCP Core team. They will add the secret to AWS Secrets Manager and configure RL Studio to inject it at runtime.

### CI/CD Integration

This repository includes GitHub Actions for ArCo validation:

- **`arco-validate.yml`** — Validates your config on every PR
- **`foundry-service-sync.yml`** — Syncs your config to RL Studio on release

### Keeping Config Updated

| If you... | Update this |
|-----------|-------------|
| Changed install/build/run commands | `[tasks.*]` in `mise.toml` |
| Added a new environment variable | `[env]` in `mise.toml` AND `[arco.env.runtime]` in `arco.toml` |
| Need a new secret | `[arco.secrets.*]` in `arco.toml` |
| Want users to configure a variable | Add `[arco.env.runtime.schema.*]` |

---


## Tools

### Structured output

`list_files`, `read_text_file`, `search_files`, `get_file_metadata` and
`get_directory_tree` declare a typed `outputSchema` and return matching
`structuredContent` alongside their text. The text content block is exactly the
text the tool has always returned; the structured payload carries the same
facts, built from the same values. Failures these tools *return* (for example
`[not found: /path]`) come back with an `error` field holding the same message,
so the payload validates on every path. Optional fields are omitted rather than
sent as `null`. `read_image_file` returns image content and has no output
schema. See `utils/structured_output.py`.

### 1. `list_files`

List files and folders in the given path.

---

### 2. `read_image_file`

Read an image file from the filesystem.

---

### 3. `read_text_file`

Read the contents of a text file. Only files with a supported extension are readable
(see the `file_path` field description for the full list).

#### File size limits

There is **no general server-side size cap**. `read_text_file` reads whatever the file
contains, so the effective ceiling is the memory available to the container. Files
larger than 3&nbsp;GB (`LARGE_FILE_WARNING_BYTES`) are still read, but the server logs
a slow / high-memory warning. The one exception is `.eml` (MIME email), which is
capped at 25 MB (`EML_MAX_BYTES`) because parsing decodes the whole message —
including its base64 parts — in memory.

Callers cap a read with `max_size`, a byte count enforced against the file on disk
before any content is read:

| `max_size` | Behavior |
|------------|----------|
| `0` (default) | No `max_size` limit. The file is read whatever its size (the 25 MB `.eml` cap still applies). |
| positive, file ≤ `max_size` | The file is read in full. The bound is inclusive. |
| positive, file > `max_size` | `ValueError: File too large: <actual> bytes exceeds max_size of <max_size> bytes`. No content is returned — the read is never silently truncated. |
| negative | `ValueError: max_size must be >= 0 (0 means no limit), got <value>` |
| non-integer | `ValueError: max_size must be an integer number of bytes` |

`max_size` counts bytes on disk, not decoded characters, so a multi-byte UTF-8 file
can exceed a limit that its character count would satisfy.

---

### 4. `search_file_contents` (keyword search, off by default)

A plain keyword search inside files: PDF, Documents, Presentations, Spreadsheets, CSV, text, Markdown
and email. The query is one or more words (case-insensitive, punctuation ignored, no
operators). A page, slide, block of rows or block of lines matches if it contains any
of the words; the ones where they occur more often are listed first (BM25).
Each match gives the file path, the page / slide / sheet and rows / paragraphs / lines
where it was found, and how many times the words occur there. There is no text excerpt:
to read a match the agent opens the file. Every response also
says how many files were searched and which ones could not be read (for example
scanned PDFs, which have no text layer). The tool never returns an error for a missing
or partial index; it says so in the text instead.

It is controlled by one runtime setting, `KEYWORD_SEARCH_CONFIG`, a JSON object
(type `json` in `arco.toml`, so it can be set in RL Studio). Every key is optional:

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `false` | Serve the tool and prepare the index (below). When false the agent never sees it and nothing is built. |
| `folder` | `"/filesystem"` | What to index, including every subfolder. |
| `file_types` | `["txt","md","csv","eml","docx","pptx","xlsx","pdf"]` | Which types to index. |
| `max_seconds_per_file` | `30` | A file still being read after this is kept as `partial`. |
| `max_total_text_mb` | `500` | Total text the index may hold, across the build, an uploaded index and every refresh. Files past it are skipped and reported as not covered. |
| `max_build_minutes` | `40` | The build stops here and records the remaining files as skipped. |
| `wait_for_build_seconds` | `40` | How long a search waits for a backup build (below). Keep it under the agent's per-tool-call timeout (60 s for `loop_final_answer_agent`). |
| `max_refresh_seconds` | `15` | Most time one search spends indexing files added or changed during the run (below). |

Broken JSON leaves the feature off. A bad value for one key uses that key's default.
Both are logged as warnings.

**When the index is prepared.** The app's `populate` task (`mise.toml`) prepares it.
Studio runs `populate` after the world and task files are copied into `/filesystem` and
before the agent starts. If keyword indexes were uploaded to `/.apps_data/filesystem/`
(see below), `populate` copies them and indexes only the files they do not cover;
otherwise it builds one from scratch. `populate` always exits 0: a failure is logged and
the run carries on, because a failing `populate` can mark the whole app unavailable.

**Backup.** If a search finds no index covering any file (for example where `populate`
did not run), the server starts the same build once and waits up to
`wait_for_build_seconds`. If it is not finished, the answer says so and lists partial
results; later searches see more.

**Files added or changed during the run.** Every search first compares the files
under `folder` with the index by size and modified time. It
indexes new and changed files (cheapest first) and drops deleted ones, then searches,
so files the agent or a virtual coworker writes into `/filesystem` are searchable on
the next search. A search spends at most `max_refresh_seconds` on this; files left
over are named in the answer and picked up by the following searches. Files an
earlier build left out for a limit of the whole index (time or total text) are
retried the same way; files that failed on their own (no text layer, broken) are not.

**Where the index lives.** Only in a private scratch folder, `/tmp/keyword_index-<uid>/`
(folder `700`, file `600`), never in `/.apps_data`: Studio saves `/.apps_data` with
every run, and uploads stay untouched. The folder is refused if it is a link or owned
by another user, so another app in the sandbox can neither read nor replace it.

**Building an index ahead of time.** For large worlds, build once and upload it, so
each run copies it in seconds instead of building. `scripts/build_keyword_index.py`
uses the same code and format as the `populate` build, and prints every file it could
not read:

```bash
uv run python scripts/build_keyword_index.py ./world_files world.sqlite
uv run python scripts/build_keyword_index.py ./turn_2_files turn_2.sqlite --prefix turn_inputs/turn_2
```

Upload the result under `.apps_data/filesystem/`. `--prefix` is the folder the files
will sit in under `/filesystem`, so the paths in results match the files on disk.
`populate` checks the copy file by file: a file whose path and size match is kept, one
whose size differs is re-indexed, a missing one is added, and an entry with no file is
dropped. A `.sqlite` file that is not a current keyword index is skipped. With a wrong
`--prefix` nothing matches, so every file is indexed again (slow, but correct) and the
log says to check the prefix. Several uploads stay separate files (indexes keep no text,
so they cannot be merged); each file is counted once.

**Index format (version 3).** One SQLite file: `meta(key, value)` with `format` =
`keyword-index`, `version` = `3` and `state` = `building` / `complete` / `failed`
(plus `built_by`, `build_seconds` and `built_at` once complete);
`files(path, size, mtime_ns, status, detail)` with one row per chosen file and `status` one of
`pending`, `indexed`, `partial`, `skipped`, `failed`; a contentless FTS5 table
`sections(text)` that stores only the documents index, never the document text; and
`sections_meta(id, path, location)` saying where each section came from. Paths are
relative to `/filesystem`, '/'-separated, with no leading slash. SQLite files without
a version 3 `meta` table are ignored.

**Readers.** Text, CSV, Markdown and email use the standard library, and so do Documents,
Presentations and Spreadsheets, which are zip archives of XML. PDF uses `pypdfium2`, loaded only
when a PDF is read. Old binary formats (`.doc`, `.xls`, `.ppt`) and OCR are not supported.
Documents files store no page numbers, so Documents matches give a paragraph range instead.
Safety limits: at most 20 MB of text per file (the file is recorded as `partial`),
Office files whose parts unpack past 256 MB are refused as compression bombs, text
with NUL bytes is refused as binary, and long text is indexed in rows of about
20,000 characters.

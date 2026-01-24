# MCP Mock Product Specification: Filesystem Server

---

## 0. App Classification

| Property | Value | Notes |
|-----------|--------|-------|
| **Stateful** | Yes | Reads from a persistent filesystem directory |
| **Mutable** | No | Read-only operations (does not modify filesystem) |
| **Visual** | No | Does not require a GUI or Computer Use |
| **Private** | No | Does not require authentication or authorization |
| **Mock Type** | Full Mock | Operates on local filesystem; no external API dependencies |
| **Setup Flags** | `APP_FS_ROOT` | Environment variable for root directory configuration |

---

## 1. Title

**Filesystem Server MCP**

---

## 2. Overview

The Filesystem Server provides read-only file and directory listing capabilities within a sandboxed filesystem environment. It enables AI agents to browse available files, check file existence and types, and read image files for vision API usage — all within a secure, sandboxed root directory.

---

## 3. Approach

### Data Source

- **Filesystem-based**: All operations target files within a configurable root directory (`APP_FS_ROOT`)
- **No external APIs**: Self-contained local filesystem operations
- **Read-only**: No file creation, modification, or deletion

### Data Modes

| Mode | Description |
|------|-------------|
| **Clean Mode** | Empty filesystem; files must be placed in root directory externally |
| **Offline Mode** | Reactive-only; lists and reads files that exist at request time |

### Core Architecture

| Component | Technology | Purpose |
|-----------|------------|---------|
| Directory Listing | Python os | List files and folders with metadata |
| MIME Detection | Python mimetypes | Detect file types by extension |
| Image Reading | FastMCP Image | Read image files for vision APIs |

### Security

- **Path traversal protection**: All paths resolved relative to `APP_FS_ROOT`
- **Sandboxed operations**: Cannot access files outside the root directory
- **Read-only**: No write operations supported

---

## 4. Prioritized User Archetypes and Workflows

| User Type | Goal / Workflow | Primary Tools |
|-----------|-----------------|---------------|
| **File Browser** | Explore available files before processing | `list_files` |
| **File Validator** | Check if files exist and verify types | `list_files` |
| **Image Analyzer** | Read images for vision API analysis | `read_image_file` |
| **Workflow Orchestrator** | Discover files for other MCP servers to process | `list_files` |

---

## 5. Extended Functionality (Future Builds)

| Feature | Description | Priority |
|---------|-------------|----------|
| **Read Text Files** | Read contents of text files | High |
| **File Search** | Search for files by name pattern | Medium |
| **File Metadata** | Get detailed file metadata (created, modified, permissions) | Medium |
| **Directory Tree** | Get recursive directory structure | Medium |
| **File Watch** | Monitor directory for changes | Low |
| **Copy/Move** | Copy or move files within root | Low |

---

## 6. Build Considerations

| Area | Description |
|------|-------------|
| **Path Resolution** | Both absolute and relative paths treated as relative to `APP_FS_ROOT` |
| **Path Validation** | All paths normalized to prevent directory escape |
| **MIME Detection** | Automatic based on file extension |
| **Image Formats** | Supports PNG, JPG, JPEG, GIF, WEBP |
| **Read-Only** | No file modification capabilities |
| **Dependencies** | `fastmcp` (for Image type) |

---

## 7. Example MCP Commands

### List Files

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `list_files` | List directory contents | `path` | Formatted list of files and folders |

```
list_files("/")

→ 'data' (folder)
  'reports' (folder)
  'config.json' (application/json file) 1024 bytes
  'readme.txt' (text/plain file) 512 bytes
  'chart.png' (image/png file) 45678 bytes

list_files("/data")

→ 'input.csv' (text/csv file) 2048 bytes
  'output.json' (application/json file) 768 bytes
  'processed' (folder)

list_files("/nonexistent")

→ [not found: /nonexistent]
```

### Read Image File

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `read_image_file` | Read image for vision APIs | `file_path` | FastMCP Image object |

```
read_image_file("/images/chart.png")

→ [Image: PNG, format=png]

read_image_file("/screenshots/dashboard.jpg")

→ [Image: JPEG, format=jpeg]

read_image_file("/photos/profile.webp")

→ [Image: WEBP, format=webp]
```

### Output Formats

**Directory Listing:**

| Item Type | Format |
|-----------|--------|
| Folder | `'<name>' (folder)` |
| File | `'<name>' (<mime_type> file) <size> bytes` |
| Not Found | `[not found: <path>]` |
| Permission Denied | `[permission denied: <path>]` |
| Not a Directory | `[not a directory: <path>]` |
| Empty | `No items found` |

**Supported Image Formats:**

| Extension | MIME Type | FastMCP Format |
|-----------|-----------|----------------|
| `.png` | image/png | png |
| `.jpg` | image/jpeg | jpeg |
| `.jpeg` | image/jpeg | jpeg |
| `.gif` | image/gif | gif |
| `.webp` | image/webp | webp |

### Common MIME Types

| Extension | MIME Type |
|-----------|-----------|
| `.txt` | text/plain |
| `.pdf` | application/pdf |
| `.py` | text/x-python |
| `.json` | application/json |
| `.csv` | text/csv |
| `.xlsx` | application/vnd.openxmlformats-officedocument.spreadsheetml.sheet |
| `.docx` | application/vnd.openxmlformats-officedocument.wordprocessingml.document |
| Unknown | unknown |

---

## 8. Notes and References

### Environment Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_FS_ROOT` | `/filesystem` | Root directory for all filesystem operations |
| `TOOLS` | All enabled | Comma-separated list to enable specific tools only |

### Key Behaviors

- **Path Normalization**: All paths normalized with `os.path.normpath` before resolution
- **Sandboxing**: Cannot escape root directory via `..` or absolute paths
- **File Size**: Displayed in bytes for all files
- **Image Reading**: Returns native FastMCP Image type for direct use with vision APIs

### Use Cases

1. **Pre-Processing Discovery**: List available files before processing with PDF/Docs/Sheets servers
2. **File Validation**: Verify files exist and check their types before operations
3. **Image Analysis**: Read images for vision AI processing
4. **Workflow Integration**: Discover files dynamically for multi-step workflows

### Dependencies

- [FastMCP](https://github.com/jlowin/fastmcp) — MCP server framework with Image type support

---



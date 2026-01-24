# MCP Mock Product Specification: Code Execution Server

---

## 0. App Classification

| Property | Value | Notes |
|-----------|--------|-------|
| **Stateful** | Yes | Executes commands in a persistent filesystem directory |
| **Mutable** | Yes | Can create, modify, and delete files through shell commands |
| **Visual** | No | Does not require a GUI or Computer Use |
| **Private** | No | Does not require authentication or authorization |
| **Mock Type** | Full Mock | Executes real shell commands in sandboxed environment |
| **Setup Flags** | `APP_FS_ROOT`, `CODE_EXEC_COMMAND_TIMEOUT` | Environment variables for configuration |

---

## 1. Title

**Code Execution Server MCP**

---

## 2. Overview

The Code Execution Server provides a sandboxed environment for executing shell commands and Python code with pre-installed scientific computing packages. It enables AI agents to run Python scripts, manipulate files, install packages, and perform data analysis within a persistent filesystem environment with configurable timeout limits.

---

## 3. Approach

### Data Source

- **Filesystem-based**: Commands execute in a persistent directory (`APP_FS_ROOT`)
- **Real Execution**: Actual shell command execution (not simulated)
- **Pre-installed Packages**: Scientific computing stack available by default

### Data Modes

| Mode | Description |
|------|-------------|
| **Clean Mode** | Empty filesystem; files created during execution persist |
| **Offline Mode** | Files and installed packages persist across command executions |

### Core Architecture

| Component | Technology | Purpose |
|-----------|------------|---------|
| Shell Executor | subprocess | Execute shell commands |
| Python Runtime | Python 3.x | Run Python scripts |
| Package Manager | uv/pip | Install additional packages |
| Timeout Handler | Signal/threading | Enforce execution time limits |

### Security

- **Sandboxed directory**: Commands scoped to `APP_FS_ROOT`
- **Timeout enforcement**: Commands terminated after timeout period
- **Network access**: Available for downloading dependencies

---

## 4. Prioritized User Archetypes and Workflows

| User Type | Goal / Workflow | Primary Tools |
|-----------|-----------------|---------------|
| **Data Scientist** | Run Python analysis scripts | `code_exec` |
| **File Manager** | Create and manipulate files | `code_exec` |
| **Package Installer** | Install Python dependencies | `code_exec` |
| **Script Runner** | Execute shell commands | `code_exec` |
| **Visualization Creator** | Generate plots and charts | `code_exec` |

---

## 5. Extended Functionality (Future Builds)

| Feature | Description | Priority |
|---------|-------------|----------|
| **Persistent Sessions** | Maintain Python session state across calls | High |
| **Virtual Environments** | Isolated package environments | Medium |
| **Output Streaming** | Stream command output in real-time | Medium |
| **Resource Limits** | Memory and CPU limits | Medium |
| **Jupyter Support** | Execute Jupyter notebook cells | Low |
| **Multiple Languages** | Support for R, Node.js, etc. | Low |

---

## 6. Build Considerations

| Area | Description |
|------|-------------|
| **Working Directory** | Commands execute in `APP_FS_ROOT` (default: `/filesystem`) |
| **Timeout** | Default 300 seconds; configurable via `CODE_EXEC_COMMAND_TIMEOUT` |
| **Error Handling** | Returns stdout, stderr, and exit code |
| **File Persistence** | All created files persist across calls |
| **Package Installation** | Use `uv pip install` for fast package installation |
| **Dependencies** | Pre-installed scientific computing stack |

---

## 7. Example MCP Commands

### Execute Python Code

| Command | Purpose | Input | Output |
|---------|---------|-------|--------|
| `code_exec` | Run shell/Python commands | `code` | stdout output or error message |

```
code_exec("python -c 'print(sum(range(100)))'")

→ 4950

code_exec("python -c 'import numpy as np; print(np.array([1,2,3,4,5]).mean())'")

→ 3.0
```

### File Operations

```
code_exec("echo 'Hello World' > greeting.txt && cat greeting.txt")

→ Hello World

code_exec("ls -la")

→ total 8
  drwxr-xr-x  2 user user 4096 Nov  9 12:00 .
  drwxr-xr-x  3 user user 4096 Nov  9 11:00 ..
  -rw-r--r--  1 user user   12 Nov  9 12:00 greeting.txt
```

### Install Packages

```
code_exec("uv pip install requests")

→ Resolved 5 packages in 0.5s
  Installed 5 packages in 0.2s
   + certifi==2023.7.22
   + charset-normalizer==3.3.2
   + idna==3.6
   + requests==2.31.0
   + urllib3==2.1.0

code_exec("python -c 'import requests; print(requests.__version__)'")

→ 2.31.0
```

### Data Analysis

```
code_exec("""
python << 'EOF'
import pandas as pd
import numpy as np

# Create sample data
data = pd.DataFrame({
    'name': ['Alice', 'Bob', 'Charlie'],
    'age': [25, 30, 35],
    'salary': [50000, 60000, 70000]
})

print(data.describe())
EOF
""")

→              age        salary
  count   3.000000      3.000000
  mean   30.000000  60000.000000
  std     5.000000  10000.000000
  min    25.000000  50000.000000
  25%    27.500000  55000.000000
  50%    30.000000  60000.000000
  75%    32.500000  65000.000000
  max    35.000000  70000.000000
```

### Output Formats

**Success:**
```
<stdout output from command>
```

**Timeout:**
```
Command execution timed out after 300 seconds
```

**Error:**
```
<stdout output if any>

Error output:
<stderr output>

Command failed with exit code <code>
```

### Pre-installed Python Packages

| Package | Purpose |
|---------|---------|
| `matplotlib` | Plotting and visualization |
| `numpy` | Numerical computing |
| `pandas` | Data analysis and manipulation |
| `scipy` | Scientific computing |
| `seaborn` | Statistical data visualization |
| `statsmodels` | Statistical models |
| `plotly` | Interactive visualizations |
| `scikit-learn` | Machine learning |
| `xgboost` | Gradient boosting |

---

## 8. Notes and References

### Environment Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_FS_ROOT` | `/filesystem` | Working directory for code execution |
| `CODE_EXEC_COMMAND_TIMEOUT` | `300` | Maximum execution time in seconds |
| `TOOLS` | All enabled | Comma-separated list to enable specific tools only |

### Key Behaviors

- **Persistent Filesystem**: Files created during execution persist across calls
- **Network Access**: Commands can download files and install packages
- **Shell Environment**: Full shell access (bash/sh)
- **Timeout Enforcement**: Long-running commands terminated automatically

### Use Cases

1. **Data Analysis**: Run pandas/numpy scripts for data processing
2. **Visualization**: Generate matplotlib/seaborn plots
3. **File Processing**: Manipulate files with shell commands
4. **Package Installation**: Install Python dependencies as needed
5. **Script Testing**: Test Python scripts in isolated environment
6. **ETL Pipelines**: Extract, transform, load data workflows

### Dependencies

- Python 3.x runtime
- Pre-installed scientific computing packages

---



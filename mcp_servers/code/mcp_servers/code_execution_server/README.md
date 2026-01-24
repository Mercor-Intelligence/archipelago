# Code Execution MCP Server

A sandboxed environment for executing shell commands and Python code with pre-installed scientific computing packages.

## Tools

### `code_exec`

Execute shell commands in a sandboxed bash environment.

**Parameters:**
| Name | Type | Default | Description |
|------|------|---------|-------------|
| `action` | `"help"` \| `"exec"` | `"exec"` | Action: 'help' for usage info, 'exec' to run code |
| `code` | string \| null | null | Shell command to execute (required for exec action) |

**Examples:**

```bash
# Run a simple command
code_exec(action="exec", code="echo 'Hello World'")

# Run Python with pre-installed packages
code_exec(action="exec", code="python -c 'import numpy as np; print(np.array([1,2,3]).mean())'")

# Get help
code_exec(action="help")
```

**Pre-installed Python Packages:**

- numpy
- pandas
- matplotlib
- scipy
- scikit-learn

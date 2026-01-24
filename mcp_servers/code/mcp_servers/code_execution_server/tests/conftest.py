"""Shared fixtures for code execution server tests."""

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))


def _run_command_without_proot(
    command: str, timeout: int, cwd: str
) -> subprocess.CompletedProcess[str]:
    """Run a command directly without proot (for testing).

    This simulates what proot would do but runs commands directly,
    allowing tests to run without proot installed.
    """
    result = subprocess.run(
        ["sh", "-c", command],
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, command, result.stdout, result.stderr
        )
    return result


@pytest.fixture
def temp_workspace(tmp_path):
    """Create a temporary workspace directory for code execution.

    Args:
        tmp_path: pytest's temporary directory fixture

    Yields:
        Path: Path to the temporary workspace
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    yield workspace


@pytest.fixture
def mock_fs_root(temp_workspace):
    """Mock the FS_ROOT and command execution to use temp workspace without proot.

    This fixture:
    1. Sets FS_ROOT to the temp workspace
    2. Mocks _run_sandboxed_command to run commands directly (no proot needed)

    Args:
        temp_workspace: Temporary workspace fixture

    Yields:
        str: Path to the temporary workspace as string
    """
    workspace_path = str(temp_workspace)

    def mock_run_sandboxed(
        command: str, timeout: int
    ) -> subprocess.CompletedProcess[str]:
        return _run_command_without_proot(command, timeout, workspace_path)

    with patch("tools.code_exec.FS_ROOT", workspace_path):
        with patch("tools.code_exec._run_sandboxed_command", mock_run_sandboxed):
            yield workspace_path


@pytest.fixture
def mock_short_timeout():
    """Mock timeout to a short value for timeout testing.

    Yields:
        str: Short timeout value
    """
    with patch("tools.code_exec.CODE_EXEC_COMMAND_TIMEOUT", "2"):
        yield "2"


@pytest.fixture
def workspace_with_test_file(temp_workspace):
    """Create a workspace with a test file.

    Args:
        temp_workspace: Temporary workspace fixture

    Returns:
        tuple: (workspace_path, test_file_path)
    """
    test_file = temp_workspace / "test.txt"
    test_file.write_text("Hello, World!")
    return temp_workspace, test_file


@pytest.fixture
def workspace_with_python_script(temp_workspace):
    """Create a workspace with a Python script.

    Args:
        temp_workspace: Temporary workspace fixture

    Returns:
        tuple: (workspace_path, script_path)
    """
    script = temp_workspace / "script.py"
    script.write_text("""
import sys
print("Python script executed")
print(f"Args: {sys.argv[1:]}")
""")
    return temp_workspace, script


@pytest.fixture
def workspace_with_data_file(temp_workspace):
    """Create a workspace with a CSV data file.

    Args:
        temp_workspace: Temporary workspace fixture

    Returns:
        tuple: (workspace_path, csv_path)
    """
    csv_file = temp_workspace / "data.csv"
    csv_file.write_text("""name,age,city
Alice,30,NYC
Bob,25,SF
Charlie,35,LA
""")
    return temp_workspace, csv_file

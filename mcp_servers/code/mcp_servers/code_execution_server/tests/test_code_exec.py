"""Comprehensive unit tests for code_exec tool."""

import os
import signal
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from models.code_exec import CodeExecRequest
from tools.code_exec import _run_sandboxed_command, code_exec, verify_proot_available

# Import assertion helpers
from .helpers.assertions import (
    assert_contains_stderr,
    assert_empty_output,
    assert_error,
    assert_exit_code,
    assert_file_created,
    assert_output_lines,
    assert_python_output,
    assert_success,
    assert_timeout,
)


def exec_cmd(code: str) -> CodeExecRequest:
    """Helper to create a CodeExecRequest for exec action."""
    return CodeExecRequest(action="exec", code=code)


# ============================================================================
# Configuration Tests
# ============================================================================


class TestConfiguration:
    """Test environment variable configuration."""

    @pytest.mark.asyncio
    async def test_default_fs_root(self):
        """Test that default FS_ROOT is used when not set."""
        with patch.dict(os.environ, {}, clear=False):
            with patch("tools.code_exec.FS_ROOT", "/filesystem"):
                result = await code_exec(exec_cmd("pwd"))
                # Should try to execute in /filesystem (may fail if doesn't exist)
                assert result.action == "exec"

    @pytest.mark.asyncio
    async def test_custom_fs_root(self, mock_fs_root):
        """Test that custom FS_ROOT is respected."""
        result = await code_exec(exec_cmd("pwd"))
        assert_success(result)
        assert mock_fs_root in (result.output or "")

    @pytest.mark.asyncio
    async def test_default_timeout(self, mock_fs_root):
        """Test that default timeout (300s) is used."""
        with patch("tools.code_exec.CODE_EXEC_COMMAND_TIMEOUT", "300"):
            result = await code_exec(exec_cmd("echo 'test'"))
            assert_success(result, "test")

    @pytest.mark.asyncio
    async def test_custom_timeout(self, mock_fs_root):
        """Test that custom timeout is respected."""
        with patch("tools.code_exec.CODE_EXEC_COMMAND_TIMEOUT", "5"):
            result = await code_exec(exec_cmd("echo 'quick'"))
            assert_success(result, "quick")

    @pytest.mark.asyncio
    async def test_invalid_timeout_value(self, mock_fs_root):
        """Test handling of invalid timeout configuration."""
        with patch("tools.code_exec.CODE_EXEC_COMMAND_TIMEOUT", "not_a_number"):
            result = await code_exec(exec_cmd("echo 'test'"))
            assert_error(result, "configuration error")
            assert "invalid timeout" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_negative_timeout_value(self, mock_fs_root):
        """Test handling of negative timeout value."""
        with patch("tools.code_exec.CODE_EXEC_COMMAND_TIMEOUT", "-10"):
            result = await code_exec(exec_cmd("echo 'test'"))
            # Should execute (negative timeout may be handled by subprocess)
            assert result.action == "exec"

    @pytest.mark.asyncio
    async def test_working_directory_not_found(self):
        """Test error handling when working directory doesn't exist."""
        with patch("tools.code_exec.FS_ROOT", "/nonexistent/directory/path"):
            result = await code_exec(exec_cmd("echo 'test'"))
            assert_error(result, "configuration error")
            assert "working directory not found" in (result.error or "").lower()


# ============================================================================
# Proot Verification Tests
# ============================================================================


class TestProotVerification:
    """Test proot availability verification."""

    def test_verify_proot_available_success(self):
        """Test verify_proot_available when proot is found."""
        with patch("shutil.which", return_value="/usr/bin/proot"):
            # Should not raise
            verify_proot_available()

    def test_verify_proot_available_not_found(self):
        """Test verify_proot_available raises when proot is not found."""
        with patch("shutil.which", return_value=None):
            with pytest.raises(RuntimeError) as exc_info:
                verify_proot_available()
            assert "proot is required" in str(exc_info.value)
            assert "not found" in str(exc_info.value)


# ============================================================================
# Sandboxed Command Tests
# ============================================================================


class TestSandboxedCommand:
    """Test _run_sandboxed_command function directly."""

    def test_run_sandboxed_command_timeout(self, tmp_path):
        """Test that timeout handling works correctly in sandboxed command."""
        with patch("tools.code_exec.FS_ROOT", str(tmp_path)):
            # Mock Popen to simulate timeout
            mock_process = MagicMock()
            mock_process.pid = 12345
            mock_process.communicate.side_effect = [
                subprocess.TimeoutExpired("sleep 100", 1),
                ("", ""),  # Second call after kill
            ]

            with patch("subprocess.Popen", return_value=mock_process):
                with patch("os.getpgid", return_value=12345):
                    with patch("os.killpg") as mock_killpg:
                        with pytest.raises(subprocess.TimeoutExpired):
                            _run_sandboxed_command("sleep 100", 1)
                        # Verify process group was killed
                        mock_killpg.assert_called_once_with(12345, signal.SIGKILL)

    def test_run_sandboxed_command_timeout_killpg_oserror(self, tmp_path):
        """Test timeout handling when killpg raises OSError."""
        with patch("tools.code_exec.FS_ROOT", str(tmp_path)):
            mock_process = MagicMock()
            mock_process.pid = 12345
            mock_process.communicate.side_effect = [
                subprocess.TimeoutExpired("sleep 100", 1),
                ("", ""),
            ]

            with patch("subprocess.Popen", return_value=mock_process):
                with patch("os.getpgid", return_value=12345):
                    with patch("os.killpg", side_effect=OSError("Process group gone")):
                        with pytest.raises(subprocess.TimeoutExpired):
                            _run_sandboxed_command("sleep 100", 1)
                        # Verify fallback to process.kill was called
                        mock_process.kill.assert_called_once()

    def test_run_sandboxed_command_success(self, tmp_path):
        """Test successful command execution."""
        with patch("tools.code_exec.FS_ROOT", str(tmp_path)):
            mock_process = MagicMock()
            mock_process.pid = 12345
            mock_process.returncode = 0
            mock_process.communicate.return_value = ("output", "")

            with patch("subprocess.Popen", return_value=mock_process):
                result = _run_sandboxed_command("echo test", 10)
                assert result.stdout == "output"
                assert result.returncode == 0

    def test_run_sandboxed_command_failure(self, tmp_path):
        """Test command execution failure."""
        with patch("tools.code_exec.FS_ROOT", str(tmp_path)):
            mock_process = MagicMock()
            mock_process.pid = 12345
            mock_process.returncode = 1
            mock_process.communicate.return_value = ("", "error output")

            with patch("subprocess.Popen", return_value=mock_process):
                with pytest.raises(subprocess.CalledProcessError) as exc_info:
                    _run_sandboxed_command("false", 10)
                assert exc_info.value.returncode == 1


# ============================================================================
# Missing Code Parameter Tests
# ============================================================================


class TestMissingCodeParameter:
    """Test handling of missing code parameter."""

    @pytest.mark.asyncio
    async def test_exec_with_none_code(self):
        """Test exec action with code=None returns error."""
        result = await code_exec(CodeExecRequest(action="exec", code=None))
        assert_error(result)
        assert result.action == "exec"
        assert "required" in (result.error or "").lower()
        assert "code" in (result.error or "").lower()


# ============================================================================
# Python Code Detection Tests
# ============================================================================


class TestPythonCodeDetection:
    """Test detection of raw Python code passed to shell executor."""

    @pytest.mark.asyncio
    async def test_python_import_statement(self, mock_fs_root):
        """Test that Python import statement is detected."""
        result = await code_exec(exec_cmd("import os"))
        assert_error(result)
        assert "raw python code" in (result.error or "").lower()
        assert "python -c" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_python_from_import(self, mock_fs_root):
        """Test that Python from import is detected."""
        result = await code_exec(exec_cmd("from pathlib import Path"))
        assert_error(result)
        assert "raw python code" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_python_def_statement(self, mock_fs_root):
        """Test that Python def statement is detected."""
        result = await code_exec(exec_cmd("def my_function():"))
        assert_error(result)
        assert "raw python code" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_python_class_statement(self, mock_fs_root):
        """Test that Python class statement is detected."""
        result = await code_exec(exec_cmd("class MyClass:"))
        assert_error(result)
        assert "raw python code" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_python_async_def(self, mock_fs_root):
        """Test that Python async def is detected."""
        result = await code_exec(exec_cmd("async def my_async_func():"))
        assert_error(result)
        assert "raw python code" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_python_decorator(self, mock_fs_root):
        """Test that Python decorator is detected."""
        result = await code_exec(exec_cmd("@property"))
        assert_error(result)
        assert "raw python code" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_imagemagick_import_with_flag(self, mock_fs_root):
        """Test that ImageMagick import with flags is NOT detected as Python."""
        # import -window root screenshot.png is ImageMagick, not Python
        result = await code_exec(exec_cmd("import -window root screenshot.png"))
        # Should attempt to execute (may fail if import not installed, but not Python error)
        assert result.action == "exec"
        if not result.success:
            assert "raw python code" not in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_imagemagick_import_with_path(self, mock_fs_root):
        """Test that ImageMagick import with path is NOT detected as Python."""
        result = await code_exec(exec_cmd("import /tmp/screenshot.png"))
        assert result.action == "exec"
        if not result.success:
            assert "raw python code" not in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_imagemagick_import_image_extension(self, mock_fs_root):
        """Test that ImageMagick import with image extension is NOT detected as Python."""
        for ext in ["png", "jpg", "jpeg", "gif", "bmp", "tiff", "webp", "pdf"]:
            result = await code_exec(exec_cmd(f"import screenshot.{ext}"))
            assert result.action == "exec"
            if not result.success:
                assert "raw python code" not in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_python_dotted_import(self, mock_fs_root):
        """Test that Python dotted import is detected."""
        result = await code_exec(exec_cmd("import os.path"))
        assert_error(result)
        assert "raw python code" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_python_import_as(self, mock_fs_root):
        """Test that Python import as is detected."""
        result = await code_exec(exec_cmd("import numpy as np"))
        assert_error(result)
        assert "raw python code" in (result.error or "").lower()


# ============================================================================
# OSError Handling Tests
# ============================================================================


class TestOSErrorHandling:
    """Test OSError handling in code execution."""

    @pytest.mark.asyncio
    async def test_oserror_during_execution(self, mock_fs_root):
        """Test that OSError during command execution is handled."""
        with patch(
            "tools.code_exec._run_sandboxed_command",
            side_effect=OSError("Permission denied"),
        ):
            result = await code_exec(exec_cmd("echo test"))
            assert_error(result, "system error")
            assert "os error" in (result.error or "").lower()

    @pytest.mark.asyncio
    async def test_oserror_eperm(self, mock_fs_root):
        """Test OSError with EPERM (permission denied)."""
        with patch(
            "tools.code_exec._run_sandboxed_command",
            side_effect=OSError(1, "Operation not permitted"),
        ):
            result = await code_exec(exec_cmd("echo test"))
            assert_error(result, "system error")


# ============================================================================
# Help Action Tests
# ============================================================================


class TestHelpAction:
    """Test help action."""

    @pytest.mark.asyncio
    async def test_help_action(self):
        """Test that help action returns tool documentation."""
        result = await code_exec(CodeExecRequest(action="help"))
        assert result.success
        assert result.action == "help"
        assert result.help is not None
        assert result.help.tool_name == "code_exec"
        assert "exec" in result.help.actions
        assert "help" in result.help.actions

    @pytest.mark.asyncio
    async def test_help_action_lists_required_params(self):
        """Test that help lists required params for exec action."""
        result = await code_exec(CodeExecRequest(action="help"))
        assert result.help is not None
        exec_info = result.help.actions.get("exec")
        assert exec_info is not None
        assert "code" in exec_info.required_params


# ============================================================================
# Basic Command Execution Tests
# ============================================================================


class TestBasicExecution:
    """Test basic command execution functionality."""

    @pytest.mark.asyncio
    async def test_simple_echo(self, mock_fs_root):
        """Test simple echo command."""
        result = await code_exec(exec_cmd("echo 'Hello, World!'"))
        assert_success(result, "Hello, World!")

    @pytest.mark.asyncio
    async def test_multiple_commands(self, mock_fs_root):
        """Test multiple commands separated by &&."""
        result = await code_exec(exec_cmd("echo 'first' && echo 'second'"))
        assert_success(result)
        assert "first" in (result.output or "")
        assert "second" in (result.output or "")

    @pytest.mark.asyncio
    async def test_command_with_pipes(self, mock_fs_root):
        """Test command with pipes."""
        result = await code_exec(
            exec_cmd("echo 'hello world' | tr '[:lower:]' '[:upper:]'")
        )
        assert_success(result, "HELLO WORLD")

    @pytest.mark.asyncio
    async def test_command_with_redirection(self, mock_fs_root):
        """Test command with output redirection."""
        result = await code_exec(
            exec_cmd("echo 'test content' > output.txt && cat output.txt")
        )
        assert_success(result, "test content")

    @pytest.mark.asyncio
    async def test_list_directory(self, mock_fs_root):
        """Test listing directory contents."""
        result = await code_exec(exec_cmd("ls -la"))
        assert_success(result)
        # Should at least show current and parent directory
        assert "." in (result.output or "")

    @pytest.mark.asyncio
    async def test_pwd_command(self, mock_fs_root):
        """Test pwd command returns correct working directory."""
        result = await code_exec(exec_cmd("pwd"))
        assert_success(result)
        assert mock_fs_root in (result.output or "")

    @pytest.mark.asyncio
    async def test_multiline_command(self, mock_fs_root):
        """Test multiline shell command."""
        result = await code_exec(
            exec_cmd("""
for i in 1 2 3; do
    echo "Number: $i"
done
""")
        )
        assert_success(result)
        output = result.output or ""
        assert "Number: 1" in output
        assert "Number: 2" in output
        assert "Number: 3" in output

    @pytest.mark.asyncio
    async def test_environment_variables(self, mock_fs_root):
        """Test accessing environment variables."""
        result = await code_exec(exec_cmd("export MY_VAR='test123' && echo $MY_VAR"))
        assert_success(result, "test123")

    @pytest.mark.asyncio
    async def test_command_with_arguments(self, mock_fs_root):
        """Test command with various arguments."""
        result = await code_exec(exec_cmd("printf '%s %s\\n' 'Hello' 'World'"))
        assert_success(result, "Hello World")


# ============================================================================
# Python Package Tests
# ============================================================================


class TestPythonPackages:
    """Test pre-installed Python packages availability."""

    @pytest.mark.asyncio
    async def test_numpy_available(self, mock_fs_root):
        """Test that numpy is available."""
        result = await code_exec(
            exec_cmd("python -c 'import numpy; print(numpy.__version__)'")
        )
        assert_success(result)
        # Should print version number
        assert any(char.isdigit() for char in (result.output or ""))

    @pytest.mark.asyncio
    async def test_pandas_available(self, mock_fs_root):
        """Test that pandas is available."""
        result = await code_exec(
            exec_cmd("python -c 'import pandas; print(pandas.__version__)'")
        )
        assert_success(result)
        assert any(char.isdigit() for char in (result.output or ""))

    @pytest.mark.asyncio
    async def test_matplotlib_available(self, mock_fs_root):
        """Test that matplotlib is available."""
        result = await code_exec(
            exec_cmd("python -c 'import matplotlib; print(matplotlib.__version__)'")
        )
        assert_success(result)
        assert any(char.isdigit() for char in (result.output or ""))

    @pytest.mark.asyncio
    async def test_scipy_available(self, mock_fs_root):
        """Test that scipy is available."""
        result = await code_exec(
            exec_cmd("python -c 'import scipy; print(scipy.__version__)'")
        )
        assert_success(result)
        assert any(char.isdigit() for char in (result.output or ""))

    @pytest.mark.asyncio
    async def test_sklearn_available(self, mock_fs_root):
        """Test that scikit-learn is available."""
        result = await code_exec(
            exec_cmd("python -c 'import sklearn; print(sklearn.__version__)'")
        )
        assert_success(result)
        assert any(char.isdigit() for char in (result.output or ""))

    @pytest.mark.asyncio
    async def test_numpy_basic_operation(self, mock_fs_root):
        """Test basic numpy operations."""
        result = await code_exec(
            exec_cmd("python -c 'import numpy as np; print(np.array([1,2,3]).mean())'")
        )
        assert_success(result, "2.0")

    @pytest.mark.asyncio
    async def test_pandas_dataframe(self, mock_fs_root):
        """Test creating and using pandas DataFrame."""
        result = await code_exec(
            exec_cmd("""python -c "
import pandas as pd
df = pd.DataFrame({'a': [1, 2, 3], 'b': [4, 5, 6]})
print(df.shape[0])
" """)
        )
        assert_success(result, "3")

    @pytest.mark.asyncio
    async def test_multiple_packages_together(self, mock_fs_root):
        """Test using multiple packages in one script."""
        result = await code_exec(
            exec_cmd("""python -c "
import numpy as np
import pandas as pd
data = np.array([1, 2, 3, 4, 5])
df = pd.DataFrame({'values': data})
print(df['values'].sum())
" """)
        )
        assert_success(result, "15")

    @pytest.mark.asyncio
    async def test_pip_install_with_pip_command(self, mock_fs_root):
        """Test that 'pip install' command works (not just uv pip)."""
        result = await code_exec(exec_cmd("pip --version"))
        assert_success(result)
        # Output should contain version info
        assert "pip" in (result.output or "").lower()

    @pytest.mark.asyncio
    async def test_pip_can_install_package(self, mock_fs_root):
        """Test that pip can actually install a package."""
        # Install a small package
        result = await code_exec(
            exec_cmd(
                "pip install --quiet requests 2>&1 && python -c 'import requests; print(requests.__version__)'"
            )
        )
        assert_success(result)
        # Should have a version number
        assert any(char.isdigit() for char in (result.output or ""))


# ============================================================================
# File Operations Tests
# ============================================================================


class TestFileOperations:
    """Test file creation, reading, writing, and deletion."""

    @pytest.mark.asyncio
    async def test_create_file(self, mock_fs_root):
        """Test creating a new file."""
        result = await code_exec(exec_cmd("echo 'test content' > newfile.txt"))
        assert_success(result)
        assert_empty_output(result)
        assert_file_created(mock_fs_root, "newfile.txt")

    @pytest.mark.asyncio
    async def test_read_file(self, workspace_with_test_file, mock_fs_root):
        """Test reading an existing file."""
        result = await code_exec(exec_cmd("cat test.txt"))
        assert_success(result, "Hello, World!")

    @pytest.mark.asyncio
    async def test_write_to_file(self, mock_fs_root):
        """Test writing content to a file."""
        result = await code_exec(
            exec_cmd(
                "echo 'line1' > file.txt && echo 'line2' >> file.txt && cat file.txt"
            )
        )
        assert_success(result)
        output = result.output or ""
        assert "line1" in output
        assert "line2" in output

    @pytest.mark.asyncio
    async def test_delete_file(self, workspace_with_test_file, mock_fs_root):
        """Test deleting a file."""
        result = await code_exec(exec_cmd("rm test.txt && ls"))
        assert_success(result)
        assert "test.txt" not in (result.output or "")

    @pytest.mark.asyncio
    async def test_create_directory(self, mock_fs_root):
        """Test creating a new directory."""
        result = await code_exec(exec_cmd("mkdir testdir && ls -d testdir"))
        assert_success(result, "testdir")

    @pytest.mark.asyncio
    async def test_nested_directory_creation(self, mock_fs_root):
        """Test creating nested directories."""
        result = await code_exec(exec_cmd("mkdir -p dir1/dir2/dir3 && ls -R"))
        assert_success(result)
        assert "dir1" in (result.output or "")

    @pytest.mark.asyncio
    async def test_file_with_spaces_in_name(self, mock_fs_root):
        """Test handling files with spaces in names."""
        result = await code_exec(
            exec_cmd(
                "echo 'content' > 'file with spaces.txt' && cat 'file with spaces.txt'"
            )
        )
        assert_success(result, "content")

    @pytest.mark.asyncio
    async def test_file_permissions(self, mock_fs_root):
        """Test file permission operations."""
        result = await code_exec(
            exec_cmd("touch file.txt && chmod 644 file.txt && ls -l file.txt")
        )
        assert_success(result)
        assert "file.txt" in (result.output or "")

    @pytest.mark.asyncio
    async def test_copy_file(self, workspace_with_test_file, mock_fs_root):
        """Test copying a file."""
        result = await code_exec(
            exec_cmd("cp test.txt test_copy.txt && cat test_copy.txt")
        )
        assert_success(result, "Hello, World!")

    @pytest.mark.asyncio
    async def test_move_file(self, workspace_with_test_file, mock_fs_root):
        """Test moving/renaming a file."""
        result = await code_exec(exec_cmd("mv test.txt renamed.txt && cat renamed.txt"))
        assert_success(result, "Hello, World!")

    @pytest.mark.asyncio
    async def test_file_count_in_directory(self, mock_fs_root):
        """Test counting files in directory."""
        result = await code_exec(
            exec_cmd("touch f1.txt f2.txt f3.txt && ls -1 *.txt | wc -l")
        )
        assert_success(result, "3")


# ============================================================================
# Python Script Execution Tests
# ============================================================================


class TestPythonScripts:
    """Test executing Python scripts."""

    @pytest.mark.asyncio
    async def test_execute_python_script(
        self, workspace_with_python_script, mock_fs_root
    ):
        """Test executing a Python script file."""
        result = await code_exec(exec_cmd("python script.py"))
        assert_success(result, "Python script executed")

    @pytest.mark.asyncio
    async def test_python_script_with_args(
        self, workspace_with_python_script, mock_fs_root
    ):
        """Test executing Python script with arguments."""
        result = await code_exec(exec_cmd("python script.py arg1 arg2"))
        assert_success(result)
        output = result.output or ""
        assert "arg1" in output
        assert "arg2" in output

    @pytest.mark.asyncio
    async def test_python_inline_code(self, mock_fs_root):
        """Test executing inline Python code."""
        result = await code_exec(exec_cmd("python -c 'print(2 + 2)'"))
        assert_python_output(result, "4")

    @pytest.mark.asyncio
    async def test_python_with_csv_data(self, workspace_with_data_file, mock_fs_root):
        """Test Python script reading CSV data."""
        result = await code_exec(
            exec_cmd("""python -c "
import pandas as pd
df = pd.read_csv('data.csv')
print(len(df))
" """)
        )
        assert_success(result, "3")

    @pytest.mark.asyncio
    async def test_python_create_plot(self, mock_fs_root):
        """Test creating a plot file (without display)."""
        result = await code_exec(
            exec_cmd("""python -c "
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
plt.plot([1, 2, 3], [1, 4, 9])
plt.savefig('plot.png')
print('Plot saved')
" """)
        )
        assert_success(result, "Plot saved")
        assert_file_created(mock_fs_root, "plot.png")


# ============================================================================
# Error Handling Tests
# ============================================================================


class TestErrorHandling:
    """Test error handling for various failure scenarios."""

    @pytest.mark.asyncio
    async def test_command_not_found(self, mock_fs_root):
        """Test handling of non-existent command."""
        result = await code_exec(exec_cmd("nonexistentcommand12345"))
        assert_error(result)
        assert_contains_stderr(result)

    @pytest.mark.asyncio
    async def test_invalid_syntax(self, mock_fs_root):
        """Test handling of invalid shell syntax."""
        result = await code_exec(exec_cmd("if then fi"))
        assert_error(result)
        assert_contains_stderr(result)

    @pytest.mark.asyncio
    async def test_command_exit_code_1(self, mock_fs_root):
        """Test command that exits with code 1."""
        result = await code_exec(exec_cmd("exit 1"))
        assert_error(result)
        assert_exit_code(result, 1)

    @pytest.mark.asyncio
    async def test_command_exit_code_127(self, mock_fs_root):
        """Test command that exits with code 127."""
        result = await code_exec(exec_cmd("nonexistentcmd"))
        assert_error(result)
        assert_contains_stderr(result)

    @pytest.mark.asyncio
    async def test_file_not_found(self, mock_fs_root):
        """Test reading non-existent file."""
        result = await code_exec(exec_cmd("cat nonexistent.txt"))
        assert_error(result)
        assert_contains_stderr(result)

    @pytest.mark.asyncio
    async def test_permission_denied_simulation(self, mock_fs_root):
        """Test permission denied error."""
        # Create a file and remove read permissions
        result = await code_exec(
            exec_cmd(
                "touch restricted.txt && chmod 000 restricted.txt && cat restricted.txt"
            )
        )
        assert_error(result)
        assert_contains_stderr(result)

    @pytest.mark.asyncio
    async def test_timeout_long_running_command(self, mock_fs_root, mock_short_timeout):
        """Test that long-running commands timeout."""
        # Sleep for longer than timeout
        result = await code_exec(exec_cmd("sleep 10"))
        assert_timeout(result)

    @pytest.mark.asyncio
    async def test_timeout_infinite_loop(self, mock_fs_root, mock_short_timeout):
        """Test that infinite loops are terminated."""
        result = await code_exec(exec_cmd("while true; do echo 'loop'; done"))
        assert_timeout(result)

    @pytest.mark.asyncio
    async def test_python_syntax_error(self, mock_fs_root):
        """Test Python syntax error."""
        result = await code_exec(exec_cmd("python -c 'print(\"test'"))
        assert_error(result)
        assert_contains_stderr(result)

    @pytest.mark.asyncio
    async def test_python_runtime_error(self, mock_fs_root):
        """Test Python runtime error."""
        result = await code_exec(exec_cmd("python -c '1/0'"))
        assert_error(result)
        assert_contains_stderr(result)

    @pytest.mark.asyncio
    async def test_python_import_error(self, mock_fs_root):
        """Test Python import error for non-existent package."""
        result = await code_exec(exec_cmd("python -c 'import nonexistent_package_xyz'"))
        assert_error(result)
        assert_contains_stderr(result)

    @pytest.mark.asyncio
    async def test_stderr_captured_with_stdout(self, mock_fs_root):
        """Test that stderr is captured along with stdout."""
        result = await code_exec(
            exec_cmd("echo 'stdout' && >&2 echo 'stderr' && exit 1")
        )
        assert_error(result)
        assert "stdout" in (result.output or "")
        assert_contains_stderr(result)


# ============================================================================
# Edge Cases Tests
# ============================================================================


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    @pytest.mark.asyncio
    async def test_empty_command(self, mock_fs_root):
        """Test executing empty command."""
        result = await code_exec(exec_cmd(""))
        # Empty command should succeed with empty output
        assert result.action == "exec"

    @pytest.mark.asyncio
    async def test_whitespace_only_command(self, mock_fs_root):
        """Test command with only whitespace."""
        result = await code_exec(exec_cmd("   "))
        assert result.action == "exec"

    @pytest.mark.asyncio
    async def test_command_with_special_characters(self, mock_fs_root):
        """Test command with special characters."""
        result = await code_exec(exec_cmd("echo '!@#$%^&*()_+-=[]{}|;:,.<>?'"))
        assert_success(result)
        assert "!@#$%^&*" in (result.output or "")

    @pytest.mark.asyncio
    async def test_command_with_unicode(self, mock_fs_root):
        """Test command with unicode characters."""
        result = await code_exec(exec_cmd("echo '你好世界 🌍'"))
        assert_success(result)
        output = result.output or ""
        assert "你好世界" in output or "🌍" in output  # May vary by system

    @pytest.mark.asyncio
    async def test_very_long_output(self, mock_fs_root):
        """Test command that produces long output."""
        result = await code_exec(exec_cmd("seq 1 1000"))
        assert_success(result)
        output = result.output or ""
        assert "1000" in output
        # Check that output has many lines
        lines = output.strip().split("\n")
        assert len(lines) >= 100

    @pytest.mark.asyncio
    async def test_command_with_quotes(self, mock_fs_root):
        """Test command with various quote types."""
        result = await code_exec(
            exec_cmd("""echo "double" && echo 'single' && echo `echo backtick`""")
        )
        assert_success(result)
        output = result.output or ""
        assert "double" in output
        assert "single" in output

    @pytest.mark.asyncio
    async def test_command_with_newlines(self, mock_fs_root):
        """Test command output with newlines."""
        result = await code_exec(exec_cmd("printf 'line1\\nline2\\nline3'"))
        assert_success(result)
        assert_output_lines(result, 3)

    @pytest.mark.asyncio
    async def test_binary_file_creation(self, mock_fs_root):
        """Test creating binary files."""
        result = await code_exec(
            exec_cmd("dd if=/dev/zero of=binary.dat bs=1024 count=1 2>&1")
        )
        assert_success(result)
        assert_file_created(mock_fs_root, "binary.dat")

    @pytest.mark.asyncio
    async def test_large_file_creation(self, mock_fs_root):
        """Test creating a larger file."""
        result = await code_exec(
            exec_cmd(
                "dd if=/dev/zero of=large.dat bs=1024 count=1024 2>&1 && ls -lh large.dat"
            )
        )
        assert_success(result)
        assert "large.dat" in (result.output or "")

    @pytest.mark.asyncio
    async def test_many_small_files(self, mock_fs_root):
        """Test creating many small files."""
        result = await code_exec(
            exec_cmd(
                "i=1; while [ $i -le 50 ]; do touch file_$i.txt; i=$((i+1)); done && ls -1 file_*.txt | wc -l"
            )
        )
        assert_success(result)
        # Should have at least 50 files
        count = int((result.output or "").strip())
        assert count >= 50

    @pytest.mark.asyncio
    async def test_command_with_subshell(self, mock_fs_root):
        """Test command with subshell execution."""
        result = await code_exec(exec_cmd("echo $(echo 'nested')"))
        assert_success(result, "nested")

    @pytest.mark.asyncio
    async def test_exit_0_explicit(self, mock_fs_root):
        """Test explicit exit 0 (success)."""
        result = await code_exec(exec_cmd("echo 'done' && exit 0"))
        assert_success(result, "done")


# ============================================================================
# Security Tests
# ============================================================================


class TestSecurity:
    """Test security features and sandboxing."""

    @pytest.mark.asyncio
    async def test_commands_scoped_to_fs_root(self, mock_fs_root):
        """Test that commands execute in FS_ROOT directory."""
        result = await code_exec(exec_cmd("pwd"))
        assert_success(result)
        assert mock_fs_root in (result.output or "")

    @pytest.mark.asyncio
    async def test_file_operations_in_workspace(self, mock_fs_root):
        """Test that file operations happen in workspace."""
        result = await code_exec(exec_cmd("touch workspace_file.txt && pwd && ls"))
        assert_success(result)
        output = result.output or ""
        assert mock_fs_root in output
        assert "workspace_file.txt" in output

    @pytest.mark.asyncio
    async def test_parent_directory_access(self, mock_fs_root):
        """Test accessing parent directory (should work within subprocess)."""
        # This should execute but may not escape the container/sandbox
        result = await code_exec(exec_cmd("cd .. && pwd"))
        assert result.action == "exec"
        # In a real sandbox, this would still be contained

    @pytest.mark.asyncio
    async def test_absolute_path_access(self, mock_fs_root):
        """Test that absolute paths work within subprocess."""
        result = await code_exec(exec_cmd("ls /"))
        assert result.action == "exec"
        # Should execute (sandbox may restrict actual access)

    @pytest.mark.asyncio
    async def test_command_injection_attempt(self, mock_fs_root):
        """Test that command injection is handled by shell."""
        # These all execute in shell context, which is expected behavior
        result = await code_exec(exec_cmd("echo 'test'; echo 'injected'"))
        assert_success(result)
        output = result.output or ""
        assert "test" in output
        assert "injected" in output

    @pytest.mark.asyncio
    async def test_environment_variable_isolation(self, mock_fs_root):
        """Test that environment variables are isolated per command."""
        result1 = await code_exec(
            exec_cmd("export TEST_VAR='value1' && echo $TEST_VAR")
        )
        result2 = await code_exec(exec_cmd("echo $TEST_VAR"))
        assert_success(result1, "value1")
        # Second command should not see TEST_VAR from first command
        assert "value1" not in (result2.output or "")


# ============================================================================
# Integration Tests
# ============================================================================


class TestIntegration:
    """Integration tests combining multiple features."""

    @pytest.mark.asyncio
    async def test_data_science_workflow(self, mock_fs_root):
        """Test complete data science workflow."""
        # Create data, analyze with pandas, plot with matplotlib
        result = await code_exec(
            exec_cmd("""python -c "
import pandas as pd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# Create data
data = {'x': [1, 2, 3, 4, 5], 'y': [2, 4, 6, 8, 10]}
df = pd.DataFrame(data)

# Analyze
mean_y = df['y'].mean()
print(f'Mean: {mean_y}')

# Plot
plt.plot(df['x'], df['y'])
plt.savefig('analysis.png')
print('Analysis complete')
" """)
        )
        assert_success(result)
        output = result.output or ""
        assert "Mean: 6.0" in output
        assert "Analysis complete" in output
        assert_file_created(mock_fs_root, "analysis.png")

    @pytest.mark.asyncio
    async def test_file_processing_pipeline(self, mock_fs_root):
        """Test file processing pipeline."""
        result = await code_exec(
            exec_cmd("""
# Create input file
echo -e "apple\\nbanana\\napple\\ncherry\\napple" > fruits.txt

# Process and count
cat fruits.txt | sort | uniq -c | sort -nr > counts.txt

# Display result
cat counts.txt
""")
        )
        assert_success(result)
        output = result.output or ""
        assert "3 apple" in output or "apple" in output

    @pytest.mark.asyncio
    async def test_multi_file_python_project(self, mock_fs_root):
        """Test creating and running multi-file Python project."""
        result = await code_exec(
            exec_cmd("""
# Create module file
cat > mymodule.py << 'EOF'
def add(a, b):
    return a + b

def multiply(a, b):
    return a * b
EOF

# Create main script
cat > main.py << 'EOF'
from mymodule import add, multiply

result1 = add(5, 3)
result2 = multiply(4, 7)
print(f"Add: {result1}, Multiply: {result2}")
EOF

# Run main script
python main.py
""")
        )
        assert_success(result)
        output = result.output or ""
        assert "Add: 8" in output
        assert "Multiply: 28" in output

    @pytest.mark.asyncio
    async def test_script_with_error_handling(self, mock_fs_root):
        """Test script that handles its own errors."""
        result = await code_exec(
            exec_cmd("""python -c "
try:
    result = 10 / 2
    print(f'Success: {result}')
except ZeroDivisionError:
    print('Error: Division by zero')
" """)
        )
        assert_success(result, "Success: 5.0")

    @pytest.mark.asyncio
    async def test_concurrent_file_operations(self, mock_fs_root):
        """Test multiple file operations in sequence."""
        result = await code_exec(
            exec_cmd("""
touch file1.txt file2.txt file3.txt
echo "content1" > file1.txt
echo "content2" > file2.txt
echo "content3" > file3.txt
cat file1.txt file2.txt file3.txt
""")
        )
        assert_success(result)
        output = result.output or ""
        assert "content1" in output
        assert "content2" in output
        assert "content3" in output

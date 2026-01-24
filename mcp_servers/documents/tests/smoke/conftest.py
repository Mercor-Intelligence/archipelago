"""Shared fixtures for docs MCP server smoke tests.

These tests use FastMCP's in-process client to test the full MCP protocol
without needing to start a separate server process.
"""

import shutil
import subprocess
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import patch

import pytest
from fastmcp import Client

# Add server path FIRST for imports (utils, tools, etc.)
SERVER_PATH = Path(__file__).parent.parent.parent / "mcp_servers" / "docs_server"
sys.path.insert(0, str(SERVER_PATH))

# Add smoke test directory for smoke_utils import (after server path)
SMOKE_PATH = Path(__file__).parent
sys.path.append(str(SMOKE_PATH))  # append, not insert

# Import after path setup
from main import mcp  # noqa: E402

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def _ensure_fixtures_exist() -> None:
    """Generate fixture files if they don't exist."""
    required_files = [
        "sample_document.docx",
        "document_with_images.docx",
        "sample_image.png",
    ]

    missing = [f for f in required_files if not (FIXTURES_DIR / f).exists()]

    if missing:
        # Run the create_fixtures script
        script = FIXTURES_DIR / "create_fixtures.py"
        if script.exists():
            subprocess.run(
                [sys.executable, str(script)],
                cwd=str(FIXTURES_DIR),
                check=True,
            )


# Ensure fixtures exist when conftest loads
_ensure_fixtures_exist()


@pytest.fixture
def smoke_test_root(tmp_path: Path) -> Path:
    """Create isolated temp directory for smoke tests.

    Args:
        tmp_path: pytest's temporary directory fixture

    Returns:
        Path to the smoke test root directory
    """
    root = tmp_path / "smoke"
    root.mkdir()
    return root


@pytest.fixture
def sample_document(smoke_test_root: Path) -> str:
    """Copy sample document to temp directory.

    Args:
        smoke_test_root: Temporary root directory

    Returns:
        File path relative to root (e.g., "/sample.docx")
    """
    source = FIXTURES_DIR / "sample_document.docx"
    dest = smoke_test_root / "sample.docx"
    shutil.copy(source, dest)
    return "/sample.docx"


@pytest.fixture
def document_with_images(smoke_test_root: Path) -> str:
    """Copy document with images to temp directory.

    Args:
        smoke_test_root: Temporary root directory

    Returns:
        File path relative to root
    """
    source = FIXTURES_DIR / "document_with_images.docx"
    dest = smoke_test_root / "doc_with_images.docx"
    shutil.copy(source, dest)
    return "/doc_with_images.docx"


@pytest.fixture
def sample_image(smoke_test_root: Path) -> str:
    """Copy sample image to temp directory.

    Args:
        smoke_test_root: Temporary root directory

    Returns:
        File path relative to root
    """
    source = FIXTURES_DIR / "sample_image.png"
    dest = smoke_test_root / "test_image.png"
    shutil.copy(source, dest)
    return "/test_image.png"


@pytest.fixture
def mcp_server(smoke_test_root: Path, monkeypatch: pytest.MonkeyPatch):
    """Configure MCP server with mocked filesystem root.

    Args:
        smoke_test_root: Temporary root directory
        monkeypatch: pytest's monkeypatch fixture

    Yields:
        The FastMCP server instance
    """
    root_str = str(smoke_test_root)

    # Set environment variables
    monkeypatch.setenv("APP_FS_ROOT", root_str)
    monkeypatch.setenv("APP_DOCS_ROOT", root_str)

    # Patch DOCS_ROOT in all tool modules
    tool_modules = [
        "tools.create_document",
        "tools.delete_document",
        "tools.read_document_content",
        "tools.get_document_overview",
        "tools.add_content_text",
        "tools.edit_content_text",
        "tools.delete_content_text",
        "tools.apply_formatting",
        "tools.add_image",
        "tools.modify_image",
        "tools.read_image",
    ]

    patches = []
    for module in tool_modules:
        try:
            p = patch(f"{module}.DOCS_ROOT", root_str)
            p.start()
            patches.append(p)
        except Exception:
            pass  # Module may not have DOCS_ROOT

    yield mcp

    for p in patches:
        p.stop()


@pytest.fixture
async def client(mcp_server) -> AsyncGenerator[Client]:
    """Create connected MCP client for testing.

    Args:
        mcp_server: The configured FastMCP server

    Yields:
        Connected MCP client
    """
    async with Client(mcp_server) as c:
        yield c

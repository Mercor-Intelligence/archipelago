"""Shared fixtures for the filesystem server tool tests.

The tools live in ``mcp_servers/filesystem_server/`` and import each other
as top-level ``tools.*`` / ``utils.*`` packages, so this directory's parent
is added to ``sys.path`` here. ``read_image_file`` resolves paths against the
active actor filesystem root, which is pointed at a fresh temporary
directory per test via the ``sandbox`` fixture.
"""

import sys
from pathlib import Path

import pytest
from mcp_actor import paths as actor_paths

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    """A fresh sandbox directory acting as the actor filesystem root."""
    monkeypatch.setattr(actor_paths, "active_filesystem_root", lambda: str(tmp_path))
    return tmp_path

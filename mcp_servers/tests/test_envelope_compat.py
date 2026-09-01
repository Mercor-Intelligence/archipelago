"""Envelope-compat coverage and behaviour across the MCP servers.

Guards archipelago#193: a meta-tool declared as ``tool(request: Model)`` publishes
a single nested ``request`` property, so an agent that passes the inner fields
flat is rejected with "request: Missing required argument".
``EnvelopeCompatMiddleware`` wraps those calls, and it only helps on servers that
actually register it.

Run from the repository root:

    python3 -m unittest discover -s mcp_servers/tests
"""

from __future__ import annotations

import asyncio
import importlib.util
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVERS_ROOT = REPO_ROOT / "mcp_servers"

# Servers that publish meta-tools behind a nested envelope. auth_server is
# excluded on purpose: it registers no envelope-style tool.
SERVERS = (
    "calendar/mcp_servers/calendar_server",
    "chat/mcp_servers/chat_server",
    "code/mcp_servers/code_execution_server",
    "documents/mcp_servers/docs_server",
    "edgar_sec/mcp_servers/edgar_sec",
    "filesystem/mcp_servers/filesystem_server",
    "fmp/mcp_servers/fmp_server",
    "mail/mcp_servers/mail_server",
    "pdfs/mcp_servers/pdf_server",
    "presentations/mcp_servers/slides_server",
    "spreadsheets/mcp_servers/sheets_server",
)

CANONICAL = SERVERS_ROOT / "pdfs/mcp_servers/pdf_server/middleware/envelope_compat.py"
_ADD = re.compile(r"add_middleware\(\s*EnvelopeCompatMiddleware\(")


def middleware_path(server: str) -> Path:
    return SERVERS_ROOT / server / "middleware" / "envelope_compat.py"


class CoverageTests(unittest.TestCase):
    """Static checks, no server dependencies needed."""

    def test_every_envelope_server_ships_the_middleware(self):
        for server in SERVERS:
            with self.subTest(server=server):
                self.assertTrue(middleware_path(server).is_file(),
                                f"{server} has no middleware/envelope_compat.py")

    def test_every_envelope_server_registers_the_middleware(self):
        for server in SERVERS:
            with self.subTest(server=server):
                main = (SERVERS_ROOT / server / "main.py").read_text()
                self.assertTrue(
                    _ADD.search(main),
                    f"{server}/main.py never calls add_middleware(EnvelopeCompatMiddleware(...))")

    def test_all_copies_match_the_canonical_one(self):
        """The four original copies had drifted into three variants."""
        canon = CANONICAL.read_text()
        for server in SERVERS:
            with self.subTest(server=server):
                self.assertTrue(
                    middleware_path(server).read_text() == canon,
                    f"{server}/middleware/envelope_compat.py has drifted from the "
                    f"pdf_server copy")

    def test_a_server_that_flattens_is_in_the_server_list(self):
        """A new flattening server must be added here, not silently skipped."""
        listed = {SERVERS_ROOT / s / "main.py" for s in SERVERS}
        for main in SERVERS_ROOT.glob("*/mcp_servers/*/main.py"):
            if "_flatten_tool_schemas" not in main.read_text():
                continue
            with self.subTest(main=str(main.relative_to(REPO_ROOT))):
                self.assertIn(main, listed)


def _load_canonical():
    spec = importlib.util.spec_from_file_location("envelope_compat_under_test", CANONICAL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


try:
    _mod = _load_canonical()
except Exception:  # fastmcp/loguru not installed
    _mod = None


@unittest.skipIf(_mod is None, "fastmcp not installed")
class WrapTests(unittest.TestCase):
    """The wrapping rule itself."""

    def wrap(self, arguments, properties):
        return _mod._wrap_flat_arguments(arguments, properties)

    def test_flat_arguments_are_wrapped(self):
        self.assertEqual(
            self.wrap({"action": "help", "file_path": "/x.pdf"}, {"request": {}}),
            {"request": {"action": "help", "file_path": "/x.pdf"}})

    def test_already_nested_is_left_alone(self):
        args = {"request": {"action": "help"}}
        self.assertIs(self.wrap(args, {"request": {}}), args)

    def test_no_arguments_becomes_an_empty_envelope(self):
        """Without this, a no-arg meta-tool call still fails on the envelope."""
        self.assertEqual(self.wrap(None, {"request": {}}), {"request": {}})

    def test_multi_property_tool_is_left_alone(self):
        args = {"a": 1}
        self.assertIs(self.wrap(args, {"a": {}, "b": {}}), args)

    def test_single_property_that_is_not_an_envelope_is_left_alone(self):
        args = {"path": "/x"}
        self.assertIs(self.wrap(args, {"path": {}}), args)

    def test_unknown_schema_is_left_alone(self):
        args = {"a": 1}
        self.assertIs(self.wrap(args, None), args)

    def test_input_is_accepted_as_an_envelope_key(self):
        self.assertEqual(self.wrap({"x": 1}, {"input": {}}), {"input": {"x": 1}})


if _mod is not None:
    from pydantic import BaseModel

    class _PdfInput(BaseModel):
        action: str
        file_path: str | None = None


@unittest.skipIf(_mod is None, "fastmcp not installed")
class EndToEndTests(unittest.TestCase):
    """A flat call against a real FastMCP server, as reported in #193."""

    def test_flat_call_succeeds_with_the_middleware(self):
        from fastmcp import Client, FastMCP

        async def scenario(with_middleware: bool):
            mcp = FastMCP("envelope-test")

            @mcp.tool
            async def pdf(request: _PdfInput) -> str:
                return f"ok:{request.action}"

            if with_middleware:
                mcp.add_middleware(_mod.EnvelopeCompatMiddleware(mcp))
            async with Client(mcp) as client:
                await client.call_tool("pdf", {"action": "help", "file_path": "/x.pdf"})

        with self.assertRaises(Exception):
            asyncio.run(scenario(False))
        asyncio.run(scenario(True))  # must not raise


if __name__ == "__main__":
    unittest.main()

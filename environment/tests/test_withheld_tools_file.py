"""Per-tool withholding: ``app:tool`` lines in the shipped disabled_mcp_servers.txt.

The delivery half of tool disabling. The .config configs tested in
``test_tool_disabling.py`` never reach the customer — the Nexus tar excludes
``.apps_data/`` wholesale — so a delivered world could neither show which tools
it withheld nor let the recipient change the set. ``tools/disabled_mcp_servers.txt``
carries them beside the whole-app lines, armed by ``--disable_optional_mcp``, and
``--serve_tools`` subtracts from it per run.

Both inputs are operator text crossing into the container, so most of what is
locked here is what happens to a malformed one.
"""

import asyncio

import pytest
from fastmcp import Client as FastMCPClient
from fastmcp import FastMCP

from runner.gateway.gateway import (
    _DisabledToolsMiddleware,
    _effective_withheld_by_app,
    _install_tool_aliases,
    _parse_app_tool_entries,
    _ToolAliasMiddleware,
    _withheld_tools_from_file,
)
from runner.gateway.models import ToolAliasSpec


def _write_withhold_file(tmp_path, body: str):
    path = tmp_path / "disabled_mcp_servers.txt"
    path.write_text(body)
    return path


def _two_app_gateway() -> FastMCP:
    """Two servers sharing one bare tool name, as the GDM pair does."""
    server = FastMCP("test")

    @server.tool
    def filesystem_read_image_file(path: str) -> str:
        return f"fs {path}"

    @server.tool
    def filesystem_list_files() -> str:
        return "listed"

    @server.tool
    def mail_send_email(to: str) -> str:
        return f"sent {to}"

    @server.tool
    def mail_read_email() -> str:
        return "read"

    return server


SERVERS = ["filesystem", "mail"]


async def _install(tmp_path, monkeypatch, *, serve: str | None = None, armed=True):
    """Boot the two-app gateway against a shipped withhold file."""
    monkeypatch.setenv("DISABLE_OPTIONAL_MCP", "1" if armed else "0")
    if serve is not None:
        monkeypatch.setenv("SERVE_TOOLS", serve)
    monkeypatch.setattr(
        "runner.gateway.gateway._WITHHELD_TOOLS_FILE",
        str(tmp_path / "disabled_mcp_servers.txt"),
    )
    server = _two_app_gateway()
    disable_mw = _DisabledToolsMiddleware()
    await _install_tool_aliases(
        server,
        _ToolAliasMiddleware(),
        disable_mw,
        SERVERS,
        apps_data_root=str(tmp_path / "apps_data_empty"),
    )
    return server, disable_mw


class TestTheFlagGatesTheFile:
    """The property the delivery contract rests on."""

    def test_the_file_is_inert_without_the_flag(self, tmp_path, monkeypatch):
        """A world run with no args must serve everything. This is the test that
        would catch the whole set silently vanishing from a delivered world."""
        _write_withhold_file(tmp_path, "mail:send_email\n")
        monkeypatch.delenv("DISABLE_OPTIONAL_MCP", raising=False)
        monkeypatch.setattr(
            "runner.gateway.gateway._WITHHELD_TOOLS_FILE",
            str(tmp_path / "disabled_mcp_servers.txt"),
        )
        assert _withheld_tools_from_file() == {}

    @pytest.mark.parametrize("value", ["0", "", "true", "yes", "1 "])
    def test_only_exactly_1_arms_it(self, tmp_path, monkeypatch, value):
        """start.sh writes exactly "1"; anything else is a shell accident and
        must not silently arm withholding."""
        _write_withhold_file(tmp_path, "mail:send_email\n")
        monkeypatch.setenv("DISABLE_OPTIONAL_MCP", value)
        monkeypatch.setattr(
            "runner.gateway.gateway._WITHHELD_TOOLS_FILE",
            str(tmp_path / "disabled_mcp_servers.txt"),
        )
        assert _withheld_tools_from_file() == {}

    def test_armed_reads_the_list(self, tmp_path, monkeypatch):
        _write_withhold_file(tmp_path, "mail:send_email\nmail:read_email\n")
        monkeypatch.setenv("DISABLE_OPTIONAL_MCP", "1")
        monkeypatch.setattr(
            "runner.gateway.gateway._WITHHELD_TOOLS_FILE",
            str(tmp_path / "disabled_mcp_servers.txt"),
        )
        assert _withheld_tools_from_file() == {"mail": {"send_email", "read_email"}}

    def test_armed_with_no_file_is_not_an_error(self, tmp_path, monkeypatch):
        """A world that withholds nothing ships no file; the flag still rides
        every task's startup line."""
        monkeypatch.setenv("DISABLE_OPTIONAL_MCP", "1")
        monkeypatch.setattr(
            "runner.gateway.gateway._WITHHELD_TOOLS_FILE",
            str(tmp_path / "absent.txt"),
        )
        assert _withheld_tools_from_file() == {}


class TestTheFileParse:
    def test_comments_blanks_and_whitespace_are_ignored(self, tmp_path, monkeypatch):
        """Parsed exactly as start.sh parses disabled_mcp_servers.txt, so the two
        readers cannot disagree about what a line says."""
        _write_withhold_file(
            tmp_path,
            "# header\n\n  mail:send_email  \n\t\n#mail:read_email\n",
        )
        monkeypatch.setenv("DISABLE_OPTIONAL_MCP", "1")
        monkeypatch.setattr(
            "runner.gateway.gateway._WITHHELD_TOOLS_FILE",
            str(tmp_path / "disabled_mcp_servers.txt"),
        )
        assert _withheld_tools_from_file() == {"mail": {"send_email"}}

    @pytest.mark.parametrize(
        "entry",
        [
            "send_email",  # no app scope
            "mail:",  # no tool
            "../../etc:passwd",  # path separator in the app half
            "mail:../secret",  # path separator in the tool half
            "mail:send email",  # space is not in the name charset
            "mail:*",  # withholding a whole app is not expressible here
            ":send_email",  # no app
        ],
    )
    def test_a_malformed_entry_is_dropped_alone(self, entry):
        """Not the permissive direction it looks like: a name failing these
        patterns cannot match any live tool or server, whose names satisfy the
        same patterns. Dropping the whole list over one bad line is what would
        serve tools — every other line's."""
        parsed = _parse_app_tool_entries(
            [entry, "mail:read_email"], source="t", allow_wildcard=False
        )
        assert parsed == {"mail": {"read_email"}}

    def test_neither_half_can_carry_a_path_separator(self):
        """The app half becomes a scope key compared against server names and the
        file path is a fixed literal, but the charset check is the guarantee."""
        assert (
            _parse_app_tool_entries(
                ["a/b:c", "a:b/c", "..:x", "a:.."],
                source="t",
                allow_wildcard=True,
            )
            == {}
        )

    def test_an_unreadable_file_serves_everything_loudly(self, tmp_path, monkeypatch):
        """Its names are unknowable, so tools the operator believes are withheld
        are about to be served — the one case that must be noisy."""
        monkeypatch.setenv("DISABLE_OPTIONAL_MCP", "1")
        monkeypatch.setattr(
            "runner.gateway.gateway._WITHHELD_TOOLS_FILE", str(tmp_path)
        )  # a directory
        assert _withheld_tools_from_file() == {}


class TestServeToolsSubtracts:
    def test_it_re_enables_one_tool(self, tmp_path, monkeypatch):
        _write_withhold_file(tmp_path, "mail:send_email\nmail:read_email\n")
        _, disable_mw = await_sync(
            _install(tmp_path, monkeypatch, serve="mail:send_email")
        )
        assert disable_mw.disabled_observed == {"mail_read_email"}

    def test_a_wildcard_re_enables_that_app_only(self, tmp_path, monkeypatch):
        _write_withhold_file(
            tmp_path, "mail:send_email\nmail:read_email\nfilesystem:list_files\n"
        )
        _, disable_mw = await_sync(_install(tmp_path, monkeypatch, serve="mail:*"))
        assert disable_mw.disabled_observed == {"filesystem_list_files"}

    def test_it_is_scoped_per_app(self, tmp_path, monkeypatch):
        """Two apps expose read_image_file; re-enabling one must not free the
        other. Same scoping rule the withholds resolve under."""
        _write_withhold_file(
            tmp_path, "filesystem:read_image_file\nfilesystem:list_files\n"
        )
        _, disable_mw = await_sync(
            _install(tmp_path, monkeypatch, serve="mail:read_image_file")
        )
        assert disable_mw.disabled_observed == {
            "filesystem_read_image_file",
            "filesystem_list_files",
        }

    def test_an_empty_value_changes_nothing(self, tmp_path, monkeypatch):
        """start.sh exports SERVE_TOOLS="" when the flag is absent."""
        _write_withhold_file(tmp_path, "mail:send_email\n")
        _, disable_mw = await_sync(_install(tmp_path, monkeypatch, serve=""))
        assert disable_mw.disabled_observed == {"mail_send_email"}

    def test_a_space_after_a_comma_still_re_enables(self, tmp_path, monkeypatch):
        """The recipient hand-edits this into tasks/*.json, so the spacing they
        would naturally type must not silently drop the re-enable."""
        _write_withhold_file(tmp_path, "mail:send_email\nmail:read_email\n")
        _, disable_mw = await_sync(
            _install(tmp_path, monkeypatch, serve="mail:send_email, mail:read_email")
        )
        assert disable_mw.disabled_observed == set()

    def test_interior_whitespace_is_still_rejected(self, tmp_path, monkeypatch):
        """Only surrounding whitespace is stripped. Collapsing it the way the
        file parser does would turn this into `send_email` and free a tool the
        operator never named."""
        _write_withhold_file(tmp_path, "mail:send_email\n")
        _, disable_mw = await_sync(
            _install(tmp_path, monkeypatch, serve="mail:send _email")
        )
        assert disable_mw.disabled_observed == {"mail_send_email"}

    def test_it_cannot_withhold_anything_new(self):
        """The only permissive input in the path, so it must be subtraction and
        nothing else: a name that was not withheld is a no-op, never an add."""
        assert _effective_withheld_by_app(
            {}, {"mail": {"send_email"}}, {"mail": {"read_email"}}
        ) == {"mail": {"send_email"}}


class TestBothWithholdSourcesUnion:
    def test_a_config_layer_and_the_file_compose(self, tmp_path, monkeypatch):
        """A per-task .config still carries its own withholds; the file carries
        the world's. Union is the only direction that cannot serve a tool one of
        the two named."""
        assert _effective_withheld_by_app(
            {"mail": ToolAliasSpec(disabled_tools=["read_email"])},
            {"mail": {"send_email"}},
            {},
        ) == {"mail": {"send_email", "read_email"}}

    def test_the_file_applies_to_an_app_with_no_config(self, tmp_path, monkeypatch):
        """The load-bearing case: after export the world's config is gone, so
        iterating the .config dirs would serve every tool the file withholds."""
        _write_withhold_file(tmp_path, "mail:send_email\n")
        _, disable_mw = await_sync(_install(tmp_path, monkeypatch))
        assert disable_mw.disabled_observed == {"mail_send_email"}

    def test_an_app_that_is_not_a_live_server_withholds_nothing(
        self, tmp_path, monkeypatch
    ):
        """An unbound withhold would hide whichever app happens to serve that
        bare name. Serving the tool is the weaker outcome but the only one that
        cannot hide a tool nobody named."""
        _write_withhold_file(tmp_path, "renamed_mail:send_email\n")
        _, disable_mw = await_sync(_install(tmp_path, monkeypatch))
        assert disable_mw.disabled_observed == set()


class TestTheServedToolsAreReallyReachable:
    @pytest.mark.asyncio
    async def test_a_re_enabled_tool_is_listed_and_callable(
        self, tmp_path, monkeypatch
    ):
        """End to end: the middleware is what the agent sees, so assert through
        it rather than on the resolved set."""
        _write_withhold_file(tmp_path, "mail:send_email\nmail:read_email\n")
        monkeypatch.setenv("DISABLE_OPTIONAL_MCP", "1")
        monkeypatch.setenv("SERVE_TOOLS", "mail:send_email")
        monkeypatch.setattr(
            "runner.gateway.gateway._WITHHELD_TOOLS_FILE",
            str(tmp_path / "disabled_mcp_servers.txt"),
        )
        server = _two_app_gateway()
        disable_mw = _DisabledToolsMiddleware()
        await _install_tool_aliases(
            server,
            _ToolAliasMiddleware(),
            disable_mw,
            SERVERS,
            apps_data_root=str(tmp_path / "none"),
        )
        server.add_middleware(disable_mw)

        async with FastMCPClient(server) as client:
            names = {t.name for t in await client.list_tools()}
        assert "mail_send_email" in names
        assert "mail_read_email" not in names

    @pytest.mark.asyncio
    async def test_a_re_enable_unwinds_a_whole_alias_chain(self, tmp_path, monkeypatch):
        """`a -> b` is only legal because `b` is withheld, and `c -> a` only
        because `a` is then served as `b`. Freeing `b` invalidates both, one
        hop apart — dropping just the first leaves `c -> a` shadowing the now
        live `a`, which used to abort the boot."""
        server = FastMCP("test")

        @server.tool
        def mail_a() -> str:
            return "a"

        @server.tool
        def mail_b() -> str:
            return "b"

        @server.tool
        def mail_c() -> str:
            return "c"

        @server.tool
        def filesystem_list_files() -> str:
            return "listed"

        config = tmp_path / "apps_data" / "mail" / ".config"
        config.mkdir(parents=True)
        (config / "tool_aliases.world.json").write_text(
            '{"aliases": {"a": "b", "c": "a"}}'
        )
        _write_withhold_file(tmp_path, "mail:b\n")
        monkeypatch.setenv("DISABLE_OPTIONAL_MCP", "1")
        monkeypatch.setenv("SERVE_TOOLS", "mail:b")
        monkeypatch.setattr(
            "runner.gateway.gateway._WITHHELD_TOOLS_FILE",
            str(tmp_path / "disabled_mcp_servers.txt"),
        )
        alias_mw, disable_mw = _ToolAliasMiddleware(), _DisabledToolsMiddleware()
        await _install_tool_aliases(
            server,
            alias_mw,
            disable_mw,
            SERVERS,
            apps_data_root=str(tmp_path / "apps_data"),
        )
        server.add_middleware(disable_mw)
        server.add_middleware(alias_mw)

        async with FastMCPClient(server) as client:
            names = [t.name for t in await client.list_tools()]
        assert len(names) == len(set(names))
        assert {"mail_a", "mail_b", "mail_c"} <= set(names)


def await_sync(coro):
    """Run a coroutine from a sync test body.

    The parametrized/sync tests above only need the resolved set, and wrapping
    each one in @pytest.mark.asyncio for that would be noise.
    """
    return asyncio.run(coro)


class TestTheFileIsSharedWithTheAppOptOut:
    """The same file carries bare ``app`` lines for start.sh. The gateway must
    pass over them silently — they are not malformed ``app:tool`` entries, they
    are the other consumer's half, and by the time this runs start.sh has
    already dropped those apps from mcp.json."""

    def test_bare_app_lines_are_ignored_not_reported(self, tmp_path, monkeypatch):
        _write_withhold_file(tmp_path, "calendar\nexcel_v2\nmail:send_email\n")
        monkeypatch.setenv("DISABLE_OPTIONAL_MCP", "1")
        monkeypatch.setattr(
            "runner.gateway.gateway._WITHHELD_TOOLS_FILE",
            str(tmp_path / "disabled_mcp_servers.txt"),
        )
        assert _withheld_tools_from_file() == {"mail": {"send_email"}}

    def test_a_file_of_only_app_lines_withholds_nothing(self, tmp_path, monkeypatch):
        _write_withhold_file(tmp_path, "calendar\nexcel_v2\n")
        monkeypatch.setenv("DISABLE_OPTIONAL_MCP", "1")
        monkeypatch.setattr(
            "runner.gateway.gateway._WITHHELD_TOOLS_FILE",
            str(tmp_path / "disabled_mcp_servers.txt"),
        )
        assert _withheld_tools_from_file() == {}

"""Tool disabling: the other half of the per-app tool_aliases configs.

The world-file half of DEP-1034. A platform mounting two apps that overlap
(Filesystem and Stirrup Code Execution both expose ``read_image_file``) can
withhold one of them without touching the other, and the config rides the same
snapshot files aliasing already uses — so it reaches every delivery island
without any per-island export code.
"""

import inspect
import json

import pytest
from fastmcp import Client as FastMCPClient
from fastmcp import FastMCP

from runner.gateway.gateway import (
    _DisabledToolsMiddleware,
    _discover_tool_alias_specs,
    _gateway_middleware,
    _install_tool_aliases,
    _ToolAliasMiddleware,
    swap_mcp_app,
)
from runner.gateway.models import ToolAliasSpec


def _write_config(
    root,
    app: str,
    *,
    aliases: dict[str, str] | None = None,
    disabled: list[str] | None = None,
    world: bool = False,
) -> None:
    """Stage a per-app config the way the task (or world) snapshot would.

    Mirrors the writer: a key is omitted when empty, so an alias-only config is
    byte-identical to what shipped before ``disabled_tools`` existed.
    """
    filename = "tool_aliases.world.json" if world else "tool_aliases.json"
    path = root / app / ".config" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, object] = {}
    if aliases:
        payload["aliases"] = aliases
    if disabled:
        payload["disabled_tools"] = disabled
    path.write_text(json.dumps(payload))


def _two_app_gateway() -> FastMCP:
    """The GDM pair: two servers, one shared bare tool name."""
    server = FastMCP("test")

    @server.tool
    def filesystem_read_image_file(path: str) -> str:
        return f"fs {path}"

    @server.tool
    def filesystem_list_files() -> str:
        return "listed"

    @server.tool
    def stirrup_code_execution_read_image_file(path: str) -> str:
        return f"stirrup {path}"

    return server


class TestTheMiddleware:
    @pytest.mark.asyncio
    async def test_a_disabled_tool_is_not_listed(self) -> None:
        server = FastMCP("test", middleware=[_DisabledToolsMiddleware({"hidden"})])

        @server.tool
        def hidden() -> str:
            return "no"

        @server.tool
        def shown() -> str:
            return "yes"

        async with FastMCPClient(server) as client:
            assert [t.name for t in await client.list_tools()] == ["shown"]

    @pytest.mark.asyncio
    async def test_a_disabled_tool_is_unreachable_not_merely_unlisted(self) -> None:
        """Hiding alone would not be enough: a golden recorded before the tool
        was disabled, or a model guessing the name, still reaches it."""
        server = FastMCP("test", middleware=[_DisabledToolsMiddleware({"hidden"})])

        @server.tool
        def hidden() -> str:
            return "no"

        @server.tool
        def shown() -> str:
            return "yes"

        async with FastMCPClient(server) as client:
            with pytest.raises(Exception, match="Unknown tool"):
                _ = await client.call_tool("hidden", {})
            assert (await client.call_tool("shown", {})).data == "yes"

    @pytest.mark.asyncio
    async def test_the_rejection_is_indistinguishable_from_a_missing_tool(self) -> None:
        """A distinct error would confirm the tool exists and is withheld, and
        a model probing names would learn which guesses were real."""
        server = FastMCP("test", middleware=[_DisabledToolsMiddleware({"hidden"})])

        @server.tool
        def hidden() -> str:
            return "no"

        async with FastMCPClient(server) as client:
            with pytest.raises(Exception) as disabled:
                _ = await client.call_tool("hidden", {})
            with pytest.raises(Exception) as never_existed:
                _ = await client.call_tool("no_such_tool", {})

        assert "hidden" in str(disabled.value)
        assert type(disabled.value) is type(never_existed.value)

    @pytest.mark.asyncio
    async def test_an_empty_set_is_a_pass_through(self) -> None:
        """Registered on every rollout, so the inert path must cost nothing."""
        server = FastMCP("test", middleware=[_DisabledToolsMiddleware()])

        @server.tool
        def shown() -> str:
            return "yes"

        async with FastMCPClient(server) as client:
            assert [t.name for t in await client.list_tools()] == ["shown"]
            assert (await client.call_tool("shown", {})).data == "yes"


class TestScopingToOneApp:
    """The property the whole feature turns on: two apps, one bare tool name."""

    @pytest.mark.asyncio
    async def test_one_app_loses_the_tool_and_the_other_keeps_it(
        self, tmp_path
    ) -> None:
        _write_config(tmp_path, "filesystem", disabled=["read_image_file"])
        server = _two_app_gateway()
        disable_mw = _DisabledToolsMiddleware()

        await _install_tool_aliases(
            server,
            _ToolAliasMiddleware(),
            disable_mw,
            ["filesystem", "stirrup_code_execution"],
            apps_data_root=str(tmp_path),
        )

        assert disable_mw.disabled_observed == {"filesystem_read_image_file"}

    @pytest.mark.asyncio
    async def test_a_single_server_gateway_matches_the_bare_name(
        self, tmp_path
    ) -> None:
        """One server serves UNPREFIXED names, so the same stored bare name has
        to match there too — which is why the config stores bare names and the
        prefix is re-derived per rollout rather than baked in."""
        _write_config(tmp_path, "filesystem", disabled=["read_image_file"])
        server = FastMCP("test")

        @server.tool
        def read_image_file(path: str) -> str:
            return path

        disable_mw = _DisabledToolsMiddleware()
        await _install_tool_aliases(
            server,
            _ToolAliasMiddleware(),
            disable_mw,
            ["filesystem"],
            apps_data_root=str(tmp_path),
        )

        assert disable_mw.disabled_observed == {"read_image_file"}

    @pytest.mark.asyncio
    async def test_an_unbound_config_cannot_withhold_a_serving_apps_tool(
        self, tmp_path
    ) -> None:
        """A disable must name a live MCP server, in single-server mode too.

        The multi-server skip above does not fire here, and _alias_prefix_for
        ignores scope_server entirely when there is one server — it matches on
        bare equality. That fallback is deliberate for ALIASES: the worst case
        is a rename applied from an unexpected directory. A disable inverts the
        cost. Here the only serving app is stirrup, the config belongs to
        filesystem (rest-only, or a directory left behind by a platform edit),
        and without the guard it withholds stirrup's read_image_file — a tool
        nobody named."""
        _write_config(tmp_path, "filesystem", disabled=["read_image_file"])
        server = FastMCP("test")

        @server.tool
        def read_image_file(path: str) -> str:
            return path

        disable_mw = _DisabledToolsMiddleware()
        await _install_tool_aliases(
            server,
            _ToolAliasMiddleware(),
            disable_mw,
            ["stirrup_code_execution"],
            apps_data_root=str(tmp_path),
        )

        assert disable_mw.disabled_observed == set()

    @pytest.mark.asyncio
    async def test_a_name_matching_nothing_withholds_nothing(self, tmp_path) -> None:
        """Warning only. An unknown name hides nothing, which is the direction
        that cannot surprise anyone — and this code runs inside delivered
        bundles, where raising would take the customer's world down."""
        _write_config(tmp_path, "filesystem", disabled=["renamed_since_authoring"])
        server = _two_app_gateway()
        disable_mw = _DisabledToolsMiddleware()

        await _install_tool_aliases(
            server,
            _ToolAliasMiddleware(),
            disable_mw,
            ["filesystem", "stirrup_code_execution"],
            apps_data_root=str(tmp_path),
        )

        assert disable_mw.disabled_observed == set()


class TestComposingWithAliases:
    @pytest.mark.asyncio
    async def test_an_alias_cannot_resurrect_a_disabled_tool(self, tmp_path) -> None:
        """Disables install first and the alias pass runs over what remains, so
        a config that both renames and withholds one tool cannot serve it."""
        _write_config(
            tmp_path,
            "filesystem",
            aliases={"read_image_file": "read_img"},
            disabled=["read_image_file"],
        )
        server = _two_app_gateway()
        alias_mw = _ToolAliasMiddleware()
        disable_mw = _DisabledToolsMiddleware()

        await _install_tool_aliases(
            server,
            alias_mw,
            disable_mw,
            ["filesystem", "stirrup_code_execution"],
            apps_data_root=str(tmp_path),
        )

        assert disable_mw.disabled_observed == {"filesystem_read_image_file"}
        assert alias_mw.alias_by_observed == {}

    @pytest.mark.asyncio
    async def test_disabling_and_aliasing_run_end_to_end_together(
        self, tmp_path
    ) -> None:
        """The stack assembled as swap_mcp_app assembles it: both middlewares in
        the constructor, disables resolved before renames."""
        _write_config(
            tmp_path,
            "filesystem",
            aliases={"list_files": "browse"},
            disabled=["read_image_file"],
        )
        alias_mw = _ToolAliasMiddleware()
        disable_mw = _DisabledToolsMiddleware()
        server = FastMCP("test", middleware=_gateway_middleware(alias_mw, disable_mw))

        @server.tool
        def filesystem_read_image_file(path: str) -> str:
            return f"fs {path}"

        @server.tool
        def filesystem_list_files() -> str:
            return "listed"

        @server.tool
        def stirrup_code_execution_read_image_file(path: str) -> str:
            return f"stirrup {path}"

        await _install_tool_aliases(
            server,
            alias_mw,
            disable_mw,
            ["filesystem", "stirrup_code_execution"],
            apps_data_root=str(tmp_path),
        )

        async with FastMCPClient(server) as client:
            names = {t.name for t in await client.list_tools()}
            # The kept sibling is still callable under its own name.
            kept = await client.call_tool(
                "stirrup_code_execution_read_image_file", {"path": "/x"}
            )
            # The rename applies to a tool that survived the disable pass.
            renamed = await client.call_tool("filesystem_browse", {})
            with pytest.raises(Exception, match="Unknown tool"):
                _ = await client.call_tool("filesystem_read_image_file", {"path": "/x"})

        assert names == {
            "filesystem_browse",
            "stirrup_code_execution_read_image_file",
        }
        assert kept.data == "stirrup /x"
        assert renamed.data == "listed"


class TestTheTwoLayersUnion:
    @pytest.mark.asyncio
    async def test_a_task_adds_to_what_the_world_withholds(self, tmp_path) -> None:
        _write_config(tmp_path, "filesystem", disabled=["read_image_file"], world=True)
        _write_config(tmp_path, "filesystem", disabled=["list_files"])

        spec = _discover_tool_alias_specs(str(tmp_path))["filesystem"]

        assert sorted(spec.disabled_tools) == ["list_files", "read_image_file"]

    @pytest.mark.asyncio
    async def test_a_task_cannot_un_withhold_what_the_world_withholds(
        self, tmp_path
    ) -> None:
        """Disables union additively, unlike aliases where the task wins: a
        per-task un-disable would serve a tool the world's delivery contract
        says is withheld, and nothing downstream would flag it."""
        _write_config(tmp_path, "filesystem", disabled=["read_image_file"], world=True)
        _write_config(tmp_path, "filesystem", aliases={"list_files": "browse"})

        spec = _discover_tool_alias_specs(str(tmp_path))["filesystem"]

        assert spec.disabled_tools == ["read_image_file"]
        assert spec.aliases == {"list_files": "browse"}

    def test_a_world_alias_loses_to_a_task_disable(self, tmp_path) -> None:
        """Disable wins over a rename for the same tool, silently: the pair can
        only arise from the union of two layers, and raising would drop the
        whole app to canonical names."""
        _write_config(tmp_path, "filesystem", aliases={"read": "fetch"}, world=True)
        _write_config(tmp_path, "filesystem", disabled=["read"])

        spec = _discover_tool_alias_specs(str(tmp_path))["filesystem"]

        assert spec.disabled_tools == ["read"]
        assert spec.aliases == {}

    def test_an_unroutable_alias_union_still_withholds(self, tmp_path) -> None:
        """The two halves fail in opposite directions, so they cannot share a
        fate. Neither layer's ALIASES collide alone; unioned they serve 'fetch'
        twice, which is unroutable. Dropping the renames is safe — the app
        serves canonical names. Dropping the DISABLES would hand back a tool the
        world's delivery contract withholds, silently, behind a container-log
        warning nobody reads.

        Reachable through Studio alone: only the task save path checks the union
        (_check_union_routable), so a task alias saved first and a colliding
        world alias added later arrives here with both files valid.
        """
        _write_config(
            tmp_path,
            "filesystem",
            aliases={"read_image_file": "fetch"},
            disabled=["delete_file"],
            world=True,
        )
        _write_config(tmp_path, "filesystem", aliases={"list_files": "fetch"})

        spec = _discover_tool_alias_specs(str(tmp_path))["filesystem"]

        assert spec.disabled_tools == ["delete_file"], "the withhold was dropped"
        assert spec.aliases == {}, "an unroutable rename must not survive"

    def test_an_unroutable_alias_union_with_nothing_withheld_drops_the_app(
        self, tmp_path
    ) -> None:
        """The alias-only case is unchanged: the app stays ABSENT rather than
        becoming an empty spec, so no caller has to tell one from the other."""
        _write_config(tmp_path, "docs", aliases={"read_document_content": "fetch"})
        _write_config(
            tmp_path, "docs", aliases={"create_document": "fetch"}, world=True
        )

        assert "docs" not in _discover_tool_alias_specs(str(tmp_path))


def _write_raw(root, app: str, body: str, *, world: bool = False) -> None:
    """Stage a config the writer would never produce — a hand-edited file."""
    filename = "tool_aliases.world.json" if world else "tool_aliases.json"
    path = root / app / ".config" / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


class TestARejectedFileStillWithholds:
    """A file the model refuses must not hand a withheld tool back.

    Same asymmetry the unroutable-union branch honours: dropping a rename costs
    a cosmetic name, dropping a withhold serves the tool. Studio never writes
    such a file, so this is the hand-edited world snapshot on a non-delivered
    rollout, where the bundle validator never runs.
    """

    def test_an_unknown_key_does_not_take_the_withholds_with_it(self, tmp_path) -> None:
        """`extra="forbid"` rejects the whole file — including, before this, the
        one key whose loss actually serves a tool."""
        _write_raw(
            tmp_path,
            "filesystem",
            '{"disabled_tools": ["read_image_file"], "typo_key": 1}',
        )
        spec = _discover_tool_alias_specs(str(tmp_path))["filesystem"]
        assert spec.disabled_tools == ["read_image_file"]
        assert spec.aliases == {}

    def test_a_rejected_rename_is_not_salvaged_with_the_withhold(
        self, tmp_path
    ) -> None:
        """Only the withholds come back. Honouring a remap out of a map the
        model refused would trust the half that can SERVE a tool under a new
        name."""
        _write_raw(
            tmp_path,
            "filesystem",
            '{"aliases": {"list_files": "bad name!"}, "disabled_tools": ["read_image_file"]}',
        )
        spec = _discover_tool_alias_specs(str(tmp_path))["filesystem"]
        assert spec.disabled_tools == ["read_image_file"]
        assert spec.aliases == {}

    def test_one_unusable_name_does_not_lose_the_usable_ones(self, tmp_path) -> None:
        """A name that cannot match a live tool is dropped alone. Discarding the
        whole list over it would give a real tool back for nothing."""
        _write_raw(
            tmp_path,
            "filesystem",
            '{"disabled_tools": ["read_image_file", "bad name!"], "typo_key": 1}',
        )
        spec = _discover_tool_alias_specs(str(tmp_path))["filesystem"]
        assert spec.disabled_tools == ["read_image_file"]

    def test_unparseable_bytes_lose_the_withholds(self, tmp_path) -> None:
        """The honest limit: with no JSON there are no names to keep. Pinned so
        the salvage is never mistaken for a guarantee."""
        _write_raw(tmp_path, "filesystem", '{"disabled_tools": ["read_image_f')
        assert _discover_tool_alias_specs(str(tmp_path)) == {}

    def test_a_rejected_file_does_not_resurrect_an_app_with_no_withholds(
        self, tmp_path
    ) -> None:
        """An alias-only rejected file still leaves the app absent, rather than
        becoming an empty spec."""
        _write_raw(tmp_path, "docs", '{"aliases": {"read": "bad name!"}}')
        assert _discover_tool_alias_specs(str(tmp_path)) == {}

    def test_a_rejected_world_file_still_withholds_under_a_valid_task_file(
        self, tmp_path
    ) -> None:
        """Layers compose as usual: the salvaged world withhold unions with the
        task's own instead of being replaced by it."""
        _write_raw(
            tmp_path,
            "filesystem",
            '{"disabled_tools": ["read_image_file"], "typo_key": 1}',
            world=True,
        )
        _write_config(tmp_path, "filesystem", disabled=["list_files"])
        spec = _discover_tool_alias_specs(str(tmp_path))["filesystem"]
        assert sorted(spec.disabled_tools) == ["list_files", "read_image_file"]


class TestUntrustedConfigInput:
    """These files are operator-authored and cross into the container."""

    def test_a_bad_app_directory_name_is_skipped(self, tmp_path) -> None:
        _write_config(tmp_path, "bad name!", disabled=["read"])
        assert _discover_tool_alias_specs(str(tmp_path)) == {}

    def test_a_config_escaping_the_apps_data_root_is_skipped(self, tmp_path) -> None:
        outside = tmp_path.parent / "outside"
        (outside / ".config").mkdir(parents=True, exist_ok=True)
        (outside / ".config" / "tool_aliases.json").write_text(
            json.dumps({"disabled_tools": ["read"]})
        )
        root = tmp_path / "root"
        root.mkdir()
        (root / "escaped").symlink_to(outside, target_is_directory=True)

        assert _discover_tool_alias_specs(str(root)) == {}

    def test_an_unparseable_config_is_skipped_not_fatal(self, tmp_path) -> None:
        path = tmp_path / "filesystem" / ".config" / "tool_aliases.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        assert _discover_tool_alias_specs(str(tmp_path)) == {}

    def test_a_disabled_name_outside_the_charset_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="invalid disabled tool name"):
            _ = ToolAliasSpec(disabled_tools=["../../etc/passwd"])

    def test_an_unknown_key_is_still_a_rejected_file(self) -> None:
        """extra="forbid" is the reason Studio omits disabled_tools when empty."""
        with pytest.raises(ValueError):
            _ = ToolAliasSpec.model_validate_json(json.dumps({"hidden": ["read"]}))


class TestInstallOrderingInSwap:
    def test_disables_install_before_the_alias_map_can_abort_the_swap(self) -> None:
        """Alias resolution can raise on an ambiguous map, and by then the new
        app is live. Disables are installed inside _install_tool_aliases BEFORE
        that resolve, so an alias failure loses renames but never a disable.
        Asserted on the source: it is a property of the install body, not of
        anything it returns.
        """
        source = inspect.getsource(_install_tool_aliases)
        install_at = source.index("disable_middleware.install(")
        validate_at = source.index("_validate_resolved_alias_map(")
        assert install_at < validate_at

    def test_swap_passes_both_middlewares(self) -> None:
        source = inspect.getsource(swap_mcp_app)
        assert "_DisabledToolsMiddleware()" in source
        assert "disable_middleware" in source

    def test_the_disable_layer_sits_between_aliasing_and_the_coordinator(self) -> None:
        """Inbound it must run after the alias->canonical rewrite so it matches
        canonical names, and before the coordinator so a refused call is never
        recorded as one the agent made."""
        stack = _gateway_middleware(_ToolAliasMiddleware(), _DisabledToolsMiddleware())
        kinds = [type(m).__name__ for m in stack]
        assert kinds == [
            "_ToolAliasMiddleware",
            "_DisabledToolsMiddleware",
            "CoordinatorToolCallMiddleware",
        ]

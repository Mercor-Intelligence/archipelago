"""Tool-name aliasing: middleware behavior, resolution, and config discovery."""

import asyncio
import inspect
import json
from typing import Any, Literal

import pytest
from fastmcp import Client as FastMCPClient
from fastmcp import FastMCP
from pydantic import BaseModel, Field

from runner.gateway.gateway import (
    _AllowedToolsMiddleware,
    _discover_tool_alias_specs,
    _gateway_middleware,
    _install_tool_aliases,
    _name_rewriter,
    _resolve_tool_alias_map,
    _rewrite_schema_prose,
    _ToolAliasMiddleware,
    _validate_resolved_alias_map,
    swap_mcp_app,
)
from runner.gateway.models import ToolAliasSpec


def _server_with_alias(alias_map: dict[str, str]) -> FastMCP:
    server = FastMCP("test", middleware=[_ToolAliasMiddleware(alias_map)])

    @server.tool
    def read_document_content(file_path: str) -> str:
        return f"read {file_path}"

    @server.tool
    def create_document(file_path: str) -> str:
        return f"created {file_path}"

    return server


def _write_config(root, app: str, aliases: dict[str, str]) -> None:
    """Stage a per-app alias config the way the task snapshot would."""
    path = root / app / ".config" / "tool_aliases.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"aliases": aliases}))


def _write_world_config(root, app: str, aliases: dict[str, str]) -> None:
    """Stage an app's WORLD-level defaults, as the world snapshot would.

    A sibling of the task layer's file, not the same path — that is what lets
    both survive the key-level snapshot merge and be unioned per tool.
    """
    path = root / app / ".config" / "tool_aliases.world.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"aliases": aliases}))


@pytest.mark.asyncio
async def test_list_tools_serves_alias_names() -> None:
    server = _server_with_alias({"read_document_content": "read_doc"})
    async with FastMCPClient(server) as client:
        tools = await client.list_tools()
    assert sorted(t.name for t in tools) == ["create_document", "read_doc"]


@pytest.mark.asyncio
async def test_call_by_alias_routes_to_canonical() -> None:
    server = _server_with_alias({"read_document_content": "read_doc"})
    async with FastMCPClient(server) as client:
        result = await client.call_tool("read_doc", {"file_path": "/a.docx"})
    assert result.data == "read /a.docx"


@pytest.mark.asyncio
async def test_call_by_canonical_is_rejected_when_aliased() -> None:
    """Fail-closed: a rollout must not be satisfiable via canonical names,
    or 'agent saw aliases' is indistinguishable from 'agent guessed'."""
    server = _server_with_alias({"read_document_content": "read_doc"})
    async with FastMCPClient(server) as client:
        with pytest.raises(Exception, match="Unknown tool"):
            await client.call_tool("read_document_content", {"file_path": "/a.docx"})


@pytest.mark.asyncio
async def test_rejection_does_not_leak_the_alias_to_the_agent() -> None:
    """The rejection above must not tell the model what it just learned.

    Tool errors are returned to the model verbatim, so an error naming the
    alias would hand over the canonical->alias pair aliasing exists to
    withhold, and a distinctly-shaped error would still confirm the guessed
    canonical name is real. Both the type and the text must match what
    fastmcp raises for a name that was never a tool.
    """
    # An alias that shares no substring with the canonical name, so the
    # "did not leak" assertion below can't pass by accident.
    server = _server_with_alias({"read_document_content": "zeta_blob"})
    errors: dict[str, tuple[str, str]] = {}
    async with FastMCPClient(server) as client:
        for name in ("read_document_content", "no_such_tool_at_all"):
            with pytest.raises(Exception) as exc_info:  # noqa: PT011
                await client.call_tool(name, {"file_path": "/a.docx"})
            errors[name] = (type(exc_info.value).__name__, str(exc_info.value))

    aliased_type, aliased_text = errors["read_document_content"]
    unknown_type, unknown_text = errors["no_such_tool_at_all"]
    assert aliased_type == unknown_type
    # Same sentence, differing only in the name the agent itself supplied.
    assert aliased_text == unknown_text.replace(
        "no_such_tool_at_all", "read_document_content"
    )
    assert "zeta_blob" not in aliased_text


@pytest.mark.asyncio
async def test_unaliased_tool_passes_through() -> None:
    server = _server_with_alias({"read_document_content": "read_doc"})
    async with FastMCPClient(server) as client:
        result = await client.call_tool("create_document", {"file_path": "/b.docx"})
    assert result.data == "created /b.docx"


@pytest.mark.asyncio
async def test_empty_middleware_is_pass_through_until_installed() -> None:
    """The middleware is registered at construction but resolved after
    readiness; the window in between must not alter anything."""
    mw = _ToolAliasMiddleware()
    server = FastMCP("test", middleware=[mw])

    @server.tool
    def read_document_content(file_path: str) -> str:
        return f"read {file_path}"

    async with FastMCPClient(server) as client:
        assert [t.name for t in await client.list_tools()] == ["read_document_content"]
        result = await client.call_tool(
            "read_document_content", {"file_path": "/a.docx"}
        )
        assert result.data == "read /a.docx"

        # Same instance, now resolved — no re-registration needed.
        mw.install({"read_document_content": "read_doc"})
        assert [t.name for t in await client.list_tools()] == ["read_doc"]
        result = await client.call_tool("read_doc", {"file_path": "/a.docx"})
        assert result.data == "read /a.docx"


class TestToolMetadataScrub:
    """Renaming Tool.name alone is not enough. The canonical name also rides
    in the description (free text, verbatim) and in title/annotations.title
    (MCP's human label, conventionally a prettified rendering of the name)."""

    @staticmethod
    def _server(mw: _ToolAliasMiddleware) -> FastMCP:
        server = FastMCP("docs", middleware=[mw])

        @server.tool(annotations={"title": "Read Document Content"})
        def read_document_content(path: str) -> str:
            """Read a doc. Use read_document_content for big ones, then send_mail."""
            return "x"

        @server.tool
        def send_mail(to: str) -> str:
            """Send mail."""
            return "x"

        @server.tool
        def helper(x: str) -> str:
            """Helper. Call read_document_content first."""
            return "x"

        return server

    @pytest.mark.asyncio
    async def test_description_no_longer_names_the_tool(self) -> None:
        mw = _ToolAliasMiddleware()
        mw.install({"read_document_content": "read_doc", "send_mail": "dispatch"})
        async with FastMCPClient(self._server(mw)) as client:
            by_name = {t.name: t for t in await client.list_tools()}
        assert "read_document_content" not in (by_name["read_doc"].description or "")
        assert "read_doc" in (by_name["read_doc"].description or "")

    @pytest.mark.asyncio
    async def test_cross_references_between_tools_are_rewritten(self) -> None:
        """The substitution uses the WHOLE map, so one tool's description
        cannot leak another tool's real name — including from a tool that is
        not itself aliased."""
        mw = _ToolAliasMiddleware()
        mw.install({"read_document_content": "read_doc", "send_mail": "dispatch"})
        async with FastMCPClient(self._server(mw)) as client:
            by_name = {t.name: t for t in await client.list_tools()}
        assert "send_mail" not in (by_name["read_doc"].description or "")
        assert "dispatch" in (by_name["read_doc"].description or "")
        # `helper` is unaliased but references an aliased tool.
        assert "read_document_content" not in (by_name["helper"].description or "")
        assert "read_doc" in (by_name["helper"].description or "")

    @pytest.mark.asyncio
    async def test_titles_are_dropped_for_aliased_tools(self) -> None:
        """ "Read Document Content" is a transformation of the name, not the
        name — no substitution can catch it, and inventing a new title from
        the alias would be fabricating text. So it is removed."""
        mw = _ToolAliasMiddleware()
        mw.install({"read_document_content": "read_doc"})
        async with FastMCPClient(self._server(mw)) as client:
            tool = {t.name: t for t in await client.list_tools()}["read_doc"]
        assert not getattr(tool, "title", None)
        assert not getattr(getattr(tool, "annotations", None), "title", None)

    @pytest.mark.asyncio
    async def test_unaliased_rollout_leaves_metadata_untouched(self) -> None:
        mw = _ToolAliasMiddleware()
        async with FastMCPClient(self._server(mw)) as client:
            tool = {t.name: t for t in await client.list_tools()}[
                "read_document_content"
            ]
        assert tool.title == "Read Document Content"
        assert "read_document_content" in (tool.description or "")


class TestSchemaProseScrub:
    """The schemas carry prose of their own, served in the same list_tools
    payload: a nested model's docstring and a Field(description=...) both land
    inside `parameters`, at arbitrary depth (Devin: "Canonical name can leak
    through parameter schema text"). Only prose is rewritten — argument names
    and validated values are the call contract, not display text.
    """

    @staticmethod
    def _server(mw: _ToolAliasMiddleware) -> FastMCP:
        server = FastMCP("docs", middleware=[mw])

        class Opts(BaseModel):
            """Options for read_document_content."""

            # An enum whose value happens to equal the tool name: data the
            # backend validates against, so it must survive untouched even
            # though it reads like a leak.
            mode: Literal["text", "read_document_content"] = "text"
            fallback: str = Field(
                "read_document_content",
                description="Tool to retry with, e.g. read_document_content.",
            )

        @server.tool
        def read_document_content(
            read_document_content_path: str,
            opts: Opts | None = None,
        ) -> str:
            """Read a doc."""
            return "x"

        return server

    def _schema(self, mw: _ToolAliasMiddleware) -> dict[str, Any]:
        async def go() -> dict[str, Any]:
            async with FastMCPClient(self._server(mw)) as client:
                (tool,) = await client.list_tools()
                return tool.inputSchema

        return asyncio.run(go())

    def test_nested_prose_no_longer_names_the_tool(self) -> None:
        mw = _ToolAliasMiddleware()
        mw.install({"read_document_content": "read_doc"})
        blob = json.dumps(self._schema(mw))
        # The nested model docstring and the Field description both sat here.
        assert "Options for read_doc." in blob
        assert "e.g. read_doc." in blob

    def test_argument_names_and_validated_values_are_untouched(self) -> None:
        """Rewriting a property KEY would unbind the backend's argument, and
        rewriting an enum/default would fail validation on a legitimate call.
        Both must survive even when they contain the canonical name."""
        mw = _ToolAliasMiddleware()
        mw.install({"read_document_content": "read_doc"})
        schema = self._schema(mw)
        props = schema["properties"]
        assert "read_document_content_path" in props
        opts = next(
            variant
            for variant in props["opts"]["anyOf"]
            if variant.get("type") == "object"
        )
        assert opts["properties"]["mode"]["enum"] == [
            "text",
            "read_document_content",
        ]
        assert opts["properties"]["fallback"]["default"] == "read_document_content"

    def test_unaliased_rollout_serves_an_identical_schema(self) -> None:
        mw = _ToolAliasMiddleware()
        before = self._schema(_ToolAliasMiddleware())
        mw.install({"create_document": "make_doc"})
        # A tool that is not itself aliased and mentions no aliased sibling:
        # the schema must come back untouched, not re-materialized.
        assert self._schema(mw) == before

    def test_output_schema_prose_is_scrubbed_without_reshaping(self) -> None:
        mw = _ToolAliasMiddleware()
        mw.install({"read_document_content": "read_doc"})
        rewritten = _rewrite_schema_prose(
            {
                "type": "object",
                "properties": {
                    "result": {
                        "type": "string",
                        "description": "Output of read_document_content.",
                    }
                },
                "required": ["result"],
                "x-fastmcp-wrap-result": True,
            },
            mw._rewrite_text,  # pyright: ignore[reportPrivateUsage]
        )
        assert rewritten["properties"]["result"]["description"] == "Output of read_doc."
        # Structure is a contract a client validates against; only prose moved.
        assert rewritten["properties"]["result"]["type"] == "string"
        assert rewritten["required"] == ["result"]
        assert rewritten["x-fastmcp-wrap-result"] is True

    def test_a_schema_with_no_mention_is_returned_as_is(self) -> None:
        """Identity, not equality: the un-rewritten path must hand back the
        same object so an unaliased tool is served without a rebuilt schema."""
        schema = {"type": "object", "properties": {"path": {"type": "string"}}}
        rewrite = _name_rewriter({"read_document_content": "read_doc"}, [])
        assert _rewrite_schema_prose(schema, rewrite) is schema


class TestNameRewriter:
    def test_name_boundaries_stop_a_short_name_corrupting_a_longer_one(self) -> None:
        """A boundary is "not in the middle of a name", so `read` cannot match
        inside read_document_content."""
        rewrite = _name_rewriter({"read": "fetch"}, [])
        assert rewrite("read_document_content and read it") == (
            "read_document_content and fetch it"
        )

    def test_a_hyphenated_name_does_not_corrupt_a_longer_sibling(self) -> None:
        """TOOL_ALIAS_NAME_RE admits `-`, which is NOT a word character — so
        `\\b` would place a boundary right after it and let `get-user` rewrite
        inside the UNALIASED `get-user-profile`, turning a real tool into one
        that does not exist (Bugbot: "Hyphenated names break rewriter
        boundaries"). Sibling prose survives; the standalone mention still
        goes."""
        rewrite = _name_rewriter({"get-user": "fetch_person"}, [])
        assert rewrite("use get-user-profile, not get-user") == (
            "use get-user-profile, not fetch_person"
        )

    def test_a_hyphen_cannot_open_a_boundary_mid_name(self) -> None:
        """The mirror of the above: a name is not matchable at its tail
        either, so `user-profile` stays put inside `get-user-profile`."""
        rewrite = _name_rewriter({"user-profile": "person_card"}, [])
        assert rewrite("get-user-profile vs user-profile") == (
            "get-user-profile vs person_card"
        )

    def test_a_hyphenated_english_word_is_left_alone(self) -> None:
        """`read` aliased must not turn the prose word `pre-read` into
        `pre-fetch`: the hyphen is part of the surrounding token, not a
        boundary."""
        rewrite = _name_rewriter({"read": "fetch"}, [])
        assert (
            rewrite("pre-read the docs, then read") == "pre-read the docs, then fetch"
        )

    def test_prefixed_and_bare_forms_both_rewrite(self) -> None:
        """App authors write the unprefixed name; the gateway serves the
        prefixed one. Both must be scrubbed."""
        rewrite = _name_rewriter({"docs_search": "docs_find"}, ["docs", "email"])
        assert rewrite("call docs_search or search") == "call docs_find or find"

    def test_no_aliases_is_identity(self) -> None:
        assert _name_rewriter({}, ["docs"])("read_document_content") == (
            "read_document_content"
        )

    def test_longest_server_prefix_wins(self) -> None:
        """A server name that prefixes another must not claim its tools.

        With servers `mail` and `mail_client`, `mail_client_send` has to
        strip `mail_client_` to recover the bare `send` an app author writes
        in a description. Stripping `mail_` instead registers `client_send`
        — a form nobody writes — and leaves the real bare mention unscrubbed,
        leaking the canonical name (Bugbot: "Rewriter prefix collision
        leak").
        """
        rewrite = _name_rewriter(
            {"mail_client_send": "mail_client_dispatch"}, ["mail", "mail_client"]
        )
        assert rewrite("call mail_client_send or send") == (
            "call mail_client_dispatch or dispatch"
        )

    def test_shorter_prefix_still_wins_when_it_is_the_real_one(self) -> None:
        """The longest-first walk is not blind: it only takes a prefix both
        sides carry. Server `mail` owning a tool literally named
        `client_send` still resolves to the bare `client_send`, because the
        alias is `mail_`-prefixed and cannot match `mail_client_`."""
        rewrite = _name_rewriter(
            {"mail_client_send": "mail_dispatch"}, ["mail", "mail_client"]
        )
        assert rewrite("call mail_client_send or client_send") == (
            "call mail_dispatch or dispatch"
        )


class TestGatewayMiddlewareOrder:
    def test_alias_runs_before_the_coordinator(self) -> None:
        """fastmcp runs middleware in add order, so the alias->canonical
        rewrite must be registered AHEAD of CoordinatorToolCallMiddleware —
        otherwise record_tool_call stores aliases and every canonical-keyed
        tool_call_seen trigger and VCA persona allowlist stops matching."""
        mw = _ToolAliasMiddleware()
        stack = _gateway_middleware(mw)
        assert stack[0] is mw
        assert type(stack[1]).__name__ == "CoordinatorToolCallMiddleware"

    def test_none_yields_a_pass_through_not_a_missing_layer(self) -> None:
        stack = _gateway_middleware(None)
        assert isinstance(stack[0], _ToolAliasMiddleware)
        assert type(stack[1]).__name__ == "CoordinatorToolCallMiddleware"

    def test_alias_install_runs_after_the_allowlist_in_swap(self) -> None:
        """swap_mcp_app must install the allowlist BEFORE resolving aliases.

        By the time either runs the new app is already published and serving,
        and _install_tool_aliases can raise on an ambiguous map. If it ran
        first, that raise would abort the swap with a LIVE gateway that never
        got _AllowedToolsMiddleware — serving every tool unfiltered while
        /apps reports failure (Bugbot: "Alias failure skips allowlist
        middleware"). Asserted on the source because the ordering is a
        property of the swap body, not of any object it returns.
        """
        source = inspect.getsource(swap_mcp_app)
        allowlist_at = source.index("_AllowedToolsMiddleware(")
        enum_strip_at = source.index("_StripNonStringEnumsMiddleware(")
        alias_at = source.index("await _install_tool_aliases(")
        assert allowlist_at < alias_at, "allowlist must be installed before aliasing"
        assert enum_strip_at < alias_at, "enum strip must be installed before aliasing"


@pytest.mark.asyncio
async def test_allowlist_and_aliasing_compose_in_both_directions() -> None:
    """Run a rollout with BOTH the tool filter and aliasing active.

    The ordering claim in _gateway_middleware is asserted statically
    elsewhere; this exercises it. The stack is assembled exactly as
    swap_mcp_app assembles it — alias layer in the CONSTRUCTOR, allowlist
    add_middleware'd afterwards — so the test fails if fastmcp's outbound
    order ever stops being the mirror of its inbound order (Devin:
    "Allowlist + alias ordering on list_tools is untested together").

    Inbound: the alias->canonical rewrite runs first, so the allowlist keeps
    matching the CANONICAL names it was configured with. Outbound: the
    allowlist filters on canonical names first and the rename happens last,
    so a filtered rollout still advertises aliases.
    """
    alias_mw = _ToolAliasMiddleware()
    server = FastMCP("test", middleware=_gateway_middleware(alias_mw))

    @server.tool
    def read_document_content(file_path: str) -> str:
        return f"read {file_path}"

    @server.tool
    def create_document(file_path: str) -> str:
        return f"created {file_path}"

    @server.tool
    def delete_document(file_path: str) -> str:
        return f"deleted {file_path}"

    server.add_middleware(
        _AllowedToolsMiddleware(["read_document_content", "create_document"])
    )
    alias_mw.install(
        {"read_document_content": "read_doc", "delete_document": "drop_doc"}
    )

    async with FastMCPClient(server) as client:
        # Filtered on canonical names, then renamed: create_document is
        # allowed and unaliased, read_doc is allowed and aliased, and
        # delete_document is gone under BOTH of its names.
        assert sorted(t.name for t in await client.list_tools()) == [
            "create_document",
            "read_doc",
        ]

        # Inbound: the allowlist sees `read_document_content`, not `read_doc`.
        result = await client.call_tool("read_doc", {"file_path": "/a.docx"})
        assert result.data == "read /a.docx"

        # A disallowed tool stays disallowed when called under its alias —
        # the rewrite hands the allowlist the canonical name it rejects.
        with pytest.raises(Exception, match="not in the allowlist"):
            await client.call_tool("drop_doc", {"file_path": "/a.docx"})


@pytest.mark.asyncio
async def test_coordinator_layer_observes_canonical_name() -> None:
    """End-to-end ordering check: a call made under the alias reaches a
    middleware sitting behind the alias layer under its CANONICAL name."""
    seen: list[str] = []

    from fastmcp.server.middleware import Middleware

    class _Recorder(Middleware):
        async def on_call_tool(self, context, call_next):  # noqa: ANN001
            seen.append(context.message.name)
            return await call_next(context)

    alias_mw = _ToolAliasMiddleware({"read_document_content": "read_doc"})
    server = FastMCP("test", middleware=[alias_mw, _Recorder()])

    @server.tool
    def read_document_content(file_path: str) -> str:
        return f"read {file_path}"

    async with FastMCPClient(server) as client:
        await client.call_tool("read_doc", {"file_path": "/a.docx"})

    assert seen == ["read_document_content"]


class TestResolveToolAliasMap:
    def test_maps_canonical_to_alias(self) -> None:
        spec = ToolAliasSpec(aliases={"read_document_content": "read_doc"})
        resolved = _resolve_tool_alias_map(["read_document_content"], spec, ["docs"])
        assert resolved == {"read_document_content": "read_doc"}

    def test_preserves_multi_server_prefix(self) -> None:
        spec = ToolAliasSpec(aliases={"read_document_content": "read_doc"})
        resolved = _resolve_tool_alias_map(
            ["docs_read_document_content"], spec, ["docs", "email"]
        )
        assert resolved == {"docs_read_document_content": "docs_read_doc"}

    def test_unconfigured_tools_are_untouched(self) -> None:
        spec = ToolAliasSpec(aliases={"read_document_content": "read_doc"})
        resolved = _resolve_tool_alias_map(["create_document"], spec, ["docs"])
        assert resolved == {}

    def test_suffix_does_not_steal_unrelated_tool(self) -> None:
        """Canonical "read" must NOT claim "mark_read" (Bugbot 54724cec).
        Single-server gateways serve unprefixed names, so only bare
        equality matches — none of these tools IS "read"."""
        spec = ToolAliasSpec(aliases={"read": "fetch"})
        resolved = _resolve_tool_alias_map(
            ["mark_read", "email_mark_read", "email_read"],
            spec,
            ["email"],
        )
        assert resolved == {}

    def test_multi_server_bare_name_does_not_collide(self) -> None:
        """Multi-server observed names are ALL {server}_-prefixed, so a
        canonical like "email_read" must NOT bind bare to server email's
        tool "read" (Bugbot multi-server bare-name collision) — while
        canonical "read" still binds via the exact prefix form."""
        observed = ["email_read", "docs_email_read"]
        servers = ["email", "docs"]
        hijack = ToolAliasSpec(aliases={"email_read": "hijack"})
        resolved = _resolve_tool_alias_map(observed, hijack, servers)
        assert resolved == {"docs_email_read": "docs_hijack"}
        legit = ToolAliasSpec(aliases={"read": "fetch"})
        resolved = _resolve_tool_alias_map(observed, legit, servers)
        assert resolved == {"email_read": "email_fetch"}

    def test_scope_server_confines_matching_to_one_app(self) -> None:
        """Each config belongs to ONE app. Unscoped, docs' config would also
        rename email's same-named tool."""
        spec = ToolAliasSpec(aliases={"search": "find"})
        observed = ["docs_search", "email_search"]
        servers = ["docs", "email"]

        assert _resolve_tool_alias_map(observed, spec, servers, "docs") == {
            "docs_search": "docs_find"
        }
        assert _resolve_tool_alias_map(observed, spec, servers, "email") == {
            "email_search": "email_find"
        }
        # Unscoped, the first observed name wins — which is exactly the
        # cross-app bleed the scope parameter exists to prevent.
        assert _resolve_tool_alias_map(observed, spec, servers) == {
            "docs_search": "docs_find",
            "email_search": "email_find",
        }

    def test_unknown_scope_falls_back_to_all_servers(self) -> None:
        """The resolver itself still falls back when handed an unknown scope.
        _install_tool_aliases is what refuses to CALL it that way in
        multi-server mode — see TestInstallSkipsUnscopableConfig."""
        spec = ToolAliasSpec(aliases={"search": "find"})
        resolved = _resolve_tool_alias_map(
            ["docs_search"], spec, ["docs", "email"], "not_a_server"
        )
        assert resolved == {"docs_search": "docs_find"}


class TestInstallSkipsUnscopableConfig:
    """A config dir that names no live MCP server can't be scoped.

    In multi-server mode that config is unusable rather than merely unscoped:
    it would bind to some other server's tool and be served under THAT
    server's prefix, while grading's apply_tool_remap keys on this config's
    own app name — so the trajectory would never normalize back to canonical.
    Single-server mode is safe (tools are served unprefixed, and grading
    matches bare names regardless of the directory).
    """

    @pytest.mark.asyncio
    async def test_multi_server_unscopable_config_is_skipped(self, tmp_path) -> None:
        _write_config(tmp_path, "word", {"read": "fetch"})
        mw = _ToolAliasMiddleware()
        server = FastMCP("test")

        @server.tool
        def microsoft_word_read() -> str:
            return "ok"

        await _install_tool_aliases(
            server, mw, ["microsoft_word", "email"], apps_data_root=str(tmp_path)
        )
        assert mw.alias_by_observed == {}

    @pytest.mark.asyncio
    async def test_single_server_unscopable_config_still_applies(
        self, tmp_path
    ) -> None:
        _write_config(tmp_path, "word", {"read": "fetch"})
        mw = _ToolAliasMiddleware()
        server = FastMCP("test")

        @server.tool
        def read() -> str:
            return "ok"

        await _install_tool_aliases(
            server, mw, ["microsoft_word"], apps_data_root=str(tmp_path)
        )
        assert mw.alias_by_observed == {"read": "fetch"}


class TestDiscoverToolAliasSpecs:
    def test_returns_empty_when_no_configs_staged(self, tmp_path) -> None:
        assert _discover_tool_alias_specs(str(tmp_path)) == {}

    def test_returns_empty_when_root_absent(self, tmp_path) -> None:
        assert _discover_tool_alias_specs(str(tmp_path / "nope")) == {}

    def test_loads_one_spec_per_app(self, tmp_path) -> None:
        _write_config(tmp_path, "docs", {"read_document_content": "read_doc"})
        _write_config(tmp_path, "email", {"send_mail": "dispatch"})
        specs = _discover_tool_alias_specs(str(tmp_path))
        assert set(specs) == {"docs", "email"}
        assert specs["docs"].aliases == {"read_document_content": "read_doc"}
        assert specs["email"].aliases == {"send_mail": "dispatch"}

    def test_skips_malformed_file_but_keeps_the_rest(self, tmp_path) -> None:
        """Grading and the gateway must degrade to canonical names on a bad
        config, never take the world down with it."""
        bad = tmp_path / "docs" / ".config" / "tool_aliases.json"
        bad.parent.mkdir(parents=True)
        bad.write_text("{not json")
        _write_config(tmp_path, "email", {"send_mail": "dispatch"})
        assert set(_discover_tool_alias_specs(str(tmp_path))) == {"email"}

    def test_skips_config_failing_validation(self, tmp_path) -> None:
        _write_config(tmp_path, "docs", {"tool_a": "bad name!"})
        assert _discover_tool_alias_specs(str(tmp_path)) == {}

    def test_skips_unknown_keys(self, tmp_path) -> None:
        """extra='forbid': the file crosses a trust boundary, so an
        unrecognized key is a rejected file, not a silently-ignored one."""
        path = tmp_path / "docs" / ".config" / "tool_aliases.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"aliases": {"a": "b"}, "mode": "random"}))
        assert _discover_tool_alias_specs(str(tmp_path)) == {}

    def test_skips_empty_alias_map(self, tmp_path) -> None:
        _write_config(tmp_path, "docs", {})
        assert _discover_tool_alias_specs(str(tmp_path)) == {}

    def test_skips_bad_app_dir_name(self, tmp_path) -> None:
        path = tmp_path / "bad name!" / ".config" / "tool_aliases.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"aliases": {"a": "b"}}))
        assert _discover_tool_alias_specs(str(tmp_path)) == {}

    def test_skips_symlink_escaping_the_root(self, tmp_path) -> None:
        """A symlinked .apps_data/<app> must not pull a config in from
        outside the root."""
        outside = tmp_path / "outside"
        (outside / ".config").mkdir(parents=True)
        (outside / ".config" / "tool_aliases.json").write_text(
            json.dumps({"aliases": {"a": "b"}})
        )
        root = tmp_path / "root"
        root.mkdir()
        (root / "docs").symlink_to(outside, target_is_directory=True)
        assert _discover_tool_alias_specs(str(root)) == {}


class TestDiscoverUnionsWorldAndTaskLayers:
    """The world layer supplies defaults; the task layer overrides them.

    Per TOOL, not per file — which is the whole reason the two layers ship at
    different filenames. See _discover_tool_alias_specs.
    """

    def test_world_only(self, tmp_path) -> None:
        _write_world_config(tmp_path, "docs", {"read_document_content": "read_doc"})
        specs = _discover_tool_alias_specs(str(tmp_path))
        assert specs["docs"].aliases == {"read_document_content": "read_doc"}

    def test_unions_different_tools_of_one_app(self, tmp_path) -> None:
        """The case the whole design exists for: a task renaming one tool must
        not discard the world's rename of a DIFFERENT tool on the same app."""
        _write_world_config(tmp_path, "docs", {"read_document_content": "read_doc"})
        _write_config(tmp_path, "docs", {"create_document": "make_doc"})
        specs = _discover_tool_alias_specs(str(tmp_path))
        assert specs["docs"].aliases == {
            "read_document_content": "read_doc",
            "create_document": "make_doc",
        }

    def test_task_wins_on_the_same_tool(self, tmp_path) -> None:
        _write_world_config(tmp_path, "docs", {"read_document_content": "read_doc"})
        _write_config(tmp_path, "docs", {"read_document_content": "grab"})
        specs = _discover_tool_alias_specs(str(tmp_path))
        assert specs["docs"].aliases == {"read_document_content": "grab"}

    def test_layers_are_scoped_per_app(self, tmp_path) -> None:
        _write_world_config(tmp_path, "docs", {"read_document_content": "read_doc"})
        _write_config(tmp_path, "email", {"send_mail": "dispatch"})
        specs = _discover_tool_alias_specs(str(tmp_path))
        assert specs["docs"].aliases == {"read_document_content": "read_doc"}
        assert specs["email"].aliases == {"send_mail": "dispatch"}

    def test_malformed_world_file_leaves_the_task_layer_in_force(
        self, tmp_path
    ) -> None:
        """Degrading to fewer renames beats degrading to none."""
        bad = tmp_path / "docs" / ".config" / "tool_aliases.world.json"
        bad.parent.mkdir(parents=True)
        bad.write_text("{not json")
        _write_config(tmp_path, "docs", {"create_document": "make_doc"})
        specs = _discover_tool_alias_specs(str(tmp_path))
        assert specs["docs"].aliases == {"create_document": "make_doc"}

    def test_drops_an_app_whose_union_is_unroutable(self, tmp_path) -> None:
        """Neither layer collides alone; together they serve one name twice.

        Dropping the app serves it canonical. Passing the union through would
        reach _validate_resolved_alias_map, which fails the whole rollout.
        """
        _write_world_config(tmp_path, "docs", {"read_document_content": "fetch"})
        _write_config(tmp_path, "docs", {"create_document": "fetch"})
        _write_config(tmp_path, "email", {"send_mail": "dispatch"})
        specs = _discover_tool_alias_specs(str(tmp_path))
        assert set(specs) == {"email"}

    def test_empty_world_map_is_not_an_app(self, tmp_path) -> None:
        _write_world_config(tmp_path, "docs", {})
        assert _discover_tool_alias_specs(str(tmp_path)) == {}

    def test_skips_world_file_under_a_bad_app_dir(self, tmp_path) -> None:
        path = tmp_path / "bad name!" / ".config" / "tool_aliases.world.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"aliases": {"a": "b"}}))
        assert _discover_tool_alias_specs(str(tmp_path)) == {}

    def test_skips_world_file_symlinked_outside_the_root(self, tmp_path) -> None:
        outside = tmp_path / "outside"
        (outside / ".config").mkdir(parents=True)
        (outside / ".config" / "tool_aliases.world.json").write_text(
            json.dumps({"aliases": {"a": "b"}})
        )
        root = tmp_path / "root"
        root.mkdir()
        (root / "docs").symlink_to(outside, target_is_directory=True)
        assert _discover_tool_alias_specs(str(root)) == {}


class TestValidateResolvedAliasMap:
    def test_accepts_clean_map(self) -> None:
        _validate_resolved_alias_map(
            {"read_document_content": "read_doc"},
            ["read_document_content", "create_document"],
        )

    def test_rejects_duplicate_served_alias(self) -> None:
        with pytest.raises(ValueError, match="collision"):
            _validate_resolved_alias_map(
                {"tool_a": "do_it", "tool_b": "do_it"},
                ["tool_a", "tool_b"],
            )

    def test_rejects_alias_shadowing_live_tool(self) -> None:
        """A served alias equal to an unaliased live tool's name would hijack
        calls meant for that tool (Bugbot 92661daa)."""
        with pytest.raises(ValueError, match="shadows"):
            _validate_resolved_alias_map(
                {"read_document_content": "create_document"},
                ["read_document_content", "create_document"],
            )


class TestToolAliasSpecValidation:
    def test_rejects_alias_shared_across_tools(self) -> None:
        with pytest.raises(ValueError, match="appears under both"):
            ToolAliasSpec(aliases={"tool_a": "do_it", "tool_b": "do_it"})

    def test_allows_a_swap_because_a_renamed_tool_frees_its_name(self) -> None:
        """{a: b, b: c} is routable: served names stay distinct, the reverse
        map is 1:1, and grading normalizes back correctly."""
        spec = ToolAliasSpec(aliases={"tool_a": "tool_b", "tool_b": "other"})
        assert spec.aliases == {"tool_a": "tool_b", "tool_b": "other"}

    def test_rejects_alias_colliding_with_a_tool_that_keeps_its_name(self) -> None:
        """An identity entry frees nothing, so tool_b is still live under its
        own name and cannot also be tool_a's alias."""
        with pytest.raises(ValueError, match="collides"):
            ToolAliasSpec(aliases={"tool_a": "tool_b", "tool_b": "tool_b"})

    def test_rejects_bad_charset(self) -> None:
        with pytest.raises(ValueError, match="invalid alias"):
            ToolAliasSpec(aliases={"tool_a": "bad name!"})

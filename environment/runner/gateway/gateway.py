"""Core MCP gateway logic for building and hot-swapping MCP apps.

This module handles creating FastMCP proxy ASGI apps and hot-swapping them
in the FastAPI application without restarting the server.
"""

import asyncio
import contextlib
import os
import re
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any, override

import mcp.types as mt
from asgi_lifespan import LifespanManager
from fastapi import FastAPI
from fastmcp import Client as FastMCPClient
from fastmcp import FastMCP
from fastmcp.server.http import StarletteWithLifespan
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.server.providers.proxy import ProxyClient, StatefulProxyClient
from fastmcp.server.server import create_proxy
from fastmcp.tools import Tool, ToolResult
from loguru import logger
from pydantic import ValidationError
from starlette.routing import Mount

from runner.coordinator.middleware import CoordinatorToolCallMiddleware
from runner.utils.tool_names import tool_counts_by_server, tool_name_matches

from .models import (
    MCPSchema,
    ServerReadinessDetails,
    ToolAliasSpec,
)
from .state import (
    StatefulProxyHandle,
    get_current_stateful,
    get_mcp_lifespan_manager,
    get_mcp_lock,
    get_mcp_mount,
    set_current_stateful,
    set_mcp_lifespan_manager,
    set_mcp_mount,
)


class _AllowedToolsMiddleware(Middleware):
    """Hide and reject MCP tools not in an allowlist.

    Used for per-task tool filtering: list_tools returns only allowed tools,
    and call_tool raises if a disallowed tool is invoked.
    """

    def __init__(self, allowed_tool_names: Sequence[str]) -> None:
        self._allowed: set[str] = set(allowed_tool_names)

    @override
    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        return [
            tool
            for tool in tools
            if any(
                tool_name_matches(
                    configured_tool_name=allowed,
                    observed_tool_name=tool.name,
                )
                for allowed in self._allowed
            )
        ]

    @override
    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        if not any(
            tool_name_matches(
                configured_tool_name=allowed,
                observed_tool_name=context.message.name,
            )
            for allowed in self._allowed
        ):
            allowed_count = len(self._allowed)
            raise ValueError(
                f"Tool {context.message.name!r} is not in the allowlist ({allowed_count} tools allowed)"
            )
        return await call_next(context)


def _name_rewriter(
    alias_by_observed: dict[str, str], server_names: Sequence[str]
) -> Callable[[str], str]:
    """Build a substitution over free text: every aliased tool name -> alias.

    Renaming only ``Tool.name`` is not enough — a description that says "use
    read_document_content for large files" hands the model the real name
    anyway, and cross-references between tools leak each other's names. So the
    same substitution runs over every tool's description, using the WHOLE
    alias map rather than just that tool's own entry.

    Name boundaries do the heavy lifting, so a short tool name cannot corrupt
    a longer one's mentions: ``read`` will not match inside
    ``read_document_content``. They are spelled as ``(?<![\\w-])`` /
    ``(?![\\w-])`` rather than ``\\b`` because ``TOOL_ALIAS_NAME_RE`` admits
    hyphens and ``-`` is NOT a word character — ``\\bget-user\\b`` happily
    matches inside ``get-user-profile``, rewriting an unaliased sibling into a
    tool that does not exist (Bugbot: "Hyphenated names break rewriter
    boundaries"). The character class is exactly the name alphabet, so a
    boundary here means "not in the middle of a name". Alternatives are
    ordered longest-first so a ``{server}_``-prefixed form wins over its own
    bare suffix.

    Known cost, accepted: a tool whose canonical name is an ordinary English
    word (``search``, ``finish``) will have that word rewritten wherever it
    appears in its prose, which can read slightly oddly. Leaving it would be a
    leak, and the model seeing the real word is exactly what aliasing is meant
    to prevent.
    """
    if not alias_by_observed:
        return lambda s: s

    subs = dict(alias_by_observed)
    # App authors write the UNPREFIXED name; fastmcp adds `{server}_` only in
    # multi-server worlds. Register the bare pair too when both sides carry
    # the same known prefix.
    #
    # Longest server name first: with servers `mail` and `mail_client`, tool
    # `mail_client_send` must strip `mail_client_` (-> `send`), not `mail_`
    # (-> `client_send`). Taking the short one registers a bare form nothing
    # writes and never registers the real one, so the bare `send` an app
    # author actually put in a description stays unscrubbed and leaks the
    # canonical name (Bugbot: "Rewriter prefix collision leak"). Requiring
    # the alias to carry the same prefix keeps this exact: a shorter prefix
    # only wins when the longer one does not fit BOTH sides.
    servers_longest_first = sorted(server_names, key=len, reverse=True)
    for observed, alias in alias_by_observed.items():
        for server in servers_longest_first:
            prefix = f"{server}_"
            if observed.startswith(prefix) and alias.startswith(prefix):
                subs.setdefault(observed[len(prefix) :], alias[len(prefix) :])
                break

    ordered = sorted(subs, key=len, reverse=True)
    pattern = re.compile(
        r"(?<![\w-])(" + "|".join(re.escape(n) for n in ordered) + r")(?![\w-])"
    )
    return lambda s: pattern.sub(lambda m: subs[m.group(1)], s)


# JSON-schema keys whose values are prose written for the model to read.
# Everything else in a schema is structure or data the backend validates
# against: `enum`/`const`/`default`/`examples`/`pattern` are values a call is
# checked against, and property KEYS are the argument names the backend binds.
# Rewriting either would corrupt the call instead of hiding a name — which is
# why the input schema was originally left alone wholesale. That reasoning
# covers keys and values, not prose.
_SCHEMA_PROSE_KEYS = frozenset({"description", "title"})


def _rewrite_schema_prose(node: Any, rewrite: Callable[[str], str]) -> Any:
    """Substitute over the prose inside a JSON schema, leaving structure alone.

    Renaming the tool and scrubbing its description is not enough: the schema
    carries free text too, and it is served to the model verbatim. A nested
    model's docstring and a ``Field(description=...)`` both land in there —

        properties.opts.anyOf[0].description
        properties.opts.anyOf[0].properties.mode.description

    — so this recurses rather than checking the top level (Devin: "Canonical
    name can leak through parameter schema text").

    Structure is untouched: no reshaping, no key rewriting, so a client that
    validates arguments against this schema sees the same contract. Returns
    the node itself when nothing was rewritten, so an unaliased rollout keeps
    serving the exact object the next layer produced.
    """
    if isinstance(node, dict):
        changed = False
        out: dict[str, Any] = {}
        for key, value in node.items():  # pyright: ignore[reportUnknownVariableType]
            if key in _SCHEMA_PROSE_KEYS and isinstance(value, str):
                new_value: Any = rewrite(value)
                if new_value != value:
                    changed = True
            else:
                new_value = _rewrite_schema_prose(value, rewrite)
                if new_value is not value:
                    changed = True
            out[key] = new_value
        return out if changed else node
    if isinstance(node, list):
        changed = False
        items: list[Any] = []
        for value in node:  # pyright: ignore[reportUnknownVariableType]
            new_value = _rewrite_schema_prose(value, rewrite)
            if new_value is not value:
                changed = True
            items.append(new_value)
        return items if changed else node
    return node


class _ToolAliasMiddleware(Middleware):
    """Serve configured tools under alias names.

    list_tools responses are rewritten canonical->alias; inbound call_tool
    requests are mapped alias->canonical before proxying, so backends, the
    allowlist middleware (configured with canonical names), the coordinator's
    recorded observations, and the event triggers keyed on them all keep
    operating on canonical names. Calls using an aliased tool's CANONICAL
    name are rejected (fail-closed): if canonical names still worked, a
    rollout couldn't distinguish "agent saw aliases" from "agent guessed the
    real name". The agent-side trajectory records the alias (the tokens the
    model actually emitted); grading normalizes via the per-task
    .apps_data/<app>/.config/tool_aliases.json that configured this rollout.
    """

    def __init__(self, alias_by_observed: dict[str, str] | None = None) -> None:
        # Observed (possibly `{server}_`-prefixed) canonical name -> served alias.
        self._alias_by_observed: dict[str, str] = {}
        self._observed_by_alias: dict[str, str] = {}
        self._rewrite_text: Callable[[str], str] = lambda s: s
        if alias_by_observed:
            self.install(alias_by_observed)

    def install(
        self,
        alias_by_observed: dict[str, str],
        server_names: Sequence[str] = (),
    ) -> None:
        """Activate a resolved map on an already-registered middleware.

        This middleware is registered in the FastMCP CONSTRUCTOR, ahead of
        CoordinatorToolCallMiddleware, because fastmcp runs middleware in add
        order and the coordinator must observe the CANONICAL name — otherwise
        record_tool_call stores aliases and every canonical-keyed
        tool_call_seen / tool_call_count trigger and VCA persona allowlist
        silently stops matching. But the map can only be resolved once the
        gateway is warm and its live tool list exists, which is after
        construction. An empty map is a pure pass-through, so the window
        between construction and install() is a no-op.

        ``server_names`` lets the text rewriter below also recognise the
        UNPREFIXED forms: an app author writes "see read_document_content" in
        a description knowing nothing about fastmcp's ``{server}_`` prefixing.
        """
        self._alias_by_observed = dict(alias_by_observed)
        self._observed_by_alias = {a: c for c, a in alias_by_observed.items()}
        self._rewrite_text = _name_rewriter(alias_by_observed, server_names)

    @property
    def alias_by_observed(self) -> dict[str, str]:
        """The resolved observed->alias map ({} until install()). Read-only
        view for logging and tests; mutate via install()."""
        return dict(self._alias_by_observed)

    @override
    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        # Registered on EVERY rollout now (it has to be, to sit ahead of the
        # coordinator), so the un-aliased path must hand back exactly what the
        # next layer produced — not a re-materialized copy of it.
        if not self._alias_by_observed:
            return tools
        return [self._rewrite_tool(tool) for tool in tools]

    def _rewrite_tool(self, tool: Tool) -> Tool:
        """Serve one tool with every trace of the canonical name removed.

        The name is only the most obvious carrier. Three others leak it:

        - ``description`` — free text, passed through verbatim, and app
          authors do write "use read_document_content for large files". Runs
          through the whole-map substitution, so cross-references between
          tools are covered too, not just this tool's own name.
        - ``title`` / ``annotations.title`` — MCP's human-readable label, by
          convention a prettified rendering of the name ("Read Document
          Content"). No substitution can catch that: it is not the name, it
          is a *transformation* of it. There is no safe automatic rewrite
          (deriving a new title from the alias would be inventing text), so
          an aliased tool's title is DROPPED. Name + description still
          describe the tool; the label is a cosmetic loss.
        - ``parameters`` / ``output_schema`` — the schemas carry prose of
          their own (a nested model's docstring, a ``Field(description=...)``)
          and it reaches the model in the same ``list_tools`` payload. Only
          the prose is rewritten; see ``_rewrite_schema_prose``.

        Unaliased tools still get the description and schema passes, since
        they can reference an aliased sibling. Their name and title are
        untouched.
        """
        alias = self._alias_by_observed.get(tool.name)
        update: dict[str, Any] = {}
        if alias:
            update["name"] = alias
        if tool.description:
            rewritten = self._rewrite_text(tool.description)
            if rewritten != tool.description:
                update["description"] = rewritten
        parameters = _rewrite_schema_prose(tool.parameters, self._rewrite_text)
        if parameters is not tool.parameters:
            update["parameters"] = parameters
        if tool.output_schema is not None:
            output_schema = _rewrite_schema_prose(
                tool.output_schema, self._rewrite_text
            )
            if output_schema is not tool.output_schema:
                update["output_schema"] = output_schema
        if alias:
            if getattr(tool, "title", None):
                update["title"] = None
            annotations = getattr(tool, "annotations", None)
            if annotations is not None and getattr(annotations, "title", None):
                update["annotations"] = annotations.model_copy(update={"title": None})
        return tool.model_copy(update=update) if update else tool

    @override
    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        if not self._alias_by_observed:
            return await call_next(context)
        name = context.message.name
        canonical = self._observed_by_alias.get(name)
        if canonical is not None:
            return await call_next(
                context.copy(
                    message=context.message.model_copy(update={"name": canonical})
                )
            )
        if name in self._alias_by_observed:
            # Fail closed, but say nothing. Tool errors go back to the model
            # verbatim, so naming the alias here would hand it the exact
            # canonical->alias pair aliasing exists to withhold — and even a
            # distinct *shape* of error ("that tool is aliased") is an oracle:
            # a model probing names would learn which guesses were real. This
            # message and type are therefore exactly what fastmcp itself
            # raises for a name that was never a tool at all (ToolError,
            # "Unknown tool: 'x'"), so the two are indistinguishable from the
            # client side. The diagnostic lives in the container log, which
            # the agent cannot read.
            logger.warning(
                f"Rejected call to canonical tool name {name!r}; it is served "
                f"as {self._alias_by_observed[name]!r} in this rollout"
            )
            raise ValueError(f"Unknown tool: {name!r}")
        return await call_next(context)


def _alias_prefix_for(
    observed: str,
    canonical: str,
    server_names: Sequence[str],
    scope_server: str | None = None,
) -> str | None:
    """Return the fastmcp prefix ("" or "{server}_") if observed IS canonical.

    Exact matching only, keyed to the gateway's serving mode. A
    single-server gateway serves UNPREFIXED tool names, so only bare
    equality matches. A multi-server gateway prefixes EVERY tool with
    ``{server}_``, so only exact ``{server}_{canonical}`` forms match —
    never bare equality, which would let canonical "email_read" bind to
    server email's tool "read" (Bugbot multi-server bare-name collision).
    Deliberately stricter than the allowlist's tool_name_matches, whose
    any-suffix rule would also let canonical "read" steal tool "mark_read"
    (Bugbot 54724cec). Returns None when observed is not this canonical.

    ``scope_server`` narrows which server prefix may match — each alias
    config belongs to ONE app, so without it two apps that both expose a
    ``search`` tool would cross-bind (app A's config renaming app B's
    ``b_search``). Mode is still decided by the FULL ``server_names``: a
    scoped lookup in a multi-server world must keep matching prefixed
    names, so the scope cannot simply be passed as a one-element list.
    """
    if len(server_names) <= 1:
        return "" if observed == canonical else None
    candidates = (
        [scope_server]
        if scope_server is not None and scope_server in server_names
        else server_names
    )
    for server in candidates:
        if observed == f"{server}_{canonical}":
            return f"{server}_"
    return None


def _resolve_tool_alias_map(
    observed_tool_names: Sequence[str],
    spec: ToolAliasSpec,
    server_names: Sequence[str],
    scope_server: str | None = None,
) -> dict[str, str]:
    """Resolve observed tool names -> served alias names.

    Canonical names in the spec are unprefixed; fastmcp prefixes tools with
    ``{server}_`` in multi-server mode, so match exactly against the known
    server prefixes and preserve the prefix on the alias.
    """
    resolved: dict[str, str] = {}
    for observed in observed_tool_names:
        for canonical, alias in spec.aliases.items():
            prefix = _alias_prefix_for(observed, canonical, server_names, scope_server)
            if prefix is None:
                continue
            resolved[observed] = f"{prefix}{alias}"
            break
    return resolved


# Alias configs are staged by Studio into the snapshot at
# .apps_data/<app>/.config/tool_aliases*.json, so they reach delivered bundles
# with no extra plumbing and grading reads the same files back.
#
# TWO filenames, one per layer (world defaults, task overrides). They cannot
# share a path: snapshot merging is key-level, so one path would mean a task
# renaming a single tool discards every world-level rename on that app.
_APPS_DATA_ROOT_ENV = "APPS_DATA_ROOT"
_DEFAULT_APPS_DATA_ROOT = "/.apps_data"
TOOL_ALIASES_FILENAME = "tool_aliases.json"
WORLD_TOOL_ALIASES_FILENAME = "tool_aliases.world.json"
# World first: later entries win the dict update below, and the task layer is
# what overrides.
_TOOL_ALIAS_FILENAMES = (WORLD_TOOL_ALIASES_FILENAME, TOOL_ALIASES_FILENAME)

# The <app> directory name becomes a scope key. fastmcp tool namespacing
# already constrains real server names to this charset; check it anyway, since
# the file arrives from outside the container.
_APP_DIR_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")


def _discover_tool_alias_specs(
    apps_data_root: str | None = None,
) -> dict[str, ToolAliasSpec]:
    """Load every per-app alias config under the apps-data root.

    Returns ``{app_dir_name: spec}``, each the per-TOOL union of that app's
    world defaults and task overrides, the task winning on the same canonical.
    Validated ONCE as a whole: a duplicate served alias can exist in neither
    layer alone, and per-layer validation would pass it to
    ``_validate_resolved_alias_map``, which fails the rollout closed.

    Untrusted input — operator-authored, crossing into the container: the
    directory name is charset-checked, the resolved path must stay under the
    root (else a symlinked ``.apps_data/<app>`` escapes), and an unparseable
    file is skipped rather than taking the gateway down.
    """
    root = Path(
        apps_data_root or os.getenv(_APPS_DATA_ROOT_ENV, _DEFAULT_APPS_DATA_ROOT)
    )
    try:
        root_resolved = root.resolve(strict=True)
    except OSError:
        return {}

    merged: dict[str, dict[str, str]] = {}
    for filename in _TOOL_ALIAS_FILENAMES:
        for path in sorted(root.glob(f"*/.config/{filename}")):
            app = path.parent.parent.name
            if not _APP_DIR_NAME_RE.match(app):
                logger.warning(f"Skipping tool alias config under bad app dir {app!r}")
                continue
            try:
                if not path.resolve(strict=True).is_relative_to(root_resolved):
                    logger.warning(f"Tool alias config escapes apps_data root: {path}")
                    continue
                spec = ToolAliasSpec.model_validate_json(path.read_text())
            except (OSError, UnicodeDecodeError, ValidationError) as e:
                logger.warning(f"Skipping malformed tool alias config {path}: {e}")
                continue
            if spec.aliases:
                merged.setdefault(app, {}).update(spec.aliases)

    specs: dict[str, ToolAliasSpec] = {}
    for app, aliases in merged.items():
        try:
            specs[app] = ToolAliasSpec(aliases=aliases)
        except ValidationError as e:
            # The UNION is unroutable though each layer was fine alone. Serving
            # this app canonical beats failing the whole rollout.
            logger.warning(
                f"Skipping tool aliases for {app!r}: the world and task layers "
                f"do not compose into a routable map: {e}"
            )
    return specs


async def _install_tool_aliases(
    mcp_proxy: FastMCP,
    alias_middleware: _ToolAliasMiddleware,
    server_names: Sequence[str],
    apps_data_root: str | None = None,
) -> None:
    """Resolve the staged alias configs and activate them on a warm gateway.

    A config that matches no live tool is a WARNING, not a raise. The names
    are free-form text authored per task, so a tool renamed since authoring
    is a routine slip — and this same code runs inside delivered bundles,
    where raising here would take the customer's whole world down rather
    than degrade one rollout to canonical names. Genuinely ambiguous routing
    still fails closed via _validate_resolved_alias_map.
    """
    specs = _discover_tool_alias_specs(apps_data_root)
    if not specs:
        return

    async with FastMCPClient(mcp_proxy) as client:
        tools = await client.list_tools()
    observed_names = [t.name for t in tools]

    resolved: dict[str, str] = {}
    multi_server = len(server_names) > 1
    for app, spec in specs.items():
        # A config dir that doesn't name a live MCP server can't be scoped (an
        # app whose STATE_LOCATION diverges from its server name).
        #
        # In MULTI-server mode that config is unusable, not merely unscoped:
        # matching would bind it to some other server's tool and serve the
        # alias under THAT server's prefix, and grading's apply_tool_remap
        # keys on this config's own app name — so the trajectory would never
        # normalize back to canonical and the rollout would grade against
        # names that don't exist. Skip it loudly instead (Devin: "Alias
        # normalization can diverge when config dir name is not the server
        # name").
        #
        # Single-server mode is safe: tools are served UNPREFIXED, so the
        # alias is bare and apply_tool_remap matches it bare regardless of
        # which directory the config came from.
        if multi_server and app not in server_names:
            logger.warning(
                f"Skipping tool alias config {app!r}: it names no live MCP "
                f"server (have {sorted(server_names)}), and in a multi-server "
                "world an unscoped alias could not be normalized at grading"
            )
            continue
        scope = app if app in server_names else None
        unmatched = {
            canonical
            for canonical in spec.aliases
            if not any(
                _alias_prefix_for(observed, canonical, server_names, scope) is not None
                for observed in observed_names
            )
        }
        if unmatched:
            logger.warning(
                f"Tool alias config for {app!r} references unknown tools: "
                f"{sorted(unmatched)}"
            )
        for observed, alias in _resolve_tool_alias_map(
            observed_names, spec, server_names, scope
        ).items():
            prior = resolved.get(observed)
            if prior is not None and prior != alias:
                raise ValueError(
                    f"Tool {observed!r} is claimed by two alias configs "
                    f"({prior!r} vs {alias!r})"
                )
            resolved[observed] = alias

    if not resolved:
        logger.warning(
            "Tool alias configs staged but no configured tool matched the "
            f"gateway's tools; serving canonical names (apps: {sorted(specs)})"
        )
        return

    _validate_resolved_alias_map(resolved, observed_names)
    alias_middleware.install(resolved, server_names)
    logger.info(
        f"Tool aliasing active: {len(resolved)} tools renamed across "
        f"{len(specs)} app(s)"
    )


def _validate_resolved_alias_map(
    resolved: dict[str, str], observed_names: Sequence[str]
) -> None:
    """Reject a resolved map with ambiguous routing (Bugbot 92661daa).

    Two failure modes, both fail-closed because a silently-shadowed tool
    would corrupt the rollout while looking healthy:
    - two tools resolving to the same served alias
    - a served alias equal to an UNALIASED live tool's real name (the
      reverse-map would hijack calls meant for that tool, and the alias
      middleware would serve two tools under one name)
    """
    served = list(resolved.values())
    if len(set(served)) != len(served):
        dupes = {a for a in served if served.count(a) > 1}
        raise ValueError(f"Tool alias collision: {sorted(dupes)}")
    unaliased = set(observed_names) - set(resolved)
    shadowed = unaliased & set(served)
    if shadowed:
        raise ValueError(
            f"Tool alias shadows an existing unaliased tool: {sorted(shadowed)}"
        )


# Mirror of hosted_envs._strip_nonstring_enums (PR #13032); kept separate because
# this module is vendored into delivered worlds and can't import across packages.
def _strip_nonstring_enums(node: Any) -> Any:
    """Return a copy of a JSON schema with every non-string ``enum`` dropped.

    External Go agent runners unmarshal a tool's
    schema with ``enum`` typed as ``[]string``; a single non-string member (an
    int id, a bool) makes the Go side reject the WHOLE tool list, so the agent
    never starts. Dropping the enum keeps the field's ``type`` (and every other
    key), so the param stays well-typed and the value the model emits is
    unchanged — only the (usually spurious) enumerated hint is removed.
    All-string enums are left untouched.
    """
    if isinstance(node, dict):
        return {
            k: _strip_nonstring_enums(v)
            for k, v in node.items()
            if not (
                k == "enum"
                and isinstance(v, list)
                and not all(isinstance(m, str) for m in v)
            )
        }
    if isinstance(node, list):
        return [_strip_nonstring_enums(v) for v in node]
    return node


class _StripNonStringEnumsMiddleware(Middleware):
    """Strip non-string ``enum`` members from served tool schemas.

    Mirrors FastMCP's own ``DereferenceRefsMiddleware``: rewrites each tool's
    ``parameters`` in ``list_tools`` so downstream agent runners that type
    ``enum`` as ``[]string`` can parse the tool list. Tool-call arguments are
    proxied through untouched, so no value ever changes type.
    """

    @override
    async def on_list_tools(
        self,
        context: MiddlewareContext[mt.ListToolsRequest],
        call_next: CallNext[mt.ListToolsRequest, Sequence[Tool]],
    ) -> Sequence[Tool]:
        tools = await call_next(context)
        stripped: list[Tool] = []
        for tool in tools:
            params = _strip_nonstring_enums(tool.parameters)
            stripped.append(
                tool.model_copy(update={"parameters": params})
                if params != tool.parameters
                else tool
            )
        return stripped


class MCPReadinessError(Exception):
    """Exception raised when MCP servers fail readiness check.

    Attributes:
        failed_servers: Dict mapping server names to readiness details
        message: Human-readable error message
    """

    failed_servers: dict[str, ServerReadinessDetails]
    message: str

    def __init__(
        self,
        failed_servers: dict[str, ServerReadinessDetails],
        message: str | None = None,
    ):
        """Initialize MCP readiness error.

        Args:
            failed_servers: Dict mapping server names to ServerReadinessDetails
            message: Optional custom error message
        """
        self.failed_servers = failed_servers
        server_list = ", ".join(failed_servers.keys())
        self.message = message or f"MCP servers not ready after 5 min: {server_list}"
        super().__init__(self.message)


# Env-gated upstream read-timeout (seconds) for the gateway's proxy client.
# Unset = FastMCP default (~5 min read), which aborts a long-running tool's
# result delivery with "Upstream request timed out" even while the upstream is
# healthy and emitting keepalive progress. When set, every proxied tool call
# may wait this long for an upstream response — size it above the longest
# expected single-tool runtime (e.g. a full antigravity_run) to decouple result
# delivery from that ceiling. (FastMCP 3.x ignores per-server `sse_read_timeout`;
# the live knob is the client's `timeout` → ClientSession `read_timeout_seconds`.)
_READ_TIMEOUT_ENV = "MCP_GATEWAY_SSE_READ_TIMEOUT_SECONDS"


def _proxy_read_timeout_seconds() -> float | None:
    raw = os.getenv(_READ_TIMEOUT_ENV)
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _gateway_middleware(
    alias_middleware: _ToolAliasMiddleware | None,
) -> list[Middleware]:
    """The gateway's middleware stack, in the order fastmcp runs it.

    The alias layer goes FIRST. fastmcp runs middleware in add order, so on
    the way in the alias->canonical rewrite has to land before
    CoordinatorToolCallMiddleware records the call — canonical-keyed
    tool_call_seen / tool_call_count triggers and VCA persona allowlists read
    what it records — and before the allowlist filter checks the name.
    First is also correct on the way out: list_tools is renamed last, after
    the allowlist has already filtered on canonical names.

    ``None`` means "no aliasing on this proxy" and yields a throwaway
    pass-through, which keeps the builders callable without threading a
    middleware through in tests. swap_mcp_app always passes a real one.
    """
    return [alias_middleware or _ToolAliasMiddleware(), CoordinatorToolCallMiddleware()]


def _build_proxy(
    config_dict: dict[str, Any],
    alias_middleware: _ToolAliasMiddleware | None = None,
) -> FastMCP:
    """Build the gateway proxy, honoring the env read-timeout when set.

    Default (env unset) is byte-identical to `FastMCP.as_proxy(config_dict)`.
    When the env timeout is set we build the same proxy explicitly so the
    upstream `ProxyClient` carries a `timeout` (→ `read_timeout_seconds`),
    which is the only knob FastMCP 3.x actually applies to the upstream read.
    """
    middleware = _gateway_middleware(alias_middleware)
    timeout = _proxy_read_timeout_seconds()
    if timeout is None:
        return FastMCP.as_proxy(config_dict, name="Gateway", middleware=middleware)

    from fastmcp.server.providers.proxy import FastMCPProxy

    base_client = ProxyClient(config_dict, timeout=timeout)
    return FastMCPProxy(
        client_factory=lambda: base_client.new(),
        name="Gateway",
        middleware=middleware,
    )


# Per-server config fields consumed by the gateway itself; not part of the
# FastMCP MCP-server config schema, so strip them before handing the dict to
# FastMCP's proxy builder.
_GATEWAY_ONLY_KEYS = {
    "serve_mcp_tools",
    "openapi_mcp_filter",
    "exposes_mcp",
    "session_affinity",
}


def _serving_config_dict(config: MCPSchema) -> dict[str, Any] | None:
    """Return the FastMCP-ready config dict for serving servers, or None if none serve.

    serve_mcp_tools=False servers are routable via /rest but excluded from the
    aggregated MCP tool list (transport "rest" only).
    """
    serving = {n: s for n, s in config.mcpServers.items() if s.serve_mcp_tools}
    if not serving:
        return None

    # FastMCP's config parser is sensitive to keys being present with null values
    # (e.g., http servers should not also include {"command": null, ...}).
    # Only emit explicitly-set fields. allowed_tool_names is gateway-specific
    # (not part of the FastMCP MCP server config schema), so strip it before
    # passing to FastMCP's proxy builder.
    config_dict = config.model_dump(exclude_none=True)
    config_dict.pop("allowed_tool_names", None)
    config_dict["mcpServers"] = {
        n: {k: v for k, v in s.items() if k not in _GATEWAY_ONLY_KEYS}
        for n, s in config_dict["mcpServers"].items()
        if n in serving
    }
    return config_dict


def _session_affinity_requested(config: MCPSchema) -> bool:
    """True if any serving server needs a reused backend MCP session (e.g. browser)."""
    return any(
        s.session_affinity for s in config.mcpServers.values() if s.serve_mcp_tools
    )


# --- Stateful (session-affine) gateway proxy ----------------------------------
# A serving server can opt into session affinity (default off). When set, the
# gateway connects ONE StatefulProxyClient for THAT server and reuses its session
# for every tool call, so the browser's page/refs/cookies survive across calls
# instead of resetting to about:blank. The connect must run in a long-lived owner
# task, never an inbound request task, or its streamable-HTTP cancel scope is
# orphaned when the request ends and crashes the next reuse. Affinity is scoped
# per server, never world-wide: a session connected outside a request never
# carries the per-call `Authorization: Bearer <actor_id>` rewrite, so sweeping a
# tenancy-enforcing backend (e.g. the email app) into the affine path breaks it
# with "Missing Authorization: Bearer <user_id> header." on every call.
#
# WARNING — this session-affine path depends on UNDOCUMENTED fastmcp internals. Do
# not upgrade fastmcp without re-validating ALL of the following against the new
# version:
#   1. StatefulProxyClient.__aexit__ is a no-op, so fastmcp's per-call nesting_counter
#      accumulates and never decrements (the reconnect logic below exists solely to
#      recover from the error this causes when a backend session later drops).
#   2. Client._connect raises a RuntimeError containing `_NESTING_COUNTER_ERROR` when a
#      session is (re)started with the counter != 0 — the exact reconnect trigger.
#   3. create_proxy() reuses a *connected* client's session for every call because
#      `type(client) is ProxyClient` is False for this subclass. This is what actually
#      defeats about:blank, and a regression here is SILENT (no error, just a broken
#      browser), so it is the most dangerous of the three to miss.
# fastmcp moves these between minor releases (3.4.2 already split the distribution into
# fastmcp + fastmcp-slim). The env pins 3.2.0 but the specifier is unbounded
# (`fastmcp>=3.2.0`) and the same monorepo already runs 3.4.2 in hosted-envs/uv.lock.
# On any bump, re-run the session-reuse + drop-recovery tests in test_mcp_gateway.py:
# they fail-closed on (1) and (2), but NOT reliably on (3) — verify (3) by hand.
_NESTING_COUNTER_ERROR = "nesting counter should be 0"


class _ReconnectingStatefulProxyClient(StatefulProxyClient[Any]):
    """StatefulProxyClient that survives a backend session drop.

    StatefulProxyClient's __aexit__ is a no-op, so fastmcp's nesting_counter
    accumulates per tool call; if the backend session then drops, the next
    connect raises the nesting-counter error and would fail the run. Catch it,
    force-disconnect (resetting the counter) and reconnect once — the browser is
    already gone for that drop, but the call succeeds on a fresh session.
    """

    @override
    async def __aenter__(self) -> "FastMCPClient[Any]":
        try:
            return await self._connect()
        except RuntimeError as e:
            if _NESTING_COUNTER_ERROR not in str(e):
                raise
            logger.warning("Session-affine backend dropped; reconnecting fresh.")
            # _disconnect resets the counter BEFORE it awaits the dead session task,
            # then that await re-raises a *dirty* drop's stored error. We only need the
            # reset here, so suppress and reconnect — otherwise a dirty drop fails this
            # call and recovers only on the next one.
            with contextlib.suppress(Exception):
                await self._disconnect(force=True)
            return await self._connect()


async def _shutdown_stateful(handle: StatefulProxyHandle | None) -> None:
    """Signal an owner task to disconnect its backend client and wait for it."""
    if handle is None:
        return
    stop, task = handle
    stop.set()
    try:
        await task
    except Exception as e:  # noqa: BLE001
        # A failed disconnect leaks a backend browser; log it for monitoring.
        logger.warning(f"Stateful proxy owner task errored on shutdown: {e}")


async def shutdown_stateful_proxy() -> None:
    """Disconnect the active session-affine backend client (e.g. on app shutdown)."""
    # Take the swap lock so this can't race a concurrent swap mutating the state.
    async with get_mcp_lock():
        handle = get_current_stateful()
        set_current_stateful(None)
        await _shutdown_stateful(handle)


async def _build_stateful_mcp_app_with_proxy(
    config_dict: dict[str, Any],
    affine_names: set[str],
    alias_middleware: _ToolAliasMiddleware | None = None,
) -> tuple[StarletteWithLifespan, FastMCP, StatefulProxyHandle]:
    """Build a session-affine proxy app: one connected, reused backend session.

    For 2+ serving servers, dispatches to _build_multi_stateful_mcp_app_with_proxy
    (only servers in affine_names get a reconnecting stateful client; the rest stay
    per-call stateless); the body below is the single-server path, where the sole
    server is the affine one by construction.

    Returns (app, proxy, handle); the caller disconnects the handle via
    _shutdown_stateful once the app is unmounted. Raises (leaving no owner task) if
    the connect or app build fails, so the caller can keep the old gateway.
    """
    if len(config_dict["mcpServers"]) >= 2:
        return await _build_multi_stateful_mcp_app_with_proxy(
            config_dict, affine_names, alias_middleware
        )

    timeout = _proxy_read_timeout_seconds()
    client = _ReconnectingStatefulProxyClient(config_dict, timeout=timeout)
    ready: asyncio.Event = asyncio.Event()
    stop: asyncio.Event = asyncio.Event()
    connect_error: dict[str, BaseException] = {}

    async def _owner() -> None:
        # Connect + disconnect both run in THIS task so the session's cancel scope
        # is never orphaned across request tasks. Catch Exception (not BaseException)
        # so a CancelledError propagates; `ready` is always set so the builder never
        # hangs on `await ready.wait()`.
        try:
            _ = await client.__aenter__()
        except Exception as e:  # noqa: BLE001
            connect_error["error"] = e
            return
        finally:
            ready.set()
        try:
            await stop.wait()
        finally:
            try:
                # Same protected force-disconnect MCPConfigTransport uses internally.
                await client._disconnect(force=True)  # pyright: ignore[reportPrivateUsage]
            except Exception as e:  # noqa: BLE001
                logger.warning(f"Stateful proxy client disconnect error: {e}")

    task = asyncio.create_task(_owner())
    try:
        await ready.wait()
    except BaseException:
        # Swap cancelled/errored before the connect settled: cancel and reap the
        # owner task so it can't park forever holding a live browser session.
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        raise
    if "error" in connect_error:
        await task  # owner already returned; reap it so it is not left pending
        raise RuntimeError(
            f"Failed to connect session-affine gateway proxy: {connect_error['error']!r}"
        )

    logger.info("Gateway proxy built in session-affine (shared-session) mode")
    handle = StatefulProxyHandle(stop, task)

    try:
        # create_proxy() over a *connected* client reuses its session per request.
        proxy = create_proxy(
            client, name="Gateway", middleware=_gateway_middleware(alias_middleware)
        )
        mcp_app = proxy.http_app(path="/")
    except BaseException:
        # Connected but failed to build the app: disconnect so the owner task and
        # backend browser don't leak.
        await _shutdown_stateful(handle)
        raise
    return mcp_app, proxy, handle


async def _build_multi_stateful_mcp_app_with_proxy(
    config_dict: dict[str, Any],
    affine_names: set[str],
    alias_middleware: _ToolAliasMiddleware | None = None,
) -> tuple[StarletteWithLifespan, FastMCP, StatefulProxyHandle]:
    """Build a mixed proxy for 2+ serving servers, session-affine per server.

    ONLY servers in affine_names get a connected, reused backend session; every
    other server is mounted as a per-call stateless proxy. Session affinity must
    stay per-server scoped: a stateful client connects from the owner task with no
    active HTTP request, so fastmcp's connect_session never sees the per-call
    `Authorization: Bearer <actor_id>` rewrite (CoordinatorToolCallMiddleware) and
    tenancy-enforcing backends reject every call with "Missing Authorization".
    Stateless mounts open a fresh backend session inside the request, which is
    what forwards that header.

    Each affine backend gets its OWN single-server reconnecting client (a
    single-server MCPConfig takes fastmcp's direct-transport path with no inner
    client, so the reconnect override is in the path for every backend) and is
    mounted into one composite gateway. Without this, fastmcp's multi-server
    transport interposes a plain per-backend client our reconnect never reaches,
    so a backend drop fails the call. Tool naming matches fastmcp's native
    multi-server composition by construction (mount each backend with
    namespace=<server name>).
    """
    timeout = _proxy_read_timeout_seconds()
    clients: dict[str, _ReconnectingStatefulProxyClient] = {
        name: _ReconnectingStatefulProxyClient(
            {"mcpServers": {name: server_cfg}}, timeout=timeout
        )
        for name, server_cfg in config_dict["mcpServers"].items()
        if name in affine_names
    }
    ready: asyncio.Event = asyncio.Event()
    stop: asyncio.Event = asyncio.Event()
    connect_error: dict[str, BaseException] = {}

    async def _owner() -> None:
        # Connect AND disconnect every backend in THIS one task so no session's
        # cancel scope is orphaned across request tasks. The outer `finally` drains
        # whatever actually connected on EVERY exit — normal stop, partial-connect
        # failure, AND CancelledError (a /apps request cancelled mid-connect) — so an
        # already-connected backend session is never silently orphaned.
        connected: list[_ReconnectingStatefulProxyClient] = []
        try:
            try:
                for client in clients.values():
                    _ = await client.__aenter__()
                    connected.append(client)
            except Exception as e:  # noqa: BLE001
                connect_error["error"] = e
            finally:
                ready.set()
            if not connect_error:
                await stop.wait()
        finally:
            for client in connected:
                try:
                    await client._disconnect(force=True)  # pyright: ignore[reportPrivateUsage]
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"Stateful proxy client disconnect error: {e}")

    task = asyncio.create_task(_owner())
    try:
        await ready.wait()
    except BaseException:
        # Swap cancelled/errored before the connects settled: cancel and reap the
        # owner task so it can't park forever holding live backend sessions.
        task.cancel()
        with contextlib.suppress(BaseException):
            await task
        raise
    if "error" in connect_error:
        await task  # owner already returned; reap it so it is not left pending
        raise RuntimeError(
            f"Failed to connect session-affine gateway proxy: {connect_error['error']!r}"
        )

    logger.info("Gateway proxy built in session-affine (multi-server) mode")
    handle = StatefulProxyHandle(stop, task)

    try:
        composite = FastMCP(
            name="Gateway", middleware=_gateway_middleware(alias_middleware)
        )
        for name, server_cfg in config_dict["mcpServers"].items():
            if name in clients:
                # create_proxy() over a *connected* client reuses its session per
                # request; mount with namespace=<name> to match fastmcp's native
                # multi-server {name}_{tool} naming.
                proxy = create_proxy(clients[name], name=f"Proxy-{name}")
            else:
                # Non-affine backend: a *disconnected* ProxyClient makes
                # create_proxy() open a fresh session per request, so the per-call
                # Authorization rewrite is forwarded (pre-affinity behavior).
                proxy = create_proxy(
                    ProxyClient({"mcpServers": {name: server_cfg}}, timeout=timeout),
                    name=f"Proxy-{name}",
                )
            composite.mount(proxy, namespace=name)
        mcp_app = composite.http_app(path="/")
    except BaseException:
        # Connected but failed to build the app: disconnect so the owner task and
        # backend sessions don't leak.
        await _shutdown_stateful(handle)
        raise
    return mcp_app, composite, handle


def _build_mcp_app_with_proxy(
    config_dict: dict[str, Any] | None,
    alias_middleware: _ToolAliasMiddleware | None = None,
) -> tuple[StarletteWithLifespan, FastMCP]:
    """Build a (stateless) FastMCP proxy ASGI app from a serving config dict.

    config_dict is the output of _serving_config_dict(); None means no serving
    servers, so a bare gateway (no aggregated tools) is returned. Returns
    (ASGI app, FastMCP gateway).
    """
    if config_dict is None:
        mcp_server = FastMCP(
            name="Gateway",
            middleware=_gateway_middleware(alias_middleware),
        )
        return mcp_server.http_app(path="/"), mcp_server

    mcp_proxy = _build_proxy(config_dict, alias_middleware)
    # Root at "/" so final URLs are under /mcp.
    return mcp_proxy.http_app(path="/"), mcp_proxy


async def warm_and_check_gateway(
    mcp_proxy: FastMCP,
    expected_servers: list[str],
    max_wait_seconds: float = 300.0,
    retry_interval: float = 1.0,
) -> int:
    """Warm up gateway connections and verify all servers are ready.

    Connects to the gateway and calls list_tools(). This forces the proxy to
    connect to all backend servers (warming the connections). Then verifies
    that every expected server contributed at least one tool.

    Args:
        mcp_proxy: The FastMCP proxy instance to warm up
        expected_servers: List of server names that must provide tools
        max_wait_seconds: Maximum time to wait for all servers (default 5 min)
        retry_interval: Time between retry attempts (default 1s)

    Returns:
        Total number of tools loaded

    Raises:
        MCPReadinessError: If any server doesn't provide tools within timeout
    """
    start_time = time.perf_counter()
    deadline = start_time + max_wait_seconds
    attempts = 0
    last_error: str = ""
    missing_servers: set[str] = set(expected_servers)
    servers_with_tools: dict[str, int] = {}

    while True:
        attempts += 1
        remaining = deadline - time.perf_counter()

        if remaining <= 0:
            last_error = "Timeout"
            break

        try:
            async with asyncio.timeout(remaining):
                async with FastMCPClient(mcp_proxy) as client:
                    tools = await client.list_tools()
                    tool_names = [t.name for t in tools]

                servers_with_tools = tool_counts_by_server(tool_names, expected_servers)

                missing_servers = set(expected_servers) - set(servers_with_tools.keys())

                if not missing_servers:
                    elapsed = time.perf_counter() - start_time
                    total_tools = len(tools)
                    logger.info(
                        f"Gateway ready after {elapsed:.1f}s: {total_tools} tools from {len(expected_servers)} servers"
                    )
                    for server, count in sorted(servers_with_tools.items()):
                        logger.info(f"  - {server}: {count} tools")
                    return total_tools

                elapsed = time.perf_counter() - start_time
                ready_list = ", ".join(
                    f"{s} ({c} tools)" for s, c in servers_with_tools.items()
                )
                missing_list = ", ".join(missing_servers)
                if ready_list:
                    logger.debug(
                        f"Attempt {attempts} ({elapsed:.1f}s): Ready: [{ready_list}], Waiting: [{missing_list}]"
                    )
                else:
                    logger.debug(
                        f"Attempt {attempts} ({elapsed:.1f}s): Waiting for all servers"
                    )

        except TimeoutError:
            last_error = "Timeout"
            break

        except Exception as e:
            elapsed = time.perf_counter() - start_time
            last_error = str(e)
            logger.debug(
                f"Attempt {attempts} ({elapsed:.1f}s): Gateway connection failed: {e}"
            )

        await asyncio.sleep(retry_interval)

    # Failure path - report results
    elapsed = time.perf_counter() - start_time
    failed_servers: dict[str, ServerReadinessDetails] = {}

    for server in missing_servers:
        error_msg = f"No tools found after {elapsed:.1f}s"
        if last_error:
            error_msg += f" (last error: {last_error})"
        failed_servers[server] = ServerReadinessDetails(
            error=error_msg,
            attempts=attempts,
        )
        logger.warning(
            f"Server '{server}' FAILED after {attempts} attempt(s) ({elapsed:.1f}s): {error_msg}"
        )

    for server, count in servers_with_tools.items():
        logger.info(
            f"Server '{server}' ready after {attempts} attempt(s) ({elapsed:.1f}s): {count} tools"
        )

    failed_count = len(failed_servers)
    ready_count = len(servers_with_tools)
    logger.error(
        f"MCP readiness check failed: {failed_count} server(s) not ready ({ready_count} server(s) ready)"
    )
    raise MCPReadinessError(failed_servers)


async def warm_and_check_servers(
    server_urls: dict[str, str],
    max_wait_seconds: float = 300.0,
    retry_interval: float = 1.0,
) -> None:
    """Wait for registered backends that are excluded from the aggregated proxy.

    rest-only servers (serve_mcp_tools=False) still expose an /mcp endpoint; we
    probe each directly so readiness covers them without adding their tools to
    the aggregated /mcp.
    """
    if not server_urls:
        return

    start_time = time.perf_counter()
    deadline = start_time + max_wait_seconds
    attempts = 0
    pending: dict[str, str] = dict(server_urls)
    last_error: dict[str, str] = {}

    while pending:
        attempts += 1
        if time.perf_counter() >= deadline:
            break
        for name, url in list(pending.items()):
            try:
                async with FastMCPClient(url) as client:
                    _ = await client.list_tools()
                del pending[name]
            except Exception as e:  # noqa: BLE001
                last_error[name] = str(e)
        if pending:
            await asyncio.sleep(retry_interval)

    if pending:
        elapsed = time.perf_counter() - start_time
        failed_servers = {
            name: ServerReadinessDetails(
                error=f"Not ready after {elapsed:.1f}s (last error: {last_error.get(name, 'unknown')})",
                attempts=attempts,
            )
            for name in pending
        }
        raise MCPReadinessError(failed_servers)

    elapsed = time.perf_counter() - start_time
    logger.info(
        f"REST-only backends ready after {elapsed:.1f}s: {', '.join(server_urls)}"
    )


async def swap_mcp_app(config: MCPSchema, app: FastAPI) -> FastMCP:
    """Hot-swap the mounted MCP app with a new configuration.

    This function:
    1. Builds a new MCP app from config
    2. Starts its lifespan
    3. Atomically replaces the Mount.app reference
    4. Shuts down the old app's lifespan
    5. Warms up gateway connections and verifies configured servers are ready

    Args:
        config: New MCP configuration schema (MCPSchema instance)
        app: The FastAPI application instance

    Raises:
        ValueError: If config is invalid
        RuntimeError: If swap fails
        MCPReadinessError: If any server fails readiness check
    """
    async with get_mcp_lock():  # Prevent concurrent swaps
        # Build the new app first. A session-affine server (e.g. browser) needs ONE
        # reused backend session; everything else uses the stateless per-call proxy.
        # The active session-affine client keeps serving the outgoing app and is
        # disconnected only after the swap succeeds, so a failed build leaves the
        # old gateway intact.
        new_stateful: StatefulProxyHandle | None = None
        config_dict = _serving_config_dict(config)
        # Registered now (ahead of the coordinator) but resolved after
        # readiness, once the live tool list exists — see _ToolAliasMiddleware.
        alias_middleware = _ToolAliasMiddleware()
        if config_dict is not None and _session_affinity_requested(config):
            # Affinity is scoped per server: only these servers get a reused
            # backend session; the rest stay per-call stateless.
            affine_names = {
                n
                for n, s in config.mcpServers.items()
                if s.serve_mcp_tools and s.session_affinity
            }
            (
                new_app,
                mcp_proxy,
                new_stateful,
            ) = await _build_stateful_mcp_app_with_proxy(
                config_dict, affine_names, alias_middleware
            )
        else:
            new_app, mcp_proxy = _build_mcp_app_with_proxy(
                config_dict, alias_middleware
            )

        new_lm = LifespanManager(new_app)
        published = False
        lm_entered = False

        try:
            _ = await new_lm.__aenter__()
            lm_entered = True

            current_mount = get_mcp_mount()
            if current_mount is None:
                app.mount("/mcp", new_app)

                mount = next(
                    (
                        r
                        for r in app.router.routes
                        if isinstance(r, Mount) and r.path == "/mcp"
                    ),
                    None,
                )
                if mount is None:
                    msg = (
                        "Failed to find mounted MCP gateway after mounting. "
                        "This should not happen and indicates a bug."
                    )
                    raise RuntimeError(msg)
                set_mcp_mount(mount)
            else:
                current_mount.app = new_app

            old_lm = get_mcp_lifespan_manager()
            if old_lm is not None:
                # The new app is already mounted and live; the old app is being
                # discarded, so a teardown failure here must NOT abort the swap and
                # roll back onto the now-live gateway. Swallow and log.
                try:
                    _ = await old_lm.__aexit__(None, None, None)
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"Old gateway lifespan teardown errored (ignored): {e}"
                    )

            set_mcp_lifespan_manager(new_lm)

            # Old app is fully torn down; publish the new session-affine client as
            # active, then disconnect the previous one (no-op when the previous
            # gateway was stateless).
            prev_stateful = get_current_stateful()
            set_current_stateful(new_stateful)
            published = True
            await _shutdown_stateful(prev_stateful)

            server_count = len(config.mcpServers)
            logger.info(
                f"Successfully swapped MCP gateway with {server_count} server(s)"
            )

            # Only serve_mcp_tools servers contribute to the aggregated /mcp list.
            server_names = [
                n for n, s in config.mcpServers.items() if s.serve_mcp_tools
            ]
            # rest-only servers are routable via /rest but excluded from /mcp;
            # still wait on them so /apps doesn't report ready before they serve.
            rest_only_urls = {
                n: s.url
                for n, s in config.mcpServers.items()
                if not s.serve_mcp_tools and s.url
            }
            if not server_names and not rest_only_urls:
                logger.debug("No MCP tool servers configured, skipping readiness check")
                return mcp_proxy

            logger.debug("Waiting 1.0 seconds before starting readiness checks...")
            await asyncio.sleep(1.0)
            if server_names:
                _ = await warm_and_check_gateway(mcp_proxy, server_names)
            await warm_and_check_servers(rest_only_urls)

            # Install the allowlist filter only after readiness, otherwise
            # warm_and_check_gateway would see only the allowed subset and
            # report missing servers for any whose tools are all excluded.
            # Truthy check: an empty list is treated the same as None (no
            # filter). Otherwise an accidentally-empty allowlist would silently
            # block every tool.
            if config.allowed_tool_names:
                mcp_proxy.add_middleware(
                    _AllowedToolsMiddleware(config.allowed_tool_names)
                )
                logger.info(
                    f"Tool allowlist active: {len(config.allowed_tool_names)} tools allowed"
                )
            # Always strip non-string enum hints: external Go agent runners
            # type JSON-Schema `enum` as []string and
            # reject the whole tool list on one non-string member. Dropping the
            # enum preserves each field's type, so emitted values are unchanged.
            mcp_proxy.add_middleware(_StripNonStringEnumsMiddleware())

            # Activate tool aliasing LAST. The middleware itself is already
            # registered (first in the constructor stack, ahead of the
            # coordinator); this only fills in its map, which needs the warm
            # gateway's live tool list. Resolution can still raise on an
            # ambiguous map, and by this point the new app is already published
            # and serving — so it has to run AFTER the allowlist and enum
            # middleware are installed, or a bad alias config would abort the
            # swap and leave a LIVE gateway serving every tool unfiltered
            # (Bugbot: "Alias failure skips allowlist middleware"). Failing here
            # now leaves the security controls in place and the rollout serving
            # canonical names, with /apps reporting the failure.
            await _install_tool_aliases(mcp_proxy, alias_middleware, server_names)
            return mcp_proxy

        except MCPReadinessError:
            raise
        except Exception as e:
            # Pre-publish failure (including new_lm.__aenter__): tear down the
            # just-built session-affine client AND the new app's lifespan so
            # neither leaks; the previous gateway stays active. Post-publish the
            # new app IS the live gateway — leave it mounted (do NOT tear it down).
            if not published:
                await _shutdown_stateful(new_stateful)
                if lm_entered:
                    _ = await new_lm.__aexit__(None, None, None)
            logger.error(f"Failed to swap MCP gateway: {e}")
            raise RuntimeError(f"Failed to swap MCP gateway: {e}") from e

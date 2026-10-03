"""OAS as living contract — resolution, ``oneOf``, URL derivation (spec §4/§5).

The request primitive takes an **OAS pointer instead of a URL** (spec §5):

- ``resolve_operation("getOrganization")`` → the operation (method, path
  template). Backed by an index of every documented operation, built by walking a
  **standard OpenAPI 3.x document** the app resolves (:meth:`OasIndex.from_document`
  / :meth:`OasIndex.from_hooks`) and running :func:`extract_response_shapes` per
  operation.
- ``build_url(op, path=..., query=...)`` templates ``{param}`` placeholders and
  appends the query string, returning a **relative** URL (the base — clone
  ``TestClient`` vs. live reference — is the client's concern, spec §6.1).
- ``resolve_response(op, status, body)`` picks the documented ``oneOf`` branch
  (spec §4). Minimal here (records status/empty); full branch resolution + the
  two contract-failure modes are deferred to the coverage/contract work.

**Baseline vs. completeness.** The baseline (the standard OpenAPI doc) is the
app's concern — however it cobbles it together. The completeness *additions* are
standard across apps and layered here as app-owned, operationId-keyed overlays in
the app's overlays dir:

- ``links.json`` — identity-correlation pointers (spec §3).
- ``branches.json`` — discrimination the vendor baseline lacks (every app's
  baseline is discrimination-poor). Applied *after* :func:`extract_response_shapes`
  so an authored shape overrides an inferred (or absent) one. Stale/malformed
  entries fail loudly (:meth:`OasIndex.validate_overlays`), mirroring ``links``.

Read-only at runtime; spec lifecycle (git three-way merge, canonicalize-on-
import) is tooling, not this module.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_HTTP_METHODS = frozenset({"get", "put", "post", "delete", "patch", "options", "head", "trace"})

_TEMPLATE_PARAM = re.compile(r"\{([^}]+)\}")


def _template_params(template: str) -> list[str]:
    """The ``{name}`` placeholder names in a template string, in order."""
    return _TEMPLATE_PARAM.findall(template or "")


@dataclass
class OperationSpec:
    operation_id: str
    method: str
    path: str  # template, e.g. /crm/v8/{module}
    tag: str = ""
    source_file: str = ""
    # Documented response shapes, keyed by status code string. Each value is
    # either ``{}`` (a single, undiscriminated schema for that status) or
    # ``{"discriminator": <field/pointer>, "branches": [<value>, ...]}`` when the
    # status resolves a ``oneOf`` union — the discriminator names the property
    # whose value selects the branch (Zoho errors use ``code``) and ``branches``
    # is the full set of documented discriminator values (one coverable shape
    # each). Derived automatically from the source OpenAPI operation by
    # :func:`extract_response_shapes`; absent ⇒ ``{}`` (one implicit
    # ``(status, "")`` shape).
    responses: dict[str, Any] = field(default_factory=dict)
    # Authored identity links (spec §3). Each entry: ``{"source": <pointer>,
    # "targets": [{"operationId", "parameter"}, ...]}``. ``source`` is a JSON
    # pointer into *this* operation's response whose value is a reference-class
    # id — the equivalence source the harness binds a token to. Merged from the
    # app-owned ``links.json`` overlay (Zoho's specs ship none).
    links: list[dict[str, Any]] = field(default_factory=list)
    # MCP wire-name template (the rule: REST path params → MCP tool-name
    # discriminants). From the operation's ``x-mcp-tool-name``, e.g.
    # ``"get_{module}"``; its ``{placeholders}`` are drawn from this op's path
    # params. Empty ⇒ identity: the wire tool name is the ``operation_id`` and the
    # discriminant travels in ``arguments`` instead of the name. Only meaningful on
    # an MCP index (a REST doc carries no ``x-mcp-tool-name``, so it stays ``""``).
    tool_name: str = ""

    def path_param_names(self) -> list[str]:
        """The ``{param}`` placeholder names in this operation's path template."""
        return _template_params(self.path)

    def response_shapes(self) -> list[tuple[str, str]]:
        """Every documented ``(status, branch)`` response shape (spec §4).

        A single-schema status contributes one ``(status, "")`` shape; a
        discriminated (``oneOf``) status contributes one ``(status, <value>)``
        shape per documented discriminator value — so each authored error code is
        its own coverable shape. Empty when the operation documents no responses:
        the shape level then can't name a gap and the operation simply doesn't
        participate in it."""
        pairs: list[tuple[str, str]] = []
        for status, spec in self.responses.items():
            disc = (spec or {}).get("discriminator")
            branches = (spec or {}).get("branches") or []
            if disc and branches:
                pairs.extend((str(status), str(b)) for b in branches)
            else:
                pairs.append((str(status), ""))
        return pairs

    def documented_statuses(self) -> list[str]:
        """The documented response status codes (numeric first, then sorted)."""
        return sorted(self.responses, key=lambda s: (not str(s).isdigit(), str(s)))


@dataclass
class ResolvedResponse:
    status: int
    schema_ref: str = ""  # e.g. RecordResponse#/oneOf/0 ; "" for empty/204
    branch_index: int | None = None


class OasIndex:
    """Indexes documented operations by ``operationId``. Read-only at runtime.

    Built from a resolved **standard OpenAPI 3.x document** — the app resolves its
    own baseline however it likes and hands the parsed dict in via
    :meth:`from_hooks` (the ``oas_document`` hook) or :meth:`from_document`. The
    app-owned overlays under ``overlays_dir`` (``links.json``, ``branches.json``)
    are then layered on top.
    """

    def __init__(
        self,
        document: dict[str, Any],
        *,
        overlays_dir: str | Path | None = None,
    ):
        self.overlays_dir = Path(overlays_dir) if overlays_dir is not None else None
        self._by_id: dict[str, OperationSpec] = {}
        self._synthetic_ids = 0
        self._load_document(document)
        self._load_overlays()
        self._validate_tool_names()

    # --- constructors ---------------------------------------------------------

    @classmethod
    def from_document(
        cls, document: dict[str, Any], *, overlays_dir: str | Path | None = None
    ) -> OasIndex:
        """Build from a resolved standard OpenAPI 3.x document (parsed dict), with
        the app-owned overlays loaded from ``overlays_dir`` (optional)."""
        return cls(document=document, overlays_dir=overlays_dir)

    @classmethod
    def from_hooks(
        cls,
        hooks: Any,
        overlays_dir: str | Path | None = None,
        *,
        which: str = "rest",
    ) -> OasIndex | McpRouter:
        """Build the binding's index from the app's document hook
        (:class:`~parity_test.hooks.Hooks`), layering ``overlays_dir`` on top.

        - ``which="rest"`` (default) reads ``oas_document`` and returns a single
          :class:`OasIndex` — the REST surface.
        - ``which="mcp"`` reads ``mcp_document`` and returns the **MCP** surface: a
          single :class:`OasIndex` when the hook yields one document, or an
          :class:`McpRouter` when it yields a list of :class:`McpServer` (payload-
          split servers). See :func:`build_mcp_index`.

        Raises if the app registered no document hook for the requested binding."""
        if which == "mcp":
            resolver = getattr(hooks, "mcp_document", None)
            if resolver is None:
                raise ValueError(
                    "no mcp_document hook registered — the app's parity_test_ext must set "
                    "hooks.mcp_document to return its MCP OpenAPI document(s); build it "
                    "with tools_to_openapi(from_fastmcp(app))"
                )
            return build_mcp_index(resolver(), overlays_dir=overlays_dir)
        if which != "rest":
            raise ValueError(f"unknown binding {which!r} for from_hooks (expected 'rest' or 'mcp')")
        resolver = getattr(hooks, "oas_document", None)
        if resolver is None:
            raise ValueError(
                "no oas_document hook registered — the app's parity_test_ext must set "
                "hooks.oas_document to return the app's standard OpenAPI document"
            )
        return cls(document=resolver(), overlays_dir=overlays_dir)

    # --- loaders --------------------------------------------------------------

    def _load_document(self, document: dict[str, Any]) -> None:
        """Walk a standard OpenAPI ``paths`` object into ``OperationSpec``s.

        One operation per ``(path, HTTP method)``; ``operationId`` is used verbatim
        (a deterministic ``method_path`` synthesised when absent, so nothing drops
        silently). Response shapes are inferred by :func:`extract_response_shapes`
        — schema-light baselines (Teams ships response *examples*, not schemas)
        simply yield undiscriminated ``{}`` shapes for the branches overlay to fill.
        """
        paths = (document or {}).get("paths") or {}
        if not isinstance(paths, dict):
            return
        for path, item in paths.items():
            if not isinstance(item, dict):
                continue
            for method, operation in item.items():
                if method.lower() not in _HTTP_METHODS or not isinstance(operation, dict):
                    continue
                op_id = operation.get("operationId")
                if not op_id:
                    op_id = self._synthetic_id(method, str(path))
                    self._synthetic_ids += 1
                op = OperationSpec(
                    operation_id=str(op_id),
                    method=method.upper(),
                    path=str(path),
                    tag=str((operation.get("tags") or [""])[0]) if operation.get("tags") else "",
                    source_file="",
                    responses=extract_response_shapes(operation),
                    tool_name=str(operation.get("x-mcp-tool-name") or ""),
                )
                self._by_id[op.operation_id] = op

    @staticmethod
    def _synthetic_id(method: str, path: str) -> str:
        cleaned = re.sub(r"[^0-9a-zA-Z]+", "_", path).strip("_")
        return f"{method.lower()}_{cleaned}" if cleaned else method.lower()

    def _load_overlays(self) -> None:
        """Layer the standard app-owned overlays (``links.json``, ``branches.json``)
        from ``overlays_dir`` onto the parsed operations. No-op when no dir is set."""
        if self.overlays_dir is None:
            return
        self._load_links()
        self._load_branches()

    def _load_links(self) -> None:
        """Merge the app-owned ``links.json`` overlay onto the parsed operations.

        A vendor baseline carries no identity ``links`` (spec §3). Rather than
        fight the spec source, identity links live in a committed, operationId-keyed
        overlay that we merge here. Absent overlay is fine (no links). Unknown
        operationIds are ignored loudly-safe: skipped, since the resolved spec is
        the operation source of truth."""
        assert self.overlays_dir is not None  # guarded by _load_overlays
        links_path = self.overlays_dir / "links.json"
        if not links_path.is_file():
            return
        data = json.loads(links_path.read_text(encoding="utf-8"))
        for op_id, entries in (data.get("links") or {}).items():
            op = self._by_id.get(op_id)
            if op is None or not isinstance(entries, list):
                continue
            op.links = [e for e in entries if isinstance(e, dict) and e.get("source")]

    def _load_branches(self) -> None:
        """Apply the app-owned ``branches.json`` discrimination overlay.

        Every vendor baseline is discrimination-poor (three of the four apps ship
        zero explicit discriminators; the fourth is near-zero), and inference from
        ``oneOf`` enums only reaches so far — schema-light baselines (Teams'
        response *examples*) carry no discrimination at all. This overlay supplies
        the residue, authored identically across apps:

            {"branches": {"<operationId>": {"<status>": {
                "discriminator": "<field/pointer>", "branches": ["<value>", ...]}}}}

        Applied *after* :func:`extract_response_shapes`, so an authored shape
        overrides an inferred (or absent) one. Unlike ``links``, a stale or
        malformed entry is a hard error (fail loud, not silent no-op) — a
        misspelled operationId or an undocumented status is almost always a bug."""
        assert self.overlays_dir is not None  # guarded by _load_overlays
        branches_path = self.overlays_dir / "branches.json"
        if not branches_path.is_file():
            return
        data = json.loads(branches_path.read_text(encoding="utf-8"))
        problems = self._apply_branches(data.get("branches") or {})
        if problems:
            raise ValueError(
                f"{branches_path} does not resolve against the baseline spec:\n  - "
                + "\n  - ".join(problems)
            )

    def _apply_branches(self, mapping: dict[str, Any]) -> list[str]:
        """Validate and apply the ``branches`` overlay mapping. Returns a list of
        human-readable problems; when non-empty, nothing should be trusted and the
        caller raises. Valid entries are applied in place onto ``op.responses``.

        Doubles as the overlay validator (:meth:`validate_overlays`)."""
        problems: list[str] = []
        for op_id, by_status in mapping.items():
            op = self._by_id.get(op_id)
            if op is None:
                problems.append(f"{op_id}: unknown operationId (not in the resolved spec)")
                continue
            if not isinstance(by_status, dict):
                problems.append(f"{op_id}: expected an object of status → shape")
                continue
            for status, shape in by_status.items():
                where = f"{op_id}.{status}"
                if str(status) not in op.responses:
                    problems.append(f"{where}: status not documented on this operation")
                    continue
                if not isinstance(shape, dict):
                    problems.append(f"{where}: expected a shape object")
                    continue
                disc = shape.get("discriminator")
                branches = shape.get("branches")
                if not (isinstance(disc, str) and disc):
                    problems.append(f"{where}: 'discriminator' must be a non-empty string")
                    continue
                if not (isinstance(branches, list) and branches):
                    problems.append(f"{where}: 'branches' must be a non-empty list")
                    continue
                names = [str(b) for b in branches]
                if len(set(names)) != len(names):
                    problems.append(f"{where}: duplicate branch values")
                    continue
                op.responses[str(status)] = {"discriminator": disc, "branches": names}
        return problems

    def validate_overlays(self) -> list[str]:
        """Re-validate the ``branches.json`` overlay against the current index,
        returning a list of problems (empty ⇒ clean). For a ``--check`` tool that
        wants the problems without raising; loading already raises on any problem."""
        if self.overlays_dir is None:
            return []
        branches_path = self.overlays_dir / "branches.json"
        if not branches_path.is_file():
            return []
        data = json.loads(branches_path.read_text(encoding="utf-8"))
        return self._apply_branches(data.get("branches") or {})

    def operation_ids(self) -> list[str]:
        """Every documented ``operationId`` in the resolved spec (the full REST
        surface the coverage roll-up diffs against)."""
        return list(self._by_id)

    def has_operation(self, operation_id: str) -> bool:
        """Whether this spec declares ``operation_id`` (``file:op`` prefix stripped).
        Used by :class:`McpRouter` to route an op to the server that owns it."""
        return operation_id.split(":", 1)[-1] in self._by_id

    def resolve_operation(self, pointer: str) -> OperationSpec:
        """``pointer`` is an ``operationId`` (``file:operationId`` disambiguation
        reserved for multi-file specs)."""
        op_id = pointer.split(":", 1)[-1]
        try:
            return self._by_id[op_id]
        except KeyError:
            raise KeyError(f"unknown operationId {op_id!r} (not in the resolved spec)") from None

    # --- MCP tool ↔ operation mapping ----------------------------------------
    # The index stays keyed by ``operationId``; the wire tool name is only ever a
    # derived property (:meth:`tool_name_for`), never a key. Callers always name
    # the operation and pass its discriminants, so the mapping is forward-only —
    # there is no reverse (concrete tool name → op) lookup, which would be
    # ambiguous for structurally-overlapping templates (``get_{module}`` vs
    # ``get_{calendar_type}`` both match ``get_julian``). The rule: a REST op's
    # path params are the MCP tool-name discriminants (``getRecords``
    # /records/{module} ↔ the ``get_{module}`` tool). Only meaningful on an MCP
    # index; a REST index (no ``x-mcp-tool-name`` anywhere) leaves every op at
    # identity, so this degrades to ``tool_name == operation_id``.

    def _validate_tool_names(self) -> None:
        """Fail loud on a malformed ``x-mcp-tool-name`` mapping (mirrors the
        ``branches.json`` validator): every template placeholder must be a path
        param of its operation, and no two operations may map to the same tool
        name (a tool mirrors at most one op). No-op for an identity-only (REST)
        index."""
        problems: list[str] = []
        seen_literal: dict[str, str] = {}
        seen_template: dict[str, str] = {}
        for op in self._by_id.values():
            template = op.tool_name
            if not template:
                continue  # identity → wire name is the (already-unique) operationId
            params = _template_params(template)
            path_params = set(op.path_param_names())
            for p in params:
                if p not in path_params:
                    problems.append(
                        f"{op.operation_id}: x-mcp-tool-name {template!r} references "
                        f"{{{p}}}, which is not a path param of {op.path!r} "
                        f"(path params: {sorted(path_params)})"
                    )
            table = seen_template if params else seen_literal
            prior = table.get(template)
            if prior is not None:
                kind = "tool-name template" if params else "tool name"
                problems.append(f"{op.operation_id} and {prior} share the {kind} {template!r}")
            else:
                table[template] = op.operation_id
        if problems:
            raise ValueError("invalid x-mcp-tool-name mapping:\n  - " + "\n  - ".join(problems))

    def tool_name_for(self, operation_id: str, path: dict[str, Any] | None = None) -> str:
        """Render the MCP wire tool name for an operation and its path discriminant.

        The ``x-mcp-tool-name`` template's ``{param}`` placeholders are filled from
        ``path`` — the REST path params that become the tool-name discriminants
        (``getRecords`` + ``{module: "leads"}`` → ``get_leads``). An operation with
        no template renders to its ``operation_id`` verbatim (identity: a single
        tool whose discriminant travels in ``arguments`` instead of the name)."""
        op = self.resolve_operation(operation_id)
        template = op.tool_name or op.operation_id
        path = path or {}

        def _fill(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in path:
                raise ValueError(
                    f"tool name template {template!r} for {op.operation_id} needs path "
                    f"param {name!r}; got {sorted(path)}"
                )
            return str(path[name])

        return _TEMPLATE_PARAM.sub(_fill, template)

    def tool_name_params(self, operation_id: str) -> list[str]:
        """The path-param names an operation's tool-name template *consumes*, in
        order (``getRecords`` ``get_{module}`` → ``["module"]``). Empty for an
        identity op (no ``x-mcp-tool-name``): its discriminant, if any, travels in
        the tool ``arguments`` rather than the name. Lets a caller split the REST
        path inputs into what the wire name eats and what stays in the envelope."""
        return _template_params(self.resolve_operation(operation_id).tool_name)

    def build_url(
        self,
        op: OperationSpec,
        *,
        path: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> str:
        url = op.path
        for name, value in (path or {}).items():
            url = url.replace("{" + name + "}", urllib.parse.quote(str(value), safe=""))
        if "{" in url:
            missing = url[url.index("{") : url.index("}") + 1] if "}" in url else url
            raise ValueError(f"unfilled path param {missing} in {op.path!r}")
        if query:
            url += "?" + urllib.parse.urlencode(query, doseq=True)
        return url

    def id_source_pointers(self, op: OperationSpec) -> list[str]:
        """The response JSON pointers whose values are reference-class ids for
        this operation (from authored links, spec §3). Empty when none."""
        return [str(e["source"]) for e in op.links if e.get("source")]

    def resolve_response(self, op: OperationSpec, status: int, body: Any) -> ResolvedResponse:
        """Resolve a response body to its documented ``oneOf`` branch (spec §4).

        ``schema_ref`` carries the resolved branch key — the matched discriminator
        value (Zoho's error ``code``) — or ``""`` when the status has a single,
        undiscriminated schema (or no documented responses). This is the value
        recorded as :attr:`~parity_test.coverage.CoverageCell.branch`."""
        return ResolvedResponse(status=status, schema_ref=resolve_branch(op, status, body))

    def branch_for(self, op: OperationSpec, outcome: int | bool, body: Any) -> str:
        """The resolved branch key for a REST status (``int``) or MCP ``isError``
        (``bool``) outcome — see :func:`resolve_branch`."""
        return resolve_branch(op, outcome, body)


def _pointer_segments(pointer: str) -> list[str]:
    """Split an OAS-runtime-expression / JSON-pointer into path segments.

    Accepts ``$response.body#/data/0/details/id``, ``#/data/0/details/id`` and a
    bare ``data/0/details/id`` — the ``$…#`` prefix (the expression source) is
    dropped; only the fragment pointer is walked."""
    p = pointer.strip()
    if "#" in p:
        p = p.split("#", 1)[1]
    return [seg for seg in p.lstrip("/").split("/") if seg != ""]


def extract_pointer(body: Any, pointer: str) -> Any:
    """Resolve a JSON pointer into ``body`` (numeric segment = list index).

    Returns ``None`` on any miss (absent key, out-of-range index, scalar dead
    end) — a missing id is simply not an equivalence to bind (spec §3)."""
    cur = body
    for seg in _pointer_segments(pointer):
        if isinstance(cur, list):
            if not seg.lstrip("-").isdigit():
                return None
            idx = int(seg)
            if not (-len(cur) <= idx < len(cur)):
                return None
            cur = cur[idx]
        elif isinstance(cur, dict):
            if seg not in cur:
                return None
            cur = cur[seg]
        else:
            return None
    return cur


# --- oneOf branch resolution & shape extraction (spec §4) --------------------


def _candidate_responses(responses: dict[str, Any], outcome: int | bool) -> list[dict[str, Any]]:
    """The documented response specs an ``outcome`` could resolve against.

    A REST ``int`` status maps to the one exact status entry. An MCP ``bool``
    ``isError`` has no numeric status, so it maps to every documented status on
    the matching side of the 400 boundary (error ⇒ ``>= 400``)."""
    if isinstance(outcome, bool):
        out: list[dict[str, Any]] = []
        for status, spec in responses.items():
            try:
                code = int(status)
            except (TypeError, ValueError):
                continue
            if (code >= 400) == outcome:
                out.append(spec or {})
        return out
    spec = responses.get(str(outcome))
    return [spec or {}] if spec is not None else []


def resolve_branch(op: Any, outcome: int | bool, body: Any) -> str:
    """Resolve a response ``body`` to its documented ``oneOf`` branch key.

    Returns the matched discriminator value (Zoho's error ``code``) when the
    outcome's documented response is a discriminated union and the body carries a
    documented value; ``""`` for a single-schema response, an undocumented value,
    or an operation with no documented responses (backward-compatible: every cell
    gets ``branch == ""``, exactly the pre-enrichment behaviour). Reads only
    ``op.responses``, so any object exposing that attribute works."""
    responses = getattr(op, "responses", None) or {}
    for spec in _candidate_responses(responses, outcome):
        disc = spec.get("discriminator")
        branches = spec.get("branches") or []
        if disc and branches:
            value = extract_pointer(body, disc)
            if value is not None and str(value) in {str(b) for b in branches}:
                return str(value)
    return ""


def _response_schema(response_obj: dict[str, Any]) -> dict[str, Any]:
    """The JSON schema for a response object, preferring ``application/json``."""
    content = (response_obj or {}).get("content") or {}
    if not isinstance(content, dict):
        return {}
    chosen = content.get("application/json")
    if chosen is None:
        chosen = next(iter(content.values()), None)
    schema = (chosen or {}).get("schema") if isinstance(chosen, dict) else None
    return schema if isinstance(schema, dict) else {}


def _detect_discriminator(one_of: list[dict[str, Any]]) -> tuple[str | None, list[str]]:
    """Infer the discriminating property of a ``oneOf`` union and its documented
    values. The discriminator is the enum-valued property present in the most
    branches whose values actually *vary* across branches — a property that is a
    constant (Zoho's ``status: "error"``) or unique to one branch discriminates
    nothing. Returns ``(field, sorted_values)`` or ``(None, [])`` when no property
    qualifies (an undiscriminated union → a single implicit shape)."""
    values_by_prop: dict[str, set[str]] = {}
    count_by_prop: dict[str, int] = {}
    for branch in one_of:
        props = (branch or {}).get("properties") or {}
        for name, sub in props.items():
            enum = (sub or {}).get("enum") if isinstance(sub, dict) else None
            if isinstance(enum, list) and enum:
                count_by_prop[name] = count_by_prop.get(name, 0) + 1
                values_by_prop.setdefault(name, set()).update(str(v) for v in enum)
    best: tuple[int, int, str] | None = None
    for name, count in count_by_prop.items():
        values = values_by_prop[name]
        if len(values) < 2:  # constant across branches → not discriminating
            continue
        score = (count, len(values), name)
        if best is None or score > best:
            best = score
    if best is None:
        return None, []
    field_name = best[2]
    return field_name, sorted(values_by_prop[field_name])


def extract_response_shapes(operation: dict[str, Any]) -> dict[str, Any]:
    """Derive an :class:`OperationSpec`'s ``responses`` map from a raw OpenAPI
    operation object (spec §4).

    The single source of truth for the response-shape format: :meth:`OasIndex`
    calls this per operation while walking the document and stores the result under
    ``responses``. For each documented status it inspects the response schema and,
    when it is a ``oneOf`` union, auto-detects the discriminator (an explicit
    OpenAPI ``discriminator.propertyName`` wins; otherwise the best enum-valued
    property — see :func:`_detect_discriminator`) and flattens the branch enum
    values into the full documented set. Statuses with a single schema (or a
    union with no usable discriminator) map to ``{}``. Because it derives purely
    from the spec, the shape level needs no hand-authored manifest — unlike
    behavioural variants."""
    responses = (operation or {}).get("responses") or {}
    out: dict[str, Any] = {}
    for status, response_obj in responses.items():
        schema = _response_schema(response_obj)
        one_of = schema.get("oneOf") if isinstance(schema, dict) else None
        entry: dict[str, Any] = {}
        if isinstance(one_of, list) and one_of:
            explicit = schema.get("discriminator")
            if isinstance(explicit, dict) and explicit.get("propertyName"):
                field_name: str | None = str(explicit["propertyName"])
                values: set[str] = set()
                for branch in one_of:
                    sub = ((branch or {}).get("properties") or {}).get(field_name) or {}
                    enum = sub.get("enum") if isinstance(sub, dict) else None
                    if isinstance(enum, list):
                        values.update(str(v) for v in enum)
                branches = sorted(values)
            else:
                field_name, branches = _detect_discriminator(one_of)
            if field_name and branches:
                entry = {"discriminator": field_name, "branches": branches}
        out[str(status)] = entry
    return out


# --- multi-server MCP routing (payload-split servers) ------------------------
# A single logical MCP surface is often split across several server URLs for
# payload/scale reasons (Zoho exposes 1k+ tools across 9 servers). The model: the
# MCP hook yields one ``(url, OAS)`` pair per server; each builds its own
# ``OasIndex``; an :class:`McpRouter` presents them as one index-shaped surface,
# routing each operation to the **first server (in hook order) whose spec declares
# it** — servers may overlap (the earliest URL wins). An op absent from every spec
# fails to resolve, so surfaces stay independent for free.


@dataclass(frozen=True)
class McpServer:
    """One payload-split MCP server: a base ``url`` and the OpenAPI ``document``
    describing the tools it hosts. What the ``mcp_document`` hook yields."""

    url: str
    document: dict[str, Any]


class McpRouter:
    """Ordered ``(url, OasIndex)`` collection that duck-types the read surface of
    :class:`OasIndex` the harness uses on its MCP index — so a single-server
    :class:`OasIndex` and a multi-server router are interchangeable as ``mcp=``.

    Resolution is **first-match-wins in construction order**: the earliest server
    whose spec declares an operationId owns it (overlap across servers is allowed;
    intra-spec tool-name uniqueness still holds within each ``OasIndex``).
    """

    def __init__(
        self,
        servers: list[McpServer] | list[tuple[str, Any]],
        *,
        overlays_dir: str | Path | None = None,
    ):
        self._servers: list[tuple[str, OasIndex]] = []
        for server in servers:
            url, index = _coerce_server(server, overlays_dir)
            self._servers.append((url, index))

    @classmethod
    def from_documents(
        cls, pairs: list[tuple[str, dict[str, Any]]], *, overlays_dir: str | Path | None = None
    ) -> McpRouter:
        """Build from ``(url, document)`` pairs (each document parsed into its own
        index). Convenience over constructing :class:`McpServer` values."""
        return cls([McpServer(url=u, document=d) for u, d in pairs], overlays_dir=overlays_dir)

    # --- routing --------------------------------------------------------------

    def _owner(self, operation_id: str) -> tuple[str, OasIndex]:
        op_id = operation_id.split(":", 1)[-1]
        for url, index in self._servers:
            if index.has_operation(op_id):
                return url, index
        raise KeyError(
            f"unknown operationId {op_id!r} (declared by no MCP server: "
            f"{[u for u, _ in self._servers]})"
        )

    def server_url_for(self, operation_id: str) -> str:
        """The base URL of the server that owns ``operation_id`` (first match)."""
        return self._owner(operation_id)[0]

    def _owner_of_spec(self, op: OperationSpec) -> OasIndex:
        return self._owner(op.operation_id)[1]

    # --- OasIndex-shaped read surface (delegated to the owning server) --------

    def resolve_operation(self, pointer: str) -> OperationSpec:
        return self._owner(pointer)[1].resolve_operation(pointer)

    def tool_name_for(self, operation_id: str, path: dict[str, Any] | None = None) -> str:
        return self._owner(operation_id)[1].tool_name_for(operation_id, path)

    def tool_name_params(self, operation_id: str) -> list[str]:
        return self._owner(operation_id)[1].tool_name_params(operation_id)

    def has_operation(self, operation_id: str) -> bool:
        op_id = operation_id.split(":", 1)[-1]
        return any(index.has_operation(op_id) for _, index in self._servers)

    def operation_ids(self) -> list[str]:
        """Every operationId across all servers, in server order, first-seen kept
        (a shared op is listed once, under its owning — earliest — server)."""
        seen: dict[str, None] = {}
        for _, index in self._servers:
            for op_id in index.operation_ids():
                seen.setdefault(op_id, None)
        return list(seen)

    def build_url(
        self,
        op: OperationSpec,
        *,
        path: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> str:
        return self._owner_of_spec(op).build_url(op, path=path, query=query)

    def id_source_pointers(self, op: OperationSpec) -> list[str]:
        return self._owner_of_spec(op).id_source_pointers(op)

    def resolve_response(self, op: OperationSpec, status: int, body: Any) -> ResolvedResponse:
        return self._owner_of_spec(op).resolve_response(op, status, body)

    def branch_for(self, op: OperationSpec, outcome: int | bool, body: Any) -> str:
        return resolve_branch(op, outcome, body)


def _coerce_server(server: Any, overlays_dir: str | Path | None) -> tuple[str, OasIndex]:
    """Normalize a router input into ``(url, OasIndex)``. Accepts an
    :class:`McpServer`, a ``(url, OasIndex)`` pair, or a ``(url, document)`` pair."""
    if isinstance(server, McpServer):
        return server.url, OasIndex.from_document(server.document, overlays_dir=overlays_dir)
    url, spec = server
    if isinstance(spec, OasIndex):
        return str(url), spec
    return str(url), OasIndex.from_document(spec, overlays_dir=overlays_dir)


def build_mcp_index(
    produced: Any, *, overlays_dir: str | Path | None = None
) -> OasIndex | McpRouter:
    """Turn what an ``mcp_document`` hook yields into an MCP index.

    - A single OpenAPI document (``dict``) → one :class:`OasIndex` (the common,
      single-server case; a scaffold twin returns the same doc it gives
      ``oas_document``).
    - A single :class:`McpServer` or a list of :class:`McpServer` /
      ``(url, document)`` pairs → an :class:`McpRouter` (payload-split servers,
      first-match-wins). A one-element list is still a router, so multi-server
      wiring stays uniform.
    """
    if isinstance(produced, McpServer):
        return McpRouter([produced], overlays_dir=overlays_dir)
    if isinstance(produced, dict):
        return OasIndex.from_document(produced, overlays_dir=overlays_dir)
    if isinstance(produced, (list, tuple)):
        return McpRouter(list(produced), overlays_dir=overlays_dir)
    raise TypeError(
        "mcp_document must return an OpenAPI document (dict) or a list of McpServer / "
        f"(url, document) pairs; got {type(produced).__name__}"
    )

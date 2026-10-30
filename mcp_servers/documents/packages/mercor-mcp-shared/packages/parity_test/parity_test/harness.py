"""The test-facing harness — the one ``api`` object (spec §1).

Everything a test touches goes through a single mode-aware object so the test
body reads like ordinary business-logic validation and parity falls out as a
side effect (spec §0). The same test runs in replay and capture unchanged:

    def test_get_org(api):
        org = api.call("getOrganization")
        assert org.body["org"][0]["company_name"]

- ``api.call(pointer, **kw)`` — REST binding. Resolves the OAS operation, and:
  * **capture** mode with a missing (or full-refresh) snapshot → calls the
    **reference**, sanitizes-on-capture, persists the snapshot. Capture then
    **serves that snapshot** and never touches the clone — a broken or absent
    clone must never fail a capture (that is exactly when you are recording a
    baseline for it). No parity diff, but the coverage cell is still recorded
    (from the reference truth) and the manifest written — the test covers the
    operation regardless of whether the clone ran.
  * **replay** → calls the **clone** (SUT) through the full middleware stack and
    verifies it against the snapshot (missing snapshot = hard fail). Records a
    coverage cell. Returns the normalized
    :class:`~parity_test.replay.ReplayResult`.
  * **spoof** (an orthogonal flag, not a mode) → makes replay serve the snapshot
    instead of the clone (no clone call, no parity diff): the TDD axis for
    validating a test against a committed snapshot before the clone endpoint
    exists (spec §0 / ``ParityConfig``). Capture already serves the snapshot, so
    spoof only changes replay's behaviour. It persists nothing and never gates, so
    it neither writes snapshots nor the coverage manifest.
- ``api.tool(...)`` — MCP binding (wired after REST validates).
- ``api.invoke(binding, operation, **kw)`` — unified adapter over ``call`` /
  ``tool`` from one signature, so a test can be parametrized over
  :data:`BINDINGS` and drive both REST and MCP from one body when the schema is
  the same. The result carries binding-agnostic accessors — ``.ok`` / ``.failed``
  (REST status ≥ 400 ↔ MCP ``isError``) and ``.data`` (REST ``body`` ↔ MCP
  ``content``) — so the body never branches on ``binding``.
- ``variant=`` (on ``call`` / ``tool``) — an author-declared behavioural-scenario
  label recorded as the deepest layer of the coverage cell, *below* the response
  shape, so two calls that resolve the same operation/discriminant/shape via
  different code paths (e.g. a ``500`` from a missing vs an invalid parameter) are
  tracked — and required — separately. Variants are **per-shape**: each is a code
  path to one specific ``(status, branch)`` response shape, declared scoped to that
  shape in a committed ``variants.json`` manifest
  (:func:`~parity_test.coverage.load_variants`). A shape with no declared variants
  has one implicit, unlabeled default path. Variants can't be detected
  automatically, hence the explicit kwarg. A call is a hard error
  (``_require_variant``) only when the shape it actually produced declares variants
  and the call named none — otherwise its coverage would vanish into that shape's
  implicit ``""`` bucket, untracked.
- ``api.on_cleanup(fn)`` — LIFO finalizer (ephemeral seed teardown, spec §8).
- ``api.is_active(side)`` — whose data is this run asserting on, ``"clone"`` or
  ``"reference"``? Exactly one is active: ``"clone"`` in plain replay (the SUT ran
  and is being diffed), ``"reference"`` in capture and spoof (the served snapshot
  is the reference truth). Guard side-only assertions with it —
  ``assert not api.is_active("clone") or value == 5`` — so a check that only holds
  for one side's data sits out when the run asserts on the other side.

The harness owns the per-test :class:`~parity_test.coverage.CoverageRecorder`,
:class:`~parity_test.identity.IdRegistry`, and resolved
:class:`~parity_test.rules.ComparisonRules`; the pytest ``api`` fixture builds
and finalizes it.
"""

from __future__ import annotations

import hashlib
import json
import logging
import urllib.parse
from collections.abc import Callable, Mapping
from typing import Any

from .compare import assert_matches
from .coverage import CoverageCell, CoverageRecorder, variant_labels_for_outcome
from .failures import ParityFailure, ParityMismatch, StaleSnapshotFailure
from .identity import IdRegistry
from .oas import extract_pointer, resolve_branch
from .replay import McpReplayResult, ReplayResult, replay_mcp, replay_rest
from .rules import ComparisonRules
from .sanitize import desanitize_placeholders, sanitize_response
from .snapshot import Snapshot, SnapshotStore

logger = logging.getLogger("parity_test")

#: The bindings :meth:`Harness.invoke` can drive, in wiring order (REST first,
#: MCP after it validates). Parametrize a test over this to exercise both from
#: one body when the schema is the same: ``@pytest.mark.parametrize("binding",
#: parity_test.BINDINGS)``.
BINDINGS: tuple[str, ...] = ("rest", "mcp")


def _merge_arguments(
    path: dict[str, Any] | None,
    query: dict[str, Any] | None,
    body: Any,
) -> dict[str, Any]:
    """Best-effort MCP tool ``arguments`` from the REST-shaped inputs, for the
    common case where a tool mirrors a REST op 1:1 (same schema).

    Path and query params merge in; a dict body merges on top (its fields usually
    *are* the tool inputs). A non-dict body (list/scalar) can't be merged into the
    envelope and is left out — pass ``arguments=`` explicitly when the tool
    envelope isn't the flat union of the REST inputs.
    """
    merged: dict[str, Any] = {}
    if path:
        merged.update(path)
    if query:
        merged.update(query)
    if isinstance(body, dict):
        merged.update(body)
    return merged


class Harness:
    """Mode-aware facade bound to one test case."""

    def __init__(
        self,
        *,
        mode: str,
        oas: Any,
        store: SnapshotStore,
        clone: Any,
        reference: Any = None,
        resolver: Any = None,
        rules: ComparisonRules | None = None,
        full_refresh: bool = False,
        spoof: bool = False,
        declared_variants: dict[str, dict[tuple[str, str], list[str]]] | None = None,
        mcp: Any = None,
        mcp_reference: Any = None,
    ):
        self.mode = mode
        # Orthogonal to ``mode``: serve the snapshot as if the clone produced it
        # and skip the clone call and the parity diff (coverage is still recorded
        # from the snapshot's outcome — the test covers the operation regardless).
        # Composes with both modes — see the spoof branches in ``call``/``tool``.
        self.spoof = spoof
        self.oas = oas
        # The MCP surface is its own index, independent of REST — an MCP-only op
        # lives only here, a REST-only op only in ``oas``; each binding resolves
        # against its own index and never falls back to the other (spec membership
        # is how a surface asserts an operation exists). Defaults to ``oas`` so a
        # single shared doc keeps working when a caller wires only one index.
        self.mcp = mcp if mcp is not None else oas
        self.store = store
        self.clone = clone
        self.reference = reference
        # A reference that speaks MCP (parallel to the REST ``reference``). When
        # present, MCP snapshots are captured by calling it directly; when absent,
        # capture falls back to reusing the REST reference as the MCP baseline.
        self.mcp_reference = mcp_reference
        self.resolver = resolver
        self.rules = rules or ComparisonRules()
        self.full_refresh = full_refresh
        # operationId → {(status, branch): [variant labels]} — the declared
        # behavioural variants grouped by the response shape each produces (the
        # implicit "" default of a variant-less shape is *not* a declared one).
        # Used to fail a call whose resolved shape declares variants but names
        # none — see ``_require_variant``.
        self._declared_variants = declared_variants or {}
        self.coverage = CoverageRecorder()
        self.ids = IdRegistry()
        self._cleanups: list[Callable[[], None]] = []
        self._ordinal = 0
        self._seed_ordinal = 0
        self._used_labels: set[tuple[str, str]] = set()

    @property
    def _serve_snapshot(self) -> bool:
        """Serve the snapshot AS IF the clone produced it — skip the clone call and
        the parity diff (but still record the coverage cell: the test covers the
        operation regardless of whether the clone ran; the outcome comes from the
        reference truth in the snapshot).

        True for **capture** and for **spoof**. Capture always serves the snapshot:
        it records the reference truth and must never fail because the clone is
        absent or half-built (that is exactly when you are capturing a baseline for
        it). Spoof does the same in replay — the TDD axis, validating a test against
        a committed snapshot before the clone endpoint exists. Only plain replay
        exercises the clone and asserts parity."""
        return self.mode == "capture" or self.spoof

    # --- staleness fingerprint (spec §7) -------------------------------------
    #
    # A snapshot is only a valid baseline for the *inputs it was captured with*.
    # When a test's inputs change but the snapshot is never re-captured, replay
    # would silently diff the clone against a snapshot recorded for a different
    # request — a false parity signal that reads as a contract bug but is really
    # just a stale baseline. We defend against that by fingerprinting the
    # tokenized inputs into the snapshot and comparing on every run.
    @staticmethod
    def _input_fingerprint(payload: Mapping[str, Any]) -> str:
        """SHA-256 over the canonicalized, **already-tokenized** call inputs.

        Tokenized (not raw) so ephemeral ids — a freshly minted ``record_id``,
        etc. — collapse to their stable tokens and don't read as an input change
        run-to-run (the same reason coverage keys tokenize). ``sort_keys`` makes
        the digest order-independent; ``default=str`` tolerates any stray
        non-JSON token object."""
        canon = json.dumps(dict(payload), sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()

    def _fingerprint_rest(
        self,
        method: str,
        path: dict[str, Any] | None,
        query: dict[str, Any] | None,
        body: Any,
    ) -> str:
        """The staleness fingerprint for a REST ``call`` (its tokenized inputs)."""
        return self._input_fingerprint(
            {
                "binding": "rest",
                "method": method,
                "path": self.ids.tokenize(dict(path)) if path else {},
                "query": self.ids.tokenize(dict(query)) if query else {},
                "body": self.ids.tokenize(body),
            }
        )

    def _fingerprint_mcp(
        self,
        name: str,
        arguments: dict[str, Any],
        path: dict[str, Any] | None,
        query: dict[str, Any] | None,
        body: Any,
    ) -> str:
        """The staleness fingerprint for an MCP ``tool`` call (tool name + the
        tokenized argument envelope, plus the REST path/query/body the fallback
        capture uses to build the reference URL)."""
        return self._input_fingerprint(
            {
                "binding": "mcp",
                "toolName": name,
                "arguments": self.ids.tokenize(arguments),
                "path": self.ids.tokenize(dict(path)) if path else {},
                "query": self.ids.tokenize(dict(query)) if query else {},
                "body": self.ids.tokenize(body),
            }
        )

    @staticmethod
    def _is_stale(snap: Snapshot | None, current_hash: str) -> bool:
        """Does a committed snapshot fail to match this call's inputs?

        Stale unless the recorded fingerprint equals the current one. A snapshot
        with **no** recorded fingerprint (captured before this field existed) is
        therefore stale too: we can't prove its inputs still match, so it must be
        regenerated — which in capture mode usually just stamps the missing hash
        onto an otherwise-identical body. A genuinely missing snapshot (``None``)
        is not "stale" — that's the separate missing-baseline path. ``getattr``
        guards duck-typed snapshot stand-ins that predate the field."""
        if snap is None:
            return False
        return getattr(snap, "request_hash", None) != current_hash

    def _gate_stale(self, snap: Snapshot | None, current_hash: str, *, context: str) -> None:
        """Refuse (replay) or warn (spoof) when a served snapshot's inputs are stale.

        Safe to call unconditionally: in capture mode a divergent snapshot has
        already been re-captured above (so its hash now matches) and a hashless
        one is never stale, so this only ever fires in a non-capture run against a
        genuinely mismatched baseline. Replay raises so CI turns red until a
        re-capture; spoof only warns (it serves the snapshot by design and gates
        nothing)."""
        if not self._is_stale(snap, current_hash):
            return
        recorded = str(getattr(snap, "request_hash", None))
        if self.spoof:
            logger.warning(
                "parity_test: serving a STALE snapshot in spoof — %s "
                "(snapshot inputs=%s, current=%s). Re-capture to refresh.",
                context,
                recorded,
                current_hash,
            )
            return
        raise StaleSnapshotFailure(context, expected=recorded, actual=current_hash)

    def is_active(self, side: str) -> bool:
        """Whose data is this run asserting on — the ``"clone"`` or the
        ``"reference"``? Exactly one side is active per run. Guard side-specific
        assertions with it so a check that only holds for one side's data sits out
        in the runs that assert on the other — instead of running (and wrongly
        failing) against it::

            val = api.call("getWidget").body["value"]
            assert not api.is_active("clone") or val == 5

        Reads as "if I'm asserting on the clone, ``val`` must be 5". In capture/spoof
        the left side is truthy, so the clone-only check is skipped; in plain replay
        it is enforced against the live SUT.

        - ``"clone"`` — the SUT's own response. Active only in plain **replay**, the
          one run that calls the clone and diffs it. This is the side for "true only
          on the clone" assertions.
        - ``"reference"`` — the recorded reference truth. Active in **capture** and
          **spoof**: both serve the committed snapshot, whose body *is* the
          reference's data (capture just recorded it from the live upstream; spoof
          replays it). The live reference client need not be wired — the served
          *data* is the reference's, which is what an assertion actually sees.

        Prefer this inline guard over skipping the whole test: a skip in capture
        would drop the snapshot/coverage the run is meant to record. The
        side-agnostic assertions (and coverage recording) still run in every mode;
        only the side-specific checks sit out. An unknown side name fails loud — a
        typo like ``is_active("clnoe")`` would otherwise read falsy and silently
        disable the assertion it guards."""
        if side == "clone":
            return not self._serve_snapshot
        if side == "reference":
            return self._serve_snapshot
        raise ValueError(f"unknown side {side!r}: expected 'clone' or 'reference'")

    def _claim_label(self, binding: str, label: str | None) -> None:
        """Fail loudly if a snapshot ``label`` is reused within one test.

        Snapshots are keyed by ``(binding, label)`` (:class:`SnapshotStore`), so a
        second ``call``/``tool`` sharing a label silently resolves to the first
        call's snapshot instead of recording its own — capture is skipped (the file
        already exists) and the clone is diffed against the wrong artifact. That is
        an authoring mistake, not a parity result, so surface it as one. ``None``
        labels fall back to the call ordinal and are unique by construction."""
        if label is None:
            return
        key = (binding, label)
        if key in self._used_labels:
            raise ValueError(
                f"duplicate snapshot label {label!r} on binding {binding!r} within one "
                "test: each api.call/api.tool needs a unique label (snapshots are keyed by "
                "(binding, label), so a reused label resolves to the earlier call's snapshot "
                "instead of recording its own). Give this call a distinct label."
            )
        self._used_labels.add(key)

    def _require_variant(
        self, op_id: str | None, outcome: int | bool, branch: str, variant: str
    ) -> None:
        """Fail loudly when a call resolves to a response *shape* that declares
        behavioural variants but names none.

        A variant is a code path to one specific shape, so the check is per-shape
        and runs *after* the outcome/branch are known: if the shape this call
        actually produced declares variants and the call named none, its coverage
        would land in the shape's implicit unlabeled bucket — indistinguishable
        from the no-variant default and dropped from the required per-shape variant
        surface. A shape with no declared variants has that single unlabeled
        default and is never policed. The ambiguity is an authoring mistake, so it
        is surfaced as one (mirroring ``_claim_label``)."""
        if variant or not op_id:
            return
        by_shape = self._declared_variants.get(op_id)
        if not by_shape:
            return
        labels = variant_labels_for_outcome(by_shape, outcome, branch)
        if not labels:
            return
        choices = ", ".join(sorted(labels))
        status = ("isError" if outcome else "ok") if isinstance(outcome, bool) else str(outcome)
        shape = status + (f"/{branch}" if branch else "")
        raise ValueError(
            f"operation {op_id!r} resolved to the {shape!r} response shape, which "
            f"declares behavioural variants ({choices}), but this call named none. "
            f'Pass variant="..." naming the scenario it exercises (one of: {choices}); '
            "otherwise its coverage is untracked. If this shape genuinely has a "
            "single path, remove its variants from the manifest."
        )

    def _branch_for(self, op_ref: Any, outcome: int | bool, body: Any, *, index: Any = None) -> str:
        """Resolve the documented ``oneOf`` branch for a coverage cell (spec §4).

        ``op_ref`` is an :class:`~parity_test.oas.OperationSpec` (already resolved)
        or an ``operationId`` string / ``None``. A string is resolved against
        ``index`` (the REST index ``self.oas`` by default; the MCP path passes
        ``self.mcp`` so an MCP-only op's shapes resolve against its own spec). An
        unknown or absent op resolves to ``""`` — the same branch a pre-enrichment
        catalogue yields — so coverage never depends on shape data being present."""
        if op_ref is None:
            return ""
        op = op_ref
        if isinstance(op_ref, str):
            resolve = (index or self.oas).resolve_operation
            try:
                op = resolve(op_ref)
            except Exception:  # noqa: BLE001 - undocumented/unknown op → no branch
                return ""
        return resolve_branch(op, outcome, body)

    def _mcp_transport(self, operation_id: str | None, client: Any) -> tuple[Any, str]:
        """Pick the concrete MCP client and request path for an operation.

        ``client`` (``self.clone`` or ``self.mcp_reference``) is **either** a single
        MCP client — one server, today's default, addressed at ``/`` — **or** a
        ``{url: client}`` mapping for payload-split servers. With a mapping we route
        by the op's server (``self.mcp.server_url_for``, first-match) and derive the
        request path from that URL. A single client is returned unchanged so the
        common case is untouched."""
        if not isinstance(client, Mapping):
            return client, "/"
        url: str | None = None
        resolver = getattr(self.mcp, "server_url_for", None)
        if resolver is not None and operation_id:
            try:
                url = resolver(operation_id)
            except Exception:  # noqa: BLE001 - unrouted op falls through to a clear error
                url = None
        if url is None or url not in client:
            raise RuntimeError(
                f"no MCP client for operation {operation_id!r}: the client mapping "
                f"has URLs {sorted(client)} but the MCP index routes it to {url!r} "
                "(a {url: client} mapping needs the op's server present as a key)"
            )
        return client[url], urllib.parse.urlsplit(url).path or "/"

    # --- REST ----------------------------------------------------------------
    def call(
        self,
        pointer: str,
        *,
        path: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        body: Any = None,
        headers: dict[str, str] | None = None,
        method: str | None = None,
        rules: ComparisonRules | None = None,
        label: str | None = None,
        variant: str = "",
    ) -> ReplayResult:
        op = self.oas.resolve_operation(pointer)
        method = (method or op.method).upper()
        final_rules = self.rules.merge(rules)
        self._claim_label("rest", label)
        self._ordinal += 1
        ordinal = self._ordinal

        # The test only ever holds clone-side ids (this method returns the SUT
        # result), so the clone request uses the values verbatim.
        clone_url = self.oas.build_url(op, path=path, query=query)

        snap = self.store.get("rest", label, ordinal)

        # Fingerprint this call's tokenized inputs so a snapshot recorded for
        # different inputs (test edited, never re-captured) can't be served as if
        # it still matched. Tokenized values so ephemeral ids don't false-positive.
        current_hash = self._fingerprint_rest(method, path, query, body)
        stale = self._is_stale(snap, current_hash)

        # Capture: fill a missing (or full-refresh) snapshot from the reference —
        # and auto-heal a stale one (inputs changed since it was recorded), logging
        # the re-capture so the author sees the baseline was refreshed, not served.
        if self.mode == "capture" and (snap is None or self.full_refresh or stale):
            if stale:
                logger.warning(
                    "parity_test: inputs changed for %s [%s] — re-capturing stale snapshot",
                    op.operation_id,
                    label or ordinal,
                )
            if self.reference is None:
                raise RuntimeError("capture mode requires a reference client")
            # Reference-side request: translate any correlated clone id to its
            # reference counterpart (spec §3) — path, query, and body alike — so
            # the live call addresses the reference's own entity.
            ref_path = self.ids.translate_deep(path, "reference") if path else path
            ref_query = self.ids.translate_deep(query, "reference") if query else query
            ref_body = self.ids.translate_deep(body, "reference") if body is not None else body
            ref_url = self.oas.build_url(op, path=ref_path, query=ref_query)
            ref = replay_rest(self.reference, method, ref_url, json=ref_body, headers=headers)
            # Bind reference-side link ids BEFORE sanitize, so this call's minted
            # ids tokenize into the snapshot (and future calls can translate).
            self._bind_link_ids(op, ref.body, "reference", ordinal)
            sanitized = sanitize_response(ref.body, final_rules, tokenize=self.ids.token_for)
            # Persist a tokenized request so no live id leaks into the committed
            # snapshot (the request is metadata; the body diff is on the response).
            snap = Snapshot(
                binding="rest",
                request={
                    "method": method,
                    "endpoint": self.oas.build_url(
                        op,
                        path=self.ids.tokenize(dict(path)) if path else None,
                        query=self.ids.tokenize(dict(query)) if query else None,
                    ),
                    "body": self.ids.tokenize(body),
                },
                response={"status": ref.status, "body": sanitized},
                label=label,
                ordinal=ordinal,
                operation_id=op.operation_id,
                discriminant=self.ids.tokenize(dict(path or {})),
                request_hash=current_hash,
            )
            self.store.put(snap)

        if snap is None:
            m = ParityMismatch(
                f"snapshot[{label or ordinal}]", expected="a committed snapshot", actual="missing"
            )
            raise ParityFailure(
                [m], context=f"replay of {op.operation_id} — run in capture mode to record"
            )

        # A committed snapshot whose recorded inputs no longer match this call is
        # stale (the test was edited but never re-captured). Replay refuses it
        # rather than diff the clone against a baseline for a different request;
        # spoof warns. (Capture already re-captured above, so this never fires there.)
        self._gate_stale(snap, current_hash, context=f"{op.operation_id} [{label or ordinal}]")

        # Serve the snapshot AS IF the clone produced it — skip the SUT call and
        # the parity diff. True for capture (just recorded the snapshot from the
        # reference above; a broken/absent clone must never fail a capture) and for
        # spoof (the replay-side TDD axis: validate a test against the committed
        # snapshot before the clone endpoint exists). Only the test's own
        # business-logic assertions (and the snapshot's existence) run; parity is
        # deliberately NOT verified. Coverage is still recorded — the test covers
        # this operation regardless, and the outcome is the reference truth in the
        # snapshot. Never a gating run — the fixture prints a loud header and
        # ParityConfig refuses spoof under CI unless explicitly allowed.
        if self._serve_snapshot:
            status = snap.response["status"]
            branch = self._branch_for(op, status, snap.response["body"])
            self._require_variant(op.operation_id, status, branch, variant)
            self.coverage.record(
                CoverageCell(
                    binding="rest",
                    operation_id=op.operation_id,
                    discriminant=tuple(sorted(self.ids.tokenize(dict(path or {})).items())),
                    outcome=status,
                    branch=branch,
                    variant=variant,
                    label=label,
                )
            )
            # Hand the test an INSTANCE of each type_only field, not the
            # ``{"$type": ...}`` descriptor stored in the snapshot: materialize
            # placeholders to neutral scalars of the right JSON type so shape /
            # ``isinstance`` assertions on ``.body`` hold identically across
            # replay (raw clone values) and capture/spoof (served snapshot). Only
            # the served copy is materialized — the persisted snapshot, branch
            # resolution, and coverage keep the canonical descriptors. Value
            # equality on a type_only field is still meaningless (that's the point
            # of type_only) and should be guarded with ``api.is_active("clone")``.
            return ReplayResult(
                status=snap.response["status"],
                body=desanitize_placeholders(snap.response["body"]),
            )

        # Both (replay/capture): exercise the clone (SUT) and verify against the snapshot.
        sut = replay_rest(self.clone, method, clone_url, json=body, headers=headers)
        # Bind clone-side link ids so this response's ids tokenize to the same
        # token as the snapshot, and downstream calls can translate clone→ref.
        self._bind_link_ids(op, sut.body, "clone", ordinal)
        branch = self._branch_for(op, sut.status, sut.body)
        self._require_variant(op.operation_id, sut.status, branch, variant)
        self.coverage.record(
            CoverageCell(
                binding="rest",
                operation_id=op.operation_id,
                # Tokenize the discriminant so ephemeral ids (e.g. record_id)
                # don't make the coverage cell churn every run (spec §5/§6.6).
                discriminant=tuple(sorted(self.ids.tokenize(dict(path or {})).items())),
                outcome=sut.status,
                branch=branch,
                variant=variant,
                label=label,
            )
        )
        assert_matches(sut, snap, final_rules, ids=self.ids)
        return sut

    def _bind_link_ids(self, op: Any, body: Any, side: str, ordinal: int) -> None:
        """Bind this operation's authored link ids (spec §3) from ``body`` into
        the :class:`~parity_test.identity.IdRegistry` for ``side``.

        The token is derived from the *structural* coordinates (operation, source
        pointer, call ordinal) — identical whether computed from the reference or
        the clone response — so both sides' concrete ids collapse to one token."""
        for pointer in self.oas.id_source_pointers(op):
            value = extract_pointer(body, pointer)
            if value is None:
                continue
            token = f"{op.operation_id}#{pointer}#{ordinal}"
            self.ids.bind(token, side, value)

    def prep(
        self,
        operation: str,
        *,
        side: str = "clone",
        method: str | None = None,
        path: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        body: Any = None,
    ) -> ReplayResult | None:
        """Run one OAS operation as uncompared setup/teardown (spec §8).

        The app-neutral escape hatch that seeders and reset helpers compose on:
        it records **no** snapshot and **no** coverage cell.

        - ``side="clone"`` hits the SUT and always runs — the clone is present in
          both modes.
        - ``side="reference"`` hits the live reference and is a **no-op in replay**
          (returns ``None`` — there is no reference; the clone's freshness is the
          contract). Used during capture so a seeded/torn-down given exists on the
          reference too, keeping the recorded snapshot faithful.

        The framework stays ignorant of any app's resource model — the caller
        names the operation and supplies the params.
        """
        client = self.clone if side == "clone" else self.reference
        if client is None:  # reference side in replay
            return None
        op = self.oas.resolve_operation(operation)
        url = self.oas.build_url(op, path=path or {}, query=query or {})
        return replay_rest(client, method or op.method, url, json=body)

    def bind_seeded_id(
        self, *, kind: str, clone: Any, reference: Any = None, token: str | None = None
    ) -> str:
        """Register a seeded entity's id(s) under one stable token so both sides
        tokenize equal (spec §3).

        Seeding mints different ids on the clone and the reference; binding both
        to a shared symbolic ``token`` lets value-substitution compare them (just
        like an OAS-link-minted id). The token is derived from ``kind`` + a
        per-test seed ordinal so it is **stable across runs** — never from a
        minted id, which would pin the committed snapshot to one run's value.
        Clone-side always; reference-side only when supplied (capture). Returns
        the token.
        """
        self._seed_ordinal += 1
        tok = token or f"seed#{kind}#{self._seed_ordinal}"
        self.ids.bind(tok, "clone", clone)
        if reference is not None:
            self.ids.bind(tok, "reference", reference)
        return tok

    # --- MCP -----------------------------------------------------------------
    def tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        operation: str | None = None,
        path: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        body: Any = None,
        rules: ComparisonRules | None = None,
        label: str | None = None,
        variant: str = "",
    ) -> McpReplayResult:
        """Replay-or-capture an MCP tool call and verify it against the snapshot
        (spec §6.1).

        ``operation`` is an *agent-declared* attribution (which operation this tool
        stands for). It is used only to attribute coverage, resolve the response
        shape (branch), and — in the REST fallback below — source the canonical
        expected body. Nothing resolves it from a registry; a self-describing tool
        merely lets the app supply it automatically.

        **Capture sources the baseline from whichever reference is configured:**

        - **MCP reference present (native):** call the MCP tool on the reference
          directly (:func:`~parity_test.replay.replay_mcp`) and record its *actual*
          ``isError`` + content. This is the default whenever ``mcp_reference`` is
          wired; ``operation`` is optional (coverage/branch attribution only).
        - **No MCP reference (fallback):** the MCP tool's canonical body is the same
          sanitized body as the REST op it mirrors, so capture fills the snapshot
          from the REST ``reference`` (``isError = status >= 400``). Requires
          ``operation=`` (the REST op) and ``path`` / ``query`` / ``body`` (the REST
          inputs, used only to build the reference URL — e.g. for ``getRecords``).

        Both modes then replay the clone's MCP tool and diff. The ``arguments``
        envelope always carries the tool inputs sent to the clone (and, in native
        capture, to the MCP reference).
        """
        arguments = arguments or {}
        final_rules = self.rules.merge(rules)
        self._claim_label("mcp", label)
        self._ordinal += 1
        ordinal = self._ordinal

        snap = self.store.get("mcp", label, ordinal)

        # Fingerprint the tokenized inputs (tool name + arguments, plus the REST
        # path/query/body the fallback uses to build the reference URL) so an
        # edited-but-not-re-captured snapshot can't be served as a match. Arguments
        # are tokenized so ephemeral ids don't false-positive.
        current_hash = self._fingerprint_mcp(name, arguments, path, query, body)
        stale = self._is_stale(snap, current_hash)

        # Capture: fill a missing (or full-refresh) snapshot — and auto-heal a stale
        # one (inputs changed since capture), logging the re-capture. Source the
        # baseline from whichever reference is configured — the MCP reference
        # natively when present, else fall back to the REST reference's mirrored op.
        if self.mode == "capture" and (snap is None or self.full_refresh or stale):
            if stale:
                logger.warning(
                    "parity_test: inputs changed for MCP tool %s [%s] — re-capturing stale "
                    "snapshot",
                    name,
                    label or ordinal,
                )
            if self.mcp_reference is not None:
                # --- native: call the MCP reference tool directly ----------------
                # ``operation=`` is optional for a single-server reference (routing
                # is trivial — one client at ``/``), but a ``{url: client}`` mapping
                # routes via ``server_url_for``, which keys on operationId. A
                # templated wire ``name`` (name != operationId) can't route, so
                # require the attribution up front with an actionable message rather
                # than letting ``_mcp_transport`` fail with a confusing "no client
                # for operation <wire-name>". Guarding at capture also protects
                # replay: a multi-server snapshot can then never lack a recorded op.
                if operation is None and isinstance(self.mcp_reference, Mapping):
                    raise RuntimeError(
                        f"native MCP capture of tool {name!r} needs operation= to "
                        "route on a multi-server {url: client} reference mapping "
                        "(server_url_for keys on operationId, and a templated wire "
                        "name isn't one). Pass operation=, or use a single MCP "
                        "reference client."
                    )
                # Translate correlated clone ids in the arguments to their
                # reference counterparts before addressing the live MCP reference
                # (spec §3), mirroring the REST capture path.
                ref_args = self.ids.translate_deep(arguments, "reference") or {}
                ref_client, ref_path = self._mcp_transport(operation or name, self.mcp_reference)
                ref = replay_mcp(ref_client, name, ref_args, mcp_path=ref_path)
                # Bind this op's link ids from the reference content BEFORE
                # sanitize so its minted ids tokenize into the snapshot. Uses the
                # MCP index; skip when no op is attributed (identity-only tool).
                if operation is not None:
                    self._bind_link_ids(
                        self.mcp.resolve_operation(operation), ref.content, "reference", ordinal
                    )
                sanitized = sanitize_response(ref.content, final_rules, tokenize=self.ids.token_for)
                snap = Snapshot(
                    binding="mcp",
                    request={"toolName": name, "arguments": self.ids.tokenize(arguments)},
                    # Native truth: record the tool's ACTUAL isError, not a
                    # translated REST status.
                    response={"isError": ref.is_error, "body": sanitized},
                    label=label,
                    ordinal=ordinal,
                    operation_id=operation or "",
                    discriminant=self.ids.tokenize(dict(path or {})),
                    request_hash=current_hash,
                )
                self.store.put(snap)
            else:
                # --- fallback: reuse the REST reference as the MCP baseline -------
                if operation is None:
                    raise RuntimeError(
                        f"MCP capture for tool {name!r} without an MCP reference needs "
                        "operation= (the REST op whose reference body is the canonical "
                        "expected content) — or wire mcp_reference to capture natively"
                    )
                if self.reference is None:
                    raise RuntimeError(
                        "MCP capture requires a reference client (mcp_reference to capture "
                        "natively, or reference to reuse the REST baseline)"
                    )
                op = self.oas.resolve_operation(operation)
                # Mirror the REST capture path (spec §3): translate any correlated
                # clone id to its reference counterpart — path, query, and body
                # alike — before addressing the live reference, then bind this op's
                # link ids from the reference response BEFORE sanitize so its minted
                # ids tokenize into the snapshot (and future calls can translate).
                ref_path = self.ids.translate_deep(path, "reference") if path else path
                ref_query = self.ids.translate_deep(query, "reference") if query else query
                ref_body = self.ids.translate_deep(body, "reference") if body is not None else body
                url = self.oas.build_url(op, path=ref_path, query=ref_query)
                ref = replay_rest(self.reference, op.method, url, json=ref_body)
                self._bind_link_ids(op, ref.body, "reference", ordinal)
                sanitized = sanitize_response(ref.body, final_rules, tokenize=self.ids.token_for)
                # An MCP tool signals failure via isError; the reference REST status
                # is the ground truth for whether this call is an error outcome.
                snap = Snapshot(
                    binding="mcp",
                    # Tokenize like the native path (and REST ``call``) so correlated
                    # clone ids never leak into the committed snapshot request.
                    request={"toolName": name, "arguments": self.ids.tokenize(arguments)},
                    response={"isError": ref.status >= 400, "body": sanitized},
                    label=label,
                    ordinal=ordinal,
                    operation_id=op.operation_id,
                    # Tokenize the discriminant so ephemeral ids don't churn it
                    # (matches the REST snapshot).
                    discriminant=self.ids.tokenize(dict(path or {})),
                    request_hash=current_hash,
                )
                self.store.put(snap)

        if snap is None:
            m = ParityMismatch(
                f"snapshot[{label or ordinal}]", expected="a committed snapshot", actual="missing"
            )
            raise ParityFailure(
                [m],
                context=f"MCP replay of {name} — run in capture mode to record",
            )

        # Refuse (replay) / warn (spoof) a snapshot recorded for different inputs —
        # the test was edited but never re-captured (capture auto-healed above).
        self._gate_stale(snap, current_hash, context=f"MCP {name} [{label or ordinal}]")

        # Serve the snapshot as the tool result (see the REST branch in ``call``).
        # ``content`` is the canonical sanitized body — exactly what a real
        # ``replay_mcp`` produces via ``parse_mcp_tool_content`` — so the served
        # result is indistinguishable to the test body; parity is skipped. True for
        # capture (never touch the clone) and for spoof (replay-side TDD). Coverage
        # is still recorded from the snapshot's error outcome.
        if self._serve_snapshot:
            body = snap.response["body"]
            is_error = bool(snap.response.get("isError", False))
            cov_op = operation or snap.operation_id or name
            branch = self._branch_for(
                operation or snap.operation_id, is_error, body, index=self.mcp
            )
            self._require_variant(cov_op, is_error, branch, variant)
            self.coverage.record(
                CoverageCell(
                    binding="mcp",
                    operation_id=cov_op,
                    # The discriminant is the path dict (tokenized so ephemeral ids
                    # don't churn it) — the same keys REST produces, so an op's MCP
                    # and REST cells collapse onto one discriminant leaf across the
                    # two report tabs. The wire tool name is NOT an axis: it's a
                    # derived property of (operation, discriminant), not a
                    # coordinate.
                    discriminant=tuple(sorted(self.ids.tokenize(dict(path or {})).items())),
                    outcome=is_error,
                    branch=branch,
                    variant=variant,
                    label=label,
                )
            )
            # Materialize type_only placeholders to type-correct instances for the
            # test (see the REST branch); the sanitized ``body`` above still drives
            # branch resolution and coverage, and the persisted snapshot is untouched.
            served = desanitize_placeholders(body)
            return McpReplayResult(
                is_error=is_error,
                content=served,
                structured_content=served,
                raw={"result": {"content": served, "isError": is_error}},
            )

        # Both (replay/capture): exercise the clone's MCP tool and verify against the snapshot.
        clone_client, clone_path = self._mcp_transport(
            operation or snap.operation_id or name, self.clone
        )
        sut = replay_mcp(clone_client, name, arguments, mcp_path=clone_path)
        # Bind clone-side link ids (spec §3) from the tool's canonical content so
        # this response's ids tokenize to the same token as the snapshot and
        # downstream calls can translate clone→ref — mirroring the REST path.
        # Needs the op attribution; fall back to the snapshot's recorded op, and
        # skip only when neither names one.
        bind_op_id = operation or snap.operation_id
        if bind_op_id:
            self._bind_link_ids(
                self.mcp.resolve_operation(bind_op_id), sut.content, "clone", ordinal
            )
        cov_op = operation or snap.operation_id or name
        branch = self._branch_for(bind_op_id, sut.is_error, sut.content, index=self.mcp)
        self._require_variant(cov_op, sut.is_error, branch, variant)
        self.coverage.record(
            CoverageCell(
                binding="mcp",
                operation_id=cov_op,
                # The discriminant is the path dict (tokenized so ephemeral ids
                # don't trip false CoverageDrift on replay) — same keys REST
                # produces, so MCP and REST cells align across tabs. The wire tool
                # name is a derived property, not a coverage axis.
                discriminant=tuple(sorted(self.ids.tokenize(dict(path or {})).items())),
                outcome=sut.is_error,
                branch=branch,
                variant=variant,
                label=label,
            )
        )
        assert_matches(sut, snap, final_rules, ids=self.ids)
        return sut

    # --- unified binding adapter ---------------------------------------------
    def invoke(
        self,
        binding: str,
        operation: str,
        *,
        path: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        body: Any = None,
        arguments: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        method: str | None = None,
        attribution: str | None = None,
        rules: ComparisonRules | None = None,
        label: str | None = None,
        variant: str = "",
    ) -> ReplayResult | McpReplayResult:
        """Drive one operation through either binding from a single unified call,
        so a test can be parametrized over :data:`BINDINGS` and exercise both REST
        and MCP from one body when the schema is the same::

            @pytest.mark.parametrize("binding", parity_test.BINDINGS)
            def test_get_records(api, binding):
                res = api.invoke(binding, "getRecords", query={"limit": 10})
                assert res.ok
                assert isinstance(res.data["items"], list)

        The result exposes binding-agnostic accessors so the test body never
        branches on ``binding``: ``res.ok`` / ``res.failed`` (the outcome axis —
        REST status ≥ 400 ↔ MCP ``isError``) and ``res.data`` (the canonical body —
        REST ``body`` ↔ MCP ``content``, the same sanitized shape on both sides).

        Params are the union of :meth:`call` and :meth:`tool`; each binding uses
        what it needs and ignores the rest:

        - **rest** → :meth:`call` with ``path`` / ``query`` / ``body`` / ``headers``
          / ``method``; ``arguments`` and ``attribution`` are ignored.
        - **mcp** → :meth:`tool`. ``operation`` names the operation (not the wire
          tool); the wire name is resolved from the MCP index via
          :meth:`~parity_test.oas.OasIndex.tool_name_for` — the REST path params it
          templates over (``getRecords`` + ``{module: "leads"}`` → ``get_leads``)
          are the tool-name discriminants. The tool ``arguments`` default to the
          REST inputs the name does *not* consume — ``query`` + ``body`` + any
          *unconsumed* ``path`` param (:func:`_merge_arguments`); a param the tool
          name eats never doubles into the envelope. Pass ``arguments=`` to override
          when the envelope differs. Coverage/branch attribution uses ``attribution``
          or the ``operation`` key. ``headers`` and ``method`` are ignored.
        """
        if binding == "rest":
            return self.call(
                operation,
                path=path,
                query=query,
                body=body,
                headers=headers,
                method=method,
                rules=rules,
                label=label,
                variant=variant,
            )
        if binding == "mcp":
            # Resolve the wire tool name from the MCP index (identity → the
            # operationId). The path params the template consumes are folded into
            # the name, so the default arguments envelope is the REST inputs minus
            # those consumed keys (query + body + any unconsumed path param).
            tool_name = self.mcp.tool_name_for(operation, path)
            if arguments is not None:
                tool_arguments = arguments
            else:
                consumed = set(self.mcp.tool_name_params(operation))
                residual_path = (
                    {k: v for k, v in path.items() if k not in consumed} if path else path
                )
                tool_arguments = _merge_arguments(residual_path, query, body)
            return self.tool(
                tool_name,
                tool_arguments,
                operation=attribution or operation,
                path=path,
                query=query,
                body=body,
                rules=rules,
                label=label,
                variant=variant,
            )
        raise ValueError(f"unknown binding {binding!r}; expected one of {BINDINGS}")

    # --- coverage ------------------------------------------------------------
    def finalize_coverage(self, *, passed: bool) -> None:
        """Persist (capture / first-time bootstrap) or verify (replay) the
        ordered coverage manifest for this test (spec §5).

        Skipped when the test didn't pass — a short-circuited run is not a valid
        baseline, and enforcing drift on an already-failing test only adds noise.
        In capture, on ``full_refresh``, or when no manifest exists yet, the
        recorded sequence is written: capture sources every cell's outcome from
        the reference truth (never the clone), so the manifest is a faithful record
        of the surface the suite covers even before the clone implements it.
        Otherwise the recorded sequence is diffed against the committed baseline
        and any :class:`~parity_test.failures.CoverageDrift` is raised.

        Ordering matters: **capture dominates spoof**. Capture records every cell
        from the reference truth (never the clone) and must always persist the
        manifest, even if the orthogonal spoof flag also happens to be set — so
        the capture write comes *before* the spoof early-return. Only then does
        spoof (a replay-side flag) short-circuit: it serves committed snapshots
        and persists nothing (symmetric with not recording snapshots) and is
        explicitly non-gating, so it neither overwrites the committed manifest
        nor fails on drift; its in-memory cells are simply discarded.
        """
        if not passed:
            return
        # Capture always writes — regardless of the spoof flag (a no-op in
        # capture, since capture never touches the clone either way).
        if self.mode == "capture":
            self.store.put_coverage(self.coverage.cells)
            return
        # Replay + spoof: non-gating, persists nothing.
        if self.spoof:
            return
        captured = self.store.get_coverage()
        if self.full_refresh or captured is None:
            self.store.put_coverage(self.coverage.cells)
            return
        drift = self.coverage.diff(captured)
        if drift:
            raise ParityFailure(
                drift,
                context=(
                    f"coverage drift for {self.store.test_id} "
                    "— if this drift is intended (e.g. you added, renamed, or removed a "
                    "variant/call), re-run with PARITY_FULL_REFRESH=1 to re-baseline the "
                    "committed coverage manifest"
                ),
            )

    # --- lifecycle -----------------------------------------------------------
    def on_cleanup(self, fn: Callable[[], None]) -> None:
        self._cleanups.append(fn)

    def finalize(self) -> None:
        errors: list[BaseException] = []
        for fn in reversed(self._cleanups):
            try:
                fn()
            except BaseException as exc:  # noqa: BLE001 - collect all, re-raise first
                errors.append(exc)
        self._cleanups.clear()
        if errors:
            raise errors[0]

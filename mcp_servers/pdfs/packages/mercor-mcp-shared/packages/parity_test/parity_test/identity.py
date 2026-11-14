"""Identity correlation — the hard problem (spec §3).

The clone mints different ids than the reference (we seed via API, not DB), so
ids can't be value-compared. Resolution:

- **OAS ``links`` are the authoritative identity map.** A link declares that
  ``$response.body#/…/id`` of op A *is* op B's path param — an equivalence
  class. Links hang off the operation, so both REST and MCP bindings inherit
  them (spec §3/§6.1).
- **Value-based substitution** replaces registered id values with a stable
  symbolic token *wherever they appear* on both sides; tokens are compared.
- **Detection proposes, promotion disposes**: capture auto-detects candidate
  links into ``link-candidates.json``; the authoring agent promotes.
- **Unattributable residue** fails loudly (:class:`~parity_test.failures.
  UnattributableId`).

Current implementation: value-substitution with a **passthrough default**. Until
a fixture or promoted link registers a value→token correspondence, ``token_for``
returns the value unchanged — so a ``reference``-class field degrades to an
ordinary value compare (correct when the clone is a byte-mirror of the
reference, e.g. a preseeded DB) rather than silently passing. Registration
(fixtures/links) then activates real tokenization symmetrically on both sides.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class IdRegistry:
    """Bidirectional, side-aware map between concrete id values and stable tokens
    for one test run. Seeded by fixtures (known ids) and by resolved OAS links.

    Two directions, both needed by the value-substitution mechanism (spec §3):

    - **value → token** (``token_for``/``tokenize``): the *comparison* direction.
      A link-minted id binds the **same token** to the reference value *and* the
      clone value, so both sides sanitize to that token and compare equal despite
      minting different concrete ids.
    - **value → counterpart value** (``translate``): the *request-building*
      direction. The test only ever holds clone-side ids (``api.call`` returns the
      SUT result). In capture the harness must issue the *reference* request with
      the *reference* id — so it translates each clone value to its reference
      counterpart via the shared token before building the reference URL/body.
    """

    _to_token: dict[Any, str] = field(default_factory=dict)
    # token -> {side -> concrete value}; the reverse of ``_to_token``, kept
    # per-side so ``translate`` can map a value to its counterpart.
    _sides: dict[str, dict[str, Any]] = field(default_factory=dict)

    def register(self, value: Any, token: str) -> None:
        """Bind an id ``value`` to a stable symbolic ``token`` (side-agnostic)."""
        self._to_token[value] = token

    def bind(self, token: str, side: str, value: Any) -> None:
        """Bind one ``side``'s concrete ``value`` to ``token`` (spec §3).

        Called once per side as each side's response is observed: the reference
        value at capture (before sanitize, so it tokenizes into the snapshot) and
        the clone value after the SUT call. ``None`` never binds (a missing id is
        not an equivalence)."""
        if value is None:
            return
        try:
            self._to_token[value] = token
        except TypeError:  # unhashable — never an id leaf
            return
        self._sides.setdefault(token, {})[side] = value
        # Self-register the token so it resolves back to any bound side. In
        # **capture** the harness serves — and the test therefore holds — the
        # *tokenized* id (sanitize replaced the concrete id with its token before
        # the served body was built), NOT a concrete clone id. When that token is
        # threaded into a downstream call, ``translate(token, side)`` must map it
        # to that side's concrete value; without this, ``_to_token`` has no entry
        # for the token itself and ``translate`` degrades to passthrough, leaking
        # the raw token into the reference request (e.g. a rule action id → 400).
        self._to_token.setdefault(token, token)

    def token_for(self, value: Any) -> Any:
        """Value-level substitution: the registered token for ``value``, else
        ``value`` unchanged (passthrough). Idempotent on already-tokenized input
        because a token string is not itself a registered *value*."""
        try:
            return self._to_token.get(value, value)
        except TypeError:  # unhashable (dict/list) — never an id leaf
            return value

    def translate(self, value: Any, to_side: str) -> Any:
        """Map ``value`` to its counterpart on ``to_side`` via the shared token.

        Passthrough when ``value`` is unregistered or the other side isn't bound
        yet — so a call that carries no correlated id (or runs before its source
        was observed) builds unchanged."""
        try:
            token = self._to_token.get(value)
        except TypeError:
            return value
        if token is None:
            return value
        return self._sides.get(token, {}).get(to_side, value)

    def translate_deep(self, obj: Any, to_side: str) -> Any:
        """Recursively translate every registered value in ``obj`` to ``to_side``
        (path params, query values, request body — 'wherever they appear', §3)."""
        if isinstance(obj, dict):
            return {k: self.translate_deep(v, to_side) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.translate_deep(v, to_side) for v in obj]
        return self.translate(obj, to_side)

    def tokenize(self, body: Any) -> Any:
        """Return ``body`` with every registered id value replaced by its token,
        recursively. Used on the SUT side at compare time (the snapshot side is
        already tokenized by sanitize-on-capture)."""
        if isinstance(body, dict):
            return {k: self.tokenize(v) for k, v in body.items()}
        if isinstance(body, list):
            return [self.tokenize(v) for v in body]
        return self.token_for(body)


@dataclass
class LinkCandidate:
    from_operation: str
    source_pointer: str  # e.g. $response.body#/data/0/id
    to_operation: str
    parameter: str
    confidence: str  # high | medium | low
    observations: int = 1


def detect_link_candidates(coverage_trace: Any) -> list[LinkCandidate]:
    """Scan a captured call sequence for a value from one response reappearing
    as a later call's path param; emit ranked candidates. Never writes the spec.

    TODO(spec §3): entropy/id-shape + name/type alignment + cross-capture
    corroboration for ranking. Not needed for the single-call read flow.
    """
    raise NotImplementedError("TODO(spec §3): candidate detection + ranking")

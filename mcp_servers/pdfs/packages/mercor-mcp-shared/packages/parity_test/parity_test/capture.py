"""Capture mode — fill snapshots from the reference (spec §1/§7).

Two modes share one code path; only where the *expected* comes from differs:

- **replay** (default, CI): load the committed snapshot; a miss is a hard fail.
- **capture** (explicit gesture): call the reference, sanitize-on-capture
  (:mod:`parity_test.sanitize`), persist, commit.

Within capture, two sub-behaviors (spec §1):

- **record-on-miss** (default): only fill snapshots that are absent; existing
  ones are untouched → minimal churn, additive.
- **full-refresh** (explicit): re-capture in scope even when present; the
  sanitize-on-capture pipeline keeps a no-change re-capture byte-identical.

Credentials are injected here (capture only), used to call the reference, then
redacted before persist (:func:`sanitize.redact_credentials`) and never
committed (spec §9).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class CaptureConfig:
    mode: str = "replay"  # "replay" | "capture"
    full_refresh: bool = False  # only meaningful in capture mode
    scope: str | None = None  # operation/binding glob limiting a full-refresh


class Capturer:
    """Fills the :class:`~parity_test.snapshot.SnapshotStore` from the reference.

    TODO(spec §1/§7): call reference for a given operation+binding, run the
    sanitize/redact/tokenize pipeline, persist via the store honoring
    record-on-miss vs full-refresh + scope. Also emits ``link-candidates.json``
    (via :func:`identity.detect_link_candidates`) as a side effect of a capture
    run.
    """

    def __init__(self, config: CaptureConfig, store: Any, reference: Any):
        self.config = config
        self.store = store
        self.reference = reference

    def capture(self, snapshot_key: Any) -> Any:
        raise NotImplementedError("TODO(spec §1/§7): reference call → sanitize → persist")

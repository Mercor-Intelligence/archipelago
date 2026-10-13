"""Committed control-plane config (spec §9).

The ParityStudio console edits *committed* config; **replay never depends on the
console** — it reads only what's on disk (spec §9). This module loads that
committed config and resolves the active mode.

Config covers:

- ``mode`` — ``replay`` (default) vs ``capture``; ``full_refresh`` + ``scope``
  for a capture run (spec §1). Resolved from env/CLI over the committed default so
  CI is always replay unless explicitly overridden. **Capture never exercises the
  clone**: it records the snapshot from the reference and then serves it, so a
  broken or absent clone can never fail a capture (that is precisely when you are
  recording a baseline for it).
- ``spoof`` — an orthogonal flag (``PARITY_SPOOF``), not a mode: it makes
  **replay** serve the committed snapshot AS IF the clone produced it (so
  ``api.call``/``api.tool`` return the reference truth) and skips the parity diff.
  This is the TDD loop — validate a test against a committed snapshot (assertions,
  OAS resolution, snapshot presence) *before* the clone endpoint exists. Capture
  already serves the snapshot, so spoof only changes replay's behaviour;
  ``PARITY_MODE=spoof`` is accepted as a friendly alias for ``replay`` + spoof.
  Spoof exercises no clone and asserts no parity, so it must never gate a merge:
  ``resolve`` refuses spoof under CI unless ``PARITY_ALLOW_SPOOF_IN_CI`` is set.
  The idiomatic pairing is to mark not-yet-implemented tests ``@pytest.mark.xfail``
  so the normal (replay) CI run tolerates them until the clone comes online (add
  ``--runxfail`` to a local spoof run so those tests report their real outcome
  instead of XPASS).
- paths — snapshots dir, specs dir, ``variants.json`` (behavioural-variants
  manifest; see :func:`~parity_test.coverage.load_variants`), ``link-candidates.json``,
  ``baseline-official`` / ``baseline-drift.json``, givens manifest.
- waivers — committed coverage-drift and field-class exceptions (spec §5), the
  *only* thing that can soften a hard failure.
- credential registry reference — names/locations resolved at capture, redacted
  before persist, never committed (spec §9).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

VALID_MODES = ("replay", "capture")

# Env values that count as "on" for boolean flags like ``PARITY_SPOOF``.
_TRUTHY = frozenset({"1", "true", "yes", "on"})


@dataclass
class ParityConfig:
    mode: str = "replay"
    # Orthogonal to ``mode``: make replay serve the snapshot as if the clone
    # produced it and skip the parity diff — the TDD loop against committed
    # snapshots before the clone endpoint exists. Capture already serves the
    # snapshot (it never touches the clone), so spoof only changes replay.
    spoof: bool = False
    full_refresh: bool = False
    scope: str | None = None
    snapshots_dir: str = "parity/snapshots"
    specs_dir: str = "parity/specs"
    # Where the suite's *aggregate* artifacts live — the roll-up
    # ``coverage_report.json`` / ``report.html`` the fixtures emit and the
    # cross-suite overlays (``variants.json`` / ``discriminants.json`` /
    # ``exclusions.json``). Per-test snapshots are **co-located** beside their test
    # file (see :func:`~parity_test.snapshot.snapshot_reltail`), so this is the one
    # place that still needs a configured home. Relative paths resolve against the
    # pytest rootdir; ``PARITY_OUTPUT_DIR`` overrides.
    output_dir: str = "parity"
    # Committed behavioural-variants manifest (operationId → [{label, description}]);
    # consumed by the coverage roll-up to size the discriminant×variant matrix.
    # ``None`` means "auto": look in ``output_dir`` (next to the
    # ``coverage_report.json`` the fixtures emit), so a non-default output dir never
    # silently misses it. ``PARITY_VARIANTS_FILE`` (or a committed value) pins an
    # explicit path instead.
    variants_file: str | None = None
    waivers: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def resolve(cls, committed: dict[str, Any] | None = None) -> ParityConfig:
        """Merge committed defaults with env overrides; CI stays replay unless
        ``PARITY_MODE=capture`` is explicitly set.

        ``PARITY_SPOOF`` (truthy) turns on spoof orthogonally to the mode.
        ``PARITY_MODE=spoof`` is accepted as a friendly alias for the common
        ``replay`` + spoof TDD loop.

        TODO(spec §9): load committed config file, validate waivers, resolve the
        credential registry reference. This stub only wires the mode override.
        """
        data = dict(committed or {})
        cfg = cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
        env_mode = os.environ.get("PARITY_MODE")
        if env_mode == "spoof":
            # Alias: spoof is a flag, not a mode; default its source to replay.
            cfg.mode = "replay"
            cfg.spoof = True
        elif env_mode:
            cfg.mode = env_mode
        if os.environ.get("PARITY_SPOOF", "").strip().lower() in _TRUTHY:
            cfg.spoof = True
        if cfg.mode not in VALID_MODES:
            raise ValueError(
                f"parity mode {cfg.mode!r} is not one of {VALID_MODES} "
                "(set PARITY_MODE to one of these, or PARITY_SPOOF=1 to spoof)"
            )
        # Spoof is a replay-only concept: it makes *replay* serve the snapshot.
        # Capture already serves the snapshot and always writes coverage (it
        # never touches the clone), so the flag is a no-op there — normalize it
        # off so the CI guard below and the SPOOF banner don't misclassify a
        # capture run, and so ``finalize_coverage`` still persists the manifest.
        if cfg.mode == "capture":
            cfg.spoof = False
        # Spoof does not exercise the clone or assert parity, so a spoof run can
        # never gate a merge — refuse it under CI unless explicitly allowed.
        if cfg.spoof and os.environ.get("CI") and not os.environ.get("PARITY_ALLOW_SPOOF_IN_CI"):
            raise RuntimeError(
                "parity spoof under CI: spoof serves the snapshot as the clone and "
                "skips the parity diff, so it must not gate a merge. Run CI in replay. "
                "Set PARITY_ALLOW_SPOOF_IN_CI=1 only for a deliberate, non-gating run."
            )
        if os.environ.get("PARITY_FULL_REFRESH"):
            cfg.full_refresh = True
        cfg.scope = os.environ.get("PARITY_SCOPE", cfg.scope)
        cfg.variants_file = os.environ.get("PARITY_VARIANTS_FILE", cfg.variants_file)
        cfg.output_dir = os.environ.get("PARITY_OUTPUT_DIR", cfg.output_dir)
        return cfg

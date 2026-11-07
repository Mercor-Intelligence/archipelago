"""Clone baseline machinery — un-API-able givens & resync (spec §8.1).

The clone starts from a fresh DB each run; tests seed **via the API**, never by
writing the DB — the same symmetry as the reference, so nothing has to be
"reset to live" (spec §8).

Get-or-create *seeding itself* is app vocabulary, not framework: an app defines
its own ``seed`` fixture (e.g. a ``Seeder`` with ``record(module, ...)``) that
composes the neutral harness primitives —
:meth:`~parity_test.Harness.prep` (uncompared create/delete on either side),
:meth:`~parity_test.Harness.bind_seeded_id` (id correlation), and
``api.on_cleanup`` (the ephemeral finalizer). Two tiers are expected (ephemeral
per-test create+delete; durable session-scoped get-or-create keyed by a seed-set
hash with template-DB reuse), but which entities exist and how they're created
is the app's business, so it lives in the app extension.

This module keeps only the app-neutral **baseline** half:

Baseline (spec §8.1): some "givens" are **readable but not writable** through the
API ("un-API-able"). For those, the baseline = snapshots-of-read-ops plus a
``seed_from_snapshot`` transform that reconstructs the entity in the clone from
the captured read. A **givens manifest** lists them. **Resync** is a scoped
full-refresh of the givens' read snapshots reconciled against ``baseline-official``
via three-way merge; drift is *proposed* (``baseline-drift.json``), never
auto-blessed.

The three-way merge keeps a **merge-base ancestor** (``base``) alongside each
blessed value in ``baseline-official.json``. Right after a bless, ``base ==
value``; a human edit to ``value`` (without touching ``base``) is exactly the
"local edit" signal. A resync re-captures the reference (``fresh``) and, per
given, classifies:

======================  ====================================  ================
condition               meaning                               proposal status
======================  ====================================  ================
no official entry       first sight of this given             ``new``
``fresh == value``      reference matches the blessed value   ``unchanged``
``fresh == base``       reference unchanged; local edit kept  ``unchanged``
``value == base``       upstream moved, no local edit         ``update``
otherwise               upstream *and* local both moved       ``conflict``
======================  ====================================  ================

:func:`resync_baseline` only ever writes ``baseline-drift.json`` — it never
mutates the blessed baseline. Accepting proposals is a separate, explicit gesture
(:func:`bless_baseline`), which is what "propose, never auto-bless" means.
"""

from __future__ import annotations

import fnmatch
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .replay import replay_rest
from .rules import ComparisonRules
from .sanitize import sanitize_response

OFFICIAL_NAME = "baseline-official.json"
DRIFT_NAME = "baseline-drift.json"

# A given whose reconciliation warrants human review before the blessed value
# changes. ``unchanged`` givens never appear as proposals.
ACTIONABLE = ("new", "update", "conflict")


@dataclass
class GivensManifest:
    """The committed list of un-API-able givens and their read operations
    (spec §8.1).

    Each entry is a mapping describing one given::

        {
          "key": "organization",          # stable id for the given (required)
          "operation": "getOrganization", # OAS operationId to re-capture (required)
          "binding": "rest",              # rest | mcp (metadata; capture is always
                                          #   the sanitized REST body — see harness)
          "path": {...}, "query": {...},  # optional reference-side request params
          "body": {...},                  # optional request body
          "label": "read",                # optional; documentation only
          "scope": "org",                 # optional grouping tag for scoped resync
          "rules": {"ignore": [...], ...} # optional per-given ComparisonRules kwargs
        }
    """

    entries: list[dict[str, Any]] = field(default_factory=list)

    def in_scope(self, scope: str | None) -> list[dict[str, Any]]:
        """Entries selected by ``scope`` (all when ``scope`` is ``None``).

        ``scope`` is an fnmatch glob tested against each entry's ``scope`` tag,
        ``key``, ``operation``, and ``binding`` — a match on any selects it. This
        lets a caller narrow a refresh to one area (``scope="org"``), one given
        (``scope="organization"``), or a family (``scope="get*"``)."""
        if scope is None:
            return list(self.entries)
        out: list[dict[str, Any]] = []
        for entry in self.entries:
            candidates = [
                str(entry.get(k, ""))
                for k in ("scope", "key", "operation", "binding")
                if entry.get(k)
            ]
            if any(fnmatch.fnmatch(c, scope) for c in candidates):
                out.append(entry)
        return out


@dataclass
class ResyncProposal:
    """One given's reconciliation outcome from a resync run."""

    key: str
    operation: str
    binding: str
    status: str  # new | update | conflict | unchanged
    fresh: Any = None
    official: Any = None
    base: Any = None
    changed_paths: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "operation": self.operation,
            "binding": self.binding,
            "status": self.status,
            "changed_paths": self.changed_paths,
            "official": self.official,
            "base": self.base,
            "fresh": self.fresh,
        }


@dataclass
class ResyncResult:
    """The outcome of a :func:`resync_baseline` run (also serialized to
    ``baseline-drift.json``)."""

    scope: str | None = None
    proposals: list[ResyncProposal] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    # Givens whose reference re-capture failed (network/4xx/5xx); left untouched.
    errors: list[dict[str, str]] = field(default_factory=list)
    # ``{key: fresh}`` for givens whose reference caught up to a prior local edit
    # (``fresh == official`` but ``base != official``): a safe base fast-forward
    # that :func:`bless_baseline` applies without changing the blessed value.
    fast_forward: dict[str, Any] = field(default_factory=dict)

    @property
    def has_drift(self) -> bool:
        """True when at least one given needs review (``new``/``update``/``conflict``)."""
        return any(p.status in ACTIONABLE for p in self.proposals)

    @property
    def conflicts(self) -> list[ResyncProposal]:
        return [p for p in self.proposals if p.status == "conflict"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "scope": self.scope,
            "has_drift": self.has_drift,
            "proposals": [p.to_dict() for p in self.proposals],
            "unchanged": list(self.unchanged),
            "fast_forward": self.fast_forward,
            "errors": self.errors,
        }

    def write(self, path: str | Path) -> Path:
        return _write_drift(Path(path), self.to_dict())


# --- resync ------------------------------------------------------------------


def _load_official(baseline_dir: Path) -> dict[str, Any]:
    path = baseline_dir / OFFICIAL_NAME
    if not path.is_file():
        return {"version": 1, "givens": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("givens", {})
    return data


def _write_official(baseline_dir: Path, official: dict[str, Any]) -> Path:
    baseline_dir.mkdir(parents=True, exist_ok=True)
    path = baseline_dir / OFFICIAL_NAME
    path.write_text(
        json.dumps(official, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def _write_drift(path: Path, drift: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(drift, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return path


def _read_drift(baseline_dir: Path) -> dict[str, Any] | None:
    """The on-disk ``baseline-drift.json`` (a durable review queue), or ``None``."""
    path = baseline_dir / DRIFT_NAME
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _merge_drift(fresh: ResyncResult, existing: dict[str, Any] | None) -> dict[str, Any]:
    """Fold this run's ``fresh`` outcome into the ``existing`` drift file, keyed
    by given ``key``, so a scoped resync never wipes pending review items for
    givens it did not re-evaluate (spec §8.1 — drift is a *durable* review queue).

    Merge semantics per key:

    * A key this run resolved with a fresh outcome (a proposal, or a clean
      ``unchanged`` that should clear any stale pending proposal) — the new entry
      wins/replaces the old one for that key.
    * A key present in ``existing`` but *not* re-evaluated this run (out of scope)
      is preserved verbatim.
    * A key that **errored** this run keeps its previously-pending proposal /
      fast-forward untouched (matching "errored givens are left untouched"); the
      error is recorded alongside.

    Output lists are sorted by key for deterministic, stable files."""
    fresh_dict = fresh.to_dict()
    if not existing:
        return fresh_dict

    # Keys with a fresh, authoritative outcome this run: their prior entries are
    # replaced (proposals) or cleared (clean unchanged). Errored keys are excluded
    # so their prior pending proposal survives.
    resolved = {p.key for p in fresh.proposals} | set(fresh.unchanged)
    errored = {str(e.get("key")) for e in fresh.errors}
    touched = resolved | errored

    # proposals: keep old entries for keys not resolved this run (out-of-scope, or
    # errored → left untouched), then layer this run's fresh proposals on top.
    proposals = [p for p in existing.get("proposals", []) if p.get("key") not in resolved]
    proposals.extend(fresh_dict["proposals"])
    proposals.sort(key=lambda p: str(p.get("key")))

    # fast_forward: same key-preserving rule, as a dict keyed by given key.
    fast_forward = {k: v for k, v in existing.get("fast_forward", {}).items() if k not in resolved}
    fast_forward.update(fresh.fast_forward)

    # unchanged: informational; preserve out-of-scope/errored keys, drop keys now
    # proposed, add this run's clean givens.
    unchanged = sorted(
        {k for k in existing.get("unchanged", []) if k not in resolved} | set(fresh.unchanged)
    )

    # errors: preserve out-of-scope errors; for keys touched this run (resolved or
    # errored again), the fresh outcome replaces the old error.
    errors = [e for e in existing.get("errors", []) if str(e.get("key")) not in touched]
    errors.extend(fresh_dict["errors"])
    errors.sort(key=lambda e: str(e.get("key")))

    return {
        "version": 1,
        "scope": fresh.scope,
        "has_drift": any(p.get("status") in ACTIONABLE for p in proposals),
        "proposals": proposals,
        "unchanged": unchanged,
        "fast_forward": fast_forward,
        "errors": errors,
    }


def _changed_paths(old: Any, new: Any, prefix: str = "") -> list[str]:
    """Dotted/index paths where ``old`` and ``new`` differ (a raw value diff, not
    a field-class comparison — resync detects *any* change to the canonical body).

    Both bodies are already sanitized, so ``ignore``/``type_only`` noise is gone;
    what remains are meaningful value changes worth a reviewer's eye."""
    if isinstance(old, dict) and isinstance(new, dict):
        out: list[str] = []
        for key in sorted(set(old) | set(new)):
            child = f"{prefix}.{key}" if prefix else str(key)
            if key not in old:
                out.append(f"+{child}")
            elif key not in new:
                out.append(f"-{child}")
            else:
                out.extend(_changed_paths(old[key], new[key], child))
        return out
    if isinstance(old, list) and isinstance(new, list):
        out = []
        for i in range(max(len(old), len(new))):
            child = f"{prefix}[{i}]"
            if i >= len(old):
                out.append(f"+{child}")
            elif i >= len(new):
                out.append(f"-{child}")
            else:
                out.extend(_changed_paths(old[i], new[i], child))
        return out
    return [] if old == new else [prefix or "<root>"]


def _recapture(
    entry: dict[str, Any],
    *,
    reference: Any,
    oas: Any,
    base_rules: ComparisonRules,
    tokenize: Callable[[Any], Any],
) -> Any:
    """Call the reference for one given and return the sanitized canonical body.

    Regardless of the given's ``binding``, the canonical expected value is the
    sanitized REST body (an MCP tool wraps the same handler — see
    :meth:`Harness.tool`), so a resync always re-captures the REST operation."""
    op = oas.resolve_operation(entry["operation"])
    url = oas.build_url(op, path=entry.get("path"), query=entry.get("query"))
    result = replay_rest(reference, op.method, url, json=entry.get("body"))
    if result.status >= 400:
        raise RuntimeError(f"reference returned {result.status}")
    rules = base_rules
    if entry.get("rules"):
        rules = base_rules.merge(ComparisonRules(**entry["rules"]))
    return sanitize_response(result.body, rules, tokenize=tokenize)


def resync_baseline(
    manifest: GivensManifest,
    *,
    reference: Any,
    oas: Any,
    baseline_dir: str | Path,
    scope: str | None = None,
    rules: ComparisonRules | None = None,
    ids: Any = None,
    write: bool = True,
) -> ResyncResult:
    """Scoped full-refresh of givens' read snapshots + three-way merge against
    ``baseline-official``; emit ``baseline-drift.json`` (spec §8.1).

    Re-captures each in-scope given from the live ``reference``, sanitizes it the
    same way capture does, and reconciles it against the blessed baseline through
    the merge-base ancestor (see the module docstring for the classification).
    Proposals are **only proposed** — the blessed ``baseline-official.json`` is
    never mutated here; feed the drift to :func:`bless_baseline` after review.

    :param manifest: The committed givens list (spec §8.1).
    :param reference: The live reference client (``request(method, url, ...)``),
        as in capture mode. There is no reference in replay, so resync is a
        capture-time maintenance operation.
    :param oas: An :class:`~parity_test.oas.OasIndex` to resolve operations/URLs.
    :param baseline_dir: Directory holding ``baseline-official.json`` (read) and
        where ``baseline-drift.json`` is written.
    :param scope: Optional fnmatch glob narrowing which givens to refresh
        (see :meth:`GivensManifest.in_scope`).
    :param rules: Base comparison rules used to sanitize captures; per-given
        ``rules`` in the manifest layer on top.
    :param ids: Optional :class:`~parity_test.identity.IdRegistry` supplying the
        ``token_for`` reference-id mapper; when ``None``, ids pass through.
    :param write: When ``True`` (default), write ``baseline-drift.json``.
    :returns: The :class:`ResyncResult` (also serialized to the drift file).
    """
    baseline_dir = Path(baseline_dir)
    base_rules = rules or ComparisonRules()
    tokenize: Callable[[Any], Any] = ids.token_for if ids is not None else (lambda v: v)
    official = _load_official(baseline_dir)
    givens = official["givens"]

    result = ResyncResult(scope=scope)
    for entry in manifest.in_scope(scope):
        if not entry.get("key") or not entry.get("operation"):
            raise ValueError(f"givens manifest entry needs 'key' and 'operation': {entry!r}")
        key = str(entry["key"])
        binding = str(entry.get("binding", "rest"))
        try:
            fresh = _recapture(
                entry, reference=reference, oas=oas, base_rules=base_rules, tokenize=tokenize
            )
        except Exception as exc:  # noqa: BLE001 — one bad given must not sink the run
            result.errors.append(
                {"key": key, "operation": str(entry["operation"]), "reason": str(exc)}
            )
            continue

        official_entry = givens.get(key)
        status, official_value, base_value = _classify(fresh, official_entry)

        if status == "unchanged":
            result.unchanged.append(key)
            # Reference caught up to a prior local edit → safe to fast-forward the
            # ancestor without touching the blessed value.
            if official_entry is not None and fresh == official_value and base_value != fresh:
                result.fast_forward[key] = fresh
            continue

        changed = (
            _changed_paths(official_value, fresh)
            if official_entry is not None
            else sorted(_top_keys(fresh))
        )
        result.proposals.append(
            ResyncProposal(
                key=key,
                operation=str(entry["operation"]),
                binding=binding,
                status=status,
                fresh=fresh,
                official=official_value,
                base=base_value,
                changed_paths=changed,
            )
        )

    if write:
        # Merge into the existing drift file so a scoped resync updates only the
        # keys it re-evaluated and preserves everything else already queued for
        # review (spec §8.1). The returned ``result`` still describes THIS run.
        merged = _merge_drift(result, _read_drift(baseline_dir))
        _write_drift(baseline_dir / DRIFT_NAME, merged)
    return result


def _classify(fresh: Any, official_entry: dict[str, Any] | None) -> tuple[str, Any, Any]:
    """Return ``(status, official_value, base_value)`` for one given."""
    if official_entry is None:
        return "new", None, None
    official_value = official_entry.get("value")
    # A missing ``base`` (hand-authored official) is treated as its own ancestor.
    base_value = official_entry.get("base", official_value)
    if fresh == official_value:
        return "unchanged", official_value, base_value
    if fresh == base_value:
        return "unchanged", official_value, base_value
    if official_value == base_value:
        return "update", official_value, base_value
    return "conflict", official_value, base_value


def _top_keys(value: Any) -> list[str]:
    if isinstance(value, dict):
        return list(value)
    if isinstance(value, list):
        return [f"[{i}]" for i in range(len(value))]
    return ["<root>"]


# --- bless (the explicit accept step) ----------------------------------------


def bless_baseline(
    baseline_dir: str | Path,
    *,
    keys: list[str] | None = None,
    include_conflicts: bool = False,
) -> list[str]:
    """Apply reviewed proposals from ``baseline-drift.json`` into the blessed
    ``baseline-official.json`` — the explicit "accept" gesture resync deliberately
    withholds.

    For each applied proposal the blessed ``value`` **and** the merge-base
    ``base`` are set to ``fresh`` (restoring the ``value == base`` post-bless
    invariant). ``conflict`` proposals are skipped unless ``include_conflicts``
    is set — a conflict means both upstream and a local edit moved, so accepting
    it discards the local edit and should be a deliberate choice. Base
    fast-forwards (``fast_forward``) advance the ancestor without changing the
    value. The consumed proposals are removed from the drift file.

    :param keys: When given, only bless these given keys; others stay pending.
    :param include_conflicts: Also accept ``conflict`` proposals (default off).
    :returns: The list of given keys that were blessed.
    """
    baseline_dir = Path(baseline_dir)
    drift_path = baseline_dir / DRIFT_NAME
    if not drift_path.is_file():
        return []
    drift = json.loads(drift_path.read_text(encoding="utf-8"))
    official = _load_official(baseline_dir)
    givens = official["givens"]

    blessed: list[str] = []
    remaining: list[dict[str, Any]] = []
    for proposal in drift.get("proposals", []):
        key = proposal.get("key")
        acceptable = proposal.get("status") in ("new", "update") or (
            proposal.get("status") == "conflict" and include_conflicts
        )
        selected = keys is None or key in keys
        if acceptable and selected:
            givens[key] = {
                "operation": proposal.get("operation"),
                "binding": proposal.get("binding", "rest"),
                "value": proposal.get("fresh"),
                "base": proposal.get("fresh"),
            }
            blessed.append(key)
        else:
            remaining.append(proposal)

    # Base fast-forwards: advance the ancestor for givens whose reference caught
    # up to a local edit, leaving the blessed value untouched.
    for key, fresh in drift.get("fast_forward", {}).items():
        if (keys is None or key in keys) and key in givens:
            givens[key]["base"] = fresh

    _write_official(baseline_dir, official)
    drift["proposals"] = remaining
    drift["fast_forward"] = {
        k: v for k, v in drift.get("fast_forward", {}).items() if keys is not None and k not in keys
    }
    drift["has_drift"] = any(p.get("status") in ACTIONABLE for p in remaining)
    drift_path.write_text(
        json.dumps(drift, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    return blessed

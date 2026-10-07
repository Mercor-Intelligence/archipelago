"""Coverage & drift manifest (spec §5/§6.6).

Coverage is derived from what actually ran (you can't call without naming an
operation). Per test case, capture records an **ordered sequence** of
*discriminated* coverage cells:

    (binding, operationId, discriminant, status | isError, resolved-branch, variant)

The plain ``(operationId, status, oneOf-branch)`` triple is the degenerate case
with no discriminant, one binding, and one (unnamed) variant. The discriminant is
what makes per-module coverage first-class (spec §6.6) — e.g. ``{"module":
"Leads"}``.

The **variant** is the deepest layer, *below* the response shape: two calls that
resolve the same ``(operation, discriminant, outcome, branch)`` can still exercise
materially different code paths — e.g. a ``500`` from a missing parameter versus a
``500`` from an invalid one. Those look identical to the shape-level matrix, so
the variant names the behavioural scenario so the two are tracked (and required)
separately. Unlike the discriminant (intrinsic — the real path params), the
variant cannot be detected automatically, so it is **author-declared** via
``api.call(..., variant="missing_param")``. Which operations have named variants,
and what they are, is a committed manifest (:func:`load_variants` — operationId →
list of ``{label, description}``); an operation absent from it has a single
implicit variant. The full test matrix an operation should exercise is therefore
the Cartesian product of its discriminant combinations and its variants (spec
§6.6 / :func:`coverage_report`).

For a **REST** cell every non-variant field is intrinsic: the ``operationId`` is
the OAS pointer the test resolved against ``catalogue.json`` (an undocumented
pointer raises), the discriminant is the real path params, the outcome is the
real HTTP status. Only the variant is agent-declared — so a REST coverage
manifest is an exact, verifiable record of which documented operations the suite
exercised, annotated with the author's scenario labels.

Replay recomputes the sequence and diffs it against the captured one via
:meth:`CoverageRecorder.diff`: **strict sequence-equality both directions**
(missing = regression/short-circuit; extra = unexpected call), loud by default.
A drift surfaces as :class:`~parity_test.failures.CoverageDrift`.

Suite coverage is the free union of captured cells (:func:`roll_up` /
:func:`coverage_report`) — roll up by ``operationId`` or drill down to
``(operationId, discriminant, branch)``, and diff against the catalogue to name
the *uncovered* surface.
"""

from __future__ import annotations

import itertools
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .failures import CoverageDrift, ParityMismatch

Binding = Literal["rest", "mcp"]


@dataclass(frozen=True)
class CoverageCell:
    binding: Binding
    operation_id: str
    discriminant: tuple[tuple[str, Any], ...] = ()  # sorted items of the discriminant dict
    outcome: int | bool = 200  # status (REST) or isError (MCP)
    branch: str = ""  # resolved oneOf / outputSchema branch
    # author-declared behavioural scenario below the shape; "" = the sole variant
    variant: str = ""
    label: str | None = None

    def identity(self) -> tuple[Any, ...]:
        """The drift-comparison key — the descriptive ``label`` is deliberately
        excluded (two calls to the same op/discriminant/outcome/variant are the
        same coverage regardless of what the test labelled them). The ``variant``
        *is* included: two calls to the same shape via different declared scenarios
        are distinct coverage that the matrix requires separately."""
        return (
            self.binding,
            self.operation_id,
            self.discriminant,
            self.outcome,
            self.branch,
            self.variant,
        )

    def describe(self) -> str:
        disc = ",".join(f"{k}={v}" for k, v in self.discriminant) or "-"
        branch = f"/{self.branch}" if self.branch else ""
        variant = f"#{self.variant}" if self.variant else ""
        return f"{self.binding}:{self.operation_id}[{disc}]->{self.outcome}{branch}{variant}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding": self.binding,
            "operation_id": self.operation_id,
            "discriminant": [list(item) for item in self.discriminant],
            "outcome": self.outcome,
            "branch": self.branch,
            "variant": self.variant,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CoverageCell:
        disc = tuple(tuple(item) for item in (data.get("discriminant") or []))
        return cls(
            binding=data.get("binding", "rest"),
            operation_id=data.get("operation_id", ""),
            discriminant=disc,  # type: ignore[arg-type]
            outcome=data.get("outcome", 200),
            branch=data.get("branch", ""),
            variant=data.get("variant", ""),
            label=data.get("label"),
        )


def _identity_str(identity: tuple[Any, ...]) -> str:
    binding, operation_id, discriminant, outcome, branch, variant = identity
    disc = ",".join(f"{k}={v}" for k, v in discriminant) or "-"
    br = f"/{branch}" if branch else ""
    var = f"#{variant}" if variant else ""
    return f"{binding}:{operation_id}[{disc}]->{outcome}{br}{var}"


@dataclass
class CoverageRecorder:
    """Accumulates the ordered cell sequence for one test case (spec §5)."""

    cells: list[CoverageCell] = field(default_factory=list)

    def record(self, cell: CoverageCell) -> None:
        self.cells.append(cell)

    def diff(self, captured: list[CoverageCell]) -> list[ParityMismatch]:
        """Strict ordered sequence-equality vs. the captured manifest.

        Returns a :class:`~parity_test.failures.CoverageDrift` per divergence:
        cells present in ``captured`` but not replayed (regression / early
        short-circuit), cells replayed but never captured (unexpected call), and
        — when the multiset matches but the order doesn't — a single ordering
        drift. Empty list ⇒ identical sequences.
        """
        got = [c.identity() for c in self.cells]
        want = [c.identity() for c in captured]
        if got == want:
            return []

        out: list[ParityMismatch] = []
        want_c, got_c = Counter(want), Counter(got)
        for identity, n in (want_c - got_c).items():
            out.append(
                CoverageDrift(
                    _identity_str(identity),
                    expected=f"exercised x{n} in the captured baseline",
                    actual="not exercised in replay (regression / short-circuit)",
                )
            )
        for identity, n in (got_c - want_c).items():
            out.append(
                CoverageDrift(
                    _identity_str(identity),
                    expected="not in the captured baseline",
                    actual=f"exercised x{n} in replay (unexpected call)",
                )
            )
        if not out:  # same multiset, different order
            out.append(
                CoverageDrift(
                    "<sequence-order>",
                    expected=[_identity_str(k) for k in want],
                    actual=[_identity_str(k) for k in got],
                )
            )
        return out


# --- persistence & roll-up ---------------------------------------------------

MANIFEST_NAME = "coverage.json"


def load_manifest(path: str | Path) -> list[CoverageCell]:
    """Load one committed ``coverage.json`` into its ordered cell list."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    raw = data.get("cells", []) if isinstance(data, dict) else data
    return [CoverageCell.from_dict(c) for c in raw]


def scan_manifests(root: str | Path) -> dict[str, list[CoverageCell]]:
    """Load every committed ``coverage.json`` under a scan root, keyed by a stable
    per-test **slug**.

    Snapshots are co-located beside their test files under a ``<stem>_snapshots/``
    folder (see :func:`~parity_test.snapshot.snapshot_reltail`), so manifests live at
    arbitrary depth scattered across the tests tree. The scan is gated on that
    ``*_snapshots/`` suffix, so ``root`` can be a broad common ancestor without
    sweeping in stray ``coverage.json`` files from elsewhere in the repo. The key
    stays the flat, slash-free ``_slug(test_id)`` — the correlation id the report's
    roll-up and the rendered test list share (and a DOM id) — read from the
    manifest's own ``test_id`` so it's independent of how deep the directory sits. A
    manifest with no ``test_id`` (a hand-written fixture) falls back to its path
    relative to root."""
    from .snapshot import _slug

    root = Path(root)
    out: dict[str, list[CoverageCell]] = {}
    for path in sorted(root.glob(f"**/*_snapshots/*/{MANIFEST_NAME}")):
        data = json.loads(path.read_text(encoding="utf-8"))
        test_id = (
            data.get("test_id") if isinstance(data, dict) else None
        ) or path.parent.relative_to(root).as_posix()
        raw = data.get("cells", []) if isinstance(data, dict) else data
        out[_slug(test_id)] = [CoverageCell.from_dict(c) for c in raw]
    return out


DISCRIMINANTS_NAME = "discriminants.json"


def load_discriminants(path: str | Path) -> dict[str, dict[str, list[Any]]]:
    """Load the committed **declared-discriminants** manifest — ``operationId`` →
    ``axis`` → the full value set that axis should be exercised over (spec §6.6,
    "realizing the matrix up front").

    This pre-declares the coverage *denominator*: instead of the discriminant
    matrix growing only as tests happen to exercise values (from ``expand_tokens``
    or observed cells), an operation's declared axes name every combo up front —
    crucially including **negative** values (``NotAModule`` — a module that
    doesn't exist, so no fan-out token ever produces it) — so the report shows a
    complete matrix at ``0%`` before any test is written. Declared values become
    the authoritative ``dims`` for :func:`_expected_combos`; a value *observed but
    not declared* is still projected out (numerator never exceeds total) and is
    surfaced as a soft ``undeclared_discriminants`` notice.

    Tolerant by design (mirrors :func:`load_variants`): a missing / empty file is
    not an error — it just means "no operation pre-declares its axes" and returns
    ``{}``, so ``expand_tokens`` remains the fallback source. Each axis value list
    is de-duplicated order-stably; a scalar is accepted as a one-element set. An
    entry whose axes are all empty is dropped.
    """
    p = Path(path)
    if not p.exists():
        return {}
    raw = json.loads(p.read_text(encoding="utf-8")) or {}
    out: dict[str, dict[str, list[Any]]] = {}
    for op, axes in raw.items():
        if not isinstance(axes, dict):
            continue
        norm_axes: dict[str, list[Any]] = {}
        for axis, values in axes.items():
            seq = values if isinstance(values, list) else [values]
            vals: list[Any] = []
            for v in seq:
                if v not in vals:
                    vals.append(v)
            if vals:
                norm_axes[str(axis)] = vals
        if norm_axes:
            out[str(op)] = norm_axes
    return out


EXCLUSIONS_NAME = "exclusions.json"


def load_exclusions(path: str | Path) -> dict[str, list[dict[str, Any]]]:
    """Load the committed **matrix-exclusions** manifest — ``operationId`` → a list
    of *impossible* ``(discriminant, status, branch, variant)`` intersections (spec
    §6.6, "carve the impossible cells").

    ``discriminants.json`` realizes the whole matrix denominator up front; but a
    fully-realized matrix has cells that can never be reached — a negative module
    (``NotAModule``) can't return ``200``, a valid module can't hit an
    ``INVALID_MODULE`` error branch. Left alone those read as permanent red gaps no
    test can fill. This deny-list marks them, so the report greys them **N/A** and
    drops them from the denominators (never from the per-axis counts — validity is a
    property of the *intersection*, not the axis).

    Each rule is a dict; **an omitted field is a wildcard** (matches anything). The
    ``discriminant`` field is an ``axis → matcher`` map; ``status`` / ``branch`` /
    ``variant`` are matchers too. A *matcher* is a scalar, a list (any-of), or
    ``{"not": value_or_list}`` (its complement) — so you exclude "every module
    *except* the negative one hits this error branch" without enumerating the valid
    set. Matching is by string form, so a JSON ``200`` matches a documented ``"200"``.

    Tolerant by design (mirrors :func:`load_variants` / :func:`load_discriminants`):
    a missing / empty file returns ``{}``. A non-dict rule is skipped; a non-dict
    ``discriminant`` degrades to no discriminant constraint. An operation whose rules
    all drop out is omitted.
    """
    p = Path(path)
    if not p.exists():
        return {}
    raw = json.loads(p.read_text(encoding="utf-8")) or {}
    out: dict[str, list[dict[str, Any]]] = {}
    for op, rules in raw.items():
        norm: list[dict[str, Any]] = []
        for rule in rules or []:
            if not isinstance(rule, dict):
                continue
            disc = rule.get("discriminant")
            entry: dict[str, Any] = {
                "discriminant": (
                    {str(k): v for k, v in disc.items()} if isinstance(disc, dict) else {}
                )
            }
            for key in ("status", "branch", "variant"):
                if rule.get(key) is not None:
                    entry[key] = rule[key]
            norm.append(entry)
        if norm:
            out[str(op)] = norm
    return out


def _match_token(matcher: Any, value: Any) -> bool:
    """Does one exclusion token match a cell's value? A scalar or list is an any-of
    allow-set; ``{"not": …}`` is its complement. Compared by string form so a JSON
    ``200`` matches a documented ``"200"``."""
    if isinstance(matcher, dict) and "not" in matcher:
        neg = matcher["not"]
        negs = neg if isinstance(neg, list) else [neg]
        return str(value) not in {str(n) for n in negs}
    allowed = matcher if isinstance(matcher, list) else [matcher]
    return str(value) in {str(a) for a in allowed}


def _rule_matches_cell(
    rule: dict[str, Any],
    disc: tuple[tuple[str, Any], ...],
    status: str,
    branch: str,
    variant: str,
) -> bool:
    """Does one exclusion rule match a grid cell? Every constrained field must match;
    an omitted field (and a discriminant axis the cell doesn't carry) is a wildcard,
    except that a named axis absent from the cell fails the rule (it constrains an
    axis this operation doesn't have)."""
    dvals = dict(disc)
    for axis, matcher in rule["discriminant"].items():
        if axis not in dvals or not _match_token(matcher, dvals[axis]):
            return False
    for key, value in (("status", status), ("branch", branch), ("variant", variant)):
        if key in rule and not _match_token(rule[key], value):
            return False
    return True


def _is_excluded(
    rules: list[dict[str, Any]],
    disc: tuple[tuple[str, Any], ...],
    status: str,
    branch: str,
    variant: str,
) -> bool:
    """Is this ``(discriminant, status, branch, variant)`` grid cell declared
    impossible by *any* exclusion rule?"""
    return any(_rule_matches_cell(r, disc, status, branch, variant) for r in rules)


VARIANTS_NAME = "variants.json"


def load_variants(path: str | Path) -> dict[str, list[dict[str, str]]]:
    """Load the committed behavioural-variants manifest — ``operationId`` → list
    of ``{"label", "description", "status", "branch"}`` (spec §6.6).

    A variant is a *code path to one specific response shape* — the same variant
    can't apply to both a ``200`` and a ``400`` — so each entry names the shape it
    produces via ``status`` (and optional ``branch``, the ``oneOf`` discriminant;
    ``""`` for a single-schema status). Variants can't be detected automatically,
    so they are author-declared on the call (``api.call(..., variant=...)``) and
    enumerated here per shape so :func:`coverage_report` knows the expected set. A
    shape with no declared variants has exactly one implicit, unnamed path.

    Tolerant by design: a missing (or empty) file is not an error — it just means
    "no operation has named variants" and returns ``{}``. Each entry is normalized
    to a ``{"label", "description", "status", "branch"}`` dict; a bare string is
    accepted as a label. A legacy entry that names no ``status`` loads scoped to the
    empty shape ``("", "")`` — it parses without error but matches no real outcome,
    a visible signal to re-scope it to the shape it actually exercises.
    """
    p = Path(path)
    if not p.exists():
        return {}
    raw = json.loads(p.read_text(encoding="utf-8")) or {}
    out: dict[str, list[dict[str, str]]] = {}
    for op, variants in raw.items():
        norm: list[dict[str, str]] = []
        for v in variants or []:
            if isinstance(v, str):
                norm.append({"label": v, "description": "", "status": "", "branch": ""})
            elif isinstance(v, dict) and v.get("label"):
                norm.append(
                    {
                        "label": str(v["label"]),
                        "description": str(v.get("description", "")),
                        "status": str(v.get("status", "")),
                        "branch": str(v.get("branch", "")),
                    }
                )
        if norm:
            out[op] = norm
    return out


def variants_by_shape(
    variants: dict[str, list[dict[str, str]]], op: str
) -> dict[tuple[str, str], list[str]]:
    """The declared variant labels for an operation, grouped by the ``(status,
    branch)`` shape each one produces (order-stable within a shape). A shape absent
    from the result declares no variants — its single implicit path is the shape
    itself."""
    out: dict[tuple[str, str], list[str]] = {}
    for v in variants.get(op) or []:
        key = (str(v.get("status", "")), str(v.get("branch", "")))
        labels = out.setdefault(key, [])
        if v["label"] not in labels:
            labels.append(v["label"])
    return out


def variant_labels_for_outcome(
    by_shape: dict[tuple[str, str], list[str]], outcome: int | bool, branch: str
) -> set[str]:
    """The declared variant labels for whichever documented shape an exercised
    ``(outcome, branch)`` resolves to (empty when that shape declares none). Used
    by the harness to police calls to a variant-bearing shape (see
    :meth:`~parity_test.harness.Harness._require_variant`)."""
    labels: set[str] = set()
    for (status, decl_branch), labs in by_shape.items():
        if _shape_matches(outcome, branch, status, decl_branch):
            labels.update(labs)
    return labels


def roll_up(cells: list[CoverageCell]) -> dict[str, dict[str, Any]]:
    """Union cells by ``operationId`` → bindings, outcomes, distinct
    discriminants, distinct variants, distinct ``(discriminant, variant)`` combos,
    distinct ``(outcome, branch)`` response shapes, ``(outcome, branch, variant)``
    shape×variant combos, the full ``(discriminant, outcome, branch, variant)``
    identity (for the drill-down pivot), and total call count."""
    agg: dict[str, dict[str, Any]] = {}
    for cell in cells:
        entry = agg.setdefault(
            cell.operation_id,
            {
                "bindings": set(),
                "outcomes": set(),
                "discriminants": set(),
                "variants": set(),
                "combos": set(),
                "shapes": set(),
                "shape_variants": set(),
                "full": set(),
                "cells": 0,
            },
        )
        entry["bindings"].add(cell.binding)
        entry["outcomes"].add(cell.outcome)
        entry["discriminants"].add(cell.discriminant)
        entry["variants"].add(cell.variant)
        entry["combos"].add((cell.discriminant, cell.variant))
        entry["shapes"].add((cell.outcome, cell.branch))
        entry["shape_variants"].add((cell.outcome, cell.branch, cell.variant))
        entry["full"].add((cell.discriminant, cell.outcome, cell.branch, cell.variant))
        entry["cells"] += 1
    return agg


def _outcome_matches(outcome: int | bool, status: str) -> bool:
    """Does an exercised ``outcome`` satisfy a documented status code? A REST
    ``int`` matches exactly; an MCP ``bool`` ``isError`` has no numeric status, so
    it matches any documented status on its side of the 400 boundary."""
    if isinstance(outcome, bool):
        try:
            code = int(status)
        except (TypeError, ValueError):
            return False
        return (code >= 400) == outcome
    return str(outcome) == str(status)


def _shape_matches(outcome: int | bool, branch: str, status: str, decl_branch: str) -> bool:
    """Does an exercised ``(outcome, branch)`` cover a documented
    ``(status, branch)`` shape? The branch key must match exactly and the outcome
    must satisfy the status (:func:`_outcome_matches`)."""
    return str(branch) == str(decl_branch) and _outcome_matches(outcome, status)


def _normalize_shapes(declared: Any) -> list[tuple[str, str]]:
    """Normalize a per-op declared-shapes value (list of ``(status, branch)``
    pairs, tuples or lists) into de-duplicated ``(str, str)`` pairs, order-stable."""
    out: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for pair in declared or []:
        try:
            status, branch = pair
        except (TypeError, ValueError):
            continue
        key = (str(status), str(branch))
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _expected_combos(dims: dict[str, list[Any]]) -> set[tuple[tuple[str, Any], ...]]:
    """The full set of discriminant combinations an operation *should* exercise,
    from the app's ``expand_tokens`` (spec §6.6). No tokens ⇒ one degenerate
    combo (the operation itself); otherwise the Cartesian product of the token
    value-sets. Each combo is a sorted ``((dim, value), ...)`` tuple, matching
    the shape of :attr:`CoverageCell.discriminant`."""
    if not dims:
        return {()}
    keys = sorted(dims)
    out = {
        tuple(sorted(zip(keys, values, strict=True)))
        for values in itertools.product(*(dims[k] for k in keys))
    }
    return out or {()}


def coverage_report(
    root: str | Path,
    all_operation_ids: Any = (),
    variants: dict[str, list[dict[str, str]]] | None = None,
    shapes: dict[str, Any] | None = None,
    *,
    discriminants: dict[str, dict[str, list[Any]]] | None = None,
    exclusions: dict[str, list[dict[str, Any]]] | None = None,
    binding: Binding | None = None,
) -> dict[str, Any]:
    """Roll up every committed manifest under ``root`` and, given the full set of
    documented ``operationId``s, name the covered and uncovered surface.

    Pure and offline: reads only committed manifests + the passed operation set
    (and optional variants + shapes maps), so ParityStudio (or a CLI) can render it
    without running the suite.

    ``shapes`` maps ``operationId`` → the documented ``(status, branch)`` response
    shapes for that operation (``OperationSpec.response_shapes()``, auto-derived
    from the OAS — see :func:`~parity_test.oas.extract_response_shapes`). It drives
    two further levels, both of which an operation with no documented responses
    simply sits out (``0/0``) rather than inflating:

    - **outcome** coverage: the documented status codes vs those exercised. A REST
      status matches exactly; an MCP ``isError`` matches any documented status on
      its side of the 400 boundary.
    - **response-shape** coverage: the documented ``(status, branch)`` shapes vs
      those exercised — each authored ``oneOf`` branch (Zoho's error ``code``) is
      its own coverable shape. ``endpoints[*].shape_grid`` carries the per-endpoint
      shape×variant grid the HTML report expands (shapes as rows, declared variants
      as columns — variants nest *below* the shape).

    Beyond operation-granular coverage this reports these nested matrices:

    - **discriminant** coverage (spec §6.6): every operation is at least one
      discriminant; a discriminated operation (one the app's ``expand_tokens`` hook
      parameterizes, e.g. ``getRecords`` over ``{module}``) contributes the product
      of its axis value-sets. ``covered_discriminants`` counts the expected combos
      actually exercised (never exceeds ``total_discriminants``). This layer is
      variant-agnostic — a discriminant is "covered" if *any* variant of it ran.
      A committed ``discriminants`` overlay (:func:`load_discriminants`) is the
      authoritative axis source when present: it realizes the *whole* denominator
      up front — including negative values no fan-out token produces (``NotAModule``)
      — so the matrix is complete at 0% before any test is written, and
      ``expand_tokens`` is only the fallback. A combo exercised but not declared is
      surfaced per-endpoint as ``undeclared_discriminants`` (a soft notice, never
      counted).
    - **variant-combination** coverage: the full test matrix, the Cartesian product
      of each operation's discriminant combos and its declared behavioural variants
      (:func:`load_variants`; an operation with no declared variants has one
      implicit variant, so this collapses to the discriminant matrix). This is the
      real determiner of how many test cases are needed when an operation has more
      than one code path behind the same shape.

    An ``exclusions`` overlay (:func:`load_exclusions`) carves the **impossible**
    cells out of the realized matrix: each rule names an unreachable
    ``(discriminant, status, branch, variant)`` intersection, which then drops from
    ``total_variant_combos`` and is marked ``excluded`` in the per-endpoint pivot
    (the HTML report greys it **N/A** rather than a red gap) — but never from the
    per-axis discriminant / outcome / shape counts, since validity is a property of
    the *intersection*, not the axis. A cell both excluded *and* exercised is a
    contradiction, surfaced per-endpoint as ``excluded_conflicts``.
    """
    from .hooks import Hooks

    variants = variants or {}
    shapes = shapes or {}
    declared_discriminants = discriminants or {}
    declared_exclusions = exclusions or {}
    manifests = scan_manifests(root)
    # Binding partition (spec: two-index parity). When a ``binding`` is named the
    # roll-up sees only that binding's cells — so the REST and MCP report tabs each
    # get their own numerator/denominator (a cell's binding never leaks across the
    # boundary). Tests with no cell on that side drop out of this partition's test
    # count. ``None`` (the default) keeps the historical union, so a REST-only suite
    # renders exactly as before.
    if binding is not None:
        manifests = {
            slug: kept
            for slug, cells in manifests.items()
            if (kept := [c for c in cells if c.binding == binding])
        }
    all_cells = [cell for cells in manifests.values() for cell in cells]
    agg = roll_up(all_cells)

    # Per-operation observed cells WITH their originating test slug (the snapshot
    # directory name), so the drill-down pivot can attribute each covered leaf to
    # the tests that exercised it. ``roll_up`` deliberately drops provenance, so
    # this keeps the (slug, discriminant, outcome, branch, variant) tuples raw.
    cells_by_op: dict[str, list[tuple[str, tuple[tuple[str, Any], ...], int | bool, str, str]]] = {}
    for slug, cells in manifests.items():
        for c in cells:
            cells_by_op.setdefault(c.operation_id, []).append(
                (slug, c.discriminant, c.outcome, c.branch, c.variant)
            )
    all_ids = set(all_operation_ids)
    covered = sorted(agg)
    uncovered = sorted(all_ids - set(covered))

    expand = Hooks.expand_tokens
    per_op_disc: dict[str, tuple[int, int]] = {}
    per_op_combo: dict[str, tuple[int, int]] = {}
    per_op_outcome: dict[str, tuple[int, int]] = {}
    per_op_shape: dict[str, tuple[int, int]] = {}
    declared_variant_count: dict[str, int] = {}
    shape_grids: dict[str, dict[str, Any]] = {}
    pivots: dict[str, dict[str, Any]] = {}
    total_discriminants = 0
    covered_discriminants = 0
    total_variant_combos = 0
    covered_variant_combos = 0
    total_outcomes = 0
    covered_outcomes = 0
    total_response_shapes = 0
    covered_response_shapes = 0
    excluded_variant_combos = 0
    undeclared_disc: dict[str, list[list[list[Any]]]] = {}
    excluded_cells: dict[str, int] = {}
    excluded_conflicts: dict[str, list[dict[str, Any]]] = {}
    for op in all_ids:
        op_exclusions = declared_exclusions.get(op, [])
        # The discriminant axes for this op. A committed ``discriminants.json`` entry
        # (spec §6.6) is authoritative — it realizes the *whole* denominator up front,
        # including negative values no fan-out token produces — so the matrix is
        # complete at 0% before any test runs. Absent one, fall back to the app's
        # ``expand_tokens`` (the convenience source), then to no axes at all.
        declared_dims = declared_discriminants.get(op)
        dims = (
            declared_dims if declared_dims is not None else ((expand(op) if expand else {}) or {})
        )
        expected_disc = _expected_combos(dims)
        keys = set(dims)
        recorded_disc = agg.get(op, {}).get("discriminants", set())
        # Project each exercised cell's discriminant onto the declared axes, then
        # count how many *expected* combos were hit (unexpected values — a value not
        # in the declared set — are ignored, keeping numerator ≤ total).
        projected_disc = {
            tuple(sorted((k, v) for k, v in disc if k in keys)) for disc in recorded_disc
        }
        disc_hit = len(projected_disc & expected_disc)
        per_op_disc[op] = (disc_hit, len(expected_disc))
        total_discriminants += len(expected_disc)
        covered_discriminants += disc_hit
        # Soft signal: a projected combo that ran but was never declared. Surfaced
        # (not counted) so an author sees they exercised a value outside the declared
        # axis set — a hint the overlay is missing a value, or the test a typo.
        stray = sorted(projected_disc - expected_disc, key=lambda d: [(k, str(v)) for k, v in d])
        if stray:
            undeclared_disc[op] = [[list(pair) for pair in disc] for disc in stray]

        # Variant axis (per-shape): a variant is a code path to ONE shape, so the
        # required surface is, for each shape that declares variants, its
        # discriminant combos × those variants. Shapes without declared variants
        # sit out entirely (accounted at the shape/discriminant levels), so an op
        # with no variants contributes 0 — the level stays about declared
        # code-path coverage and never inflates with implicit ""s.
        by_shape = variants_by_shape(variants, op)
        declared_variant_count[op] = sum(len(labs) for labs in by_shape.values())
        observed_full = agg.get(op, {}).get("full", set())
        all_variant_triples = {
            (d, status, branch, lab)
            for (status, branch), labs in by_shape.items()
            for d in expected_disc
            for lab in labs
        }
        # Carve the impossible variant combos out of the denominator: an excluded
        # intersection can never be exercised, so it must not read as an uncovered
        # gap (validity is a property of the intersection, not the variant axis).
        excluded_variant_triples = {
            t for t in all_variant_triples if _is_excluded(op_exclusions, t[0], t[1], t[2], t[3])
        }
        expected_variant_triples = all_variant_triples - excluded_variant_triples
        excluded_variant_combos += len(excluded_variant_triples)
        covered_variant_triples: set[tuple[Any, str, str, str]] = set()
        for disc_full, o, b, v in observed_full:
            dproj = tuple(sorted((k, val) for k, val in disc_full if k in keys))
            for (status, branch), labs in by_shape.items():
                if v in labs and _shape_matches(o, b, status, branch):
                    covered_variant_triples.add((dproj, status, branch, v))
        combo_hit = len(covered_variant_triples & expected_variant_triples)
        per_op_combo[op] = (combo_hit, len(expected_variant_triples))
        total_variant_combos += len(expected_variant_triples)
        covered_variant_combos += combo_hit

        # Response-shape & outcome matrices (spec §4). Both are driven by the
        # documented shapes (from the enriched catalogue, auto-derived — see
        # ``extract_response_shapes``). An operation with no documented responses
        # doesn't participate in either level (0/0): the shape layer can't name a
        # gap it doesn't know about, so it degrades to silence rather than noise.
        declared_shapes = _normalize_shapes(shapes.get(op))
        observed_shapes = agg.get(op, {}).get("shapes", set())
        observed_shape_variants = agg.get(op, {}).get("shape_variants", set())

        declared_statuses = sorted({status for status, _ in declared_shapes})
        covered_statuses = {
            status
            for status in declared_statuses
            if any(_outcome_matches(o, status) for (o, _b) in observed_shapes)
        }
        per_op_outcome[op] = (len(covered_statuses), len(declared_statuses))
        total_outcomes += len(declared_statuses)
        covered_outcomes += len(covered_statuses)

        covered_shapes = {
            (status, branch)
            for (status, branch) in declared_shapes
            if any(_shape_matches(o, b, status, branch) for (o, b) in observed_shapes)
        }
        per_op_shape[op] = (len(covered_shapes), len(declared_shapes))
        total_response_shapes += len(declared_shapes)
        covered_response_shapes += len(covered_shapes)

        # Contradiction guard: a cell declared impossible that a test nonetheless
        # exercised. It never affects the counts (it's already carved out of the
        # denominator) — but a snapshot for an "impossible" cell means either the
        # exclusion or the test is wrong, so surface it loudly per-endpoint.
        conflicts: set[tuple[Any, str, str, str]] = set()
        for disc_full, o, b, v in observed_full:
            dproj = tuple(sorted((k, val) for k, val in disc_full if k in keys))
            for status, branch in declared_shapes:
                if _shape_matches(o, b, status, branch) and _is_excluded(
                    op_exclusions, dproj, status, branch, v
                ):
                    conflicts.add((dproj, status, branch, v))
        if conflicts:
            excluded_conflicts[op] = [
                {
                    "discriminant": [list(pair) for pair in disc],
                    "status": status,
                    "branch": branch,
                    "variant": variant,
                }
                for disc, status, branch, variant in sorted(
                    conflicts, key=lambda c: ([(k, str(v)) for k, v in c[0]], c[1], c[2], c[3])
                )
            ]

        # Per-endpoint shape grid for the HTML report: one row per documented
        # shape, carrying that shape's *own* declared variants (a variant belongs
        # to one shape). A shape with none has an empty variant list and is
        # covered/uncovered as a whole.
        grid_rows: list[dict[str, Any]] = []
        for status, branch in declared_shapes:
            labs = by_shape.get((status, branch), [])
            grid_rows.append(
                {
                    "status": status,
                    "branch": branch,
                    "covered": (status, branch) in covered_shapes,
                    "variants": [
                        {
                            "label": lab,
                            "covered": any(
                                v == lab and _shape_matches(o, b, status, branch)
                                for (o, b, v) in observed_shape_variants
                            ),
                        }
                        for lab in labs
                    ],
                }
            )
        shape_grids[op] = {"rows": grid_rows}

        # Drill-down pivot (Hybrid): discriminant combos are the rows; the columns
        # are the documented shapes, each expanded into its own variant leaves
        # (status → branch → variant). A shape with no declared variants is a
        # single leaf (the shape itself, label ""). Exploratory only — it never
        # feeds the per-axis totals above. Rows are every *expected* discriminant
        # combo unioned with any actually-exercised one, so nothing that ran is
        # hidden and every planned combo is visible.
        # Each leaf coordinate → the set of test slugs that exercised it (a leaf is
        # covered iff at least one test reaches it, so this both marks coverage and
        # names the contributing tests the HTML report tooltips / links to).
        pivot_tests: dict[tuple[Any, str, str, str], set[str]] = {}
        for slug, disc_full, o, b, v in cells_by_op.get(op, []):
            dproj = tuple(sorted((k, val) for k, val in disc_full if k in keys))
            for status, branch in declared_shapes:
                if _shape_matches(o, b, status, branch):
                    # shape-level leaf, plus the variant leaf when one was declared
                    pivot_tests.setdefault((dproj, status, branch, ""), set()).add(slug)
                    if v:
                        pivot_tests.setdefault((dproj, status, branch, v), set()).add(slug)
        disc_rows = sorted(
            expected_disc | projected_disc, key=lambda d: [(k, str(val)) for k, val in d]
        )
        op_excluded_cells = 0
        pivot_rows: list[dict[str, Any]] = []
        for disc in disc_rows:
            shape_rows: list[dict[str, Any]] = []
            for status, branch in declared_shapes:
                labs = by_shape.get((status, branch), [])
                leaf_labels = labs or [""]  # a variant-less shape is one "" leaf
                coords = [(disc, status, branch, lab) for lab in leaf_labels]
                leaves = []
                for coord in coords:
                    is_excl = _is_excluded(op_exclusions, disc, status, branch, coord[3])
                    op_excluded_cells += is_excl
                    leaves.append(
                        {
                            "label": coord[3],
                            # An excluded cell is neither covered nor a gap — it's N/A.
                            "covered": (not is_excl) and coord in pivot_tests,
                            "excluded": is_excl,
                            "tests": sorted(pivot_tests.get(coord, ())),
                        }
                    )
                shape_rows.append(
                    {
                        "status": status,
                        "branch": branch,
                        "covered": any(leaf["covered"] for leaf in leaves),
                        "variants": leaves,
                    }
                )
            pivot_rows.append({"discriminant": [list(pair) for pair in disc], "shapes": shape_rows})
        pivots[op] = {"discriminant_keys": sorted(keys), "rows": pivot_rows}
        if op_excluded_cells:
            excluded_cells[op] = op_excluded_cells

    endpoints = [
        {
            "operation_id": op,
            "covered": op in agg,
            "bindings": sorted(agg.get(op, {}).get("bindings", set())),
            "outcomes": sorted(agg.get(op, {}).get("outcomes", set()), key=str),
            "cells": agg.get(op, {}).get("cells", 0),
            "covered_discriminants": per_op_disc.get(op, (0, 0))[0],
            "total_discriminants": per_op_disc.get(op, (0, 0))[1],
            "covered_variant_combos": per_op_combo.get(op, (0, 0))[0],
            "total_variant_combos": per_op_combo.get(op, (0, 0))[1],
            "covered_outcomes": per_op_outcome.get(op, (0, 0))[0],
            "total_outcomes": per_op_outcome.get(op, (0, 0))[1],
            "covered_shapes": per_op_shape.get(op, (0, 0))[0],
            "total_shapes": per_op_shape.get(op, (0, 0))[1],
            "shape_grid": shape_grids.get(op, {}).get("rows", []),
            "pivot": pivots.get(op, {"discriminant_keys": [], "rows": []}),
            "undeclared_discriminants": undeclared_disc.get(op, []),
            "excluded_cells": excluded_cells.get(op, 0),
            "excluded_conflicts": excluded_conflicts.get(op, []),
        }
        for op in sorted(all_ids)
    ]

    return {
        "tests": len(manifests),
        "cells": len(all_cells),
        "total_operations": len(all_ids),
        "covered_count": len(covered),
        "uncovered_count": len(uncovered),
        "total_discriminants": total_discriminants,
        "covered_discriminants": covered_discriminants,
        "uncovered_discriminants": total_discriminants - covered_discriminants,
        "total_variant_combos": total_variant_combos,
        "covered_variant_combos": covered_variant_combos,
        "uncovered_variant_combos": total_variant_combos - covered_variant_combos,
        "excluded_variant_combos": excluded_variant_combos,
        "total_outcomes": total_outcomes,
        "covered_outcomes": covered_outcomes,
        "uncovered_outcomes": total_outcomes - covered_outcomes,
        "total_response_shapes": total_response_shapes,
        "covered_response_shapes": covered_response_shapes,
        "uncovered_response_shapes": total_response_shapes - covered_response_shapes,
        "covered": covered,
        "uncovered": uncovered,
        "endpoints": endpoints,
        "by_operation": {
            op: {
                "bindings": sorted(v["bindings"]),
                "outcomes": sorted(v["outcomes"], key=str),
                "discriminants": len(v["discriminants"]),
                "covered_discriminants": per_op_disc.get(op, (0, 0))[0],
                "total_discriminants": per_op_disc.get(op, (0, 0))[1],
                "variants": declared_variant_count.get(op, 0),
                "covered_variant_combos": per_op_combo.get(op, (0, 0))[0],
                "total_variant_combos": per_op_combo.get(op, (0, 0))[1],
                "cells": v["cells"],
            }
            for op, v in sorted(agg.items())
        },
    }

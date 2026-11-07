"""Self-contained HTML report (spec §5 companion).

The suite writes a machine-readable ``coverage_report.json`` at session end
(:func:`parity_test.coverage.coverage_report`). This module renders the same
data — plus the per-test cell sequences and the captured request/response
snapshots — as a single, dependency-free ``report.html`` a human can open
directly. It is the replacement for the old browsable UI: everything lives in
one file (inline CSS/JS, no CDN), so it can be committed as an artifact, emailed,
or served from a static path.

Like :func:`~parity_test.coverage.coverage_report`, :func:`render_html_report` is
**pure and offline**: it reads only the committed manifests, snapshots and the
passed operation/variant sets under a snapshots ``root``, so ParityStudio or a
CLI can render it without running the suite. It never touches a clone or
reference.

The rendered document has four layers, coarse → fine:

1. **Summary** — operations / outcomes / response-shapes / discriminants /
   variant-combinations covered vs the documented surface (the OAS-derived levels
   appear only when the catalogue documents them), plus test and cell counts.
2. **Endpoint coverage** — one expandable row per documented operation: bindings
   exercised, observed outcomes, per-level coverage bars and a covered/uncovered
   badge; expanding reveals the shape×variant grid (documented response shapes as
   rows, declared variants as columns, each cell covered/uncovered). A covered cell
   names the tests that reached it (tooltip) and clicks through to open + scroll to
   them in the per-test detail below.
3. **Per-test detail** — each committed test's ordered coverage-cell sequence and
   every captured snapshot, request and response shown as pretty JSON.
4. **Appendix** — the raw ``coverage_report`` object, for exact numbers.

All dynamic content is HTML-escaped; big integers survive because the bodies are
re-serialized with :mod:`json` (no float coercion — spec §7).
"""

from __future__ import annotations

import html
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .coverage import MANIFEST_NAME, CoverageCell, coverage_report
from .snapshot import Snapshot, load_snapshot

# --- small formatting helpers ------------------------------------------------


def _esc(value: Any) -> str:
    """HTML-escape any value (quotes included, so it is safe in attributes)."""
    return html.escape(str(value), quote=True)


def _pretty(obj: Any) -> str:
    """Deterministic, escaped pretty-JSON for a ``<pre>`` block. ``json.dumps``
    keeps ints as ints, so 19-digit ids round-trip losslessly (spec §7)."""
    try:
        text = json.dumps(obj, indent=2, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        text = repr(obj)
    return _esc(text)


def _disc_pairs(pairs: Any) -> str:
    """Render a discriminant (an iterable of ``(key, value)``) as ``k=v, k=v``."""
    items = list(pairs.items()) if isinstance(pairs, dict) else list(pairs)
    return ", ".join(f"{_esc(k)}={_esc(v)}" for k, v in items) or "—"


def _conflict_label(conflict: dict[str, Any]) -> str:
    """Render an excluded-but-exercised cell (``{discriminant, status, branch,
    variant}``) as ``disc → status/branch#variant`` for the contradiction notice."""
    disc = _disc_pairs(conflict.get("discriminant") or [])
    shape = _esc(conflict.get("status", ""))
    if conflict.get("branch"):
        shape += f"/{_esc(conflict['branch'])}"
    if conflict.get("variant"):
        shape += f"#{_esc(conflict['variant'])}"
    return f"{disc} → {shape}"


def _cov_class(covered: int, total: int) -> str:
    if total <= 0 or covered >= total:
        return "cov-full"
    if covered <= 0:
        return "cov-none"
    return "cov-partial"


def _bar(covered: int, total: int) -> str:
    """A labelled coverage bar, coloured by fullness."""
    pct = 100 if total <= 0 else round(100 * covered / total)
    cls = _cov_class(covered, total)
    return (
        f'<div class="bar" title="{covered}/{total}">'
        f'<div class="bar-fill {cls}" style="width:{pct}%"></div>'
        f'<span class="bar-label">{covered}/{total}</span></div>'
    )


def _outcome_label(outcome: Any) -> str:
    """A REST status is an int; an MCP outcome is a bool ``isError``."""
    if isinstance(outcome, bool):
        return "isError" if outcome else "ok"
    return str(outcome)


def _snapshot_status(snap: Snapshot) -> str:
    if snap.status is not None:
        return str(snap.status)
    return "error" if snap.is_error else "ok"


# --- per-test view assembled from disk ---------------------------------------


@dataclass
class _TestView:
    slug: str
    test_id: str
    cells: list[CoverageCell]
    snapshots: list[Snapshot]


def _load_test_views(root: Path) -> list[_TestView]:
    """Collect every committed test directory under ``root``: its coverage-cell
    sequence (from ``coverage.json``) and its captured snapshots (every other
    ``*.json``). Directories with neither are skipped.

    Snapshots are co-located beside their test files under a ``<stem>_snapshots/``
    folder (see :func:`~parity_test.snapshot.snapshot_reltail`), so we gather every
    per-test leaf under a ``*_snapshots/`` directory (at any depth) — the suffix gate
    keeps a broad common-ancestor ``root`` from sweeping in unrelated ``*.json``.
    The view's ``slug`` is the flat ``_slug(test_id)`` — the DOM id / correlation key
    shared with the coverage roll-up — independent of how deep the directory sits."""
    from .snapshot import _slug

    if not root.exists():
        return []
    views: list[_TestView] = []
    leaf_dirs = sorted({f.parent for f in root.glob("**/*_snapshots/*/*.json")})
    for d in leaf_dirs:
        fallback_id = d.relative_to(root).as_posix()
        test_id = fallback_id
        cells: list[CoverageCell] = []
        manifest = d / MANIFEST_NAME
        if manifest.exists():
            try:
                data = json.loads(manifest.read_text(encoding="utf-8"))
                test_id = data.get("test_id", fallback_id)
                cells = [CoverageCell.from_dict(c) for c in data.get("cells", [])]
            except (ValueError, OSError):
                pass
        snapshots: list[Snapshot] = []
        for f in sorted(d.glob("*.json")):
            if f.name == MANIFEST_NAME:
                continue
            try:
                snapshots.append(load_snapshot(f))
            except (ValueError, OSError):
                continue
        if cells or snapshots:
            views.append(_TestView(_slug(test_id), test_id, cells, snapshots))
    return views


# --- section renderers -------------------------------------------------------


def _render_summary(report: dict[str, Any]) -> str:
    # Coarse → fine, with the OAS-derived levels (outcomes, response shapes) shown
    # only when the catalogue documents them (else they'd read as a misleading
    # 0/0 = "complete"). Variants sit *below* shapes in the hierarchy.
    cards = [
        (
            "Operations",
            report["covered_count"],
            report["total_operations"],
            f"{report['uncovered_count']} uncovered",
        )
    ]
    if report.get("total_outcomes", 0):
        cards.append(
            (
                "Outcomes",
                report["covered_outcomes"],
                report["total_outcomes"],
                f"{report['uncovered_outcomes']} uncovered",
            )
        )
    if report.get("total_response_shapes", 0):
        cards.append(
            (
                "Response shapes",
                report["covered_response_shapes"],
                report["total_response_shapes"],
                f"{report['uncovered_response_shapes']} uncovered",
            )
        )
    cards.append(
        (
            "Discriminants",
            report["covered_discriminants"],
            report["total_discriminants"],
            f"{report['uncovered_discriminants']} uncovered",
        )
    )
    # Variants sit out entirely when nothing declares them (0/0 would read as a
    # misleading "complete") — only show the card once some shape has variants.
    if report.get("total_variant_combos", 0):
        cards.append(
            (
                "Variant combinations",
                report["covered_variant_combos"],
                report["total_variant_combos"],
                f"{report['uncovered_variant_combos']} uncovered",
            )
        )
    card_html = []
    for label, covered, total, sub in cards:
        card_html.append(
            f'<div class="card">'
            f'<div class="card-label">{_esc(label)}</div>'
            f'<div class="card-value">{covered}<span class="card-total">/{total}</span></div>'
            f"{_bar(covered, total)}"
            f'<div class="card-sub">{_esc(sub)}</div>'
            f"</div>"
        )
    # Bare counts (no ratio) for tests & cells.
    for label, value in (("Tests", report["tests"]), ("Coverage cells", report["cells"])):
        card_html.append(
            f'<div class="card"><div class="card-label">{_esc(label)}</div>'
            f'<div class="card-value">{value}</div></div>'
        )
    return f'<section class="cards">{"".join(card_html)}</section>'


def _grid_cell(
    covered: bool,
    extra: str = "",
    tests: list[str] | None = None,
    slug_to_id: dict[str, str] | None = None,
) -> str:
    """One pivot cell. A covered cell carries the tests that reached it: its tooltip
    lists their ids and it becomes a ``cell-link`` whose ``data-tests`` (the test
    *slugs*) the script uses to open and scroll to those tests in the list below."""
    tests = tests or []
    slug_to_id = slug_to_id or {}
    cls = "cell cell-yes" if covered else "cell cell-no"
    if extra:
        cls = f"{cls} {extra}"
    glyph = "✓" if covered else "✗"
    if tests:
        ids = [slug_to_id.get(slug, slug) for slug in tests]
        n = len(ids)
        tip = f"{n} test{'' if n == 1 else 's'} cover this cell — click to open:\n" + "\n".join(ids)
        return (
            f'<td class="{cls} cell-link" title="{_esc(tip)}" '
            f'data-tests="{_esc(" ".join(tests))}">{glyph}</td>'
        )
    title = "covered" if covered else "not covered"
    return f'<td class="{cls}" title="{title}">{glyph}</td>'


def _na_cell(extra: str = "") -> str:
    """A cell declared impossible by the exclusions overlay: greyed **N/A**, so it
    reads as "not applicable" rather than an uncovered (red ✗) gap."""
    cls = "cell cell-na"
    if extra:
        cls = f"{cls} {extra}"
    return f'<td class="{cls}" title="excluded — impossible intersection (N/A)">N/A</td>'


def _status_tone(status: str) -> str:
    """Map a status to an HTTP-class tone so a status header and its branch
    sub-headers share one colour — making it obvious which branches belong to
    which status (2xx green, 3xx blue, 4xx amber, 5xx red, else neutral). MCP's
    ``ok``/``error`` string outcomes are folded onto the success/error tones."""
    s = status.strip().lower()
    if s[:1].isdigit():
        return f"tone-{s[0]}xx"
    if s in ("ok", "success"):
        return "tone-2xx"
    if "error" in s:
        return "tone-4xx"
    return "tone-other"


def _leaf_columns(row: dict[str, Any]) -> list[tuple[str, str, str]]:
    """Flatten a pivot row's shapes into ordered leaf columns ``(status, branch,
    variant_label)``. A shape that declares variants contributes one leaf per label;
    a shape with none contributes a single unlabeled (``""``) default leaf. Every
    discriminant row shares this column structure, so it is taken from one row."""
    cols: list[tuple[str, str, str]] = []
    for sh in row.get("shapes") or []:
        status, branch = str(sh["status"]), str(sh["branch"])
        leaves = sh.get("variants") or [{"label": ""}]
        for leaf in leaves:
            cols.append((status, branch, leaf["label"]))
    return cols


def _leaf_coverage(row: dict[str, Any]) -> dict[tuple[str, str, str], bool]:
    """Map each ``(status, branch, variant_label)`` leaf of a pivot row to whether
    it is covered."""
    out: dict[tuple[str, str, str], bool] = {}
    for sh in row.get("shapes") or []:
        status, branch = str(sh["status"]), str(sh["branch"])
        leaves = sh.get("variants") or [{"label": "", "covered": sh.get("covered", False)}]
        for leaf in leaves:
            out[(status, branch, leaf["label"])] = bool(leaf["covered"])
    return out


def _leaf_tests(row: dict[str, Any]) -> dict[tuple[str, str, str], list[str]]:
    """Map each ``(status, branch, variant_label)`` leaf of a pivot row to the test
    *slugs* that exercised it (:func:`~parity_test.coverage.coverage_report` carries
    them per leaf). Empty for an uncovered leaf."""
    out: dict[tuple[str, str, str], list[str]] = {}
    for sh in row.get("shapes") or []:
        status, branch = str(sh["status"]), str(sh["branch"])
        for leaf in sh.get("variants") or [{"label": ""}]:
            out[(status, branch, leaf["label"])] = list(leaf.get("tests") or [])
    return out


def _leaf_excluded(row: dict[str, Any]) -> dict[tuple[str, str, str], bool]:
    """Map each ``(status, branch, variant_label)`` leaf of a pivot row to whether it
    is declared **impossible** (:func:`~parity_test.coverage.load_exclusions`) — such
    a cell renders N/A, dropping out of both the covered and the total counts."""
    out: dict[tuple[str, str, str], bool] = {}
    for sh in row.get("shapes") or []:
        status, branch = str(sh["status"]), str(sh["branch"])
        for leaf in sh.get("variants") or [{"label": ""}]:
            out[(status, branch, leaf["label"])] = bool(leaf.get("excluded", False))
    return out


def _grid_totals(ep: dict[str, Any]) -> tuple[int, int]:
    """``(covered, total)`` over the endpoint's *whole* pivot grid — every
    discriminant row × every leaf column (shapes exploded into branch×variant).
    This is the ``5/30`` that summarizes an endpoint's full state in one fraction:
    5 discriminants × 6 shapes/branches/variants = 30 cells, 5 filled. Returns
    ``(0, 0)`` when the endpoint documents no response shapes (empty grid)."""
    pivot = ep.get("pivot") or {}
    rows = pivot.get("rows") or []
    first = next((r for r in rows if r.get("shapes")), None)
    if first is None:
        return (0, 0)
    leaf_cols = _leaf_columns(first)
    covered = total = 0
    for row in rows:
        cov = _leaf_coverage(row)
        exc = _leaf_excluded(row)
        for col in leaf_cols:
            if exc.get(col, False):
                continue  # an impossible cell is N/A — out of both numerator & total
            total += 1
            covered += 1 if cov.get(col, False) else 0
    return (covered, total)


def _render_pivot(ep: dict[str, Any], slug_to_id: dict[str, str] | None = None) -> str:
    """The per-endpoint drill-down pivot: **discriminant combos (modules) are the
    rows; the documented response shapes, exploded into their behavioural variants,
    are the columns** — a three-level header ``status → branch → variant``. A shape
    with no declared variants is a single unlabeled ("default") leaf column; a
    single-schema status collapses its branch level to ``—``. Each cell is a green ✓
    (covered) or red ✗ (not covered).

    Header rows that carry no information are dropped — the branch row only appears
    when some status has ``oneOf`` branches, the variant row only when some shape
    declares variants — so **each surviving header row is self-labelled in the stub
    column** (``Status`` / ``Branch`` / ``Variant``). That label is what lets a reader
    tell *which* level is absent: an endpoint whose errors are a generic ``$ref``
    (no OAS discrimination) shows ``Status`` then ``Variant`` with no ``Branch`` row,
    and the missing label makes that legible rather than looking like a broken grid.

    Empty ⇒ the catalogue documents no response shapes for this operation
    (pre-enrichment / a Pydantic-model app)."""
    pivot = ep.get("pivot") or {}
    rows = pivot.get("rows") or []
    first = next((r for r in rows if r.get("shapes")), None)
    if first is None:
        return (
            '<p class="muted">No documented response shapes for this endpoint '
            "(enrich the catalogue to populate this grid).</p>"
        )
    keys = pivot.get("discriminant_keys") or []
    multi_disc = bool(keys) or len(rows) > 1

    leaf_cols = _leaf_columns(first)
    has_branches = any(branch for _s, branch, _l in leaf_cols)
    has_variants = any(label for _s, _b, label in leaf_cols)

    # ``grp-start`` marks the first column of each new status group (after the
    # first), carrying a divider down every header row and body cell so the
    # branch/variant→status grouping reads at a glance.
    col_group_start: list[bool] = []
    prev_status: str | None = None
    for status, _b, _l in leaf_cols:
        col_group_start.append(prev_status is not None and status != prev_status)
        prev_status = status

    # Level 1 — status: one header spanning its leaves, tinted by HTTP class.
    status_ths: list[str] = []
    i, n = 0, len(leaf_cols)
    while i < n:
        status = leaf_cols[i][0]
        j = i
        while j < n and leaf_cols[j][0] == status:
            j += 1
        div = " grp-start" if i > 0 else ""
        status_ths.append(
            f'<th colspan="{j - i}" class="axis {_status_tone(status)}{div}">{_esc(status)}</th>'
        )
        i = j

    # Level 2 — branch: one header per ``(status, branch)`` run (only when some
    # status is a ``oneOf``); a single-schema status shows ``—``.
    branch_ths: list[str] = []
    if has_branches:
        i = 0
        while i < n:
            status, branch, _l = leaf_cols[i]
            j = i
            while j < n and leaf_cols[j][0] == status and leaf_cols[j][1] == branch:
                j += 1
            div = " grp-start" if col_group_start[i] else ""
            label = _esc(branch) if branch else "—"
            branch_ths.append(
                f'<th colspan="{j - i}" class="branch-col {_status_tone(status)}{div}">{label}</th>'
            )
            i = j

    # Level 3 — variant: one header per leaf (only when some shape declares
    # variants); a shape's implicit sole path is labelled ``default``.
    variant_ths: list[str] = []
    if has_variants:
        for idx, (status, _b, label) in enumerate(leaf_cols):
            div = " grp-start" if col_group_start[idx] else ""
            text = _esc(label) if label else '<span class="vdefault">default</span>'
            variant_ths.append(f'<th class="vcol {_status_tone(status)}{div}">{text}</th>')

    # The stub (leading) column names each surviving level so a dropped row is
    # identifiable. Present whenever there's *any* level to label or more than one
    # discriminant row; a lone single-shape row stays compact with no stub.
    show_lead = multi_disc or has_branches or has_variants
    axis_names = (
        ["Status"] + (["Branch"] if has_branches else []) + (["Variant"] if has_variants else [])
    )
    level_ths = (
        [status_ths]
        + ([branch_ths] if has_branches else [])
        + ([variant_ths] if has_variants else [])
    )
    header_rows: list[str] = []
    for name, ths in zip(axis_names, level_ths):
        lead_th = f'<th class="axis-label">{name}</th>' if show_lead else ""
        header_rows.append(f"<tr>{lead_th}{''.join(ths)}</tr>")
    thead = "".join(header_rows)

    body: list[str] = []
    for row in rows:
        cov = _leaf_coverage(row)
        exc = _leaf_excluded(row)
        leaf_tests = _leaf_tests(row)
        cells = [
            (
                _na_cell("grp-start" if col_group_start[idx] else "")
                if exc.get(col, False)
                else _grid_cell(
                    cov.get(col, False),
                    "grp-start" if col_group_start[idx] else "",
                    leaf_tests.get(col),
                    slug_to_id,
                )
            )
            for idx, col in enumerate(leaf_cols)
        ]
        disc = row.get("discriminant") or []
        # _disc_pairs already HTML-escapes each key/value (and "(all)" is a plain
        # safe literal) — do not _esc again, or "&"/"<" render double-escaped.
        disc_label = _disc_pairs(disc) if disc else "(all)"
        lead_td = f'<td class="disc">{disc_label}</td>' if show_lead else ""
        body.append(f"<tr>{lead_td}{''.join(cells)}</tr>")
    return (
        '<div class="grid-scroll"><table class="grid"><thead>'
        f"{thead}</thead><tbody>{''.join(body)}</tbody></table></div>"
    )


def _render_endpoint(
    ep: dict[str, Any], tests: list[str], slug_to_id: dict[str, str] | None = None
) -> str:
    """One expandable endpoint: a summary line (badge, bindings, observed outcomes,
    per-level bars, exercising-test count) that opens to the shape×variant grid."""
    op = ep["operation_id"]
    if ep["covered"]:
        badge = '<span class="badge badge-ok">covered</span>'
    else:
        badge = '<span class="badge badge-none">uncovered</span>'
    bindings = ", ".join(ep["bindings"]) or "—"
    outcomes = ", ".join(_outcome_label(o) for o in ep["outcomes"]) or "—"

    def _mini(name: str, covered: int, total: int) -> str:
        return f'<span class="mini">{name} {_bar(covered, total)}</span>'

    bars: list[str] = []
    # Whole-grid fraction first: the single ``5/30`` that states this endpoint's
    # full state (all discriminant rows × all shape/branch/variant leaves) before
    # the per-level breakdown. Sits out only when the grid is empty (no shapes).
    grid_covered, grid_total = _grid_totals(ep)
    if grid_total:
        bars.append(_mini("cells", grid_covered, grid_total))
    if ep["total_shapes"]:
        bars.append(_mini("shapes", ep["covered_shapes"], ep["total_shapes"]))
    bars.append(_mini("disc", ep["covered_discriminants"], ep["total_discriminants"]))
    # The variant bar only when this endpoint declares behavioural variants (else
    # 0/0 reads as a misleading "complete").
    if ep["total_variant_combos"]:
        bars.append(_mini("var", ep["covered_variant_combos"], ep["total_variant_combos"]))
    title = _esc("\n".join(tests))
    tests_html = f'<span class="tests" title="{title}">{len(tests)} tests</span>'
    needle = " ".join([op, *tests]).lower()
    # Soft notice: a discriminant value exercised but never declared in the
    # discriminants overlay (a hint the overlay is missing a value, or a test typo).
    # It never affects the counts — just flags the stray combo for the author.
    stray = ep.get("undeclared_discriminants") or []
    undeclared = ""
    if stray:
        combos = "; ".join(_disc_pairs(disc) for disc in stray)
        undeclared = (
            f'<p class="undeclared" title="{_esc(combos)}">⚠ exercised '
            f"{len(stray)} undeclared discriminant{'' if len(stray) == 1 else 's'}: "
            f"{_esc(combos)}</p>"
        )
    # Loud notice: a cell declared impossible by the exclusions overlay that a test
    # nonetheless exercised — a contradiction (the exclusion or the test is wrong).
    # It never affects the counts (the cell is already carved out); it's a red flag.
    conflicts = ep.get("excluded_conflicts") or []
    conflict = ""
    if conflicts:
        cells = "; ".join(_conflict_label(c) for c in conflicts)
        conflict = (
            f'<p class="conflict" title="{_esc(cells)}">⛔ exercised '
            f"{len(conflicts)} cell{'' if len(conflicts) == 1 else 's'} declared "
            f"impossible by exclusions.json: {_esc(cells)}</p>"
        )
    return (
        f'<details class="op-row" data-filter="{_esc(needle)}"><summary>'
        f'<span class="op">{_esc(op)}</span>{badge}'
        f'<span class="meta">{_esc(bindings)} · {_esc(outcomes)}</span>'
        f'<span class="bars">{"".join(bars)}</span>{tests_html}'
        "</summary>"
        f'<div class="op-body">{undeclared}{conflict}{_render_pivot(ep, slug_to_id)}</div>'
        "</details>"
    )


def _render_untested(eps: list[dict[str, Any]]) -> str:
    """The untested endpoints, de-emphasized in a collapsed table. Each row shows
    how many coverable cells the endpoint carries at each level (documented
    response shapes, discriminant combos, variant combos) so the size of the
    remaining surface is visible without stealing focus from what's covered.
    Ordered largest-surface-first so the biggest gaps sort to the top."""
    ordered = sorted(
        eps,
        key=lambda e: (
            -e["total_shapes"],
            -e["total_variant_combos"],
            -e["total_discriminants"],
            e["operation_id"],
        ),
    )
    rows: list[str] = []
    tot_shapes = tot_disc = tot_var = 0
    for ep in ordered:
        s, d, v = ep["total_shapes"], ep["total_discriminants"], ep["total_variant_combos"]
        tot_shapes += s
        tot_disc += d
        tot_var += v
        rows.append(
            f'<tr data-filter="{_esc(ep["operation_id"].lower())}">'
            f'<td class="op">{_esc(ep["operation_id"])}</td>'
            f'<td class="num">{s or "—"}</td>'
            f'<td class="num">{d}</td>'
            f'<td class="num">{v}</td>'
            "</tr>"
        )
    footer = (
        "<tfoot><tr><td>Total coverable cells</td>"
        f'<td class="num">{tot_shapes}</td><td class="num">{tot_disc}</td>'
        f'<td class="num">{tot_var}</td></tr></tfoot>'
    )
    table = (
        '<table class="untested"><thead><tr><th>Endpoint</th>'
        "<th>Response shapes</th><th>Discriminants</th><th>Variant combos</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody>{footer}</table>"
    )
    return (
        '<details class="untested-group"><summary>'
        f'<span class="grp">Untested endpoints <span class="meta">({len(eps)})</span></span>'
        '<span class="meta"> — no tests yet; counts are the coverable cells still to '
        "reach at each level</span></summary>"
        f"{table}</details>"
    )


def _render_operation_matrix(
    report: dict[str, Any],
    tests_by_op: dict[str, list[str]],
    slug_to_id: dict[str, str] | None = None,
) -> str:
    covered, total = report["covered_count"], report["total_operations"]
    remaining = report["uncovered_count"]
    caption = (
        f'<p class="caption"><strong>{covered}</strong> of <strong>{total}</strong> '
        f"endpoints covered · <strong>{remaining}</strong> remaining</p>"
    )
    endpoints = report.get("endpoints") or []
    if not endpoints:
        empty = '<p class="empty">No operations.</p>'
        return f"<section><h2>Endpoint coverage</h2>{caption}{empty}</section>"
    tested = [ep for ep in endpoints if ep["covered"]]
    untested = [ep for ep in endpoints if not ep["covered"]]
    parts = [caption]
    # Focus: the endpoints that actually have coverage.
    if tested:
        parts.append(
            f'<div class="grp">Tested endpoints <span class="meta">({len(tested)})</span></div>'
        )
        parts.append(
            "".join(
                _render_endpoint(ep, tests_by_op.get(ep["operation_id"], []), slug_to_id)
                for ep in tested
            )
        )
    else:
        parts.append('<p class="muted">No endpoints have tests yet.</p>')
    # De-emphasized: the untested remainder, with its coverable-cell counts.
    if untested:
        parts.append(_render_untested(untested))
    return f"<section><h2>Endpoint coverage</h2>{''.join(parts)}</section>"


def _render_binding_tabs(
    rest_report: dict[str, Any],
    mcp_report: dict[str, Any],
    rest_panel: str,
    mcp_panel: str,
) -> str:
    """Wrap the two per-binding summary+matrix panels in a REST | MCP tab switcher.

    The tab buttons carry each binding's operation fraction so the split coverage is
    legible before a click; the MCP panel starts hidden. The switching is a few
    lines of vanilla JS (:data:`_SCRIPT`); with JS off both panels simply stack, so
    the report stays readable."""

    def _btn(tab: str, label: str, report: dict[str, Any], active: bool) -> str:
        cls = "tab-btn active" if active else "tab-btn"
        count = (
            f'<span class="tab-count">{report["covered_count"]}/{report["total_operations"]}</span>'
        )
        return f'<button type="button" class="{cls}" data-tab="{tab}">{label} {count}</button>'

    tabs = (
        '<div class="tabs" role="tablist">'
        f"{_btn('rest', 'REST', rest_report, True)}"
        f"{_btn('mcp', 'MCP', mcp_report, False)}"
        "</div>"
    )
    panels = (
        '<div class="tab-panel" data-panel="rest">'
        f"{rest_panel}</div>"
        '<div class="tab-panel hidden" data-panel="mcp">'
        f"{mcp_panel}</div>"
    )
    return f'<div class="binding-tabs">{tabs}{panels}</div>'


def _render_cells_table(cells: list[CoverageCell]) -> str:
    if not cells:
        return '<p class="muted">No coverage cells recorded for this test.</p>'
    rows = []
    for i, c in enumerate(cells, 1):
        rows.append(
            "<tr>"
            f'<td class="num">{i}</td>'
            f"<td>{_esc(c.binding)}</td>"
            f'<td class="op">{_esc(c.operation_id)}</td>'
            f"<td>{_disc_pairs(c.discriminant)}</td>"
            f"<td>{_esc(_outcome_label(c.outcome))}</td>"
            f"<td>{_esc(c.branch or '—')}</td>"
            f"<td>{_esc(c.variant or '—')}</td>"
            f"<td>{_esc(c.label or '—')}</td>"
            "</tr>"
        )
    return (
        '<table class="cells"><thead><tr>'
        "<th>#</th><th>Binding</th><th>Operation</th><th>Discriminant</th>"
        "<th>Outcome</th><th>Branch</th><th>Variant</th><th>Label</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table>"
    )


def _render_snapshot(snap: Snapshot) -> str:
    name = snap.label or (f"ord-{snap.ordinal}" if snap.ordinal is not None else "ord-0")
    op = snap.operation_id or "—"
    disc = _disc_pairs(snap.discriminant)
    disc_html = f' · <span class="muted">{disc}</span>' if snap.discriminant else ""
    return (
        '<details class="snap"><summary>'
        f'<span class="tag tag-{_esc(snap.binding)}">{_esc(snap.binding)}</span> '
        f'<span class="snap-name">{_esc(name)}</span> · '
        f'<span class="op">{_esc(op)}</span> · '
        f'<span class="status">{_esc(_snapshot_status(snap))}</span>{disc_html}'
        "</summary>"
        '<div class="snap-cols">'
        f'<div class="snap-col"><h5>request</h5><pre>{_pretty(snap.request)}</pre></div>'
        f'<div class="snap-col"><h5>response</h5><pre>{_pretty(snap.response)}</pre></div>'
        "</div></details>"
    )


# Live per-test outcomes from *this session* (spec §5). Coverage is still derived
# from committed snapshots (written only on a real pass), but the report also
# reflects what the current run did — including xfailed tests that have no
# committed snapshot yet ("not ready"). Ordered worst→best for the summary chips.
_OUTCOME_BADGES: dict[str, tuple[str, str]] = {
    "failed": ("badge-fail", "failed"),
    "xpassed": ("badge-xpass", "xpassed"),
    "xfailed": ("badge-xfail", "xfailed"),
    "skipped": ("badge-skip", "skipped"),
    "passed": ("badge-ok", "passed"),
}


def _outcome_badge(outcome: str) -> str:
    cls, label = _OUTCOME_BADGES.get(outcome, ("badge-skip", outcome or "—"))
    return f'<span class="badge {cls}">{_esc(label)}</span>'


def _render_session_summary(outcomes: dict[str, str]) -> str:
    """A one-line chip row of this session's per-test outcomes. Empty string when
    no session outcomes were supplied (offline render), keeping the document
    deterministic for external consumers."""
    if not outcomes:
        return ""
    counts: dict[str, int] = {}
    for outcome in outcomes.values():
        counts[outcome] = counts.get(outcome, 0) + 1
    chips = []
    for key in _OUTCOME_BADGES:  # worst→best, only those present
        if counts.get(key):
            cls, label = _OUTCOME_BADGES[key]
            chips.append(f'<span class="chip {cls}">{counts[key]} {_esc(label)}</span>')
    return (
        f'<section><h2>Session results <span class="meta">({len(outcomes)} tests ran)</span></h2>'
        f'<div class="chips">{"".join(chips)}</div></section>'
    )


def _render_tests(views: list[_TestView], outcomes: dict[str, str]) -> str:
    if not views:
        return '<section><h2>Tests</h2><p class="muted">No committed tests found.</p></section>'
    ran_session = bool(outcomes)
    blocks = []
    for view in views:
        snaps = "".join(_render_snapshot(s) for s in view.snapshots) or (
            '<p class="muted">No snapshots captured.</p>'
        )
        ops = {c.operation_id for c in view.cells} | {
            s.operation_id for s in view.snapshots if s.operation_id
        }
        # sorted(): ``ops`` is a set, and its unsorted iteration order varies
        # across processes (PYTHONHASHSEED) — sort so the data-filter needle (and
        # thus report.html bytes) is stable run-to-run for committing to git.
        needle = " ".join([view.test_id, *(o for o in sorted(ops) if o)]).lower()
        if view.test_id in outcomes:
            status = _outcome_badge(outcomes[view.test_id])
        elif ran_session:
            status = '<span class="badge badge-muted">not run this session</span>'
        else:
            status = ""
        blocks.append(
            f'<details class="test" id="test-{_esc(view.slug)}" data-filter="{_esc(needle)}">'
            f'<summary><span class="test-id">{_esc(view.test_id)}</span>{status}'
            f'<span class="meta">{len(view.cells)} cells · {len(view.snapshots)} snapshots</span>'
            "</summary>"
            '<div class="test-body">'
            f"<h4>Coverage cells</h4>{_render_cells_table(view.cells)}"
            f"<h4>Snapshots</h4>{snaps}"
            "</div></details>"
        )
    heading = f'<h2>Tests <span class="meta">({len(views)})</span></h2>'
    return f"<section>{heading}{''.join(blocks)}</section>"


_STYLE = """
:root {
  --bg: #f6f7f9; --fg: #1c2230; --muted: #6b7280; --line: #e2e5ea;
  --card: #ffffff; --accent: #2563eb;
  --full: #16a34a; --partial: #d97706; --none: #dc2626;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg);
  font: 14px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica,
    Arial, sans-serif; }
header { position: sticky; top: 0; z-index: 5; background: var(--card);
  border-bottom: 1px solid var(--line); padding: 14px 24px;
  display: flex; align-items: baseline; gap: 16px; flex-wrap: wrap; }
header h1 { font-size: 18px; margin: 0; }
header .gen { color: var(--muted); font-size: 12px; }
.banner { background: #fef3c7; color: #92400e; border: 1px solid #fde68a;
  padding: 8px 24px; font-weight: 600; }
main { max-width: 1200px; margin: 0 auto; padding: 24px; }
section { margin-bottom: 32px; }
h2 { font-size: 16px; border-bottom: 2px solid var(--line); padding-bottom: 6px; }
h4 { margin: 16px 0 6px; font-size: 13px; text-transform: uppercase;
  letter-spacing: .04em; color: var(--muted); }
h5 { margin: 0 0 4px; font-size: 12px; color: var(--muted); }
.meta, .muted { color: var(--muted); font-weight: 400; font-size: 12px; }
.caption { margin: 6px 0 12px; font-size: 14px; }
.caption strong { font-variant-numeric: tabular-nums; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 14px; margin-bottom: 28px; }
.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 14px 16px; }
.card-label { font-size: 12px; color: var(--muted); text-transform: uppercase;
  letter-spacing: .04em; }
.card-value { font-size: 28px; font-weight: 700; margin: 4px 0; }
.card-total { font-size: 16px; color: var(--muted); font-weight: 500; }
.card-sub { font-size: 12px; color: var(--muted); }
.bar { position: relative; height: 16px; background: #eef0f3; border-radius: 8px;
  overflow: hidden; min-width: 90px; }
.bar-fill { position: absolute; inset: 0 auto 0 0; height: 100%; border-radius: 8px; }
.bar-fill.cov-full { background: var(--full); }
.bar-fill.cov-partial { background: var(--partial); }
.bar-fill.cov-none { background: var(--none); }
.bar-label { position: relative; display: block; text-align: center; font-size: 11px;
  line-height: 16px; color: #14203a; mix-blend-mode: luminosity; font-weight: 600; }
table { width: 100%; border-collapse: collapse; background: var(--card);
  border: 1px solid var(--line); border-radius: 8px; overflow: hidden; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid var(--line);
  vertical-align: top; }
th { background: #f0f2f5; font-size: 12px; text-transform: uppercase; letter-spacing: .03em; }
tr:last-child td { border-bottom: none; }
td.num, td.op { font-variant-numeric: tabular-nums; }
td.num { text-align: right; }
.op { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }
.badge { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 999px; }
.badge-ok { background: #dcfce7; color: #166534; }
.badge-none, .badge-fail { background: #fee2e2; color: #991b1b; }
.badge-xfail { background: #fef3c7; color: #92400e; }
.badge-xpass { background: #ede9fe; color: #5b21b6; }
.badge-skip { background: #e5e7eb; color: #374151; }
.badge-muted { background: #eef0f3; color: #6b7280; font-weight: 500; }
.chips { display: flex; gap: 8px; flex-wrap: wrap; }
.chip { font-size: 13px; font-weight: 600; padding: 4px 12px; border-radius: 999px; }
details.test > summary { justify-content: flex-start; }
details.test > summary .meta { margin-left: auto; }
.empty { text-align: center; color: var(--muted); }
details.test, details.snap { background: var(--card); border: 1px solid var(--line);
  border-radius: 8px; margin: 8px 0; }
details.test > summary, details.snap > summary { cursor: pointer; padding: 10px 12px;
  display: flex; gap: 12px; align-items: baseline; flex-wrap: wrap; user-select: none; }
details.test > summary { font-weight: 600; }
details[open] > summary { border-bottom: 1px solid var(--line); }
.test-id { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12.5px; }
.test-body { padding: 4px 14px 14px; }
.tag { font-size: 11px; font-weight: 700; padding: 1px 7px; border-radius: 4px; }
.tag-rest { background: #dbeafe; color: #1e40af; }
.tag-mcp { background: #ede9fe; color: #5b21b6; }
.snap-name { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px; }
.snap-cols { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; padding: 10px 12px; }
@media (max-width: 800px) { .snap-cols { grid-template-columns: 1fr; } }
pre { margin: 0; background: #0f172a; color: #e2e8f0; padding: 10px 12px; border-radius: 6px;
  overflow: auto; max-height: 420px; font-size: 12px;
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.filter { margin: 0 0 14px; }
.filter input { width: 100%; max-width: 420px; padding: 8px 12px; font-size: 14px;
  border: 1px solid var(--line); border-radius: 8px; }
.hidden { display: none !important; }
details.op-row { background: var(--card); border: 1px solid var(--line);
  border-radius: 8px; margin: 6px 0; }
details.op-row > summary { cursor: pointer; padding: 9px 12px; display: flex;
  gap: 10px; align-items: center; flex-wrap: wrap; user-select: none; }
details.op-row > summary::-webkit-details-marker { display: none; }
details.op-row .meta { margin-right: auto; }
details.op-row .bars { display: flex; gap: 10px; flex-wrap: wrap; }
.mini { display: inline-flex; align-items: center; gap: 5px; font-size: 11px;
  color: var(--muted); }
.mini .bar { min-width: 64px; height: 12px; }
.mini .bar-label { line-height: 12px; font-size: 10px; }
.tests { font-size: 11px; color: var(--muted); white-space: nowrap; }
.op-body { padding: 4px 12px 12px; }
.grid-scroll { overflow-x: auto; margin-top: 4px; }
table.grid { margin-top: 0; min-width: max-content; }
table.grid th, table.grid td { text-align: center; }
table.grid th:first-child, table.grid td:first-child { text-align: left; }
table.grid th.axis { font-variant-numeric: tabular-nums; font-weight: 700; font-size: 12px;
  border-bottom: 1px solid var(--line); }
table.grid th.branch-col { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 11px; font-weight: 600; white-space: nowrap; }
table.grid th.vcol { font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 10.5px; font-weight: 600; white-space: nowrap; }
table.grid th.vcol .vdefault { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
  sans-serif; font-style: italic; font-weight: 500; opacity: .7; }
/* Stub-column axis labels (Status / Branch / Variant) — name each surviving
   header level so a dropped level is legible, not a mystery gap. */
table.grid th.axis-label { text-align: right; vertical-align: middle; font-size: 10px;
  font-weight: 700; text-transform: uppercase; letter-spacing: .04em; color: var(--muted);
  background: var(--bg); border-right: 1px solid var(--line); white-space: nowrap;
  padding-right: 8px; }
/* Status-class tints, shared by a status header and its branch sub-headers. */
table.grid th.tone-2xx { background: #d1fae5; color: #065f46; }
table.grid th.tone-3xx { background: #dbeafe; color: #1e3a8a; }
table.grid th.tone-4xx { background: #fef08a; color: #854d0e; }
table.grid th.tone-5xx { background: #fecaca; color: #991b1b; }
table.grid th.tone-1xx, table.grid th.tone-other { background: #e2e8f0; color: #334155; }
/* Divider carried down each status group's first column. */
table.grid th.grp-start, table.grid td.grp-start { border-left: 2px solid #94a3b8; }
td.cell { font-weight: 700; font-size: 14px; }
td.cell-yes { color: var(--full); }
td.cell-no { color: var(--none); }
/* An excluded (impossible) cell: greyed, hatched N/A — neither covered nor a gap. */
td.cell-na { color: #94a3b8; font-size: 10px; font-weight: 600; letter-spacing: .02em;
  background: repeating-linear-gradient(45deg, #f1f5f9, #f1f5f9 4px, #e7ebf0 4px, #e7ebf0 8px); }
/* Covered cells link to the tests that reached them: pointer + hover affordance. */
td.cell-link { cursor: pointer; }
td.cell-link:hover { background: #ecfdf5; outline: 1px solid var(--full); }
/* A clicked-through test is offset below the sticky header and briefly flashed. */
details.test { scroll-margin-top: 72px; }
details.test.flash { outline: 2px solid var(--accent); outline-offset: 2px;
  background: #eff4ff; }
/* Cursor-following hover panel: instant, follows the mouse across the grid, and
   enumerates every test that reached the cell under the pointer. */
.cell-tip { position: fixed; z-index: 50; pointer-events: none; max-width: 460px;
  background: #0f172a; color: #e2e8f0; border-radius: 8px; padding: 8px 11px;
  box-shadow: 0 6px 24px rgba(15, 23, 42, .35); }
.cell-tip h6 { margin: 0 0 5px; font-size: 10.5px; text-transform: uppercase;
  letter-spacing: .04em; color: #94a3b8; font-weight: 700; }
.cell-tip ul { margin: 0; padding: 0 0 0 16px; }
.cell-tip li { font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 12px;
  line-height: 1.55; white-space: nowrap; }
.cell-tip .hint { margin-top: 6px; font-size: 11px; color: #94a3b8; font-style: italic; }
table.grid td.disc { text-align: left; vertical-align: middle; white-space: nowrap;
  font-weight: 600; border-right: 1px solid var(--line); background: #fafbfc; }
.grp { font-size: 12px; font-weight: 700; text-transform: uppercase; letter-spacing: .04em;
  color: var(--muted); margin: 18px 0 8px; }
details.untested-group { margin-top: 14px; }
details.untested-group > summary { cursor: pointer; user-select: none; list-style: none;
  display: flex; align-items: baseline; gap: 8px; flex-wrap: wrap; }
details.untested-group > summary::-webkit-details-marker { display: none; }
details.untested-group > summary .grp { margin: 0; }
details.untested-group[open] > summary { margin-bottom: 8px; }
table.untested th:not(:first-child), table.untested td.num { text-align: right;
  font-variant-numeric: tabular-nums; }
table.untested tfoot td { font-weight: 700; border-top: 2px solid var(--line); }
/* REST | MCP binding tabs. The active button is underlined in the accent; each
   button carries its binding's covered/total fraction. With JS off both panels
   stack (the .hidden default is toggled by script), so nothing is lost. */
.tabs { display: flex; gap: 4px; border-bottom: 2px solid var(--line); margin-bottom: 18px; }
.tab-btn { appearance: none; background: none; border: none; cursor: pointer;
  padding: 8px 16px; font: inherit; font-weight: 600; color: var(--muted);
  border-bottom: 2px solid transparent; margin-bottom: -2px; }
.tab-btn:hover { color: var(--fg); }
.tab-btn.active { color: var(--accent); border-bottom-color: var(--accent); }
.tab-count { font-variant-numeric: tabular-nums; font-weight: 500; font-size: 12px;
  color: var(--muted); }
.tab-btn.active .tab-count { color: var(--accent); }
/* Soft "undeclared discriminant" notice on an endpoint row — amber, informational
   (it never changes the counts), sat above the pivot grid. */
p.undeclared { margin: 2px 0 8px; font-size: 12px; color: #92400e;
  background: #fef3c7; border: 1px solid #fde68a; border-radius: 6px;
  padding: 5px 9px; }
/* Loud "contradiction" notice — a test exercised a cell the exclusions overlay
   declared impossible. Red, because one of the two must be wrong. */
p.conflict { margin: 2px 0 8px; font-size: 12px; color: #991b1b;
  background: #fee2e2; border: 1px solid #fecaca; border-radius: 6px;
  padding: 5px 9px; }
"""

_SCRIPT = """
(function () {
  // REST | MCP tab switcher: clicking a tab button shows its panel and hides the
  // other. Only present when the report was rendered with an MCP index.
  document.querySelectorAll('.tab-btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      var tab = btn.getAttribute('data-tab');
      document.querySelectorAll('.tab-btn').forEach(function (b) {
        b.classList.toggle('active', b === btn);
      });
      document.querySelectorAll('.tab-panel').forEach(function (p) {
        p.classList.toggle('hidden', p.getAttribute('data-panel') !== tab);
      });
    });
  });

  var box = document.getElementById('filter');
  if (box) {
    box.addEventListener('input', function () {
      var q = box.value.trim().toLowerCase();
      document.querySelectorAll('[data-filter]').forEach(function (el) {
        var hit = !q || el.getAttribute('data-filter').indexOf(q) !== -1;
        el.classList.toggle('hidden', !hit);
      });
    });
  }

  // slug -> readable test id, embedded as JSON so the hover panel can name tests.
  var names = {};
  var nEl = document.getElementById('test-names');
  if (nEl) { try { names = JSON.parse(nEl.textContent || '{}'); } catch (e) {} }

  // A single cursor-following panel: as the mouse scans across covered cells it
  // updates instantly (no native-title delay) and lists EVERY test that reached
  // the cell under the pointer. Rebuilt with textContent, so ids are injection-safe.
  var tip = document.getElementById('cell-tip');
  function slugsOf(td) {
    return (td.getAttribute('data-tests') || '').split(' ').filter(Boolean);
  }
  function fill(td) {
    var slugs = slugsOf(td);
    if (!slugs.length || !tip) return false;
    tip.textContent = '';
    var h = document.createElement('h6');
    h.textContent = slugs.length + ' test' + (slugs.length === 1 ? '' : 's') + ' cover this cell';
    tip.appendChild(h);
    var ul = document.createElement('ul');
    slugs.forEach(function (slug) {
      var li = document.createElement('li');
      li.textContent = names[slug] || slug;
      ul.appendChild(li);
    });
    tip.appendChild(ul);
    var hint = document.createElement('div');
    hint.className = 'hint';
    hint.textContent = 'click to open below';
    tip.appendChild(hint);
    return true;
  }
  function place(e) {
    if (!tip) return;
    var pad = 14, r = tip.getBoundingClientRect();
    var x = e.clientX + pad, y = e.clientY + pad;
    if (x + r.width > window.innerWidth) x = e.clientX - r.width - pad;
    if (y + r.height > window.innerHeight) y = e.clientY - r.height - pad;
    tip.style.left = Math.max(4, x) + 'px';
    tip.style.top = Math.max(4, y) + 'px';
  }
  document.querySelectorAll('td.cell-link').forEach(function (td) {
    td.removeAttribute('title');  // JS panel replaces the delayed native tooltip
    td.addEventListener('mouseenter', function (e) {
      if (fill(td)) { tip.classList.remove('hidden'); place(e); }
    });
    td.addEventListener('mousemove', place);
    td.addEventListener('mouseleave', function () { if (tip) tip.classList.add('hidden'); });
    // Click through: open + scroll to the tests that reached this cell.
    td.addEventListener('click', function () {
      var first = null;
      slugsOf(td).forEach(function (slug) {
        var el = document.getElementById('test-' + slug);
        if (!el) return;
        el.classList.remove('hidden');  // unhide if a filter had excluded it
        el.open = true;
        if (!first) first = el;
      });
      if (first) {
        if (tip) tip.classList.add('hidden');
        first.scrollIntoView({ behavior: 'smooth', block: 'start' });
        first.classList.add('flash');
        setTimeout(function () { first.classList.remove('flash'); }, 1600);
      }
    });
  });
})();
"""


def render_html_report(
    root: str | Path,
    all_operation_ids: Any = (),
    variants: dict[str, list[dict[str, str]]] | None = None,
    *,
    shapes: dict[str, Any] | None = None,
    mcp_operation_ids: Any = (),
    mcp_shapes: dict[str, Any] | None = None,
    discriminants: dict[str, dict[str, list[Any]]] | None = None,
    exclusions: dict[str, list[dict[str, Any]]] | None = None,
    title: str = "Parity coverage report",
    generated_at: str | None = None,
    mode: str | None = None,
    spoof: bool = False,
    test_outcomes: dict[str, str] | None = None,
) -> str:
    """Render a single self-contained HTML report for the snapshots under ``root``.

    Coverage is derived purely from the committed artifacts under ``root`` (the
    same inputs as :func:`~parity_test.coverage.coverage_report`). ``shapes`` — the
    documented ``(status, branch)`` response shapes per operation (auto-derived
    from the OAS) — drives the outcome and response-shape summary cards and the
    per-endpoint expandable shape×variant grid; omit it and those levels sit out.
    ``test_outcomes``
    — a ``{nodeid: outcome}`` map from *this session* (``passed`` / ``xfailed`` /
    ``xpassed`` / ``failed`` / ``skipped``) — is layered on top: it drives the
    "Session results" summary and each test's status badge, and surfaces tests that
    ran but committed no snapshot (e.g. an xfailed not-ready test). Omit it (the
    offline/ParityStudio path) for a deterministic, session-independent document;
    ``generated_at`` / ``mode`` / ``spoof`` likewise only affect the header/banner.

    **REST | MCP tabs (two-index parity).** Passing ``mcp_operation_ids`` (the MCP
    index's operation set, with optional ``mcp_shapes``) splits the summary +
    endpoint-coverage sections into two tabs, each rolled up from *only* its
    binding's coverage cells — so REST and MCP never share a numerator/denominator
    (:func:`~parity_test.coverage.coverage_report` filters by ``binding``). The
    Session-results and per-test-detail sections stay shared (a test's detail table
    already tags each cell's binding). Omit ``mcp_operation_ids`` (a REST-only
    suite) and the document renders single-tab exactly as before.

    ``discriminants`` (:func:`~parity_test.coverage.load_discriminants`) pre-declares
    each operation's axis value-sets so the discriminant matrix is realized in full
    up front (negative values included); it is passed through to both binding
    partitions and an operation exercised at an *undeclared* value gets a soft note
    on its endpoint row. ``exclusions``
    (:func:`~parity_test.coverage.load_exclusions`) carves the impossible cells out
    of that realized matrix: the pivot greys them **N/A** and they drop from the
    grid/variant denominators; a cell both excluded and exercised gets a loud
    contradiction note.
    """
    root = Path(root)
    mcp_active = bool(mcp_operation_ids)
    report = coverage_report(
        root,
        all_operation_ids,
        variants,
        shapes,
        discriminants=discriminants,
        exclusions=exclusions,
        binding="rest" if mcp_active else None,
    )
    mcp_report = (
        coverage_report(
            root,
            mcp_operation_ids,
            variants,
            mcp_shapes,
            discriminants=discriminants,
            exclusions=exclusions,
            binding="mcp",
        )
        if mcp_active
        else None
    )
    views = _load_test_views(root)
    outcomes = dict(test_outcomes or {})
    # Surface tests that ran this session but left no committed snapshot (e.g. an
    # xfailed not-ready test) as empty views, so their live status still shows.
    known = {v.test_id for v in views}
    for test_id in outcomes:
        if test_id not in known:
            views.append(_TestView(slug=test_id, test_id=test_id, cells=[], snapshots=[]))
    views.sort(key=lambda v: v.test_id)

    gen = f'<span class="gen">generated {_esc(generated_at)}</span>' if generated_at else ""
    mode_bits = []
    if mode:
        mode_bits.append(f"mode: {_esc(mode)}")
    if mode_bits:
        gen = f'<span class="gen">{" · ".join(mode_bits)}{" · " if gen else ""}</span>{gen}'
    banner = (
        '<div class="banner">SPOOF run — the clone was not exercised and parity was '
        "not asserted; only test-body assertions ran against the committed snapshot. "
        "Do not gate merges on this report.</div>"
        if spoof
        else ""
    )

    def _tests_by_op(binding: str | None) -> dict[str, list[str]]:
        """Which tests exercised each operation — optionally scoped to one binding,
        so a tab's endpoint rows link only the tests that hit that binding."""
        out: dict[str, list[str]] = {}
        for view in views:
            ops = {
                c.operation_id
                for c in view.cells
                if c.operation_id and (binding is None or c.binding == binding)
            }
            for op in ops:
                out.setdefault(op, []).append(view.test_id)
        return out

    tests_by_op = _tests_by_op("rest" if mcp_active else None)

    # slug (snapshot dir name, what the pivot leaves carry) → readable test id, so a
    # cell's hover panel can name its tests and a click can open the matching
    # ``#test-<slug>``. Embedded as JSON (sorted → deterministic) for the client to
    # resolve slugs → names; ``</`` is neutralized so a hostile id can't close the
    # script early. One cell can list many tests — the panel enumerates them all.
    slug_to_id = {v.slug: v.test_id for v in views}
    names_json = json.dumps(slug_to_id, ensure_ascii=False, sort_keys=True).replace("</", "<\\/")

    session = _render_session_summary(outcomes)
    tests = _render_tests(views, outcomes)
    filter_box = (
        '<div class="filter"><input id="filter" type="search" '
        'placeholder="Filter operations &amp; tests…" autocomplete="off"></div>'
    )

    if mcp_active and mcp_report is not None:
        # Two-index parity: REST | MCP tabs, each with its own summary + endpoint
        # matrix rolled up from only that binding's cells. The MCP tab's endpoint
        # rows link the tests that hit the MCP binding.
        mcp_tests_by_op = _tests_by_op("mcp")
        rest_panel = _render_summary(report) + _render_operation_matrix(
            report, tests_by_op, slug_to_id
        )
        mcp_panel = _render_summary(mcp_report) + _render_operation_matrix(
            mcp_report, mcp_tests_by_op, slug_to_id
        )
        coverage_body = _render_binding_tabs(report, mcp_report, rest_panel, mcp_panel)
        appendix = (
            '<section><details><summary><h2 style="display:inline">Raw coverage_report.json</h2>'
            f"</summary><h3>REST</h3><pre>{_pretty(report)}</pre>"
            f"<h3>MCP</h3><pre>{_pretty(mcp_report)}</pre></details></section>"
        )
    else:
        coverage_body = _render_summary(report) + _render_operation_matrix(
            report, tests_by_op, slug_to_id
        )
        appendix = (
            '<section><details><summary><h2 style="display:inline">Raw coverage_report.json</h2>'
            f"</summary><pre>{_pretty(report)}</pre></details></section>"
        )

    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{_esc(title)}</title><style>{_STYLE}</style></head><body>"
        f"<header><h1>{_esc(title)}</h1>{gen}</header>{banner}"
        f"<main>{filter_box}{session}{coverage_body}{tests}{appendix}</main>"
        '<div id="cell-tip" class="cell-tip hidden"></div>'
        f'<script id="test-names" type="application/json">{names_json}</script>'
        f"<script>{_SCRIPT}</script></body></html>"
    )

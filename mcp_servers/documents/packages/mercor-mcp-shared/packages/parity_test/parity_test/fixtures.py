"""Pytest integration — the app-neutral fixtures & markers (spec §1/§8).

This is the *rightful home* for the harness fixtures. The per-app ``conftest.py``
activates them with a single import and supplies only the app-specific
ingredients::

    # conftest.py
    from parity_test.fixtures import (  # noqa: F401 — re-exported as pytest plugin
        api,
        pytest_configure,
        pytest_generate_tests,
        pytest_runtest_makereport,  # REQUIRED: records per-test pass/fail so
        pytest_report_header,       #   coverage is captured/verified (without it
        pytest_sessionfinish,       #   finalize_coverage always no-ops), prints
    )                               #   the spoof banner, and emits the roll-up.

Import ALL of these names — pytest only invokes hooks that are present in the
conftest namespace. Dropping ``pytest_runtest_makereport`` in particular leaves
every test's ``_parity_passed`` unset, so ``finalize_coverage`` silently skips
and no coverage manifest is ever written or drift-checked.

    # ...then define the app-specific fixtures the library ones depend on:
    #   clone_client, oas_index, reference_ref
    # ...and any app seed fixtures, composed on the harness primitives
    #   (api.prep / api.bind_seeded_id / api.on_cleanup).

The library fixtures below are 100% generic — they build a
:class:`~parity_test.harness.Harness` from its ingredients, apply the test's
seed markers, yield, and finalize. Nothing here is app-specific; the app seam is
the four named fixtures the app provides plus the :class:`~parity_test.hooks.Hooks`
registry (populated by ``load_extensions()``).

Required app-provided fixtures (the contract):

- ``clone_client`` (session): the SUT client (the app's full ASGI stack over a
  fresh DB). How it is built is app-specific.
- ``oas_index`` (session): an :class:`~parity_test.oas.OasIndex` over the app's
  OpenAPI specs.
- ``reference_ref`` (session): the live reference client in **capture** mode, or
  ``None`` in replay (never touched offline).

Snapshots are **co-located** beside each test file (``<stem>_snapshots/<test>/``),
so the suite no longer supplies a snapshots-root fixture. The aggregate roll-up
artifacts and cross-suite overlays live in ``ParityConfig.output_dir``
(``$PARITY_OUTPUT_DIR``; default ``parity/`` under the pytest rootdir).

Exposed fixtures:

- ``api`` (function): a mode-aware harness (replay/capture from
  :class:`~parity_test.config.ParityConfig`), with the test's ``@populate_seed`` /
  ``@seed_from_snapshot`` givens applied first, then ``finalize()`` on teardown.

Get-or-create ``seed`` fixtures are **app-owned** (see the app's ``conftest.py`` /
extension): the library provides the mechanism on the harness — ``api.prep``,
``api.bind_seeded_id`` and ``api.on_cleanup`` — not a seed vocabulary.

Markers (registered in :func:`pytest_configure`):

- ``populate_seed`` / ``seed_from_snapshot`` — declarative seeding (spec §8).
- ``modules`` — discriminated fan-out; :func:`pytest_generate_tests` expands it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

try:  # pytest is a dev/test dependency of the clone, not of the library import
    import pytest
except Exception:  # pragma: no cover - import-time guard for non-test contexts
    pytest = None  # type: ignore


def _cov_shapes_for(index: Any) -> dict[str, list[list[str]]]:
    """The documented ``(status, branch)`` response shapes per operation for an
    index (REST ``oas_index`` or ``mcp_index``), auto-derived from the spec. Drives
    the outcome / response-shape coverage levels and the shape×variant grid. A
    fake/partial index (or an op whose spec documents no responses) simply
    contributes no shapes, so coverage degrades to the op/discriminant/variant
    levels rather than erroring."""
    shapes: dict[str, list[list[str]]] = {}
    for op_id in index.operation_ids():
        try:
            pairs = index.resolve_operation(op_id).response_shapes()
        except Exception:  # noqa: BLE001 - a fake/partial index just contributes no shapes
            continue
        if pairs:
            shapes[op_id] = [list(pair) for pair in pairs]
    return shapes


def _classify_outcome(report: Any) -> str:
    """Map a pytest call/setup report to a report-friendly outcome label. ``xfail``
    is distinguished from a plain skip via pytest's ``wasxfail`` attribute (set on
    an expected-failure report regardless of pass/fail)."""
    if hasattr(report, "wasxfail"):
        return "xpassed" if report.passed else "xfailed"
    if report.passed:
        return "passed"
    if report.failed:
        return "failed"
    return "skipped"


def _parity_output_dir(config: Any, cfg: Any) -> Path:
    """The aggregate/overlay output dir: ``cfg.output_dir`` resolved against the
    pytest rootdir (an absolute value passes through). This is where the roll-up
    artifacts and cross-suite overlays live now that per-test snapshots are
    co-located beside their test files."""
    p = Path(cfg.output_dir)
    return p if p.is_absolute() else Path(config.rootpath) / p


def pytest_configure(config: Any) -> None:
    """Register the library's markers so ``--strict-markers`` stays clean, and
    load the app's ``parity_test_ext`` so ``Hooks`` (populate, default_rules,
    expand_tokens, …) are populated before any test builds a harness. Without
    this, following the documented conftest wiring would leave the hooks unset
    and seeded tests would fail or run with empty rules."""
    if pytest is None:
        return
    from . import load_extensions

    load_extensions()
    config.addinivalue_line(
        "markers", "populate_seed(filename, content=None): declare a seed fixture (spec §8)"
    )
    config.addinivalue_line(
        "markers",
        "seed_from_snapshot(filename, *, binding, label, ordinal, path, operation, transform): "
        "reconstruct a given from a captured read (spec §8.1)",
    )
    config.addinivalue_line(
        "markers", "modules(*tokens): discriminated fan-out over the expand set (spec §6.6)"
    )


def pytest_report_header(config: Any) -> str | None:
    """Print a loud banner when running in spoof mode so a non-gating TDD run is
    never mistaken for a real parity run (the clone is not exercised and parity is
    not asserted — see :class:`~parity_test.config.ParityConfig`)."""
    if pytest is None:
        return None
    from .config import ParityConfig

    if ParityConfig.resolve().spoof:
        return (
            "parity: SPOOF — clone NOT exercised and parity NOT asserted; only "
            "test-body assertions run against the committed snapshot. Do not gate merges "
            "on this run."
        )
    return None


def pytest_generate_tests(metafunc: Any) -> None:
    """Expand ``@pytest.mark.modules`` into parametrized cases (spec §6.6).

    Tokens are either a module name (``"Leads"``) or a ``(module, expected_status)``
    pair for exclusions (spec §6.6). A test parametrizes on whichever of ``module``
    / ``expected_status`` it actually declares. No marker → no-op, so importing
    this hook into a conftest is always safe.
    """
    if pytest is None:
        return
    marker = metafunc.definition.get_closest_marker("modules")
    if marker is None:
        return
    modules: list[Any] = []
    statuses: list[Any] = []
    for token in marker.args:
        if isinstance(token, (tuple, list)):
            modules.append(token[0])
            statuses.append(token[1] if len(token) > 1 else None)
        else:
            modules.append(token)
            statuses.append(None)
    ids = [str(m) for m in modules]
    if "expected_status" in metafunc.fixturenames:
        pairs = list(zip(modules, statuses, strict=True))
        metafunc.parametrize("module,expected_status", pairs, ids=ids)
    elif "module" in metafunc.fixturenames:
        metafunc.parametrize("module", modules, ids=ids)


if pytest is not None:

    @pytest.fixture
    def api(request, clone_client, oas_index, reference_ref):  # noqa: ANN001
        """The mode-aware harness, with the test's ``@populate_seed`` /
        ``@seed_from_snapshot`` givens applied through the app's populate hook
        first (spec §8). Seeding uses the harness's store/reference so the record
        source is the committed snapshot (offline) or the live reference (capture
        bootstrap)."""
        from .config import ParityConfig
        from .harness import Harness
        from .hooks import Hooks
        from .seeding import apply_seed_markers
        from .snapshot import SnapshotStore

        cfg = ParityConfig.resolve()
        # Snapshots are co-located beside the test file: the store is anchored at the
        # file's own directory and lays down a ``<stem>_snapshots/<test>/`` tree (see
        # snapshot_reltail). Nothing app-provided is needed to locate them.
        test_dir = Path(request.node.path).parent
        store = SnapshotStore(test_id=request.node.nodeid, root=test_dir)
        # The aggregate/overlay home (roll-up artifacts + variants/discriminants/
        # exclusions), configured relative to the pytest rootdir (spec §9).
        output_dir = _parity_output_dir(request.config, cfg)
        # The declared behavioural variants per operation, grouped by the shape each
        # one produces — so the harness can fail a call to a variant-bearing *shape*
        # that names none (a shape with no declared variants has one implicit,
        # unlabeled default and is never policed). Resolved from the same manifest
        # the coverage roll-up uses (anchored to the output dir;
        # $PARITY_VARIANTS_FILE wins) and cached so it's read once per session.
        declared_variants = getattr(request.config, "_parity_declared_variants", None)
        if declared_variants is None:
            from .coverage import VARIANTS_NAME, load_variants, variants_by_shape

            explicit = cfg.variants_file
            variants_path = Path(explicit) if explicit else output_dir / VARIANTS_NAME
            manifest = load_variants(variants_path)
            declared_variants = {op: variants_by_shape(manifest, op) for op in manifest}
            request.config._parity_declared_variants = declared_variants
        # The MCP index is an OPTIONAL app fixture (two-index parity): resolved
        # lazily so a REST-only suite that defines no ``mcp_index`` is unaffected.
        # When present it drives the MCP binding's resolution AND the report's MCP
        # coverage tab; when absent the harness falls back to ``oas_index`` for
        # ``api.tool`` (its historical behaviour) and no MCP tab is emitted.
        try:
            mcp_index = request.getfixturevalue("mcp_index")
        except pytest.FixtureLookupError:
            mcp_index = None
        # The MCP reference client is likewise an OPTIONAL app fixture, resolved the
        # same lazy way — it's the seam that unlocks NATIVE MCP capture. When present
        # (capture mode only, same lifecycle as ``reference``), ``api.tool`` calls the
        # MCP reference tool directly; when absent it's None and capture falls back to
        # the REST reference's mirrored op (today's behaviour). For payload-split
        # servers it is a ``{server_url: client}`` mapping keyed by the same base URLs
        # the ``mcp_index`` router resolves via ``server_url_for``; a single client is
        # the one-server ``/`` case.
        try:
            mcp_reference = request.getfixturevalue("mcp_reference")
        except pytest.FixtureLookupError:
            mcp_reference = None
        harness = Harness(
            mode=cfg.mode,
            oas=oas_index,
            mcp=mcp_index,
            store=store,
            clone=clone_client,
            reference=reference_ref,
            mcp_reference=mcp_reference,
            rules=Hooks.get_default_rules(),
            full_refresh=cfg.full_refresh,
            spoof=cfg.spoof,
            declared_variants=declared_variants,
        )
        # Stash what the session-end coverage roll-up needs (idempotent).
        # Manifest scan root: the common ancestor of every test file's directory
        # (co-located manifests live under each file's ``*_snapshots/`` tree). Kept
        # as tight as possible so the roll-up doesn't rglob the whole repo; the scan
        # is gated on the ``*_snapshots/`` suffix regardless of how broad this is.
        prev = getattr(request.config, "_parity_cov_root", None)
        request.config._parity_cov_root = (
            str(test_dir) if prev is None else os.path.commonpath([prev, str(test_dir)])
        )
        request.config._parity_output_dir = str(output_dir)
        request.config._parity_cov_ops = tuple(oas_index.operation_ids())
        # The documented (status, branch) response shapes per operation, derived
        # from the OAS (auto — no manifest). Drives the outcome / response-shape
        # coverage levels and the per-endpoint shape×variant grid in the report.
        # Absent shape data (a pre-enrichment catalogue) simply yields no shapes,
        # so this degrades to the operation/discriminant/variant levels only.
        request.config._parity_cov_shapes = _cov_shapes_for(oas_index)
        # The MCP partition (its own op-set + shapes), stashed only when an MCP
        # index is provided — this is the flag pytest_sessionfinish keys the second
        # coverage roll-up and the REST | MCP report tabs off of.
        if mcp_index is not None:
            request.config._parity_cov_mcp_ops = tuple(mcp_index.operation_ids())
            request.config._parity_cov_mcp_shapes = _cov_shapes_for(mcp_index)
        apply_seed_markers(request, harness)
        try:
            yield harness
        finally:
            passed = bool(getattr(request.node, "_parity_passed", False))
            try:
                harness.finalize_coverage(passed=passed)
            finally:
                harness.finalize()

    @pytest.hookimpl(wrapper=True)
    def pytest_runtest_makereport(item, call):  # noqa: ANN001, ANN201
        """Stash two things off each test's reports:

        1. ``item._parity_passed`` — the call-phase pass flag the ``api`` teardown
           gates coverage capture/verify on (a failed test isn't a valid baseline;
           unchanged behaviour).
        2. ``config._parity_outcomes[nodeid]`` — the session's per-test outcome
           (``passed`` / ``xfailed`` / ``xpassed`` / ``failed`` / ``skipped``) so
           the end-of-run HTML report can show live status, including xfailed
           not-ready tests that commit no snapshot. Skips/xfails resolved at setup
           (which pre-empt the call phase) are recorded there.
        """
        report = yield
        outcomes = getattr(item.config, "_parity_outcomes", None)
        if outcomes is None:
            outcomes = item.config._parity_outcomes = {}
        if report.when == "call":
            item._parity_passed = report.passed
            outcomes[item.nodeid] = _classify_outcome(report)
        elif report.when == "setup" and (report.failed or report.skipped):
            # The call phase never runs for a setup skip/xfail/error — record it
            # here (setdefault so a later call-phase result always wins).
            outcomes.setdefault(item.nodeid, _classify_outcome(report))
        return report

    def pytest_sessionfinish(session, exitstatus):  # noqa: ANN001, ANN201
        """Emit the suite coverage roll-up: how much of the documented surface the
        run exercised, with the uncovered operations named. Prints a one-line
        summary and writes two committed artifacts so external consumers can render
        the results without running the suite:

        - ``<output-dir>/coverage_report.json`` — the machine-readable roll-up
          (``$PARITY_COVERAGE_REPORT`` overrides).
        - ``<output-dir>/report.html`` — a self-contained, human-readable report
          (endpoint coverage breakdown, per-test cell sequences and captured
          request/response snapshots); ``$PARITY_HTML_REPORT`` overrides.

        ``<output-dir>`` is the configured aggregate home (``ParityConfig.output_dir``
        / ``$PARITY_OUTPUT_DIR``); per-test snapshots/manifests are co-located beside
        the tests and scanned from the pytest rootdir (``scan_root``), gated on the
        ``*_snapshots/`` suffix.

        Both regenerate on every run and reflect the manifests currently on disk
        (``coverage_report`` / ``render_html_report`` scan all of them from the
        rootdir, so the artifacts are independent of which tests ran this session —
        a ``-k`` / path-filtered / xdist-sharded run still rolls up the whole
        suite)."""
        root = getattr(session.config, "_parity_cov_root", None)
        if not root:
            return
        out_dir = Path(getattr(session.config, "_parity_output_dir", None) or Path(root).parent)
        # Scan from the pytest rootdir when available so the roll-up reflects EVERY
        # on-disk ``*_snapshots`` manifest, not just the dirs whose tests happened to
        # run this session — a ``-k`` / path-filtered / xdist-sharded partial run must
        # not silently under-count. The ``*_snapshots/`` suffix gate keeps a broad root
        # from sweeping in stray ``coverage.json``. Falls back to the accumulated
        # common-ancestor of ran tests when rootpath is unavailable (fakes/tests).
        scan_root = str(getattr(session.config, "rootpath", None) or root)
        from .config import ParityConfig
        from .coverage import (
            DISCRIMINANTS_NAME,
            EXCLUSIONS_NAME,
            VARIANTS_NAME,
            coverage_report,
            load_discriminants,
            load_exclusions,
            load_variants,
        )
        from .report import render_html_report

        cfg = ParityConfig.resolve()
        ops = getattr(session.config, "_parity_cov_ops", ())
        # Anchor the variants manifest to the configured output dir (where the
        # coverage_report.json / report.html land), not CWD — otherwise a suite
        # silently misses it and every op collapses to one implicit variant. An
        # explicit committed/env path (ParityConfig.variants_file, from
        # $PARITY_VARIANTS_FILE) still wins.
        explicit = cfg.variants_file
        variants_path = Path(explicit) if explicit else out_dir / VARIANTS_NAME
        variants = load_variants(variants_path)
        shapes = getattr(session.config, "_parity_cov_shapes", {})
        # Declared-discriminants overlay (spec §6.6, matrix-first authoring):
        # anchored to the output dir like variants.json. When present it realizes
        # the full discriminant denominator up front (negative values included);
        # absent, expand_tokens remains the source. Binding-agnostic — one overlay
        # keyed by operationId feeds both the REST and MCP partitions.
        discriminants = load_discriminants(out_dir / DISCRIMINANTS_NAME)
        # Matrix exclusions (spec §6.6): the paired deny-list that carves the
        # impossible cells out of the realized matrix so they read N/A, not red gaps.
        # Anchored to the output dir like the other overlays; binding-agnostic.
        exclusions = load_exclusions(out_dir / EXCLUSIONS_NAME)
        # Optional MCP partition (two-index parity): present only when a suite wired
        # an ``mcp_index`` fixture. Drives the second coverage roll-up and the
        # report's MCP tab; absent → single-binding REST report exactly as before.
        mcp_ops = getattr(session.config, "_parity_cov_mcp_ops", None)
        mcp_shapes = getattr(session.config, "_parity_cov_mcp_shapes", {})
        has_mcp = mcp_ops is not None
        report = coverage_report(
            scan_root,
            ops,
            variants,
            shapes,
            discriminants=discriminants,
            exclusions=exclusions,
            binding="rest" if has_mcp else None,
        )
        line = (
            f"parity coverage: {report['covered_count']}/{report['total_operations']} "
            f"operations exercised across {report['tests']} tests "
            f"({report['uncovered_count']} uncovered); "
            f"{report['covered_variant_combos']}/{report['total_variant_combos']} "
            f"variant combinations"
        )
        if report["total_response_shapes"]:
            line += (
                f"; {report['covered_response_shapes']}/{report['total_response_shapes']} "
                f"response shapes"
            )
        mcp_report = None
        if has_mcp:
            mcp_report = coverage_report(
                scan_root,
                mcp_ops,
                variants,
                mcp_shapes,
                discriminants=discriminants,
                exclusions=exclusions,
                binding="mcp",
            )
            line = f"parity coverage [REST]: {line[len('parity coverage: ') :]}"
            line += (
                f"\nparity coverage [MCP]: "
                f"{mcp_report['covered_count']}/{mcp_report['total_operations']} "
                f"operations exercised across {mcp_report['tests']} tests "
                f"({mcp_report['uncovered_count']} uncovered)"
            )
        reporter = session.config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.write_line("")
            reporter.write_line(line)
        else:  # pragma: no cover - non-terminal runs
            print(line)
        out = os.environ.get("PARITY_COVERAGE_REPORT") or str(out_dir / "coverage_report.json")
        out_path = Path(out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Backward-compatible artifact shape: a REST-only suite writes the flat
        # report as before; a two-index suite writes ``{"rest": ..., "mcp": ...}``
        # so each binding's numerator/denominator stays its own.
        payload = {"rest": report, "mcp": mcp_report} if has_mcp else report
        out_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

        html_out = os.environ.get("PARITY_HTML_REPORT") or str(out_dir / "report.html")
        html_path = Path(html_out)
        html_path.parent.mkdir(parents=True, exist_ok=True)
        outcomes = getattr(session.config, "_parity_outcomes", {})
        html_path.write_text(
            render_html_report(
                scan_root,
                ops,
                variants,
                shapes=shapes,
                mcp_operation_ids=mcp_ops if has_mcp else (),
                mcp_shapes=mcp_shapes,
                discriminants=discriminants,
                exclusions=exclusions,
                mode=cfg.mode,
                spoof=cfg.spoof,
                test_outcomes=outcomes,
            ),
            encoding="utf-8",
        )
        if reporter is not None:
            reporter.write_line(f"parity report: {html_path}")

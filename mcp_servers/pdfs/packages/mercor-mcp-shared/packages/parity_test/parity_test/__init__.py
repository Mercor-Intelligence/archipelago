"""``parity_test`` — unit-test-forward parity testing (spec §0).

Ordinary pytest tests run against the target (clone); the reference is hit only
incidentally, during capture. Parity is a *side effect* of validating business
logic: a test names an operation, asserts what it cares about, and the harness
verifies the whole response against a committed snapshot for free.

The public surface is deliberately small — most tests only ever touch the ``api``
and ``seed`` fixtures. The rest is here for authoring tools and app extensions.

Distribution name ``parity-test``; import name ``parity_test`` (Python can't
import a hyphen, and ``tests/parity-test/`` is the clone's export root — see
``ARCHITECTURE.md``).
"""

from __future__ import annotations

from . import defaults
from .bindings import Binding, BindingResolver, BindingSource
from .compare import ComparisonResult, assert_matches, compare
from .config import ParityConfig
from .coverage import (
    CoverageCell,
    CoverageRecorder,
    coverage_report,
    load_discriminants,
    load_exclusions,
    load_manifest,
    load_variants,
    roll_up,
    scan_manifests,
)
from .failures import (
    ContractSchemaMismatch,
    CoverageDrift,
    ExtraProperty,
    HeaderMismatch,
    LengthMismatch,
    McpErrorMismatch,
    MissingProperty,
    OutputSchemaMismatch,
    ParityFailure,
    ParityMismatch,
    PropertyValueMismatch,
    ResponseCodeMismatch,
    StaleSnapshotFailure,
    TypeMismatch,
    UnattributableId,
)
from .harness import BINDINGS, Harness
from .hooks import Hooks
from .identity import IdRegistry, LinkCandidate, detect_link_candidates
from .mcp_convert import from_fastmcp, tools_to_openapi
from .oas import (
    McpRouter,
    McpServer,
    OasIndex,
    OperationSpec,
    build_mcp_index,
    extract_response_shapes,
    resolve_branch,
)
from .report import render_html_report
from .rules import ComparisonRules, McpRules
from .seed import (
    GivensManifest,
    ResyncProposal,
    ResyncResult,
    bless_baseline,
    resync_baseline,
)
from .seeding import apply_seed_markers, materialize_seeds, populate_seed, seed_from_snapshot
from .snapshot import Binding as SnapshotBinding
from .snapshot import Snapshot, SnapshotStore, load_snapshot

__version__ = "0.1.0"

__all__ = [
    # rules & comparison
    "ComparisonRules",
    "McpRules",
    "compare",
    "assert_matches",
    "ComparisonResult",
    # failures
    "ParityFailure",
    "ParityMismatch",
    "ResponseCodeMismatch",
    "PropertyValueMismatch",
    "TypeMismatch",
    "MissingProperty",
    "ExtraProperty",
    "LengthMismatch",
    "HeaderMismatch",
    "McpErrorMismatch",
    "ContractSchemaMismatch",
    "OutputSchemaMismatch",
    "CoverageDrift",
    "UnattributableId",
    "StaleSnapshotFailure",
    # harness & fixtures surface
    "Harness",
    "BINDINGS",
    "Hooks",
    # snapshots
    "Snapshot",
    "SnapshotStore",
    "SnapshotBinding",
    "load_snapshot",
    # bindings & coverage
    "Binding",
    "BindingResolver",
    "BindingSource",
    "CoverageCell",
    "CoverageRecorder",
    "coverage_report",
    "scan_manifests",
    "roll_up",
    "load_manifest",
    "load_variants",
    "load_discriminants",
    "load_exclusions",
    # reporting
    "render_html_report",
    # identity
    "IdRegistry",
    "LinkCandidate",
    "detect_link_candidates",
    # seeding
    "GivensManifest",
    "ResyncResult",
    "ResyncProposal",
    "resync_baseline",
    "bless_baseline",
    "populate_seed",
    "seed_from_snapshot",
    "apply_seed_markers",
    "materialize_seeds",
    # config
    "ParityConfig",
    # shared hook factories
    "defaults",
    # oas
    "OasIndex",
    "OperationSpec",
    "extract_response_shapes",
    "resolve_branch",
    "McpServer",
    "McpRouter",
    "build_mcp_index",
    # mcp converter
    "tools_to_openapi",
    "from_fastmcp",
    # extensions
    "load_extensions",
]


def load_extensions() -> bool:
    """Import the app's optional ``parity_test_ext`` and let it ``register`` hooks
    (spec §1/§9). Returns ``True`` if an extension was found and registered.

    Mirrors the legacy ``parity`` loader: the copied-in package stays app-neutral;
    all per-app behavior arrives through this one seam.
    """
    try:
        import parity_test_ext  # type: ignore
    except ImportError:
        return False
    register = getattr(parity_test_ext, "register", None)
    if register is None:
        return False
    register(Hooks)
    return True

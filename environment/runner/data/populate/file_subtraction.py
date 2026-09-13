"""Resolve task-snapshot removal markers — the last step of materialization.

A task snapshot overlays its world snapshot, and the overlay is additive: it can
add a file or replace one by name, never remove one. Subtraction closes that with
marker files carried inside the task snapshot:

    filesystem/reports/q3.pdf.rls_removed   removes filesystem/reports/q3.pdf
    filesystem/reports/.rls_removed         removes filesystem/reports/ entirely

Overlaying leaves the marker AND its target both on disk, because their names
differ. Resolving that pair is what this module does, and it runs as a STEP OF
POPULATE rather than as a post-populate hook: "this file is not in this
environment" is part of materializing the filesystem, not an optional side effect
attached to it. Consequences of that choice, all deliberate:

- It always runs, within one scope: a populate carrying an authored ``tasks/``
  layer. ``main.py`` reads that off the source URLs (``_has_authored_task_layer``),
  so a world-only or continuation populate resolves nothing — do not read "always"
  as "any populate has already had its markers resolved". Inside that scope
  nothing decides whether to attach it, so no caller can forget, which is the
  property being bought here.
- It runs ONCE PER SUBSYSTEM ROOT, on either side of the post-populate hooks.
  ``filesystem`` goes before them: nothing writes there after the download
  stage, so an early pass is final and a hook observes a finished public tree.
  ``.apps_data`` goes after them, because service state is the one root a hook
  writes — a mysql hook loading its dump re-creates the very path a task
  subtracted. Splitting by root rather than running both twice is forced by the
  next line: a marker is consumed with its target, so whichever pass reaches a
  root first is the only one that can act on it, and a pass covering both roots
  early leaves the later one nothing to rediscover. The island splice shipped
  that bug and was split the same way (``ISLAND_ROOTS``, server copy).
- It can fail the populate. A hook was best-effort by construction; a populate
  step that cannot enforce what it was asked to enforce should say so.

MIRRORS ``rl-studio/server/packages/snapshots/file_subtraction.py``, and the two
copies are deliberate — the runner and the server are separate deployables that
reach each other over HTTP, the same reason ``WorldMount`` is defined twice (see
``archipelago/agents/modal_models.py``). They are not the same function anyway:
the server copy parses S3 KEYS to PREDICT what materialization will produce, so
the grading diff can exclude those paths; this copy walks a FILESYSTEM to ENFORCE
it. Same rules, different substrate.

DRIFT IS THE RISK, and it has bitten once already: the shell hook this replaces
matched directories while the server parser did not, so a path was deleted that
the exclusion set never knew about — and the diff then reported it as a file the
agent deleted. ``RULE_VECTORS`` in ``tests/test_populate_subtraction.py`` is the
guard against a repeat: the same table is carried beside each copy of the rule, so
one that changes on only one side fails a test rather than shipping a phantom
deletion. All three tables exist now, the server's having landed with the PR that
retired the hook.

The ARMING rule — whether to resolve at all — is mirrored the same three ways and
has its own table on the two sides that can express it (``ARMING_VECTORS`` beside
the grading copy). Here it is ``_has_authored_task_layer`` in ``main.py``, read
off the populate source URLs; the server reads it off the diff's task half and
grading off its download arguments. It is the easier of the two rules to drift,
because no two copies read it from the same variable.

ONE DIVERGENCE IS ACCEPTED, deliberately, and the direction is worth stating.
The server masks a subtracted path only when the TASK layer carries a marker
(``resolve_subtracted_paths``), while this side walks the MERGED tree — so a
marker reaching the sandbox from the WORLD layer is honored here and masked
nowhere, and the removal reads as the agent's. It stays open because it is not
reachable by authoring: the task file editor writes markers into a task snapshot
alone, and no surface puts one into a world.

The route usually raised against that — a playground promoted back to a world —
DOES NOT EXIST. A playground's only promote target is its own task
(``POST /playgrounds/task/{task_id}/promote-snapshot``, gated on
``snapshot_belongs_to_task``), and ``apply-to-world`` belongs to pipeline runs,
copying a run's snapshot rather than a playground's. Nothing writes a ``worlds/``
prefix from a ``playgrounds/`` one.

ONE REAL ROUTE DOES EXIST, and it is a remix rather than an upload:
``task_spawn_world`` copies ``tasks/{task_data_id}/`` straight into a new world
snapshot, markers included. What lands there is a marker WITHOUT its target —
the target only ever lived in the ORIGINAL world, which is not copied — so it
subtracts nothing in the spawned world. The symptom is correspondingly small and
worth stating exactly: this side deletes the stray marker, the server does not
mask it because the task layer armed nothing, and one zero-byte file is reported
as deleted by every trajectory in that world. A wrong attribution over a file
nobody authored, rather than a lost world file, which is why it is recorded here
instead of blocking this change.
"""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

from loguru import logger

#: Suffix marking a sibling path for removal. Also the whole filename of a
#: directory marker, so ``<dir>/.rls_removed`` removes ``<dir>``.
SUBTRACTION_MARKER = ".rls_removed"

#: Characters that disqualify a marker. A control character is legal in a
#: filename, so the server-side parser refuses these to keep its exclusion set
#: honest — and this side has to refuse the same names or the two disagree about
#: what is on disk.
_FORBIDDEN = frozenset(chr(c) for c in range(0x20)) | {chr(0x7F)}


class ResolveResult(NamedTuple):
    """What resolution did, for the populate log."""

    markers_resolved: int
    targets_removed: int


def marker_target(marker: Path, root: Path) -> Path | None:
    """The path ``marker`` removes, or ``None`` if the marker is refused.

    Pure and filesystem-free so the rule can be tested against ``RULE_VECTORS``
    without touching a disk. Every refusal here has a counterpart in the server
    copy; see the module docstring.
    """
    if any(ch in _FORBIDDEN for part in marker.parts for ch in part):
        return None
    # No `..` may participate. Cannot occur in a path `rglob` produced, so this
    # only fires if a caller hands us something it did not get from the walk.
    if any(part == ".." for part in marker.parts):
        return None

    if marker.name == SUBTRACTION_MARKER:
        # `<dir>/.rls_removed` — the marker names its own parent directory.
        target = marker.parent
    else:
        # `<file>.rls_removed` — the marker names its sibling. Vet the stripped
        # NAME before building a path from it: `foo/..rls_removed` strips to `.`
        # and `foo/...rls_removed` to `..`, and `Path.with_name` raises on those
        # rather than producing something to reject downstream.
        stripped = marker.name[: -len(SUBTRACTION_MARKER)]
        if stripped in ("", ".", ".."):
            return None
        target = marker.with_name(stripped)

    # Strictly INSIDE the root. A marker sitting at a subsystem root would
    # otherwise subtract the entire world, which is what the Task-Specific
    # Environments toggle is for and far too blunt to trigger from a filename.
    if target == root or root not in target.parents:
        return None
    # Refuses `foo/..rls_removed` (target `foo/.`) and `foo/...rls_removed`
    # (target `foo/..`), both of which name something other than a sibling.
    if target.name in (".", ".."):
        return None
    return target


def resolve_markers(roots: Iterable[Path]) -> ResolveResult:
    """Delete every marked path, and the markers themselves.

    Bottom-up, so a marker nested inside a directory that is itself being removed
    is handled before its parent disappears.

    A marker must not be a DIRECTORY. ``rglob`` matches directories too, and one
    whose name ends in the suffix would otherwise strip to a sibling the task
    never subtracted.

    Stated as ``not is_dir()`` rather than ``is_file()`` because both follow
    symlinks, and ``is_file()`` answers False for a DANGLING one — dropping the
    marker from this list before any of the handling below can see it. The
    server-side parser reads object NAMES, so it excludes that marker and its
    target from the diff's BEFORE side regardless of what the link points at;
    leaving both on disk is the desync this module exists to prevent. Symlinks
    TO a directory are still excluded, as they were.

    Raises on a failed removal rather than continuing: the caller turns that into
    a failed populate, which is the point of this being a step rather than a hook.
    """
    markers_resolved = 0
    targets_removed = 0
    started = time.perf_counter()

    for root in roots:
        if not root.is_dir():
            # Plenty of worlds carry no service state, so a missing root is
            # ordinary rather than an error.
            continue
        # Deepest first, so a marker nested inside a directory that is itself
        # being removed is handled before its parent disappears.
        markers = sorted(
            (p for p in root.rglob(f"*{SUBTRACTION_MARKER}") if not p.is_dir()),
            key=lambda p: len(p.parts),
            reverse=True,
        )
        for marker in markers:
            if not os.path.lexists(marker):
                # Already went with a directory removed earlier in this pass.
                # `lexists` for the same reason as below — a marker that is a
                # dangling symlink is still an entry on disk to resolve.
                continue
            target = marker_target(marker, root)
            if target is None:
                logger.warning(f"Ignoring unusable subtraction marker: {marker}")
                continue
            markers_resolved += 1
            targets_removed += _remove(target)
            # A directory removal took the marker with it; a file target leaves
            # the marker as a sibling, and leaving it behind would put it in
            # front of the agent and in the grading diff as a created file.
            #
            # `lexists`, not `exists`: a marker that is ITSELF A SYMLINK to the
            # path it removes is dangling by now, and `exists` follows the link
            # and answers False — so the marker would survive, which is the
            # exact outcome this branch exists to prevent.
            if os.path.lexists(marker):
                _remove(marker)

    # Logged even when nothing was resolved, which is the case worth watching:
    # the walk runs on every populate carrying a task layer, so with no markers
    # the elapsed time IS the walk. On a Task-Specific-Environments world or a
    # mounted one the per-entry stat costs far more than it does on local disk,
    # and this line is where that shows up rather than being estimated.
    logger.info(
        f"Resolved {markers_resolved} subtraction marker(s), "
        f"removing {targets_removed} target(s), "
        f"in {time.perf_counter() - started:.2f}s"
    )
    return ResolveResult(
        markers_resolved=markers_resolved, targets_removed=targets_removed
    )


def _remove(path: Path) -> int:
    """Remove a file or a whole directory tree. Returns 1 if anything went.

    A target that is already absent is not an error — subtraction is declarative
    ("this must not be here"), so the world having changed underneath must not
    fail the run. An actual failure to remove is left to raise.
    """
    if path.is_symlink():
        path.unlink()
        return 1
    if path.is_dir():
        shutil.rmtree(path)
        return 1
    if path.exists():
        path.unlink()
        return 1
    return 0

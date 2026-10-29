"""Resolve task-snapshot removal markers while re-deriving the initial state.

Grading rebuilds "the files the trajectory started from" by merging a world
snapshot with a task snapshot — the same overlay populate performs in the sandbox,
expressed as a zip instead of a filesystem. That merge is a re-derivation of
materialization, so it owes the same third step: after overlaying, resolve the
markers.

Without it the merged zip carries BOTH the marker and the path it removes, while
the trajectory snapshot it is diffed against carries neither — so the grading
runner's ``SNAPSHOT_DIFF`` helper reports two files the agent deleted, and
``EvalIds.OUTPUT_LLM`` grades against that.

MIRRORS ``rl-studio/server/packages/snapshots/file_subtraction.py``, and will
mirror ``archipelago/environment/runner/data/populate/file_subtraction.py`` once
#17811 lands — that path does not exist yet, so today this is the SECOND copy of
the rule, not the third. Copies at all, deliberately: these are separate
deployables that reach each other over HTTP.

The ``RULE_VECTORS`` table in ``tests/test_grading_subtraction.py`` is duplicated
beside each copy — this one and, as of this change,
``rl-studio/server/tests/unit/packages/snapshots/test_file_subtraction.py`` — so a
rule that changes in one place fails a test instead of shipping a phantom
deletion. In one direction only it is worth little: nothing on the server side
pinned the table until this change, and that copy is the oldest — the one feeding
the trajectory UI diff. Both divergences review found were between it and this one.

Not hypothetical: the shell hook matched directories and the parser did not, so a
directory removal graded as an agent deletion. #17729 was closed unmerged and the
parser now handles the shape instead (``is_removed`` covers a subtree), which is
why the table is the artifact worth keeping rather than the link.

WHETHER a grade resolves markers at all is decided before any of this runs, by
``_resolves_markers`` in ``modal_helpers``: an authored ``tasks/`` overlay, which
is the same condition populate arms on. That is the ARMING rule, mirrored three
ways just like the marker rule here, and it has its own vectors table for the
same reason.

This copy is KEY-based rather than filesystem-based, because both grading paths
that need it have the keys already:

- the boto3 per-file path streams objects into the zip and can skip one before it
  is ever fetched
- the s5cmd path syncs prefixes into a temp dir and packs it, and lists every
  prefix anyway to count files — so the same key set names what to delete from the
  merged dir before packing

Path 1 (a prebuilt world archive fetched in one GET) cannot resolve anything: its
world entries were sealed by a different process at snapshot-commit time, and
``zipfile`` cannot delete an entry, so skipping them would mean rebuilding the
archive. It therefore DECLINES rather than mis-resolves — ``download_world_snapshot``
looks for a marker in both halves and falls through to a re-derive path if either
carries one, so which path ran cannot change the baseline a trajectory is graded
against. Each half is read where it is free: the task half from the listing the
overlay append takes anyway, before the archive is fetched at all, and the world
half from the archive's own entry names, which cost the central directory that path
already reads to validate it.
"""

from __future__ import annotations

import posixpath
import shutil
from collections.abc import Iterable
from pathlib import Path
from typing import NamedTuple

from loguru import logger

#: Suffix marking a sibling path for removal. Also the whole filename of a
#: directory marker, so ``<dir>/.rls_removed`` removes ``<dir>``.
SUBTRACTION_MARKER = ".rls_removed"

#: Subsystem roots a marker may target. Anything outside them is not part of the
#: environment the agent sees.
SUBTRACTION_ROOTS = ("filesystem/", ".apps_data/")

_FORBIDDEN = frozenset(chr(c) for c in range(0x20)) | {chr(0x7F)}


class RemovalSet(NamedTuple):
    """Snapshot-relative paths a task removes, plus the markers themselves.

    ``removed`` covers subtrees — use :func:`is_removed` rather than comparing
    membership directly, so a directory marker takes everything under it.
    """

    removed: frozenset[str]
    markers: frozenset[str]

    @property
    def is_empty(self) -> bool:
        return not self.removed

    @property
    def drops_nothing(self) -> bool:
        """True when applying this set is a no-op.

        Distinct from :attr:`is_empty`, which answers "does this task subtract?"
        — a markers-only set subtracts nothing and still has to be applied, so
        the two questions cannot share a predicate.
        """
        return not self.removed and not self.markers


EMPTY_REMOVAL = RemovalSet(frozenset(), frozenset())


def normalize(key: str) -> str | None:
    """Collapse a snapshot-relative key, or ``None`` if it is not usable.

    The single place a key becomes comparable. EVERYTHING that compares keys goes
    through it — the marker set, the targets, and the per-object predicate — so a
    raw key and its collapsed form can never sort onto opposite sides of a
    comparison. Storing one and testing the other is precisely how a marker
    survives into the packed baseline and grades as an agent deletion.

    Collapsing (rather than refusing) `./` and doubled slashes matches the server
    copy, and matches what the sandbox does in effect: the downloader writes
    `filesystem/./a.txt` to `filesystem/a.txt`, so the file lands under its
    collapsed name and is resolved there.
    """
    if any(ch in _FORBIDDEN for ch in key):
        return None
    # No backslash check: a backslash is a legal character in an S3 key and in a
    # POSIX filename, and neither the server parser nor the sandbox refuses one.
    if key.startswith("/"):
        return None
    norm = posixpath.normpath(key)
    if norm in (".", "..") or norm.startswith("../") or "/../" in norm:
        return None
    if not norm.startswith(SUBTRACTION_ROOTS):
        return None
    return norm


def _target(marker: str) -> str | None:
    """The path an ALREADY-NORMALIZED marker removes, or ``None`` if refused.

    Every refusal has a counterpart in the other two copies; see the module
    docstring for why that matters more than it looks.
    """
    parts = marker.split("/")
    if parts[-1] == SUBTRACTION_MARKER:
        # `<dir>/.rls_removed` — the marker names its own parent directory.
        target = "/".join(parts[:-1])
    else:
        stripped = parts[-1][: -len(SUBTRACTION_MARKER)]
        # `foo/..rls_removed` strips to `foo/.` and `foo/...rls_removed` to
        # `foo/..`; neither names a sibling. (normpath already collapsed any
        # interior dot segments, so this is the only place they can appear.)
        if stripped in ("", ".", ".."):
            return None
        target = "/".join([*parts[:-1], stripped])

    # Strictly INSIDE a root. A marker at a subsystem root would subtract the
    # whole world, which is what the Task-Specific Environments toggle is for and
    # far too blunt to trigger from a filename.
    if f"{target}/" in SUBTRACTION_ROOTS or not target.startswith(SUBTRACTION_ROOTS):
        return None
    return target


def parse_markers(rel_keys: Iterable[str]) -> RemovalSet:
    """Read snapshot-relative keys into the set of paths they remove.

    Both the markers and their targets are stored NORMALIZED, so everything that
    later joins them onto a path or compares them against a key is working in one
    form. Reads keys from BOTH halves of the merge, because the sandbox does:
    populate resolves markers by walking the overlaid tree, where a marker is a
    marker regardless of which snapshot delivered it. A world snapshot really can
    carry one — a playground promoted back to a world via apply-to-world takes its
    markers with it.

    An unusable marker is skipped rather than raising: a task must not fail to
    grade because someone uploaded an oddly named file, and skipping leaves the
    file visible, which is the status quo.
    """
    removed: set[str] = set()
    markers: set[str] = set()
    for key in rel_keys:
        norm = normalize(key)
        # The suffix is tested on the NORMALIZED key, as the server copy does,
        # because a marker can arrive as a directory-placeholder object whose key
        # ends in a slash — `filesystem/dir/.rls_removed/`. Raw, that is not a
        # marker; collapsed, it is the one that removes `filesystem/dir`, and the
        # hook agrees because `find -name '*.rls_removed'` matches a directory as
        # readily as a file. Testing the raw key made this copy the outlier, and
        # the whole subtree read as an agent deletion.
        #
        # A key that looks like a marker in EITHER form is what gates the warning,
        # so a refused one is reported while an ordinary file stays quiet.
        target = (
            _target(norm)
            if norm is not None and norm.endswith(SUBTRACTION_MARKER)
            else None
        )
        if norm is None or target is None:
            if key.endswith(SUBTRACTION_MARKER) or (norm or "").endswith(
                SUBTRACTION_MARKER
            ):
                logger.warning(f"Ignoring unusable subtraction marker: {key}")
            continue
        removed.add(target)
        markers.add(norm)
    return RemovalSet(removed=frozenset(removed), markers=frozenset(markers))


def baseline_removal(
    world_relpaths: Iterable[str],
    task_relpaths: Iterable[str],
    *,
    resolve_targets: bool,
) -> RemovalSet:
    """What a re-derived baseline must drop, which is not all-or-nothing.

    MARKERS ALWAYS. A marker is an instruction, never content. A resolving run
    deletes every one it finds, so that environment has none. A continuation
    populates the world plus the parent trajectory's snapshot, and the parent has
    none either — turn 1 consumed them — so the markers the baseline carries come
    from the ROOT task snapshot, which that environment never saw. Either way,
    keeping them reports files the AGENT DELETED.

    WHICH IS WHY THE HALVES ARE SEPARATE. A marker can reach a WORLD snapshot —
    `task_spawn_world` copies `tasks/{id}/` into one, markers included — and a
    non-resolving run leaves it on disk, since nothing removed it. Dropping that
    one would report it as agent-created: the same inversion pointing the other
    way. So a non-resolving baseline drops the task half's markers only.

    TARGETS ONLY WHEN THE RUN RESOLVED. That is the half `subtraction_resolved`
    governs. For a continuation the practical effect is small and the reason is
    worth stating exactly, because the obvious one is wrong: a continuation row
    carries `world_snapshot_id = NULL` and `task_data_id = parent.trajectory_snapshot_id`
    (`orchestration/trajectories/db.py`), and `get_snapshot_ids` then rewrites the
    task half to the ROOT task snapshot while leaving the world half NULL. So the
    graded baseline is that task snapshot ALONE — there is no world layer in it to
    restore anything. Keeping its targets is therefore close to a no-op, since a
    task snapshot does not normally carry the world file it subtracts; dropping
    its markers is the part that matters.

    The two used to move together, so a continuation either lost its targets
    (mis-attributing them as created) or kept its markers (mis-attributing them
    as deleted). It needs both halves, in opposite directions.
    """
    if resolve_targets:
        # Populate walked the MERGED tree and honored a marker from either half,
        # so both halves are gone from that environment.
        return parse_markers([*world_relpaths, *task_relpaths])
    # The TASK half only. A non-resolving run populated the parent trajectory's
    # snapshot, which carries no markers, so the ones the baseline holds from the
    # ROOT task were never in that environment. A WORLD-half marker was — nothing
    # removed it — so dropping that one would report it as agent-created, which is
    # the same inversion pointing the other way.
    return RemovalSet(removed=frozenset(), markers=parse_markers(task_relpaths).markers)


def is_removed(rel: str, removal: RemovalSet) -> bool:
    """True when ``rel`` is a marker, a removed path, or under a removed one.

    Normalizes the candidate, because the set is normalized: an object key really
    can arrive as `filesystem/./a.txt` while the set holds `filesystem/a.txt`, and
    comparing the two forms directly would let the file through.

    KNOWN DIVERGENCE, in this direction on purpose: the server's reader-side
    predicate (``apply_subtraction`` → ``is_subtracted``) compares the candidate
    RAW against its normalized set, so it keeps `filesystem/./a.txt` where this
    drops it. This copy is the one that matches the sandbox — the downloader
    writes that key to `filesystem/a.txt`, so the collapsed name is the one that
    exists on disk and the hook resolves it there. Recorded rather than fixed from
    here: the server predicate backs the trajectory UI diff, and changing it
    belongs with that copy. ``CANDIDATE_VECTORS`` in the test module pins this
    half of the rule, which ``RULE_VECTORS`` (marker → removed path) cannot reach.
    """
    norm = normalize(rel)
    if norm is None:
        return False
    if norm in removal.markers:
        return True
    return any(norm == gone or norm.startswith(f"{gone}/") for gone in removal.removed)


def _under(root_real: Path, path: Path) -> bool:
    """Does ``path`` resolve to ``root_real`` or somewhere inside it?"""
    real = path.resolve()
    return real == root_real or root_real in real.parents


def apply_to_dir(root: Path, removal: RemovalSet) -> int:
    """Delete the removed paths, and the markers, from an already-merged tree.

    For the s5cmd path, which syncs both prefixes into one directory and then
    packs it: by that point there is a single tree, so a world file and a task
    file that share a name have already collapsed and one deletion covers both.

    Returns the number of FILES removed, not the number of paths named — a
    directory removal takes everything under it. The caller subtracts this from a
    count of downloaded objects, so the two have to be in the same unit or the
    reported file count drifts (or goes negative) on any directory removal.

    Raises on a failed removal — a re-derivation that cannot apply what it was
    asked to apply must not quietly hand the judge an unresolved baseline.
    """
    # `drops_nothing`, not `is_empty`: a markers-only set removes no TARGETS and
    # so is "empty" by that predicate, while still having markers to delete. The
    # s5cmd path gates its call correctly and this early return then undid it.
    if removal.drops_nothing:
        return 0
    root_real = root.resolve()
    files_removed = 0
    # Longest first, so a marker nested inside a directory being removed is gone
    # before its parent is.
    for rel in sorted(removal.removed | removal.markers, key=len, reverse=True):
        path = root / rel
        # `normalize` blocks escape through the KEY; this blocks escape through
        # the TREE. A symlinked component — `filesystem/link` pointing at `/` —
        # puts `root / rel` outside the download root, and this loop deletes.
        # Snapshot downloads write plain files today, so nothing can produce one;
        # confining anyway is the same defence `_confine` gives every destination
        # in ``s3_transfer``, and cheap here because only removed paths are
        # resolved.
        if not _under(root_real, path.parent):
            logger.warning(
                f"Refusing to subtract {rel}: it resolves outside the download root"
            )
            continue
        if path.is_symlink() or path.is_file():
            path.unlink()
            files_removed += 1
        elif path.is_dir():
            # Counted before the tree goes, and bounded by the subtree being
            # deleted rather than the whole merge.
            files_removed += sum(1 for p in path.rglob("*") if p.is_file())
            shutil.rmtree(path)
    if files_removed:
        logger.info(
            f"Resolved subtraction: removed {files_removed} file(s) before packing"
        )
    return files_removed

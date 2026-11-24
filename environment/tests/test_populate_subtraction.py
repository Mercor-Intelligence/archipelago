"""Populate's marker-resolution step, exercised against a real directory tree.

Resolution deletes files inside a sandbox on the strength of user-chosen S3 object
keys, so these run it rather than asserting on its shape. The pure rule
(``marker_target``) is checked separately against ``RULE_VECTORS``.

``RULE_VECTORS`` IS DUPLICATED, ON PURPOSE. The same table lives beside the
grading copy of the rule, and lands beside the server one with the PR that retires
the hook. The two implementations cannot share
code — separate deployables that talk over HTTP — so the table is what keeps them
from drifting: the server predicts which paths the environment will lose (for the
grading diff) and this side performs the loss, and a rule that changes on only one
side must fail a test rather than ship a phantom deletion. That failure mode is not
hypothetical; the shell hook this replaced matched directories while the parser did
not, and the diff reported the difference as a file the agent deleted.

Each vector is (marker relpath under the root, expected target relpath or None
for "refused").
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from runner.data.populate.file_subtraction import (
    SUBTRACTION_MARKER,
    marker_target,
    resolve_markers,
)
from runner.data.populate.main import _has_authored_task_layer
from runner.data.populate.models import PopulateSource

RULE_VECTORS: list[tuple[str, str | None]] = [
    # Sibling form: the marker names the file next to it.
    ("reports/q3.pdf.rls_removed", "reports/q3.pdf"),
    ("notes.txt.rls_removed", "notes.txt"),
    ("a/b/c/deep.txt.rls_removed", "a/b/c/deep.txt"),
    # Directory form: the marker names its own parent.
    ("reports/.rls_removed", "reports"),
    ("a/b/.rls_removed", "a/b"),
    # Refused: the target would be the subsystem root itself. That is what the
    # Task-Specific Environments toggle is for, far too blunt for a filename.
    (".rls_removed", None),
    # Refused: the target resolves to a dot segment rather than a sibling.
    ("..rls_removed", None),
    ("a/..rls_removed", None),
    ("a/...rls_removed", None),
    # Refused: a control character has no place in a snapshot key, and the
    # server-side parser refuses these — so this side must too.
    ("we\nird.txt.rls_removed", None),
    ("tab\there.txt.rls_removed", None),
]


@pytest.mark.parametrize(("marker", "expected"), RULE_VECTORS)
def test_the_rule(tmp_path: Path, marker: str, expected: str | None) -> None:
    root = tmp_path / "filesystem"
    result = marker_target(root / marker, root)

    assert result == (None if expected is None else root / expected)


def _write(root: Path, rel: str, body: bytes = b"x") -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


def _survivors(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def test_it_removes_exactly_the_marked_paths(tmp_path: Path) -> None:
    fs = tmp_path / "filesystem"
    apps = tmp_path / ".apps_data"
    _write(fs, "reports/q3.pdf")
    _write(fs, f"reports/q3.pdf{SUBTRACTION_MARKER}", b"")
    _write(fs, "reports/keep.pdf")
    _write(fs, "scratch/a.txt")
    _write(fs, "scratch/deep/b.txt")
    _write(fs, f"scratch/{SUBTRACTION_MARKER}", b"")
    # A directory whose name merely starts with a removed one's.
    _write(fs, "reports_archive/x.csv")
    _write(apps, "mysql/seed.sql")
    _write(apps, f"mysql/seed.sql{SUBTRACTION_MARKER}", b"")
    _write(apps, "mysql/keep.sql")

    result = resolve_markers([fs, apps])

    assert _survivors(fs) == ["reports/keep.pdf", "reports_archive/x.csv"]
    assert _survivors(apps) == ["mysql/keep.sql"]
    assert result.markers_resolved == 3


def test_markers_do_not_survive(tmp_path: Path) -> None:
    """A marker left behind lands in the agent's listing, and in the grading diff
    as a file the agent created."""
    fs = tmp_path / "filesystem"
    _write(fs, "a.txt")
    _write(fs, f"a.txt{SUBTRACTION_MARKER}", b"")

    resolve_markers([fs])

    assert not list(fs.rglob(f"*{SUBTRACTION_MARKER}"))


def test_a_symlinked_marker_does_not_survive(tmp_path: Path) -> None:
    """A marker that is a symlink to its own target is dangling once the target
    goes, and `Path.exists` follows the link and answers False. Skipping the
    cleanup on that answer leaves the marker in front of the agent — and the
    server excludes marker paths from the diff's BEFORE side, so it reads as a
    file the agent CREATED."""
    fs = tmp_path / "filesystem"
    _write(fs, "a.txt")
    (fs / f"a.txt{SUBTRACTION_MARKER}").symlink_to(fs / "a.txt")

    resolve_markers([fs])

    assert not (fs / "a.txt").exists()
    assert not list(fs.rglob(f"*{SUBTRACTION_MARKER}"))


def test_a_dangling_symlink_marker_is_still_resolved(tmp_path: Path) -> None:
    """The work list filters out directories, and `is_file()` would have filtered
    out a marker whose link target is already gone — dropping it before any of
    the handling below can see it. The server-side parser reads object NAMES, so
    it excludes the pair from the diff's BEFORE side either way, and both would
    survive on disk to be reported as files the agent created."""
    fs = tmp_path / "filesystem"
    _write(fs, "a.txt")
    (fs / f"a.txt{SUBTRACTION_MARKER}").symlink_to(fs / "never_existed.txt")

    resolve_markers([fs])

    assert not (fs / "a.txt").exists()
    assert not list(fs.rglob(f"*{SUBTRACTION_MARKER}"))


def test_a_directory_named_like_a_marker_subtracts_nothing(tmp_path: Path) -> None:
    """`filesystem/foo.rls_removed/bar.txt` creates a DIRECTORY ending in the
    suffix. Stripping it would delete `filesystem/foo`, which no marker named —
    and the server-side parser would not have excluded it, so the diff would
    report it as an agent deletion. A marker is a file."""
    fs = tmp_path / "filesystem"
    _write(fs, "foo/keep.txt")
    _write(fs, f"foo{SUBTRACTION_MARKER}/bar.txt")

    resolve_markers([fs])

    assert (fs / "foo" / "keep.txt").exists()
    assert (fs / f"foo{SUBTRACTION_MARKER}" / "bar.txt").exists()


def test_a_symlink_to_a_directory_named_like_a_marker_subtracts_nothing(
    tmp_path: Path,
) -> None:
    """`not is_dir()` follows the link, so this stays excluded exactly as a real
    directory does — the loosening is for dangling links only."""
    fs = tmp_path / "filesystem"
    _write(fs, "foo/keep.txt")
    _write(fs, "target_dir/x.txt")
    (fs / f"foo{SUBTRACTION_MARKER}").symlink_to(fs / "target_dir")

    resolve_markers([fs])

    assert (fs / "foo" / "keep.txt").exists()
    assert (fs / "target_dir" / "x.txt").exists()


def test_a_marker_at_a_subsystem_root_deletes_nothing(tmp_path: Path) -> None:
    fs = tmp_path / "filesystem"
    _write(fs, "a.txt")
    _write(fs, "nested/b.txt")
    _write(fs, SUBTRACTION_MARKER, b"")

    resolve_markers([fs])

    assert _survivors(fs) == [SUBTRACTION_MARKER, "a.txt", "nested/b.txt"]


def test_a_nested_marker_inside_a_removed_directory(tmp_path: Path) -> None:
    """Deepest-first ordering: the inner marker is handled before the directory
    containing it disappears."""
    fs = tmp_path / "filesystem"
    _write(fs, "outer/inner/x.txt")
    _write(fs, f"outer/inner/x.txt{SUBTRACTION_MARKER}", b"")
    _write(fs, f"outer/{SUBTRACTION_MARKER}", b"")
    _write(fs, "keep.txt")

    resolve_markers([fs])

    assert _survivors(fs) == ["keep.txt"]


def test_a_control_character_target_is_left_alone(tmp_path: Path) -> None:
    """A newline is legal in a filename, and the server-side parser refuses such
    a marker — so honoring it here would delete a path the grading diff still
    shows on its BEFORE side."""
    fs = tmp_path / "filesystem"
    weird = "note\nwith_newline.txt"
    _write(fs, weird)
    _write(fs, f"{weird}{SUBTRACTION_MARKER}", b"")
    _write(fs, "gone.txt")
    _write(fs, f"gone.txt{SUBTRACTION_MARKER}", b"")

    resolve_markers([fs])

    assert (fs / weird).exists()
    assert not (fs / "gone.txt").exists()


def test_a_missing_root_is_not_an_error(tmp_path: Path) -> None:
    """Plenty of worlds carry no service state."""
    fs = tmp_path / "filesystem"
    _write(fs, "a.txt")

    result = resolve_markers([fs, tmp_path / ".apps_data"])

    assert result.markers_resolved == 0
    assert _survivors(fs) == ["a.txt"]


def test_a_tree_with_no_markers_is_untouched(tmp_path: Path) -> None:
    """The common case — resolution runs on every populate."""
    fs = tmp_path / "filesystem"
    _write(fs, "a.txt")
    _write(fs, "nested/b.txt")
    before = _survivors(fs)

    result = resolve_markers([fs])

    assert _survivors(fs) == before
    assert result == (0, 0)


def test_a_marker_for_an_absent_target_is_harmless(tmp_path: Path) -> None:
    """Declarative: the task says what must not be there, so the world having
    changed underneath must not fail the run."""
    fs = tmp_path / "filesystem"
    _write(fs, "other.txt")
    _write(fs, f"never_existed.txt{SUBTRACTION_MARKER}", b"")

    result = resolve_markers([fs])

    assert _survivors(fs) == ["other.txt"]
    assert result.markers_resolved == 1
    assert result.targets_removed == 0


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_a_removal_failure_raises(tmp_path: Path) -> None:
    """The difference between this and the best-effort hook it replaces. A
    populate step that cannot enforce what it was asked to enforce says so, and
    the caller turns that into a failed populate rather than a trajectory that
    runs against a tree nobody masked."""
    fs = tmp_path / "filesystem"
    locked = fs / "locked"
    _write(fs, "locked/target.txt")
    _write(fs, f"locked/target.txt{SUBTRACTION_MARKER}", b"")
    locked.chmod(0o500)  # readable and traversable, not writable
    try:
        with pytest.raises(OSError):
            resolve_markers([fs])
    finally:
        locked.chmod(0o700)


def test_one_root_per_pass_leaves_the_other_roots_markers_alone(
    tmp_path: Path,
) -> None:
    """`handle_populate` resolves `filesystem` before the post-populate hooks and
    `.apps_data` after them. That only works because a pass is scoped to a root:
    resolution consumes a marker along with its target, so a first pass covering
    BOTH roots would leave the second nothing to rediscover — the bug the island
    splice shipped and had to be split to fix."""
    fs = tmp_path / "filesystem"
    apps = tmp_path / ".apps_data"
    _write(fs, "public.txt")
    _write(fs, f"public.txt{SUBTRACTION_MARKER}", b"")
    _write(apps, "mysql/seed.sql")
    _write(apps, f"mysql/seed.sql{SUBTRACTION_MARKER}", b"")

    resolve_markers([fs])

    assert _survivors(fs) == []
    assert _survivors(apps) == [
        "mysql/seed.sql",
        f"mysql/seed.sql{SUBTRACTION_MARKER}",
    ]


def test_a_target_re_created_by_a_hook_is_removed_by_the_second_pass(
    tmp_path: Path,
) -> None:
    """The reason `.apps_data` waits for the hooks. A service populate hook
    loading its dump re-creates the path a task subtracted; the pass that runs
    after it is the only one that can take it back out."""
    fs = tmp_path / "filesystem"
    apps = tmp_path / ".apps_data"
    _write(apps, "mysql/seed.sql")
    _write(apps, f"mysql/seed.sql{SUBTRACTION_MARKER}", b"")

    resolve_markers([fs])  # pass 1 — filesystem only, service state untouched
    _write(apps, "mysql/seed.sql", b"reseeded by the mysql hook")
    result = resolve_markers([apps])  # pass 2 — after the hooks

    assert _survivors(apps) == []
    assert result.markers_resolved == 1


def _source(url: str, subsystem: str = "filesystem") -> PopulateSource:
    return PopulateSource(url=url, subsystem=subsystem)


def test_an_authored_task_layer_arms_resolution() -> None:
    sources = [
        _source("s3://bucket/worlds/snap_w/filesystem/"),
        _source("s3://bucket/tasks/snap_t/filesystem/"),
    ]

    assert _has_authored_task_layer(sources) is True


def test_a_continuation_does_not_arm_resolution() -> None:
    """A continuation's task half is the PARENT TRAJECTORY's snapshot — agent
    output, not authored intent. A `.rls_removed` the agent happened to create in
    turn 1 must not start deleting the world out from under turn 2, and the
    merged tree cannot say which layer delivered a file, so the source URLs are
    the only place that distinction survives into the sandbox."""
    sources = [_source("s3://bucket/trajectories/snap_parent/filesystem/")]

    assert _has_authored_task_layer(sources) is False


def test_a_world_only_populate_does_not_arm_resolution() -> None:
    assert (
        _has_authored_task_layer([_source("s3://bucket/worlds/snap_w/filesystem/")])
        is False
    )


def test_an_unparseable_source_url_is_skipped() -> None:
    """A malformed URL must not decide the question either way; the real sources
    beside it still do."""
    sources = [
        _source("not-an-s3-url"),
        _source("s3://bucket/tasks/snap_t/filesystem/"),
    ]

    assert _has_authored_task_layer(sources) is True
    assert _has_authored_task_layer([_source("not-an-s3-url")]) is False

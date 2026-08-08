"""Every subtree the store creates belongs to exactly one plane.

The planes constrain principals inside the sandbox: control is what no actor
reads, record is what no actor writes, agent is per-principal. Grading sits
outside and reads all three, which is why it is not a plane.
"""

from pathlib import Path

from runner.coordinator.state.store import (
    COORDINATOR_SUBTREE_PLANES,
    ROOT_ONLY_SUBTREE_NAMES,
    CoordinatorStore,
    StatePlane,
)


def test_every_directory_the_store_creates_declares_a_plane(tmp_path: Path) -> None:
    """The point of the declaration: a new subtree cannot arrive unclassified.

    Adding one to ``CoordinatorStore`` without adding it to the map fails here,
    which is the only moment anybody is thinking about that subtree at all.
    """
    root = tmp_path / "state"
    store = CoordinatorStore(root=root)
    store.agent_filesystems.filesystem_dir("vca_riley")

    created = {child.name for child in root.iterdir() if child.is_dir()}

    assert created, "the store created nothing — this test would pass vacuously"
    undeclared = created - set(COORDINATOR_SUBTREE_PLANES)
    assert not undeclared, f"subtrees with no declared plane: {sorted(undeclared)}"


def test_root_only_names_are_the_non_agent_planes() -> None:
    assert set(ROOT_ONLY_SUBTREE_NAMES) == {
        name
        for name, plane in COORDINATOR_SUBTREE_PLANES.items()
        if plane is not StatePlane.AGENT
    }


def test_per_actor_subtrees_are_not_root_only() -> None:
    """A per-principal tree locked to root would take every VCA down with it."""
    for name, plane in COORDINATOR_SUBTREE_PLANES.items():
        if plane is StatePlane.AGENT:
            assert name not in ROOT_ONLY_SUBTREE_NAMES


def test_the_grading_substrate_is_record_plane() -> None:
    """What grading scores, an actor must not be able to write."""
    assert COORDINATOR_SUBTREE_PLANES["checkpoint_observations"] is StatePlane.RECORD
    assert COORDINATOR_SUBTREE_PLANES["event_occurrences"] is StatePlane.RECORD


def test_the_world_script_is_control_plane() -> None:
    """It holds every persona, brief and unfired beat."""
    assert COORDINATOR_SUBTREE_PLANES["config"] is StatePlane.CONTROL

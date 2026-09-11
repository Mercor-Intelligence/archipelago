"""The call log carries its own cursor.

The sequence number is in every record, so a second file holding a copy of it
buys nothing and can disagree with the log after a failed append. These pin
that the counter survives a restart and that nothing writes the old file.
"""

import json
from pathlib import Path

from runner.coordinator.state.store import ToolCallCheckpointObservationStore


def _record(store: ToolCallCheckpointObservationStore, tool_name: str) -> int:
    return store.record(
        actor_id="target_agent",
        tool_name=tool_name,
        arguments={},
        result_summary=None,
        error=None,
    ).sequence


def test_sequence_counts_up_within_one_store(tmp_path: Path) -> None:
    store = ToolCallCheckpointObservationStore(tmp_path)

    assert [_record(store, "a"), _record(store, "b"), _record(store, "c")] == [1, 2, 3]


def test_sequence_resumes_after_a_restart(tmp_path: Path) -> None:
    """A coordinator restart re-reads the tree; it must not replay sequence 1."""
    first = ToolCallCheckpointObservationStore(tmp_path)
    _record(first, "a")
    _record(first, "b")

    resumed = ToolCallCheckpointObservationStore(tmp_path)

    assert _record(resumed, "c") == 3
    assert [o.sequence for o in resumed.read()] == [1, 2, 3]


def test_no_sequence_file_is_written(tmp_path: Path) -> None:
    store = ToolCallCheckpointObservationStore(tmp_path)
    _record(store, "a")

    assert not (tmp_path / "sequence.txt").exists()
    assert [p.name for p in tmp_path.iterdir()] == ["mcp_calls.jsonl"]


def test_a_stale_cursor_file_does_not_move_the_sequence(tmp_path: Path) -> None:
    """An older image's leftover cursor must not decide where the log resumes."""
    (tmp_path / "sequence.txt").write_text("99", encoding="utf-8")
    seeded = ToolCallCheckpointObservationStore(tmp_path)
    _record(seeded, "a")

    assert _record(ToolCallCheckpointObservationStore(tmp_path), "b") == 2


def test_a_corrupt_line_does_not_reset_the_sequence(tmp_path: Path) -> None:
    """Recovery skips what it cannot parse rather than restarting at 1."""
    store = ToolCallCheckpointObservationStore(tmp_path)
    _record(store, "a")
    _record(store, "b")
    with (tmp_path / "mcp_calls.jsonl").open("a", encoding="utf-8") as handle:
        handle.write("{not json\n")

    assert _record(ToolCallCheckpointObservationStore(tmp_path), "c") == 3


def test_recorded_sequence_matches_the_line_it_wrote(tmp_path: Path) -> None:
    store = ToolCallCheckpointObservationStore(tmp_path)
    _record(store, "a")
    _record(store, "b")

    lines = [
        json.loads(line)
        for line in (tmp_path / "mcp_calls.jsonl").read_text().splitlines()
        if line.strip()
    ]

    assert [entry["sequence"] for entry in lines] == [1, 2]

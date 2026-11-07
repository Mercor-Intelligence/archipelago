import asyncio

import pytest

from runner.utils import metrics

CountCall = tuple[str, list[str] | None]
DistCall = tuple[str, float, list[str] | None]


class Emits:
    def __init__(self) -> None:
        self.count: list[CountCall] = []
        self.dist: list[DistCall] = []


@pytest.fixture
def emits(monkeypatch: pytest.MonkeyPatch) -> Emits:
    captured = Emits()

    def fake_increment(
        metric: str, tags: list[str] | None = None, value: int = 1
    ) -> None:
        captured.count.append((metric, tags))

    def fake_distribution(
        metric: str, value: float, tags: list[str] | None = None
    ) -> None:
        captured.dist.append((metric, value, tags))

    monkeypatch.setattr(metrics, "increment", fake_increment)
    monkeypatch.setattr(metrics, "distribution", fake_distribution)
    return captured


def test_record_latency_emits_duration_only(emits: Emits) -> None:
    with metrics.record_latency("op.duration_seconds", ["k:v"]):
        pass
    assert emits.dist == [
        ("op.duration_seconds", pytest.approx(0, abs=5), ["k:v", "status:completed"])
    ]
    assert emits.count == []


def test_record_latency_post_hoc_tags_and_elapsed(emits: Emits) -> None:
    with metrics.record_latency("op.duration_seconds", ["base:1"]) as op:
        op.tags.append("backend:s5cmd")
    assert emits.dist[0][2] == ["base:1", "backend:s5cmd", "status:completed"]
    assert op.elapsed >= 0.0


def test_record_latency_and_outcome_emits_both(emits: Emits) -> None:
    with metrics.record_latency_and_outcome(
        "op.duration_seconds", "op.completed", ["k:v"]
    ):
        pass
    assert emits.dist[0][2] == ["k:v", "status:completed"]
    assert emits.count == [("op.completed", ["k:v", "status:completed"])]


def test_status_override(emits: Emits) -> None:
    with metrics.record_latency_and_outcome(
        "op.duration_seconds", "op.completed"
    ) as op:
        op.status = "skipped"
    assert emits.count == [("op.completed", ["status:skipped"])]
    assert emits.dist[0][2] == ["status:skipped"]


def test_exception_forces_failed_and_reraises(emits: Emits) -> None:
    with pytest.raises(ValueError):
        with metrics.record_latency_and_outcome("op.duration_seconds", "op.completed"):
            raise ValueError("boom")
    assert emits.count == [("op.completed", ["status:failed"])]
    assert emits.dist[0][2] == ["status:failed"]


def test_cancellation_skips_emit_and_reraises(emits: Emits) -> None:
    with pytest.raises(asyncio.CancelledError):
        with metrics.record_latency_and_outcome("op.duration_seconds", "op.completed"):
            raise asyncio.CancelledError()
    assert emits.dist == []
    assert emits.count == []

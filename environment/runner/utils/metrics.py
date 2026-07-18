"""Datadog metric submission for the environment runner.

Mirrors the agents/grading runner metrics modules, kept intentionally small:
the environment runner only needs a handful of fire-and-forget emits (snapshot
size + peak memory) and no phase timing. Design goals, in priority order:

  1. NEVER crash the caller. A misconfigured client, serialization bug, or
     network outage cannot break a snapshot/upload. The flow runs identically
     with or without metrics.
  2. NEVER block the event loop. Serialization AND HTTP both happen on worker
     threads via ``asyncio.to_thread`` when a loop is running.
  3. Fire-and-forget. Failed submissions are dropped at debug level.

Gated on ``DATADOG_API_KEY`` alone (metrics submission needs only the API key,
unlike the logger which also requires the APP key). When the key is unset the
module no-ops rather than attempting unconfigured network I/O.
"""

from __future__ import annotations

import asyncio
import resource
import time
from collections.abc import Callable
from typing import Any

from datadog_api_client import Configuration, ThreadedApiClient
from datadog_api_client.v1.api.metrics_api import MetricsApi as MetricsApiV1
from datadog_api_client.v1.model.distribution_point import DistributionPoint
from datadog_api_client.v1.model.distribution_points_payload import (
    DistributionPointsPayload,
)
from datadog_api_client.v1.model.distribution_points_series import (
    DistributionPointsSeries,
)
from datadog_api_client.v2.api.metrics_api import MetricsApi
from datadog_api_client.v2.model.metric_intake_type import MetricIntakeType
from datadog_api_client.v2.model.metric_payload import MetricPayload
from datadog_api_client.v2.model.metric_point import MetricPoint
from datadog_api_client.v2.model.metric_series import MetricSeries
from loguru import logger

from .settings import get_settings

settings = get_settings()

_api_client: ThreadedApiClient | None = None
_counts_api: MetricsApi | None = None
_dists_api: MetricsApiV1 | None = None

# Wrapped so a bad key / broken Configuration cannot break import.
try:
    if settings.DATADOG_API_KEY:
        _config = Configuration()
        _config.api_key["apiKeyAuth"] = settings.DATADOG_API_KEY
        _api_client = ThreadedApiClient(_config)
        _counts_api = MetricsApi(api_client=_api_client)
        _dists_api = MetricsApiV1(api_client=_api_client)
except Exception as e:
    logger.debug(f"Datadog metrics disabled — client init failed: {e}")
    _api_client = None
    _counts_api = None
    _dists_api = None

BASE_TAGS = [f"env:{settings.ENV.value}", "service:rl-studio-environment-runner"]

# Strong refs for in-flight emits so they aren't GC'd mid-flight.
_inflight: set[asyncio.Task[None]] = set()


def snapshot_size_bucket(num_bytes: int) -> str:
    """Low-cardinality size bucket, matching the agents/grading runners so the
    snapshot's size metrics line up with trajectory and grading dashboards.
    Six edges aligned to system regime points - ~500m (Cloudflare /
    direct-upload), 5g (multipart threshold), 16g (env-sandbox memory cap),
    50g and 100g split the upper end."""
    gb = num_bytes / 1e9
    if gb < 0.5:
        return "lt500m"
    if gb < 5:
        return "500m-5g"
    if gb < 16:
        return "5-16g"
    if gb < 50:
        return "16-50g"
    if gb < 100:
        return "50-100g"
    return "100g_plus"


def peak_memory_bytes() -> int:
    """Best-effort peak memory of the environment sandbox, in bytes.

    Prefers the cgroup peak (cgroup v2 ``memory.peak``, then v1
    ``memory.max_usage_in_bytes``) so memory used by app-server subprocesses
    (the populate/DB-load OOM site) is counted, not just the runner process.
    Falls back to self+children ``ru_maxrss`` (KiB on Linux). Reads are of
    well-known read-only sysfs paths wrapped in error handling; returns 0 when
    nothing is readable."""
    for path in (
        "/sys/fs/cgroup/memory.peak",
        "/sys/fs/cgroup/memory/memory.max_usage_in_bytes",
    ):
        try:
            with open(path) as f:
                return int(f.read().strip())
        except (OSError, ValueError):
            continue
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        usage += resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        return usage * 1024  # ru_maxrss is KiB on Linux
    except Exception:
        return 0


def _all_tags(tags: list[str] | None) -> list[str]:
    return BASE_TAGS + (tags or [])


def _do_count(api: MetricsApi, metric: str, tags: list[str], value: int) -> None:
    series = MetricSeries(
        metric=metric,
        type=MetricIntakeType.COUNT,
        points=[MetricPoint(timestamp=int(time.time()), value=float(value))],
        tags=tags,
    )
    api.submit_metrics(body=MetricPayload(series=[series]))


def _do_dist(api: MetricsApiV1, metric: str, tags: list[str], v: float) -> None:
    point = DistributionPoint(value=[float(int(time.time())), [float(v)]])
    series = DistributionPointsSeries(metric=metric, points=[point], tags=tags)
    api.submit_distribution_points(body=DistributionPointsPayload(series=[series]))


async def _safely(fn: Callable[..., Any], *args: Any) -> None:
    """Run fn off the loop; swallow Exception (not BaseException) so nothing
    reaches the caller and cancellation/shutdown still propagate."""
    try:
        await asyncio.to_thread(fn, *args)
    except Exception as e:
        logger.debug(f"DD emit dropped: {e}")


def _fire_and_forget(fn: Callable[..., Any], *args: Any) -> None:
    """Schedule fn(*args) so the caller returns immediately. Never raises."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        try:
            fn(*args)
        except Exception as e:
            logger.debug(f"DD emit dropped (sync path): {e}")
        return

    try:
        task = loop.create_task(_safely(fn, *args))
        _inflight.add(task)
        task.add_done_callback(_inflight.discard)
    except Exception as e:
        logger.debug(f"DD emit task-schedule failed: {e}")


def increment(metric: str, tags: list[str] | None = None, value: int = 1) -> None:
    """Fire-and-forget COUNT submit. Returns immediately. Never raises."""
    if _counts_api is None:
        return
    try:
        _fire_and_forget(_do_count, _counts_api, metric, _all_tags(tags), value)
    except Exception as e:
        logger.debug(f"DD increment dropped: {e}")


def distribution(metric: str, value: float, tags: list[str] | None = None) -> None:
    """Fire-and-forget DISTRIBUTION submit. Returns immediately. Never raises."""
    if _dists_api is None:
        return
    try:
        _fire_and_forget(_do_dist, _dists_api, metric, _all_tags(tags), value)
    except Exception as e:
        logger.debug(f"DD distribution dropped: {e}")

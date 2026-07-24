"""Agent-side uploader for offloaded trajectory logs.

The agent writes its logs to S3 as batched gzip NDJSON *part* objects using a
server-minted presigned POST policy — it holds **no AWS credentials** and makes
no per-batch server call. This module is the pure HTTP + gzip primitive; the log
sink in ``api_logger.py`` calls it to persist log parts to S3.

Wire format is the shared contract with the server reader in
``rl-studio/server/packages/trajectory_logs/s3_store.py`` — keep the part/manifest
naming and the gzip-NDJSON encoding in lockstep with that module.

Durability & the raise-on-exhaustion contract
----------------------------------------------
"Exhaustion" = every retry of a *single* part/manifest POST failed (a persistent
S3/auth/network fault, not a transient blip). On exhaustion these functions
**raise** the last error. Raising is how the primitive *surfaces* the failure —
it is the deliberate opposite of a silent drop (an ``except Exception:`` that
swallows the batch, or an overflow that discards it), so a persistent write
failure is always visible rather than lost without a trace.

**Raising is NOT a signal to abort the run.** The caller (the log sink in
``api_logger.py``) MUST catch the exception, log it loudly + emit a failure
metric, and **keep the agent running** — logs are lossy telemetry, and no event
depends solely on this writer: each is independently in the Redis live-tail
stream. Recovery is out-of-band: the reconcile sweep rebuilds any missing parts
from Redis (within its 12h TTL) and seals the manifest. So the contract is "loud
+ recoverable," never "block or kill the run." Do not let a raise from here
propagate into agent business logic.
"""

from __future__ import annotations

import gzip
import json
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential,
)

from runner.utils.studio_http import is_retryable_studio_http_failure

# --- Wire contract (must match server s3_store.py) ----------------------------

TRAJECTORY_LOGS_ROOT_PREFIX = "trajectory-logs"
MANIFEST_FILENAME = "manifest.json"
PART_SUFFIX = ".ndjson.gz"

_UPLOAD_TIMEOUT_SECONDS = 30.0

# A presigned POST is a bearer credential ({"url", "fields"}); the agent supplies
# a `key` under the policy's prefix + the gzip file.
UploadPolicy = dict[str, Any]


def _trajectory_prefix(trajectory_id: str) -> str:
    return f"{TRAJECTORY_LOGS_ROOT_PREFIX}/{trajectory_id}"


def part_key(trajectory_id: str, part_number: int) -> str:
    """Key for the Nth part (1-based, zero-padded so lexical == numeric order)."""
    return f"{_trajectory_prefix(trajectory_id)}/part-{part_number:05d}{PART_SUFFIX}"


def manifest_key(trajectory_id: str) -> str:
    return f"{_trajectory_prefix(trajectory_id)}/{MANIFEST_FILENAME}"


def encode_part(records: list[dict[str, Any]]) -> bytes:
    """gzip-compressed NDJSON for one batch of log records (inverse of the
    server's ``decode_part``)."""
    ndjson = "\n".join(json.dumps(r, default=str) for r in records)
    return gzip.compress(ndjson.encode("utf-8"))


@retry(
    stop=stop_after_attempt(4),
    wait=wait_exponential(multiplier=1, min=1, max=15),
    retry=retry_if_exception(is_retryable_studio_http_failure),
    reraise=True,
)
async def _post_to_s3(
    policy: UploadPolicy, key: str, body: bytes, content_type: str
) -> None:
    """POST one object to S3 via the presigned policy. Retries transient
    failures; on exhaustion raises to surface the failure (see the module
    docstring's raise-on-exhaustion contract) — never a silent drop."""
    # Presigned POST: all form fields (with `key` overriding the policy default)
    # must precede the file part. httpx serializes `data` before `files`, so the
    # ordering S3 requires holds.
    fields = {**policy["fields"], "key": key}
    async with httpx.AsyncClient(timeout=_UPLOAD_TIMEOUT_SECONDS) as client:
        response = await client.post(
            policy["url"],
            data=fields,
            files={"file": (key.rsplit("/", 1)[-1], body, content_type)},
        )
        response.raise_for_status()


async def upload_log_part(
    policy: UploadPolicy,
    trajectory_id: str,
    part_number: int,
    records: list[dict[str, Any]],
) -> str:
    """Upload one batch as ``part-NNNNN.ndjson.gz``. Returns the written key.

    Raises on retry exhaustion to surface the failure (see the module docstring):
    the caller must catch, log loudly + emit a metric, and keep the run going —
    never treat the raise as a reason to drop the log silently or abort the agent.
    The reconcile sweep recovers the missed part from Redis.
    """
    key = part_key(trajectory_id, part_number)
    await _post_to_s3(policy, key, encode_part(records), "application/gzip")
    return key


async def seal_manifest(
    policy: UploadPolicy, trajectory_id: str, part_count: int
) -> None:
    """Write the sealed ``manifest.json`` (completeness marker) at finalization.
    Schema mirrors ``s3_store.TrajectoryLogsManifest``."""
    body = json.dumps({"sealed": True, "part_count": part_count}).encode("utf-8")
    await _post_to_s3(policy, manifest_key(trajectory_id), body, "application/json")

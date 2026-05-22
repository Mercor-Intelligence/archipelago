"""Playwright trace parser helper for BUA verification.

Parses Playwright trace files from snapshot zips to extract:
- Actions (goto, click, fill, type) with timing and errors
- Console messages (log, warning, error)
- Network requests/responses
- DOM snapshots with form values
- Screenshots

Trace structure:
    final_snapshot.zip
    └── .apps_data/playwright/traces/
        ├── trace-1709123456.zip
        │   ├── trace.trace      # JSONL: actions, console, DOM snapshots
        │   ├── trace.network    # JSONL: network requests
        │   └── screenshots/
"""

import io
import json
import zipfile
from typing import Any

from loguru import logger

from runner.helpers.trace_models import (
    ConsoleMessage,
    FrameSnapshot,
    NetworkRequest,
    PlaywrightTraceData,
    TraceAction,
)
from runner.helpers.trace_utils import find_nested_zips, load_screenshots_from_dir
from runner.models import AgentTrajectoryOutput

TRACES_GLOB = ".apps_data/playwright/traces/*.zip"


async def playwright_trace_parser_helper(
    initial_snapshot_bytes: io.BytesIO,
    final_snapshot_bytes: io.BytesIO,
    trajectory: AgentTrajectoryOutput,
) -> dict[str, Any]:
    """Parse Playwright trace files from snapshot."""
    final_snapshot_bytes.seek(0)

    if not zipfile.is_zipfile(final_snapshot_bytes):
        logger.warning("[PLAYWRIGHT_TRACE] Invalid zip file")
        final_snapshot_bytes.seek(0)
        return PlaywrightTraceData(parse_errors=["Invalid zip file"]).model_dump()

    final_snapshot_bytes.seek(0)
    result = PlaywrightTraceData()

    try:
        with zipfile.ZipFile(final_snapshot_bytes, "r") as outer_zip:
            trace_zips = find_nested_zips(outer_zip, TRACES_GLOB)

            if not trace_zips:
                logger.info("[PLAYWRIGHT_TRACE] No trace files found")
                final_snapshot_bytes.seek(0)
                return result.model_dump()

            logger.info(f"[PLAYWRIGHT_TRACE] Found {len(trace_zips)} trace zip(s)")

            for trace_zip_path in trace_zips:
                try:
                    _parse_trace_zip(outer_zip, trace_zip_path, result)
                    result.trace_files_parsed += 1
                except Exception as e:
                    error_msg = f"Failed to parse {trace_zip_path}: {e}"
                    logger.warning(f"[PLAYWRIGHT_TRACE] {error_msg}")
                    result.parse_errors.append(error_msg)

        # Sort all events by wall_time
        result.actions.sort(key=lambda a: a.wall_time)
        result.console_messages.sort(key=lambda m: m.wall_time)
        result.network_requests.sort(key=lambda r: r.wall_time)
        result.frame_snapshots.sort(key=lambda s: s.wall_time)

        # Calculate total duration
        if result.actions:
            first_time = result.actions[0].wall_time
            last_time = result.actions[-1].wall_time + (
                result.actions[-1].duration_ms / 1000.0
            )
            result.total_duration_seconds = last_time - first_time

        logger.info(
            f"[PLAYWRIGHT_TRACE] Parsed {len(result.actions)} actions, "
            f"{len(result.console_messages)} console, "
            f"{len(result.network_requests)} network, "
            f"{len(result.frame_snapshots)} snapshots"
        )

    except Exception as e:
        error_msg = f"Unexpected error: {e}"
        logger.error(f"[PLAYWRIGHT_TRACE] {error_msg}")
        result.parse_errors.append(error_msg)

    final_snapshot_bytes.seek(0)
    return result.model_dump()


def _parse_trace_zip(
    outer_zip: zipfile.ZipFile,
    trace_zip_path: str,
    result: PlaywrightTraceData,
) -> None:
    """Parse a single trace zip file."""
    trace_zip_bytes = io.BytesIO(outer_zip.read(trace_zip_path))

    with zipfile.ZipFile(trace_zip_bytes, "r") as trace_zip:
        for name in trace_zip.namelist():
            if name.endswith(".trace"):
                _parse_trace_file(trace_zip, name, result)
            elif name.endswith(".network"):
                _parse_network_file(trace_zip, name, result)

        screenshots = load_screenshots_from_dir(trace_zip, "screenshots")
        for filename, content in screenshots:
            result.screenshots.append(content)
            result.screenshot_paths.append(filename)


def _parse_trace_file(
    trace_zip: zipfile.ZipFile,
    file_path: str,
    result: PlaywrightTraceData,
) -> None:
    """Parse a .trace JSONL file."""
    raw_bytes = trace_zip.read(file_path)
    content = raw_bytes.decode("utf-8", errors="replace")
    # Log if encoding replacement occurred (corrupted bytes become \ufffd)
    if "\ufffd" in content:
        logger.warning(
            f"[PLAYWRIGHT_TRACE] Encoding errors in {file_path}, some characters replaced"
        )
    before_events: dict[str, dict[str, Any]] = {}

    for line in content.strip().split("\n"):
        if not line.strip():
            continue

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        # Skip non-dict JSON values (e.g., null, numbers, arrays)
        if not isinstance(event, dict):
            continue

        event_type = event.get("type")

        if event_type == "before":
            call_id = event.get("callId")
            if call_id is not None:
                before_events[call_id] = event

        elif event_type == "after":
            call_id = event.get("callId")
            if call_id is not None and call_id in before_events:
                before = before_events.pop(call_id)
                try:
                    action = _create_action(before, event)
                    if action:
                        result.actions.append(action)
                except Exception as e:
                    result.parse_errors.append(f"Error creating action: {e}")

        elif event_type == "console":
            try:
                message = _create_console_message(event)
                if message:
                    result.console_messages.append(message)
            except Exception as e:
                result.parse_errors.append(f"Error creating console message: {e}")

        elif event_type == "frame-snapshot":
            try:
                snapshot = _create_frame_snapshot(event)
                if snapshot:
                    result.frame_snapshots.append(snapshot)
            except Exception as e:
                result.parse_errors.append(f"Error creating frame snapshot: {e}")

    # Handle unmatched before events (actions that started but never completed)
    # These get duration_ms=0 as per spec
    for call_id, before in before_events.items():
        try:
            logger.debug(
                f"[PLAYWRIGHT_TRACE] Action {before.get('method')} (callId={call_id}) "
                "has no matching after event, using duration_ms=0"
            )
            action = _create_action(before, None)
            if action:
                result.actions.append(action)
        except Exception as e:
            result.parse_errors.append(
                f"Error creating action from unmatched before: {e}"
            )


def _parse_network_file(
    trace_zip: zipfile.ZipFile,
    file_path: str,
    result: PlaywrightTraceData,
) -> None:
    """Parse a .network JSONL file."""
    raw_bytes = trace_zip.read(file_path)
    content = raw_bytes.decode("utf-8", errors="replace")
    # Log if encoding replacement occurred
    if "\ufffd" in content:
        logger.warning(
            f"[PLAYWRIGHT_TRACE] Encoding errors in {file_path}, some characters replaced"
        )
    pending_requests: dict[str, dict[str, Any]] = {}

    for line in content.strip().split("\n"):
        if not line.strip():
            continue

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        # Skip non-dict JSON values (e.g., null, numbers, arrays)
        if not isinstance(event, dict):
            continue

        event_type = event.get("type")

        if event_type == "request":
            request_id = event.get("requestId")
            if request_id:
                pending_requests[request_id] = event

        elif event_type == "response":
            request_id = event.get("requestId")
            if request_id and request_id in pending_requests:
                request = pending_requests.pop(request_id)
                result.network_requests.append(
                    NetworkRequest(
                        request_id=request_id,
                        url=request.get("url", ""),
                        method=request.get("method", "GET"),
                        status=event.get("status"),
                        wall_time=request.get("wallTime", 0.0),
                    )
                )

    # Handle unmatched requests
    for request_id, request in pending_requests.items():
        result.network_requests.append(
            NetworkRequest(
                request_id=request_id,
                url=request.get("url", ""),
                method=request.get("method", "GET"),
                status=None,
                wall_time=request.get("wallTime", 0.0),
            )
        )


def _create_action(
    before: dict[str, Any], after: dict[str, Any] | None
) -> TraceAction | None:
    """Create a TraceAction from before/after event pair."""
    method = before.get("method")
    if not method:
        return None

    params = before.get("params", {})
    wall_time = before.get("wallTime", 0.0)

    duration_ms = 0.0
    error: str | None = None
    if after:
        after_time = after.get("wallTime", wall_time)
        duration_ms = (after_time - wall_time) * 1000.0
        # Playwright error is a dict with message/name/stack keys, not a string
        raw_error = after.get("error")
        if raw_error:
            if isinstance(raw_error, dict):
                error = raw_error.get("message") or str(raw_error)
            else:
                error = str(raw_error)

    return TraceAction(
        call_id=before.get("callId", ""),
        method=method,
        selector=params.get("selector"),
        params=params,
        wall_time=wall_time,
        duration_ms=duration_ms,
        error=error,
    )


def _create_console_message(event: dict[str, Any]) -> ConsoleMessage | None:
    """Create a ConsoleMessage from a console event."""
    text = event.get("text")
    if text is None:
        return None

    return ConsoleMessage(
        message_type=event.get("messageType", "log"),
        text=str(text),
        wall_time=event.get("wallTime", 0.0),
    )


def _create_frame_snapshot(event: dict[str, Any]) -> FrameSnapshot | None:
    """Create a FrameSnapshot from a frame-snapshot event."""
    url = event.get("url")
    if url is None:
        return None

    return FrameSnapshot(
        wall_time=event.get("wallTime", 0.0),
        url=url,
        dom_tree=event.get("snapshot", []),
    )

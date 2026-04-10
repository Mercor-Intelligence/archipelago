"""Models for Playwright trace parsing and browser state extraction.

These models support two helpers:
- playwright_trace_parser: Low-level extraction of raw trace events
- browser_state: High-level browser state with multi-source fallback
"""

from typing import Any, Literal

from pydantic import BaseModel, Field


class TraceAction(BaseModel):
    """Single Playwright action (goto, click, fill, etc.)."""

    call_id: str  # Playwright uses "call@N" format (e.g., "call@1")
    method: str  # "goto", "click", "fill", "type"
    selector: str | None = None  # CSS selector for element actions
    params: dict[str, Any] = Field(default_factory=dict)
    wall_time: float  # Unix timestamp
    duration_ms: float = 0.0  # Time between before/after
    error: str | None = None  # Error message if action failed


class ConsoleMessage(BaseModel):
    """Browser console message."""

    message_type: str  # "log", "warning", "error"
    text: str
    wall_time: float


class NetworkRequest(BaseModel):
    """Network request/response pair."""

    request_id: str
    url: str
    method: str
    status: int | None = None
    wall_time: float


class FrameSnapshot(BaseModel):
    """DOM snapshot at a point in time."""

    wall_time: float
    url: str
    dom_tree: list[Any] = Field(default_factory=list)  # Playwright's nested list format


class PlaywrightTraceData(BaseModel):
    """Complete parsed trace data from Playwright traces."""

    actions: list[TraceAction] = Field(default_factory=list)
    console_messages: list[ConsoleMessage] = Field(default_factory=list)
    network_requests: list[NetworkRequest] = Field(default_factory=list)
    frame_snapshots: list[FrameSnapshot] = Field(default_factory=list)
    screenshots: list[bytes] = Field(default_factory=list)  # Raw PNG bytes
    screenshot_paths: list[str] = Field(default_factory=list)
    total_duration_seconds: float = 0.0
    trace_files_parsed: int = 0
    parse_errors: list[str] = Field(default_factory=list)


class BrowserState(BaseModel):
    """Final browser state for verification.

    Used by verifiers to check URL, form values, visible text, etc.
    """

    url: str | None = None
    title: str | None = None
    form_values: dict[str, str] = Field(default_factory=dict)  # selector -> value
    visible_text: list[str] = Field(default_factory=list)
    cookies: dict[str, str] = Field(default_factory=dict)
    local_storage: dict[str, str] = Field(default_factory=dict)
    screenshots: list[str] = Field(default_factory=list)  # base64 data URIs
    screenshot_paths: list[str] = Field(default_factory=list)
    source: Literal[
        "browser_state_dir",  # From .apps_data/browser/
        "playwright_traces",  # Extracted from traces
        "screenshots_only",  # Fallback: just screenshots
        "empty",  # No data found
    ] = "empty"
    fallback_errors: list[str] = Field(
        default_factory=list,
        description="Errors encountered during fallback chain (for debugging)",
    )

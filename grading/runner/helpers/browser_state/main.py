"""Browser state helper for BUA verification.

Extracts final browser state with a 3-tier fallback chain:

Priority 1: .apps_data/browser/
├── state.json     → url, title, cookies, localStorage
├── forms.json     → form_values
└── screenshots/   → screenshots

Priority 2: Playwright traces (via playwright_trace_parser)
├── Last frame-snapshot → url, form_values (from __playwright_value__)
├── Last goto action    → url (fallback)
└── screenshots/        → screenshots

Priority 3: filesystem/screenshots/
└── *.png files → screenshots only (url/forms unavailable)
"""

import io
import json
import zipfile
from typing import Any

from loguru import logger

from runner.helpers.playwright_trace_parser import playwright_trace_parser_helper
from runner.helpers.trace_models import BrowserState, PlaywrightTraceData
from runner.helpers.trace_utils import (
    extract_form_values,
    load_screenshots_from_dir,
    screenshots_to_data_uris,
)
from runner.models import AgentTrajectoryOutput

BROWSER_STATE_DIR = ".apps_data/browser"
SCREENSHOTS_FALLBACK_DIR = "filesystem/screenshots"


async def browser_state_helper(
    initial_snapshot_bytes: io.BytesIO,
    final_snapshot_bytes: io.BytesIO,
    trajectory: AgentTrajectoryOutput,
) -> dict[str, Any]:
    """Extract final browser state with fallback chain."""
    final_snapshot_bytes.seek(0)
    fallback_errors: list[str] = []

    if not zipfile.is_zipfile(final_snapshot_bytes):
        logger.warning("[BROWSER_STATE] Invalid zip file")
        final_snapshot_bytes.seek(0)
        return BrowserState(
            source="empty", fallback_errors=["Invalid zip file"]
        ).model_dump()

    # Priority 1: Try .apps_data/browser/
    try:
        final_snapshot_bytes.seek(0)
        with zipfile.ZipFile(final_snapshot_bytes, "r") as zip_file:
            browser_state = _try_browser_state_dir(zip_file)
            if browser_state:
                logger.info("[BROWSER_STATE] Using browser_state_dir source")
                final_snapshot_bytes.seek(0)
                return browser_state.model_dump()
    except Exception as e:
        error_msg = f"Priority 1 (browser_state_dir) failed: {e}"
        logger.warning(f"[BROWSER_STATE] {error_msg}")
        fallback_errors.append(error_msg)

    # Priority 2: Try Playwright traces
    try:
        final_snapshot_bytes.seek(0)
        trace_data_dict = await playwright_trace_parser_helper(
            initial_snapshot_bytes, final_snapshot_bytes, trajectory
        )
        trace_data = PlaywrightTraceData(**trace_data_dict)

        if trace_data.frame_snapshots or trace_data.actions or trace_data.screenshots:
            browser_state = _browser_state_from_traces(trace_data)
            if (
                browser_state.url
                or browser_state.form_values
                or browser_state.screenshots
            ):
                logger.info("[BROWSER_STATE] Using playwright_traces source")
                final_snapshot_bytes.seek(0)
                return browser_state.model_dump()
    except Exception as e:
        error_msg = f"Priority 2 (playwright_traces) failed: {e}"
        logger.warning(f"[BROWSER_STATE] {error_msg}")
        fallback_errors.append(error_msg)

    # Priority 3: Screenshots-only fallback
    try:
        final_snapshot_bytes.seek(0)
        with zipfile.ZipFile(final_snapshot_bytes, "r") as zip_file:
            browser_state = _screenshots_only_fallback(zip_file)
            if browser_state.screenshots:
                logger.info("[BROWSER_STATE] Using screenshots_only source")
                final_snapshot_bytes.seek(0)
                return browser_state.model_dump()
    except Exception as e:
        error_msg = f"Priority 3 (screenshots_only) failed: {e}"
        logger.warning(f"[BROWSER_STATE] {error_msg}")
        fallback_errors.append(error_msg)

    final_snapshot_bytes.seek(0)
    logger.info("[BROWSER_STATE] No browser state found, returning empty")
    return BrowserState(source="empty", fallback_errors=fallback_errors).model_dump()


def _try_browser_state_dir(zip_file: zipfile.ZipFile) -> BrowserState | None:
    """Try to extract browser state from .apps_data/browser/ directory."""
    state_json_paths = [
        f"{BROWSER_STATE_DIR}/state.json",
        f"snapshot/{BROWSER_STATE_DIR}/state.json",
    ]

    state_data: dict[str, Any] = {}
    for path in state_json_paths:
        if path in zip_file.namelist():
            try:
                content = zip_file.read(path).decode("utf-8")
                state_data = json.loads(content)
                break
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

    if not state_data:
        return None

    # Load forms.json if present
    form_values: dict[str, str] = {}
    forms_paths = [
        f"{BROWSER_STATE_DIR}/forms.json",
        f"snapshot/{BROWSER_STATE_DIR}/forms.json",
    ]
    for path in forms_paths:
        if path in zip_file.namelist():
            try:
                content = zip_file.read(path).decode("utf-8")
                form_values = json.loads(content)
                break
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

    # Load screenshots
    screenshot_paths_to_try = [
        f"{BROWSER_STATE_DIR}/screenshots",
        f"snapshot/{BROWSER_STATE_DIR}/screenshots",
    ]
    screenshots: list[bytes] = []
    screenshot_paths: list[str] = []
    for dir_path in screenshot_paths_to_try:
        loaded = load_screenshots_from_dir(zip_file, dir_path)
        if loaded:
            for filename, content in loaded:
                screenshots.append(content)
                screenshot_paths.append(filename)
            break

    return BrowserState(
        url=state_data.get("url"),
        title=state_data.get("title"),
        form_values=form_values,
        cookies=state_data.get("cookies", {}),
        local_storage=state_data.get("localStorage", {}),
        screenshots=screenshots_to_data_uris(screenshots),
        screenshot_paths=screenshot_paths,
        source="browser_state_dir",
    )


def _browser_state_from_traces(trace_data: PlaywrightTraceData) -> BrowserState:
    """Extract browser state from Playwright trace data."""
    url: str | None = None
    form_values: dict[str, str] = {}

    # Try to get URL and form values from last frame snapshot
    if trace_data.frame_snapshots:
        last_snapshot = trace_data.frame_snapshots[-1]
        url = last_snapshot.url
        form_values = extract_form_values(last_snapshot.dom_tree)

    # Fallback: get URL from last goto action
    if not url and trace_data.actions:
        for action in reversed(trace_data.actions):
            if action.method == "goto" and "url" in action.params:
                url = action.params["url"]
                break

    return BrowserState(
        url=url,
        form_values=form_values,
        screenshots=screenshots_to_data_uris(trace_data.screenshots),
        screenshot_paths=trace_data.screenshot_paths,
        source="playwright_traces",
    )


def _screenshots_only_fallback(zip_file: zipfile.ZipFile) -> BrowserState:
    """Fallback: extract screenshots from filesystem/screenshots/."""
    paths_to_try = [
        SCREENSHOTS_FALLBACK_DIR,
        f"snapshot/{SCREENSHOTS_FALLBACK_DIR}",
        "screenshots",
        "snapshot/screenshots",
    ]

    for dir_path in paths_to_try:
        loaded = load_screenshots_from_dir(zip_file, dir_path)
        if loaded:
            screenshots = [content for _, content in loaded]
            screenshot_paths = [filename for filename, _ in loaded]
            return BrowserState(
                screenshots=screenshots_to_data_uris(screenshots),
                screenshot_paths=screenshot_paths,
                source="screenshots_only",
            )

    return BrowserState(source="empty")

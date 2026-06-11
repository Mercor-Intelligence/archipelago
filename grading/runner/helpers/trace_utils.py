"""Shared utilities for trace parsing and browser state extraction."""

import base64
import fnmatch
from typing import Any
from zipfile import ZipFile

from loguru import logger


def extract_form_values(dom_tree: list[Any]) -> dict[str, str]:
    """Walk DOM tree and extract __playwright_value__ attributes.

    Playwright's DOM snapshots encode form values in __playwright_value__:
        ["input", {"id": "email", "__playwright_value__": "user@test.com"}, []]

    Handles both a single root node [tag, attrs, children] and a list of
    top-level nodes [[node1], [node2], ...] (e.g. sibling roots).

    Returns:
        Dict mapping selectors to values: {"#email": "user@test.com"}
    """
    form_values: dict[str, str] = {}
    if not dom_tree:
        return form_values
    # If first element is a list, dom_tree is a list of sibling nodes
    if isinstance(dom_tree[0], list):
        for node in dom_tree:
            _walk_dom_for_form_values(node, form_values)
    else:
        _walk_dom_for_form_values(dom_tree, form_values)
    return form_values


def _walk_dom_for_form_values(node: Any, form_values: dict[str, str]) -> None:
    """Recursively walk DOM tree to extract form values."""
    if not isinstance(node, list) or len(node) < 2:
        return

    tag_name = node[0] if isinstance(node[0], str) else None
    attrs = node[1] if len(node) > 1 and isinstance(node[1], dict) else {}
    children = node[2] if len(node) > 2 and isinstance(node[2], list) else []

    # Process current node if it has a valid tag name
    if tag_name:
        playwright_value = attrs.get("__playwright_value__")
        if playwright_value is not None:
            selector = _build_selector(tag_name, attrs, form_values)
            if selector:
                form_values[selector] = str(playwright_value)

    # Always recurse into children, even for unrecognized nodes
    for child in children:
        _walk_dom_for_form_values(child, form_values)


def _build_selector(
    tag_name: str, attrs: dict[str, Any], existing_selectors: dict[str, str]
) -> str | None:
    """Build a CSS selector from tag name and attributes.

    Args:
        tag_name: The HTML tag name.
        attrs: The element's attributes.
        existing_selectors: Already-collected selectors, used to avoid collisions.

    Returns:
        A unique selector string, or None if the element can't be uniquely identified.
    """
    # Prefer id (guaranteed unique in valid HTML)
    if "id" in attrs:
        return f"#{attrs['id']}"

    # Fall back to name attribute
    if "name" in attrs:
        return f"[name='{attrs['name']}']"

    # For inputs without id/name, use type with index to avoid collisions
    if tag_name == "input" and "type" in attrs:
        input_type = attrs["type"]
        base_selector = f"input[type='{input_type}']"

        # Find a unique selector by appending a counter suffix
        # Note: We use a simple counter scheme instead of CSS :nth-of-type()
        # because nth-of-type is position-based in the DOM, not a sequence counter
        if base_selector not in existing_selectors:
            return base_selector

        # Collision detected - append counter to make unique
        index = 2
        while f"{base_selector}--{index}" in existing_selectors:
            index += 1
        return f"{base_selector}--{index}"

    return None


def load_screenshots_from_dir(
    zip_file: ZipFile, dir_path: str
) -> list[tuple[str, bytes]]:
    """Load screenshots from a directory in a zip.

    Returns:
        List of (filename, bytes) pairs for PNG files found.
    """
    screenshots: list[tuple[str, bytes]] = []
    dir_path = dir_path.rstrip("/") + "/"

    for name in zip_file.namelist():
        if not name.startswith(dir_path):
            continue
        if not name.lower().endswith(".png"):
            continue
        if name.endswith("/"):
            continue

        try:
            content = zip_file.read(name)
            filename = name.split("/")[-1]
            screenshots.append((filename, content))
        except Exception as e:
            logger.debug(f"Failed to read screenshot {name}: {e}")
            continue

    return screenshots


def find_nested_zips(zip_file: ZipFile, glob_pattern: str) -> list[str]:
    """Find zip files within a zip matching a glob pattern.

    Deduplicates by basename to avoid processing the same zip file found
    under different paths (e.g., snapshot/traces/foo.zip and traces/foo.zip).
    """
    matching: list[str] = []
    seen_basenames: set[str] = set()

    for name in zip_file.namelist():
        if name.endswith("/"):
            continue
        if not name.lower().endswith(".zip"):
            continue

        # Deduplicate by basename
        basename = name.split("/")[-1]
        if basename in seen_basenames:
            continue

        parts = name.split("/", 1)
        normalized = parts[1] if len(parts) > 1 else name

        if matches_glob(normalized, glob_pattern) or matches_glob(name, glob_pattern):
            seen_basenames.add(basename)
            matching.append(name)

    return matching


def matches_glob(path: str, pattern: str) -> bool:
    """Check if a path matches a glob pattern, supporting multiple **.

    Handles patterns like:
    - "filesystem/**/*.kicad_sch" - matches any .kicad_sch under filesystem/
    - "*.sql" - matches any .sql file at any level
    - ".apps_data/**/*.sql" - matches .sql files under .apps_data/
    - "a/**/b/**/*.txt" - matches .txt files in any b/ subdir under a/

    Args:
        path: The file path to check.
        pattern: The glob pattern to match against.

    Returns:
        True if the path matches the pattern.
    """
    # Normalize paths - remove leading "./" prefix
    path = path.removeprefix("./")
    pattern = pattern.removeprefix("./")

    if "**" not in pattern:
        # Try full path first, then basename for patterns without directory prefix
        # This allows patterns like "data*.sql" to match "subdir/data_foo.sql"
        return fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(
            path.split("/")[-1], pattern
        )

    # Split pattern on ** and match recursively
    return _match_double_star(path.split("/"), pattern.split("**"))


def _match_double_star(path_parts: list[str], pattern_parts: list[str]) -> bool:
    """Recursively match path against pattern split on **.

    Args:
        path_parts: Path components (e.g., ["a", "b", "c.txt"])
        pattern_parts: Pattern segments split on ** (e.g., ["a/", "/b/", "/*.txt"])

    Returns:
        True if the path matches the pattern.
    """
    if not pattern_parts:
        return not path_parts

    first_pattern = pattern_parts[0].strip("/")
    rest_patterns = pattern_parts[1:]

    if not rest_patterns:
        # Last segment - match remaining path against it
        if not first_pattern:
            return True  # Pattern ends with **, matches anything
        remaining = "/".join(path_parts)
        return fnmatch.fnmatch(remaining, first_pattern) or fnmatch.fnmatch(
            remaining, f"*/{first_pattern}"
        )

    if not first_pattern:
        # Pattern starts with ** or has consecutive **
        # Try matching rest at every position
        for i in range(len(path_parts) + 1):
            if _match_double_star(path_parts[i:], rest_patterns):
                return True
        return False

    # Match first_pattern against prefix of path
    first_parts = first_pattern.split("/")
    if len(first_parts) > len(path_parts):
        return False

    for i, fp in enumerate(first_parts):
        if not fnmatch.fnmatch(path_parts[i], fp):
            return False

    # Recurse with ** matching zero or more directories
    remaining_path = path_parts[len(first_parts) :]
    for i in range(len(remaining_path) + 1):
        if _match_double_star(remaining_path[i:], rest_patterns):
            return True
    return False


def screenshots_to_data_uris(screenshots: list[bytes]) -> list[str]:
    """Convert raw PNG bytes to base64 data URIs."""
    return [
        f"data:image/png;base64,{base64.b64encode(png_bytes).decode('ascii')}"
        for png_bytes in screenshots
    ]

#!/usr/bin/env python3
"""Patch a TOML file (typically arco.toml) into one of its declared shapes.

Generic line-toggling utility for repos that maintain multiple parallel
shapes of the same TOML file — most commonly arco.toml swapping between
``main`` and ``dev`` deployments for Studio, but the same machinery
works for any pair (or set) of named shapes the project declares.

Driven entirely by a sibling ``arco-patcher.toml`` config: no
application-specific constants, no fixed shape names, no fixed branch
labels.

Usage
-----
::

    # The positional arg is whatever shape name the config declares.
    # For the typical Foundry-* main/dev convention:
    python -m mcp_scripts.patch_arco_for_branch main
    python -m mcp_scripts.patch_arco_for_branch dev

    # In CI: pass github.base_ref so a PR into main produces main shape,
    # a PR into dev produces dev shape, etc.
    python -m mcp_scripts.patch_arco_for_branch "${{ github.base_ref }}"

    # Override file paths
    python -m mcp_scripts.patch_arco_for_branch dev \\
        --file arco.toml \\
        --config arco-patcher.toml

Exit codes
----------
0   Patched (or already in target shape — idempotent no-op).
1   ``--file`` not found.
2   ``--config`` not found, malformed, or requested shape not declared.
3   A configured swap key's variants don't appear in the file — drift
    between arco.toml and arco-patcher.toml.

Config file (``arco-patcher.toml``, repo root, next to arco.toml)
----------------------------------------------------------------
::

    [arco_patcher]
    # Required: the set of shapes this file can be patched into.
    # Any number of shapes; any names you like.
    shapes = ["main", "dev"]

    # Required: the `name = "..."` line that identifies the service,
    # one value per shape declared above.
    [arco_patcher.name]
    main = "Foundry-zoho"
    dev  = "Foundry-zoho-dev"

    # Optional: any number of line-toggle swaps.  Each entry needs
    # `key` plus a value for the shapes it should appear active in.
    # Shapes omitted from a swap entry get the commented form on those
    # shapes (so REPO_BRANCH below is active on dev, commented on main
    # because there's no `main = ...` line).
    [[arco_patcher.swap]]
    key = "REPO_BRANCH"
    dev = "dev"

    [[arco_patcher.swap]]
    key  = "STATE_LOCATION"
    main = "/.apps_data/foundry_zoho"
    dev  = "/.apps_data/foundry_zoho_dev"

    [[arco_patcher.swap]]
    key  = "DATABASE_URL"
    main = "sqlite:////.apps_data/foundry_zoho/studio.db"
    dev  = "sqlite:////.apps_data/foundry_zoho_dev/studio.db"

For each swap entry the patcher recognises the active and commented
forms of every shape's value and rewrites them to the target form for
the requested shape:

* the shape being patched to → that value's line becomes **active**
* every other shape's value → that line becomes **commented**

Shapes that don't declare a value for a given swap key are simply
absent on that shape (e.g. ``REPO_BRANCH`` above on main).
"""

from __future__ import annotations

import argparse
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

_RESERVED_SWAP_KEYS = frozenset({"key"})


@dataclass(frozen=True)
class Swap:
    """One key whose line form the patcher toggles between shapes.

    *values* maps shape name to the string the line should carry when
    that shape is the target.  Shapes absent from this map don't get a
    line at all (neither active nor commented) on the target side.
    """

    key: str
    values: dict[str, str]

    def _active(self, value: str) -> str:
        return f'{self.key} = "{value}"'

    def _commented(self, value: str) -> str:
        return f'# {self.key} = "{value}"'

    def variants(self) -> set[str]:
        """All possible TOML line forms (stripped) for this swap."""
        vs: set[str] = set()
        for value in self.values.values():
            vs.add(self._active(value))
            vs.add(self._commented(value))
        return vs

    def target_map(self, shape: str) -> dict[str, str]:
        """Map every recognised variant to its replacement on *shape*.

        Returns ``{stripped_input_line: replacement_line}``.  Both
        active and commented input forms map to the right target so
        the function is idempotent (patching twice is a no-op).

        On the target shape, that shape's value becomes the active
        form; every other shape's value becomes the commented form.
        If *shape* isn't in :attr:`values`, all known variants are
        commented out — the key simply doesn't have an active form on
        this shape.
        """
        mapping: dict[str, str] = {}
        for s, value in self.values.items():
            if s == shape:
                target = self._active(value)
            else:
                target = self._commented(value)
            mapping[self._active(value)] = target
            mapping[self._commented(value)] = target
        return mapping


@dataclass
class Config:
    """Parsed ``arco-patcher.toml``."""

    shapes: list[str]
    names: dict[str, str]
    swaps: list[Swap] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


class ConfigError(Exception):
    """Raised by :func:`load_config` for missing or malformed configs.

    The CLI catches this and surfaces ``message`` on stderr before
    returning exit code 2.
    """

    def __init__(self, path: Path, message: str) -> None:
        super().__init__(f"{path}: {message}")
        self.path = path
        self.message = message


def load_config(path: Path) -> Config:
    """Load ``arco-patcher.toml``.

    Raises :class:`ConfigError` if the file is missing or malformed.

    Required keys:
    * ``[arco_patcher].shapes`` — list of shape names (≥ 1).
    * ``[arco_patcher.name]`` — one string per shape.

    Optional:
    * Any number of ``[[arco_patcher.swap]]`` entries, each requiring a
      ``key`` and at least one ``<shape>`` key whose value is the line
      string for that shape.  Unknown shape names → error.
    """
    if not path.is_file():
        raise ConfigError(path, "not found")
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(path, f"not valid TOML: {exc}") from exc

    section = data.get("arco_patcher")
    if not isinstance(section, dict):
        raise ConfigError(path, "missing [arco_patcher] section")

    raw_shapes = section.get("shapes")
    if not isinstance(raw_shapes, list) or not raw_shapes:
        raise ConfigError(path, "missing or empty 'shapes' (list of shape names)")
    shapes: list[str] = []
    for i, s in enumerate(raw_shapes):
        if not isinstance(s, str) or not s:
            raise ConfigError(path, f"shapes[{i}] must be a non-empty string")
        shapes.append(s)
    if len(set(shapes)) != len(shapes):
        raise ConfigError(path, "duplicate entries in 'shapes'")

    raw_names = section.get("name")
    if not isinstance(raw_names, dict):
        raise ConfigError(path, "missing [arco_patcher.name] table")
    names: dict[str, str] = {}
    for shape in shapes:
        value = raw_names.get(shape)
        if not isinstance(value, str) or not value:
            raise ConfigError(path, f"missing or empty name for shape '{shape}'")
        names[shape] = value
    # Surface unexpected name entries so typos don't silently lose effect.
    extra_names = set(raw_names) - set(shapes)
    if extra_names:
        raise ConfigError(
            path,
            f"[arco_patcher.name] references undeclared shape(s): {sorted(extra_names)}",
        )

    raw_swaps = section.get("swap", [])
    if not isinstance(raw_swaps, list):
        raise ConfigError(path, "[[arco_patcher.swap]] must be an array of tables")

    swaps: list[Swap] = []
    shapes_set = set(shapes)
    for i, entry in enumerate(raw_swaps):
        if not isinstance(entry, dict):
            raise ConfigError(path, f"swap entry #{i + 1} is not a table")
        key = entry.get("key")
        if not isinstance(key, str) or not key:
            raise ConfigError(path, f"swap entry #{i + 1} missing 'key'")
        values: dict[str, str] = {}
        for shape_name, value in entry.items():
            if shape_name in _RESERVED_SWAP_KEYS:
                continue
            if shape_name not in shapes_set:
                raise ConfigError(
                    path,
                    f"swap '{key}' references undeclared shape '{shape_name}' "
                    f"(declared shapes: {shapes})",
                )
            if not isinstance(value, str):
                raise ConfigError(
                    path, f"swap '{key}' value for shape '{shape_name}' must be a string"
                )
            values[shape_name] = value
        if not values:
            raise ConfigError(
                path,
                f"swap '{key}' must declare a value for at least one shape (any of {shapes})",
            )
        swaps.append(Swap(key=key, values=values))

    return Config(shapes=shapes, names=names, swaps=swaps)


# ---------------------------------------------------------------------------
# Patching
# ---------------------------------------------------------------------------


_NAME_KEY = "name"


def patch(content: str, shape: str, config: Config) -> tuple[str, list[str]]:
    """Return ``(patched_content, missing_keys)``.

    *shape* must be one of :attr:`Config.shapes` — callers wanting a
    user-facing error should validate before calling.

    ``missing_keys`` enumerates configured swap keys (including the
    special ``name``) whose variants didn't appear in *content* — the
    CLI surfaces this as exit code 3 so drift between arco.toml and
    arco-patcher.toml is loud rather than silent.
    """
    name_variants = {f'{_NAME_KEY} = "{v}"' for v in config.names.values()}
    name_target = f'{_NAME_KEY} = "{config.names[shape]}"'

    swap_mappings: list[tuple[Swap, dict[str, str]]] = [
        (s, s.target_map(shape)) for s in config.swaps
    ]

    found_name = False
    found_swap_keys: set[str] = set()
    out_lines: list[str] = []

    for line in content.splitlines():
        stripped = line.strip()
        if stripped in name_variants:
            out_lines.append(name_target)
            found_name = True
            continue
        replaced = False
        for swap, mapping in swap_mappings:
            if stripped in mapping:
                out_lines.append(mapping[stripped])
                found_swap_keys.add(swap.key)
                replaced = True
                break
        if not replaced:
            out_lines.append(line)

    missing: list[str] = []
    if not found_name:
        missing.append(_NAME_KEY)
    for swap in config.swaps:
        if swap.key not in found_swap_keys:
            missing.append(swap.key)

    trailing_newline = content.endswith("\n")
    patched_content = "\n".join(out_lines)
    if trailing_newline:
        patched_content += "\n"
    return patched_content, missing


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Patch a TOML file (typically arco.toml) into one of the shapes "
            "declared in arco-patcher.toml.  Shape names are entirely "
            "config-driven; the CLI does not bake in 'main' / 'dev' or any "
            "other label."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "shape",
        metavar="SHAPE",
        help=(
            "Name of the shape to patch the file into.  Must match one of "
            "the shape names declared in [arco_patcher].shapes in the config."
        ),
    )
    parser.add_argument(
        "--file",
        default="arco.toml",
        metavar="PATH",
        help="Path to the file to patch (default: arco.toml in cwd).",
    )
    parser.add_argument(
        "--config",
        default="arco-patcher.toml",
        metavar="PATH",
        help="Path to arco-patcher.toml (default: arco-patcher.toml in cwd).",
    )
    args = parser.parse_args(argv)

    arco_path = Path(args.file)
    if not arco_path.is_file():
        print(f"error: {arco_path} not found", file=sys.stderr)
        return 1

    try:
        config = load_config(Path(args.config))
    except ConfigError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.shape not in config.shapes:
        print(
            f"error: shape {args.shape!r} not declared in {args.config} "
            f"(known shapes: {config.shapes})",
            file=sys.stderr,
        )
        return 2

    original = arco_path.read_text(encoding="utf-8")
    updated, missing = patch(original, args.shape, config)

    if missing:
        print(
            f"error: configured key(s) not found in {arco_path}: "
            f"{', '.join(missing)}\n"
            "  arco.toml has drifted from arco-patcher.toml — update one to match the other.",
            file=sys.stderr,
        )
        return 3

    if updated != original:
        arco_path.write_text(updated, encoding="utf-8")
    print(f"Patched {arco_path} for shape={args.shape}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

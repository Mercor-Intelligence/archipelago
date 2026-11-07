"""The shared ``Hooks.populate`` factory (spec §8).

The library core (``parity_test/``) is app-neutral: it knows *when* to seed and
*how to shape* a fixture dir, but nothing about how an app loads its DB. That
last mile is an app concern — and the only faithful way to bridge it is to run
the app's *own* end-to-end populate lifecycle. :func:`script_populate` does
exactly that, so a per-app ``parity_test_ext`` reduces to::

    from parity_test import defaults
    from scripts import populate_engine

    def register(H):
        H.populate = defaults.script_populate(populate_engine.main)

Any drift between how the clone is seeded and how the shipped app builds its DB
would defeat parity, so there is deliberately no "bare csv import" shortcut here:
seeding goes through the identical process ``mise populate`` runs.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

# ``mcp_middleware`` ships in the same distribution as ``parity_test`` (both are
# flattened into the ``mercor-mcp-shared`` wheel), so it is always importable —
# use its real signal constants rather than hard-coding the env names.
from mcp_middleware.lifecycle import MCP_LIFECYCLE_SEED_IN_PLACE_ENV

if TYPE_CHECKING:
    from collections.abc import Iterator

__all__ = ["script_populate"]


def script_populate(
    entrypoint: Callable[[], int | None],
    *,
    state_location_env: str = "STATE_LOCATION",
    bind_in_place: bool = True,
    clean_argv: bool = True,
) -> Callable[[Path], None]:
    """Seed by running the app's **own end-to-end populate lifecycle** against the
    per-test seed dir — the exact process the real deploy uses.

    :param entrypoint: The app's populate script — the same callable it runs for
        ``mise populate``, e.g. ``scripts.populate_engine.main`` (which delegates
        to :func:`mcp_middleware.populate_main` /
        :func:`~mcp_middleware.csv_engine.snapshot_with_populate`). It must read
        its source directory from ``$STATE_LOCATION`` and its target DB from
        ``$DATABASE_PATH`` (the shared lifecycle convention), and return an ``int``
        exit code (``0`` = success) or ``None``. Apps that build their entrypoint
        from kwargs pass a thunk: ``lambda: populate_main(**app_kwargs)``.
    :param state_location_env: Env var the app's lifecycle reads its source dir
        from. Pointed at the seed tempdir for the duration of the call.
    :param bind_in_place: When ``True`` (default), export
        ``MCP_LIFECYCLE_SEED_IN_PLACE=1`` around the call, which tells the populate
        facade the already-present canonical (written by the harness's app boot) is
        a disposable seed baseline, not an authoritative pre-built artifact — so the
        import OVERLAYS it rather than auto-skipping (the facade's default
        real-deploy behaviour when a delivered DB is present). Binding is always
        in-place now — ``bind_engine`` binds the canonical exactly where it lies,
        the file the test's client already serves — so no separate in-place toggle
        is needed.
    :param clean_argv: When ``True`` (default), blank ``sys.argv`` to just the
        program name for the duration of the call. The app entrypoint ultimately
        reaches ``mcp_middleware.populate_main``, which argparses ``sys.argv`` when
        no explicit ``argv`` is passed — under pytest that inherits pytest's own
        flags (``-q``, ``-k`` …) and dies on "unrecognized arguments". Blanking
        argv here means adopting apps don't each have to scrub it in their thunk.
        The seed dir travels via ``$STATE_LOCATION`` (env), not argv, so nothing
        is lost.

    Running the identical entrypoint means the seeded clone reflects every
    csv-engine hook (directive fan-out, app-registered readers/key-normalizers)
    *and* every post-import step (type coercion, junction/tag materialization,
    derived config, default-user wiring, index rebuild) the deploy performs —
    nothing to keep in sync by hand. The only thing that differs from
    ``mise populate`` is the input directory (seed fixtures vs. the SME world
    corpus) and, in-place, that no snapshot artifact is relocated (harvest
    degrades to a no-op / an in-place canonical write).

    :returns: A ``(fixture_dir) -> None`` callable for ``Hooks.populate``. The
        app's own exit code (plus its internal asserts and, ultimately, the parity
        response comparison) is the success contract.
    :raises RuntimeError: When the entrypoint returns a non-zero exit code.
    """

    def _populate(fixture_dir: Path) -> None:
        overrides = {state_location_env: str(Path(fixture_dir))}
        if bind_in_place:
            overrides[MCP_LIFECYCLE_SEED_IN_PLACE_ENV] = "1"
        saved_argv = sys.argv
        if clean_argv:
            sys.argv = [saved_argv[0] if saved_argv else "populate"]
        try:
            with _pushed_env(overrides):
                rc = entrypoint()
        finally:
            sys.argv = saved_argv
        if rc not in (0, None):
            raise RuntimeError(
                f"app populate lifecycle (entrypoint) returned non-zero exit code {rc!r}; "
                f"seed dir={fixture_dir}"
            )

    return _populate


@contextmanager
def _pushed_env(overrides: dict[str, str]) -> Iterator[None]:
    """Set each ``overrides`` entry for the block; restore prior values (or their
    absence) on exit, even if the block raises."""
    sentinel = object()
    prior: dict[str, str | object] = {k: os.environ.get(k, sentinel) for k in overrides}
    os.environ.update(overrides)
    try:
        yield
    finally:
        for key, value in prior.items():
            if value is sentinel:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value  # type: ignore[assignment]

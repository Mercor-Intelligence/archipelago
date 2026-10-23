"""Opt-in test helpers for ``@endpoint`` registry isolation.

``@endpoint`` captures each declaration into a *process-global* registry (and
``register_name_mutator`` installs a *process-global* name mutator). A test
suite that decorates endpoints therefore leaks state into every test that runs
after it in the same process: later ``register_all`` calls see the earlier
suite's tools, and a mutator installed by one test silently reshapes another's
tool names. Each of this package's own test modules already guards against this
with a private autouse fixture; this module promotes that guard to a supported,
**consumer-facing** helper so an app's own endpoint tests can get the same
isolation without reaching into private internals.

Two things distinguish it from the package's internal ``_reset_for_tests``:

* **Snapshot / restore, not nuke.** :func:`registry_snapshot` captures the
  declarations and name mutator that exist *before* the test and restores
  *exactly* that set afterward. Declarations another module legitimately
  registered at import time survive; only what the test itself added is rolled
  back. ``_reset_for_tests`` unconditionally empties the registry, which would
  clobber those pre-existing declarations — wrong for a shared helper.
* **Opt-in, never automatic.** This is a plain importable module, **not** an
  auto-loaded ``pytest11`` plugin. Importing ``mcp_unified_endpoint`` (or even
  ``mcp_unified_endpoint.testing``) activates nothing. A consumer enables
  isolation explicitly — by ``yield``-wrapping :func:`registry_snapshot`, or by
  using / re-exporting the :func:`isolate_registry` fixture below. Nothing here
  runs unless an app asks for it, so the fixture cannot surprise the fleet.

Typical opt-in use in an app's ``conftest.py`` — make it autouse *for that
suite* by re-exporting the fixture with ``autouse=True``::

    import pytest
    from mcp_unified_endpoint.testing import registry_snapshot


    @pytest.fixture(autouse=True)
    def _isolate_endpoints():
        with registry_snapshot():
            yield

Or import the ready-made fixture and request it per test::

    from mcp_unified_endpoint.testing import isolate_registry  # noqa: F401


    def test_registers_my_tool(isolate_registry):
        ...

Or wrap an ad-hoc block outside pytest::

    from mcp_unified_endpoint.testing import registry_snapshot

    with registry_snapshot():
        register_all(mcp, openapi_spec=spec)
        ...  # assertions
    # registry is back to exactly what it was before the block
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator

import pytest

from .registry import _restore_state, _snapshot_state

__all__ = ["isolate_registry", "registry_snapshot"]


@contextlib.contextmanager
def registry_snapshot() -> Iterator[None]:
    """Snapshot the endpoint registry + name mutator, restoring on exit.

    Captures :func:`~mcp_unified_endpoint.registry.get_declarations` (already a
    copy) plus the currently installed ``NameMutator`` on entry, and on exit —
    including when the body raises — restores *exactly* that set: declarations
    added inside the block are dropped, declarations present beforehand are kept,
    and the mutator is re-installed or cleared to match the captured value.

    Pytest-free, so it is equally usable as a bare context manager or as the
    body of a fixture (see :func:`isolate_registry`).
    """
    state = _snapshot_state()
    try:
        yield
    finally:
        _restore_state(state)


@pytest.fixture
def isolate_registry() -> Iterator[None]:
    """Opt-in pytest fixture wrapping :func:`registry_snapshot`.

    Request it by name (``def test_x(isolate_registry): ...``) to isolate a
    single test, or re-export it from a ``conftest.py`` with ``autouse=True`` to
    isolate a whole suite. It is intentionally **not** autouse and **not** a
    registered plugin entry point — importing this module never activates it.
    """
    with registry_snapshot():
        yield

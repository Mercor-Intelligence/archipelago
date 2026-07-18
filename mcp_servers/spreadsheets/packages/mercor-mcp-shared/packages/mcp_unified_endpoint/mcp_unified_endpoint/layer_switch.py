"""``MCP_LAYER_SWITCH`` runtime gate for endpoint-registered MCP tools.

The switch is a string-typed env var that gates whether the MCP-layer
wrapper tools appear in ``tools/list`` and respond to ``tools/call``.
**The REST surface stays fully live regardless of the switch** — the
switch only filters the MCP-side tool registration.

Contract (matches the convention in Foundry-zoho's
``middleware/mcp_layer.py`` and the rest of the Mercor MCP server
family):

* ``MCP_LAYER_SWITCH`` unset → **ON** (default).
* ``MCP_LAYER_SWITCH=1`` / ``true`` / ``yes`` / ``on`` (any case) → **ON**.
* Any other value (``0``, ``false``, ``no``, ``off``, ``garbage``, …) →
  **OFF**.

Studio bakes the operator-selected value into the container image's
``start.sh`` at build time, so flipping the switch rebuilds the image
rather than mutating a running process. This module reads the env var
*each call* (no caching) to match that bake-time discipline — tests
can flip the value with ``monkeypatch.setenv`` and observe the new
state immediately.
"""

from __future__ import annotations

import os

#: Tokens that, case-folded, count as truthy. Anything else is OFF.
_TRUTHY: frozenset[str] = frozenset({"1", "true", "yes", "on"})

#: The env-var name the switch reads from.
ENV_VAR: str = "MCP_LAYER_SWITCH"


def is_layer_enabled() -> bool:
    """Return ``True`` when the MCP wrapper layer should be active.

    Reads :data:`ENV_VAR` from ``os.environ`` each call (no caching).
    See the module docstring for the truthy contract.

    Returns:
        ``True`` if the env var is unset or matches a truthy token;
        ``False`` otherwise.
    """
    raw = os.environ.get(ENV_VAR)
    if raw is None:
        return True
    return raw.strip().lower() in _TRUTHY


__all__ = ["ENV_VAR", "is_layer_enabled"]

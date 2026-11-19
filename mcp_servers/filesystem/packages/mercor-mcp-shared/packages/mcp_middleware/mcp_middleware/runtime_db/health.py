"""Canonical shared ``/health`` liveness route.

One route, identical for every server that goes through
:func:`mcp_middleware.run_server`. It is **pure liveness**: it returns ``200``
regardless of DB state and NEVER opens a DB connection.

Why DB-free is mandatory
------------------------

Under the no-proactive-bind lifecycle the HTTP DB gate is CLOSED at boot and
again during every populate window. A health probe that pinged the DB would
hit that closed gate and **503 itself** — exactly when an orchestrator most
needs a truthful "the process is alive, don't kill the pod" signal. So
``/health`` must stay DB-free. ``/health`` is already in
:data:`~mcp_middleware.runtime_db.db_gate.DEFAULT_WHITELIST`, so the gate lets
it through while closed; this module backs that whitelist entry with an actual
handler.

Why shared owns it
------------------

The only inputs a liveness probe has are process-global (is the process up and
serving HTTP?). There is nothing an app can meaningfully specialise, so shared
provides the single canonical route and apps must not register their own.
:func:`mcp_middleware.run_server` auto-mounts it unconditionally, so an app
gains the route for free by upgrading the pin and deleting its own ``/health``.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_HEALTH_PATH", "register_health_route"]

#: The one canonical liveness path. Matches the entry in
#: :data:`~mcp_middleware.runtime_db.db_gate.DEFAULT_WHITELIST`.
DEFAULT_HEALTH_PATH = "/health"


def _liveness_body() -> dict[str, Any]:
    """The canonical liveness body: ``{"status":"ok","db_disabled":<bool>}``.

    Both keys are baseline (never app-specific). ``db_disabled`` reflects the
    process-global gate flag via :func:`is_db_disabled` — a pure in-memory read,
    NOT a DB touch — so an orchestrator can tell "up but gated (boot/populate)"
    from "up and serving" without the probe ever opening a connection.
    """
    from .db_gate import is_db_disabled

    return {"status": "ok", "db_disabled": is_db_disabled()}


def _existing_paths(mcp_or_app: Any) -> set[str]:
    """Best-effort set of already-registered route paths on ``mcp_or_app``.

    Reads FastMCP's ``_additional_http_routes`` or a Starlette-shaped app's
    ``router.routes`` / ``routes``. Used only for the defensive double-mount
    guard; a miss (returns empty) simply means we proceed to register.
    """
    routes = getattr(mcp_or_app, "_additional_http_routes", None)  # FastMCP
    if routes is None:
        router = getattr(mcp_or_app, "router", None)
        if router is not None:
            routes = getattr(router, "routes", None)
    if routes is None:
        routes = getattr(mcp_or_app, "routes", None)  # bare Starlette app
    if not routes:
        return set()
    return {path for r in routes if (path := getattr(r, "path", None)) is not None}


def register_health_route(mcp_or_app: Any, *, path: str = DEFAULT_HEALTH_PATH) -> bool:
    """Mount the canonical pure-liveness ``/health`` route on ``mcp_or_app``.

    Duck-types the target: a FastMCP instance (via ``custom_route``) or a
    Starlette-shaped app (via ``add_route`` / ``router``).

    Defensive against a pre-existing ``/health``: if one is already registered
    the existing route is left in place and this is a no-op (returns ``False``)
    with a warning. Apps are expected to DROP their own ``/health`` so every
    server serves the identical probe, but a lingering one must never crash
    boot during the migration window.

    Args:
        mcp_or_app: FastMCP instance or Starlette/FastAPI-shaped app.
        path: Route path. Defaults to :data:`DEFAULT_HEALTH_PATH`; override
            only if the whitelist is customised to match.

    Returns:
        ``True`` if this call mounted the route, ``False`` if it deferred to a
        pre-existing ``/health``.

    Raises:
        TypeError: If ``mcp_or_app`` exposes neither ``custom_route`` (FastMCP)
            nor ``add_route`` / ``router`` (Starlette / FastAPI).
    """
    # Mount BOTH the exact path and its trailing-slash variant ("/health" and
    # "/health/"). The DB gate whitelist is exact-string (no prefix/redirect),
    # and the gate is outermost — so a probe hitting "/health/" while the gate
    # is closed would 503 before Starlette's slash-redirect could run. Serving
    # both paths directly keeps liveness truthful regardless of trailing slash.
    base = path.rstrip("/")
    variants = [p for p in (base, base + "/") if p] or [path]

    existing = _existing_paths(mcp_or_app)
    if variants[0] in existing:
        logger.warning(
            "register_health_route: %s already registered — leaving the existing "
            "route in place. Shared now provides the canonical /health; drop the "
            "app-local route so every server serves the identical liveness probe.",
            variants[0],
        )
        return False

    to_mount = [p for p in variants if p not in existing]

    from starlette.responses import JSONResponse

    custom_route = getattr(mcp_or_app, "custom_route", None)
    if callable(custom_route):
        for p in to_mount:

            @custom_route(p, methods=["GET"])
            async def _health(_request: Any) -> JSONResponse:
                # Pure liveness: constant 200, no DB touch (see module docstring).
                return JSONResponse(_liveness_body(), status_code=200)

        logger.info("register_health_route: mounted %s (FastMCP)", ", ".join(to_mount))
        return True

    add_route = getattr(mcp_or_app, "add_route", None)
    router = getattr(mcp_or_app, "router", None)
    if callable(add_route) or router is not None:

        async def _health(_request: Any) -> JSONResponse:
            return JSONResponse(_liveness_body(), status_code=200)

        for p in to_mount:
            if callable(add_route):
                add_route(p, _health, methods=["GET"])
            else:
                router.add_route(p, _health, methods=["GET"])
        logger.info("register_health_route: mounted %s (Starlette)", ", ".join(to_mount))
        return True

    raise TypeError(
        f"register_health_route: {type(mcp_or_app).__name__} has no custom_route() "
        "(FastMCP) or add_route()/router (Starlette/FastAPI)"
    )

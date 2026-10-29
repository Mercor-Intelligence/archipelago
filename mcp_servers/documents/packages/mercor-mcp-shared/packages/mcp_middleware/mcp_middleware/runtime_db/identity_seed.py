"""Blank-world fallback default-user seed (D4).

A server whose default-user identity gate is *enforced* refuses every request
until a usable identity row exists (``actor=None`` → 401 at the app's permission
layer). That is exactly right while a world is being delivered. But a world that
**never had a drop** — populate is not configured for this deployment and no DB
was ever delivered — would refuse forever. This module lets such a server seed a
single *fallback* identity so it can serve best-effort instead of 401ing.

The trigger is deliberately narrow (operator-adjudicated). The fallback is a
**request-driven last resort**, evaluated only when a gated request forces the
lazy bind, and fires *only* for a world that genuinely never had a drop:

    seed iff  spec.identity_seed set
         AND  the bind is NOT a delivery (``not binding.delivered``)
         AND  no drop is configured / pending / happened
              (STATE_LOCATION has no staged input CSVs
               AND not populate_in_progress)
         AND  the default-user requirement is enforced
         AND  the identity table is empty (idempotent no-op otherwise)

Why STATE_LOCATION emptiness is the discriminator
-------------------------------------------------

``not binding.delivered`` alone matches BOTH "blank forever" and
"blank *now* but a drop is coming/failed". The operator's rule: **stay closed on
an incorrectly-configured drop, but a world that never had a drop should serve
best-effort.** Staged input CSVs under ``$STATE_LOCATION`` mean a drop *was
configured for this deployment* — so we stay closed (refuse) even if it hasn't
landed or is broken. An empty ``$STATE_LOCATION`` (no staged input) with no
delivered DB and no populate in flight means the world never had a drop — so we
seed. In this infra populate always precedes the first gated request when it is
going to run, so a to-be-populated world is already ``delivered`` or
``populate_in_progress`` by request time and never reaches the seed.

The fallback NEVER touches the delivered path, so a delivered-but-broken world
(empty or dangling ``default_users``) keeps refusing through the existing
identity gate — the fallback cannot rescue a bad drop.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from .binding import EngineBinding
    from .ready import DbLifecycleSpec

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_IDENTITY_TABLE",
    "invoke_identity_seed",
    "seed_fallback_default_user",
    "should_seed_fallback",
]

#: Conventional single-row default-user identity table. Matches the default
#: ``default_user_table`` used by the identity gate.
DEFAULT_IDENTITY_TABLE = "default_users"


def _quote_ident(name: str) -> str:
    """Quote a SQLite identifier; reject an embedded double-quote defensively.

    Table/column names come from app config (``DbLifecycleSpec.identity_table``,
    the seed ``row`` keys), not request input, so this is belt-and-braces — but
    raising on a suspicious identifier beats emitting a malformed statement.
    """
    if '"' in name:
        raise ValueError(f"refusing to quote identifier containing double-quote: {name!r}")
    return f'"{name}"'


def _has_staged_input(state_location: str | None) -> bool:
    """True when ``$STATE_LOCATION`` holds ≥1 staged input CSV.

    Staged input CSVs mean a drop was *configured* for this deployment (populate
    reads its inputs from ``$STATE_LOCATION``). Their presence is the "stay
    closed on a configured-but-not-yet/incorrectly-delivered drop" signal — the
    runtime ``.db`` living in the same directory is deliberately NOT counted, so
    a blank-forever world (which still has an empty runtime DB there) is not
    mistaken for a configured drop.

    Unset / non-directory / unreadable ``$STATE_LOCATION`` reads as "no staged
    input" (allow the seed) — we only *block* on an affirmative CSV sighting.
    """
    if not state_location:
        return False
    root = Path(state_location)
    if not root.is_dir():
        return False
    try:
        for candidate in root.rglob("*.csv"):
            if candidate.is_file():
                return True
    except OSError as exc:  # pragma: no cover - defensive
        logger.warning("could not scan STATE_LOCATION %s for staged input: %s", state_location, exc)
        return False
    return False


def _identity_table_empty(engine: Engine, table: str) -> bool:
    """True when ``table`` has no rows (or does not exist / is unreadable).

    A missing or unreadable table reads as empty: on the blank-world path
    ``cold_init`` creates the schema, and a create_all-aware seed callback will
    establish the row regardless — so "can't see a row" must not block the seed.
    """
    from sqlalchemy import text

    try:
        with engine.connect() as conn:
            row = conn.execute(text(f"SELECT 1 FROM {_quote_ident(table)} LIMIT 1")).first()
        return row is None
    except Exception as exc:  # noqa: BLE001 - any read failure ⇒ treat as empty
        logger.debug("identity table %r not readable (treating as empty): %s", table, exc)
        return True


def should_seed_fallback(spec: DbLifecycleSpec, binding: EngineBinding) -> bool:
    """Evaluate the full fallback-seed predicate (conditions 1–5).

    Called from :func:`~mcp_middleware.runtime_db.ensure_db_ready` on the
    request-driven bind, AFTER ``cold_init`` and BEFORE the gate opens. Returns
    ``True`` only for a world that genuinely never had a drop and needs a
    fallback identity to serve. See the module docstring for the rationale.
    """
    # (1) The app opted in with a seed callback.
    if spec.identity_seed is None:
        return False
    # (2) Never had a valid drop — the fallback never touches the delivered path.
    if binding.delivered:
        return False
    # (3) No drop configured / pending / happened.
    from .db_gate import is_populate_in_progress

    if is_populate_in_progress():
        return False
    if _has_staged_input(os.getenv("STATE_LOCATION")):
        return False
    # (4) The default-user requirement is enforced (never seed to dodge it).
    from mcp_middleware.runner import default_user_enforced

    if not default_user_enforced(spec.enforce_default_user):
        return False
    # (5) The identity table is empty (idempotent no-op if a row already exists).
    return _identity_table_empty(binding.engine, spec.identity_table)


def invoke_identity_seed(spec: DbLifecycleSpec, engine: Engine) -> None:
    """Run ``spec.identity_seed`` inside a shared-managed, committed session.

    Shared owns the transaction so the callback is a single call that commits
    exactly once (the callback need not, and should not, manage the session
    lifecycle). The callback receives a :class:`sqlalchemy.orm.Session`; derive
    the engine via ``session.get_bind()`` if it needs Core access.
    """
    from sqlalchemy.orm import Session

    assert spec.identity_seed is not None  # guarded by should_seed_fallback
    with Session(engine) as session:
        spec.identity_seed(session)
        session.commit()


def seed_fallback_default_user(
    target: Any,
    *,
    table: str = DEFAULT_IDENTITY_TABLE,
    row: dict[str, Any],
) -> None:
    """Idempotent single-table fallback-identity insert (``INSERT OR IGNORE``).

    The shared base helper for apps whose fallback identity is a single row in
    one table with no foreign-key targets (e.g. GWS, Atlassian). Apps with FK
    chains (users → default_user → scopes) write their own multi-row callback
    instead.

    ``target`` may be a SQLAlchemy :class:`~sqlalchemy.orm.Session` /
    :class:`~sqlalchemy.engine.Connection` (the statement executes on it; the
    caller / :func:`invoke_identity_seed` commits) or an
    :class:`~sqlalchemy.Engine` (a transaction is opened and committed here).
    ``INSERT OR IGNORE`` makes a re-run a no-op, so this is safe to call even if
    a row already exists. SQLite-runtime only (all Mercor runtime DBs are
    SQLite).
    """
    from sqlalchemy import Engine, text

    if not row:
        raise ValueError("seed_fallback_default_user: row must be a non-empty mapping")

    columns = ", ".join(_quote_ident(col) for col in row)
    placeholders = ", ".join(f":{col}" for col in row)
    stmt = text(f"INSERT OR IGNORE INTO {_quote_ident(table)} ({columns}) VALUES ({placeholders})")

    if isinstance(target, Engine):
        with target.begin() as conn:
            conn.execute(stmt, row)
    else:
        # Session or Connection — execute on it; the caller owns the commit.
        target.execute(stmt, row)

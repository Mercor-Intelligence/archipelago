"""Working-location facade for the runtime DB.

The delivered ``.db`` arrives at a *receive* path (today
``$STATE_LOCATION/studio.db``) and is bound **in place** — the runtime
file IS the canonical file (see :func:`~.binding.bind_engine`). The only
placement knob that survives is the snapshot *deliver* directory: when
``MCP_SNAPSHOT_DIR`` points somewhere other than the receive dir, the
snapshot export is written there instead of beside the served DB.

This module used to implement a decision "ladder" that moved the working
DB into per-uid tmpfs/workdir to keep an indexed working copy out of the
snapshot export. That machinery is gone: the FTS index lives in a
``.fts.db`` sidecar the snapshot excludes, so the served file can stay put.
What remains is the small enum + the deliver-dir resolver the binding and
snapshot layers still consume.
"""

from __future__ import annotations

import logging
import os
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)


def _warn_loudly(message: str, *args: object) -> None:
    """Emit an operator-facing warning through BOTH logging stacks.

    Foundry apps route logs through loguru with no stdlib bridge, so a
    stdlib-only warning is silently dropped exactly where visibility
    matters most (Datadog) — the same failure mode that hid the bind-mode
    line before ``log_binding`` went loguru-native. These warnings fire on
    misconfiguration, i.e. rarely, so the duplicate line in apps that bridge
    both stacks is an acceptable cost for guaranteed visibility in either one.
    """
    logger.warning(message, *args)
    from loguru import logger as loguru_logger  # deferred: keep module import cheap

    loguru_logger.opt(depth=1).warning(message % args if args else message)


__all__ = [
    "MCP_SNAPSHOT_DIR_ENV",
    "WorkingMode",
    "resolve_deliver_dir",
]

#: Directory the snapshot export must be written to. Unset -> the receive
#: path's own directory (receive and deliver collide — the served in-place
#: DB is snapshot in place, minus the excluded FTS sidecar).
MCP_SNAPSHOT_DIR_ENV = "MCP_SNAPSHOT_DIR"


class WorkingMode(StrEnum):
    """How the working DB relates to the receive path.

    Doubles as the binding mode on :class:`~.binding.EngineBinding` —
    string-valued so it round-trips through JSON / structured log fields.
    """

    #: The delivered file is bound where it lies; runtime IS canonical.
    IN_PLACE = "in-place"
    #: In-memory SQLite (``:memory:``); no file involved.
    MEMORY = "memory"


def resolve_deliver_dir(receive: str | os.PathLike[str]) -> Path:
    """The snapshot-export directory: ``MCP_SNAPSHOT_DIR`` or receive's dir."""
    raw = os.environ.get(MCP_SNAPSHOT_DIR_ENV, "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return Path(os.fspath(receive)).expanduser().resolve().parent

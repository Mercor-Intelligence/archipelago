"""
Opt-in logger for grading_logs Postgres/Redis sinks.

Import ``logger`` from this module and use the same ``.bind(message_type=...)``
pattern as agents. Only INFO/WARNING/ERROR records with a ``message_type`` bind
and a ``grading_run_id`` (from context or explicit bind) reach grading_logs.
Regular ``loguru.logger`` calls stay on stdout/Datadog only.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from loguru import logger as _loguru_logger

from runner.utils.llm import grading_run_id_ctx

if TYPE_CHECKING:
    from loguru import Record


def _inject_grading_run_id(record: Record) -> None:
    if record["extra"].get("grading_run_id") is None:
        run_id = grading_run_id_ctx.get()
        if run_id is not None:
            record["extra"]["grading_run_id"] = run_id


logger = _loguru_logger.bind(grading_db_log=True).patch(_inject_grading_run_id)


def grading_db_filter(record: Record) -> bool:
    """Loguru sink filter: grading_db_log + message_type + grading_run_id + INFO+."""
    extra = record["extra"]
    return (
        extra.get("grading_db_log") is True
        and extra.get("message_type") is not None
        and extra.get("grading_run_id") is not None
        and record["level"].no >= _loguru_logger.level("INFO").no
    )

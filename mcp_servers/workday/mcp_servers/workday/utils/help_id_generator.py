"""ID generation utilities for Workday Help entities.

DEPRECATED: This module is a legacy artifact from the raw SQLite implementation
of workday_help. It is NOT used by the SQLAlchemy-based implementation.

Current ID generation strategy:
- Case IDs: Client-provided in format CASE-YYYYMMDD-### (required by schema)
- Event IDs: UUID-based, generated in timeline_repository.py as EVT-{uuid12}
- Message IDs: UUID-based, generated in message_repository.py as MSG-{uuid12}
- Attachment IDs: UUID-based, generated in attachment_repository.py as ATT-{uuid12}
- Audit Log IDs: UUID-based, generated in audit_repository.py as AUDIT-{uuid12}

This file is kept for reference and potential future use if sequential IDs
are preferred. The functions reference old table names (timeline_events,
audit_log, messages, attachments, cases) which do not match the new schema
(help_timeline_events, help_audit_log, help_messages, help_attachments, help_cases).
"""

import sqlite3
from datetime import UTC, datetime


def generate_event_id(conn: sqlite3.Connection) -> str:
    """
    Generate unique event ID in format EVT-###### (6-digit zero-padded).

    Uses atomic numeric extraction to avoid lexicographic ordering issues
    and handles race conditions via transaction isolation.
    """
    cursor = conn.execute(
        """
        SELECT CAST(SUBSTR(event_id, 5) AS INTEGER) AS num
        FROM timeline_events
        WHERE event_id LIKE 'EVT-%'
        ORDER BY num DESC
        LIMIT 1
        """
    )
    row = cursor.fetchone()

    if row is None:
        next_num = 1
    else:
        next_num = row[0] + 1

    return f"EVT-{next_num:06d}"


def generate_audit_log_id(conn: sqlite3.Connection) -> str:
    """
    Generate unique audit log ID in format AUDIT-###### (6-digit zero-padded).

    Uses atomic numeric extraction to avoid lexicographic ordering issues
    and handles race conditions via transaction isolation.
    """
    cursor = conn.execute(
        """
        SELECT CAST(SUBSTR(log_id, 7) AS INTEGER) AS num
        FROM audit_log
        WHERE log_id LIKE 'AUDIT-%'
        ORDER BY num DESC
        LIMIT 1
        """
    )
    row = cursor.fetchone()

    if row is None:
        next_num = 1
    else:
        next_num = row[0] + 1

    return f"AUDIT-{next_num:06d}"


def generate_message_id(conn: sqlite3.Connection) -> str:
    """
    Generate unique message ID in format MSG-###### (6-digit zero-padded).

    Uses atomic numeric extraction to avoid lexicographic ordering issues
    and handles race conditions via transaction isolation.
    """
    cursor = conn.execute(
        """
        SELECT CAST(SUBSTR(message_id, 5) AS INTEGER) AS num
        FROM messages
        WHERE message_id LIKE 'MSG-%'
        ORDER BY num DESC
        LIMIT 1
        """
    )
    row = cursor.fetchone()

    if row is None:
        next_num = 1
    else:
        next_num = row[0] + 1

    return f"MSG-{next_num:06d}"


def generate_attachment_id(conn: sqlite3.Connection) -> str:
    """
    Generate unique attachment ID in format ATT-###### (6-digit zero-padded).

    Uses atomic numeric extraction to avoid lexicographic ordering issues
    and handles race conditions via transaction isolation.
    """
    cursor = conn.execute(
        """
        SELECT CAST(SUBSTR(attachment_id, 5) AS INTEGER) AS num
        FROM attachments
        WHERE attachment_id LIKE 'ATT-%'
        ORDER BY num DESC
        LIMIT 1
        """
    )
    row = cursor.fetchone()

    if row is None:
        next_num = 1
    else:
        next_num = row[0] + 1

    return f"ATT-{next_num:06d}"


def get_current_timestamp() -> str:
    """Get current UTC timestamp in ISO 8601 format."""
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def generate_case_id(
    conn: sqlite3.Connection,
    *,
    date_prefix: str | None = None,
) -> str:
    """
    Generate a case ID in the format CASE-YYYYMMDD-###.

    The numerical suffix resets every day and is zero-padded to at least three digits.
    """
    prefix = date_prefix or datetime.now(UTC).strftime("%Y%m%d")
    cursor = conn.execute(
        """
        SELECT CAST(SUBSTR(case_id, 15) AS INTEGER) AS seq
        FROM cases
        WHERE case_id LIKE ?
        ORDER BY seq DESC
        LIMIT 1
        """,
        (f"CASE-{prefix}-%",),
    )
    row = cursor.fetchone()
    next_num = (row[0] + 1) if row and row[0] is not None else 1
    return f"CASE-{prefix}-{next_num:03d}"

"""Helpers for loading SQL dump fixtures into a live database for tests.

These are test-only utilities for standing up a *real* schema (and any seed
rows a dump carries) before driving the csv_engine importer against it. They
live in the installed package — not inside a test module — so consumer repos
can import them from their own test suites::

    from mcp_testing import load_mysql_dump

    load_mysql_dump("/path/to/schema.sql", "mysql://root:root@127.0.0.1:3306/app")
"""

from __future__ import annotations

from pathlib import Path

__all__ = ["load_mysql_dump"]


def load_mysql_dump(dump_path: str | Path, url: str) -> None:
    """Load a ``mysqldump`` ``.sql`` file into the server addressed by ``url``.

    Dump-agnostic and CI-portable: no ``mysql``/``mariadb`` client binary is
    required. Uses pymysql with ``CLIENT.MULTI_STATEMENTS`` so the whole script
    (including the dump's own ``CREATE DATABASE`` / ``USE``) runs server-side in
    one round trip — sidestepping fragile client-side statement splitting. The
    connection is opened WITHOUT a default database, since a ``--databases``
    dump creates and selects its own.

    Suitable for a fresh-schema fixture whose dump carries no ``DELIMITER``
    blocks (stored routines / triggers); e.g. the OpenMRS core schema does not.

    ``pymysql`` is imported lazily so importing :mod:`mcp_testing` never
    requires the MySQL driver — only calling this helper does.
    """
    import pymysql
    from pymysql.constants import CLIENT
    from sqlalchemy.engine import make_url

    parsed = make_url(str(url))
    sql = Path(dump_path).read_text(encoding="utf-8")
    conn = pymysql.connect(
        host=parsed.host or "127.0.0.1",
        port=parsed.port or 3306,
        user=parsed.username or "root",
        password=parsed.password or "",
        client_flag=CLIENT.MULTI_STATEMENTS,
        autocommit=True,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            # Drain every result set the multi-statement script produced,
            # otherwise the next execute() raises "Commands out of sync".
            while cur.nextset():
                pass
    finally:
        conn.close()

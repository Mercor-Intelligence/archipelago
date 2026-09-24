#!/usr/bin/env python3
"""Materialize schema-validate sample + negative-control SQLite DBs.

Committed controls are this builder (deterministic SQL) plus schemas/samples/.
Run before proving negatives:

    python3 scripts/build_sv_fixtures.py
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SAMPLES = REPO / "schemas" / "samples"
NEG = REPO / "schemas" / "negative"

NOW = "2024-01-02 16:00:00"
DATE = "2024-01-02"


def make_db(path: Path) -> sqlite3.Connection:
    if path.exists():
        path.unlink()
    for side in (f"{path}-wal", f"{path}-shm", f"{path}-journal"):
        Path(side).unlink(missing_ok=True)
    path.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(path))


def create_full_schema(con: sqlite3.Connection) -> None:
    con.executescript(
        """
        CREATE TABLE companies (
            symbol VARCHAR(20) NOT NULL PRIMARY KEY,
            company_name VARCHAR(255),
            exchange VARCHAR(50),
            exchange_short_name VARCHAR(20),
            currency VARCHAR(10),
            country VARCHAR(100),
            sector VARCHAR(100),
            industry VARCHAR(100),
            cik VARCHAR(20),
            cusip VARCHAR(20),
            isin VARCHAR(20),
            description TEXT,
            website VARCHAR(255),
            logo VARCHAR(255),
            ceo VARCHAR(255),
            full_time_employees INTEGER,
            ipo_date VARCHAR(20),
            address VARCHAR(255),
            city VARCHAR(100),
            state VARCHAR(50),
            zip VARCHAR(20),
            phone VARCHAR(50),
            is_etf BOOLEAN,
            is_actively_trading BOOLEAN,
            profile_data TEXT,
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        );
        CREATE TABLE stock_quotes (
            symbol VARCHAR(20) NOT NULL PRIMARY KEY,
            price FLOAT, open FLOAT, high FLOAT, low FLOAT, previous_close FLOAT,
            change FLOAT, change_percent FLOAT,
            volume INTEGER, avg_volume INTEGER, market_cap INTEGER,
            pe_ratio FLOAT, eps FLOAT,
            fifty_two_week_high FLOAT, fifty_two_week_low FLOAT,
            shares_outstanding INTEGER,
            quote_data TEXT, quote_timestamp DATETIME,
            created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
        );
        CREATE TABLE historical_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol VARCHAR(20) NOT NULL,
            date DATE NOT NULL,
            open FLOAT, high FLOAT, low FLOAT, close FLOAT,
            volume INTEGER, unadjusted_volume INTEGER,
            adj_open FLOAT, adj_high FLOAT, adj_low FLOAT, adj_close FLOAT,
            change FLOAT, change_percent FLOAT, vwap FLOAT,
            label VARCHAR(50), change_over_time FLOAT, price_data TEXT,
            created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
            UNIQUE(symbol, date)
        );
        CREATE TABLE financial_statements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol VARCHAR(20) NOT NULL,
            statement_type VARCHAR(50) NOT NULL,
            period VARCHAR(20) NOT NULL,
            fiscal_date DATE NOT NULL,
            fiscal_year INTEGER, calendar_year INTEGER,
            reported_currency VARCHAR(10),
            filing_date DATE, accepted_date VARCHAR(50), cik VARCHAR(20),
            statement_data TEXT NOT NULL,
            created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
            UNIQUE(symbol, statement_type, period, fiscal_date)
        );
        CREATE TABLE technical_indicators (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol VARCHAR(20) NOT NULL,
            indicator_type VARCHAR(30) NOT NULL,
            period INTEGER NOT NULL,
            interval VARCHAR(20) NOT NULL,
            date DATE NOT NULL,
            value FLOAT, open FLOAT, high FLOAT, low FLOAT, close FLOAT, volume INTEGER,
            indicator_data TEXT,
            created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
            UNIQUE(symbol, indicator_type, period, interval, date)
        );
        CREATE TABLE generic_api_responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint VARCHAR(255) NOT NULL,
            params_hash VARCHAR(64) NOT NULL,
            params_json TEXT, symbol VARCHAR(20),
            response_data TEXT NOT NULL,
            http_status INTEGER,
            created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
            UNIQUE(endpoint, params_hash)
        );
        """
    )


def seed_minimal_rows(con: sqlite3.Connection) -> None:
    profile = json.dumps({"symbol": "AAPL", "companyName": "Apple Inc."})
    quote = json.dumps({"symbol": "AAPL", "price": 186.5})
    statement = json.dumps({"symbol": "AAPL", "date": DATE, "revenue": 100})
    params = {"symbol": "AAPL"}
    params_json = json.dumps(params, sort_keys=True)
    params_hash = hashlib.sha256(params_json.encode()).hexdigest()
    response = json.dumps([{"symbol": "AAPL", "price": 186.5}])

    con.execute(
        """
        INSERT INTO companies (
            symbol, company_name, exchange, exchange_short_name, currency, country,
            sector, industry, is_etf, is_actively_trading, profile_data,
            created_at, updated_at
        ) VALUES (
            'AAPL', 'Apple Inc.', 'NASDAQ', 'NASDAQ', 'USD', 'US',
            'Technology', 'Consumer Electronics', 0, 1, ?,
            ?, ?
        )
        """,
        (profile, NOW, NOW),
    )
    con.execute(
        """
        INSERT INTO stock_quotes (
            symbol, price, open, high, low, previous_close, volume, quote_data,
            created_at, updated_at
        ) VALUES ('AAPL', 186.5, 185.0, 187.5, 184.0, 185.0, 50000000, ?, ?, ?)
        """,
        (quote, NOW, NOW),
    )
    con.execute(
        """
        INSERT INTO historical_prices (
            symbol, date, open, high, low, close, adj_close, volume,
            created_at, updated_at
        ) VALUES ('AAPL', ?, 185.0, 187.5, 184.0, 186.5, 186.5, 50000000, ?, ?)
        """,
        (DATE, NOW, NOW),
    )
    con.execute(
        """
        INSERT INTO financial_statements (
            symbol, statement_type, period, fiscal_date, fiscal_year,
            statement_data, created_at, updated_at
        ) VALUES ('AAPL', 'income', 'annual', ?, 2023, ?, ?, ?)
        """,
        (DATE, statement, NOW, NOW),
    )
    con.execute(
        """
        INSERT INTO generic_api_responses (
            endpoint, params_hash, params_json, symbol, response_data, http_status,
            created_at, updated_at
        ) VALUES ('/quote-short', ?, ?, 'AAPL', ?, 200, ?, ?)
        """,
        (params_hash, params_json, response, NOW, NOW),
    )
    # technical_indicators intentionally left empty (matches production fixtures)


def finalize(con: sqlite3.Connection) -> None:
    con.commit()
    con.close()


def reset_neg_dir() -> None:
    NEG.mkdir(parents=True, exist_ok=True)
    for child in list(NEG.iterdir()):
        if child.name in {"README.md"}:
            continue
        if child.is_dir():
            shutil.rmtree(child)
        else:
            child.unlink()


def write_sample() -> None:
    SAMPLES.mkdir(parents=True, exist_ok=True)
    path = SAMPLES / "fmp.db"
    con = make_db(path)
    create_full_schema(con)
    seed_minimal_rows(con)
    finalize(con)


def main() -> int:
    write_sample()
    reset_neg_dir()

    # empty_bundle
    (NEG / "empty_bundle").mkdir(parents=True)
    (NEG / "empty_bundle" / ".gitkeep").write_text("")

    # too_many_db
    d = NEG / "too_many_db"
    d.mkdir(parents=True)
    for name in ("a.db", "b.db"):
        c = make_db(d / name)
        create_full_schema(c)
        seed_minimal_rows(c)
        finalize(c)

    # not_sqlite
    d = NEG / "not_sqlite"
    d.mkdir(parents=True)
    (d / "fmp.db").write_bytes(b"this is not a sqlite database\n")

    # missing_required_table (no companies)
    d = NEG / "missing_required_table"
    d.mkdir(parents=True)
    c = make_db(d / "fmp.db")
    c.executescript(
        """
        CREATE TABLE stock_quotes (
            symbol VARCHAR(20) NOT NULL PRIMARY KEY,
            price FLOAT, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
        );
        INSERT INTO stock_quotes VALUES ('AAPL', 1.0, '2024-01-02 16:00:00', '2024-01-02 16:00:00');
        """
    )
    finalize(c)

    # missing_required_column (companies without symbol)
    d = NEG / "missing_required_column"
    d.mkdir(parents=True)
    c = make_db(d / "fmp.db")
    create_full_schema(c)
    c.execute("ALTER TABLE companies RENAME TO companies_full")
    c.executescript(
        """
        CREATE TABLE companies (
            company_name VARCHAR(255),
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        );
        INSERT INTO companies VALUES ('Apple', '2024-01-02 16:00:00', '2024-01-02 16:00:00');
        """
    )
    # keep other tables empty of child symbols to avoid cascading FK noise
    finalize(c)

    # duplicate_identity
    d = NEG / "duplicate_identity"
    d.mkdir(parents=True)
    c = make_db(d / "fmp.db")
    c.executescript(
        """
        CREATE TABLE companies (
            symbol VARCHAR(20) NOT NULL,
            company_name VARCHAR(255),
            created_at DATETIME NOT NULL,
            updated_at DATETIME NOT NULL
        );
        CREATE TABLE stock_quotes (
            symbol VARCHAR(20) NOT NULL PRIMARY KEY,
            price FLOAT, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
        );
        CREATE TABLE historical_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol VARCHAR(20) NOT NULL,
            date DATE NOT NULL,
            open FLOAT, high FLOAT, low FLOAT, close FLOAT, volume INTEGER,
            created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
        );
        CREATE TABLE financial_statements (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol VARCHAR(20) NOT NULL,
            statement_type VARCHAR(50) NOT NULL,
            period VARCHAR(20) NOT NULL,
            fiscal_date DATE NOT NULL,
            statement_data TEXT NOT NULL,
            created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
        );
        CREATE TABLE technical_indicators (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol VARCHAR(20) NOT NULL,
            indicator_type VARCHAR(30) NOT NULL,
            period INTEGER NOT NULL,
            interval VARCHAR(20) NOT NULL,
            date DATE NOT NULL,
            value FLOAT,
            created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
        );
        CREATE TABLE generic_api_responses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            endpoint VARCHAR(255) NOT NULL,
            params_hash VARCHAR(64) NOT NULL,
            response_data TEXT NOT NULL,
            created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL
        );
        INSERT INTO companies VALUES
          ('AAPL', 'Apple', '2024-01-02 16:00:00', '2024-01-02 16:00:00'),
          ('AAPL', 'Apple Dup', '2024-01-02 16:00:00', '2024-01-02 16:00:00');
        """
    )
    finalize(c)

    # bad_type (volume as VARCHAR)
    d = NEG / "bad_type"
    d.mkdir(parents=True)
    c = make_db(d / "fmp.db")
    create_full_schema(c)
    c.execute("DROP TABLE historical_prices")
    c.executescript(
        """
        CREATE TABLE historical_prices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            symbol VARCHAR(20) NOT NULL,
            date DATE NOT NULL,
            open FLOAT, high FLOAT, low FLOAT, close FLOAT,
            volume VARCHAR(50),
            unadjusted_volume INTEGER,
            adj_open FLOAT, adj_high FLOAT, adj_low FLOAT, adj_close FLOAT,
            change FLOAT, change_percent FLOAT, vwap FLOAT,
            label VARCHAR(50), change_over_time FLOAT, price_data TEXT,
            created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL,
            UNIQUE(symbol, date)
        );
        """
    )
    seed_minimal_rows(c)
    finalize(c)

    # broken_fk
    d = NEG / "broken_fk"
    d.mkdir(parents=True)
    c = make_db(d / "fmp.db")
    create_full_schema(c)
    seed_minimal_rows(c)
    c.execute(
        """
        INSERT INTO historical_prices (
            symbol, date, open, high, low, close, volume, created_at, updated_at
        ) VALUES ('ZZZZ', '2024-01-03', 1,1,1,1,1, ?, ?)
        """,
        (NOW, NOW),
    )
    finalize(c)

    # unparseable_date
    d = NEG / "unparseable_date"
    d.mkdir(parents=True)
    c = make_db(d / "fmp.db")
    create_full_schema(c)
    seed_minimal_rows(c)
    c.execute(
        """
        INSERT INTO historical_prices (
            symbol, date, open, high, low, close, volume, created_at, updated_at
        ) VALUES ('AAPL', 'not-a-date', 1,1,1,1,1, ?, ?)
        """,
        (NOW, NOW),
    )
    finalize(c)

    # unknown_column_warn
    d = NEG / "unknown_column_warn"
    d.mkdir(parents=True)
    c = make_db(d / "fmp.db")
    create_full_schema(c)
    seed_minimal_rows(c)
    c.execute("ALTER TABLE companies ADD COLUMN extra_annotation VARCHAR(50) DEFAULT 'x'")
    finalize(c)

    # bad_enum_warn
    d = NEG / "bad_enum_warn"
    d.mkdir(parents=True)
    c = make_db(d / "fmp.db")
    create_full_schema(c)
    seed_minimal_rows(c)
    c.execute(
        """
        INSERT INTO financial_statements (
            symbol, statement_type, period, fiscal_date, statement_data,
            created_at, updated_at
        ) VALUES ('AAPL', 'not_a_real_type', 'annual', '2023-01-01', '{}', ?, ?)
        """,
        (NOW, NOW),
    )
    finalize(c)

    # all_empty_tables
    d = NEG / "all_empty_tables"
    d.mkdir(parents=True)
    c = make_db(d / "fmp.db")
    create_full_schema(c)
    finalize(c)

    print(f"OK: wrote sample under {SAMPLES} and negatives under {NEG}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Validate an FMP offline SQLite seed against the populate/loader contract.

Calibrated to:

  mise [tasks.populate]
    → copies at most one ``*.db`` from STATE_LOCATION
      → mcp_servers/fmp_server/data/fmp.db
  OfflineProvider / ORM models in ``mcp_servers/fmp_server/db/models/*``
    → companies, stock_quotes, historical_prices, financial_statements,
      technical_indicators, generic_api_responses

Populate input modes:
  1. Exactly one ``*.db`` in the seed dir — copied into place (validated).
  2. Zero ``*.db`` files — populate no-ops. Accept-gate rejects this for
     generated bundles (empty seed).
  3. More than one ``*.db`` — populate exits 1 (error here too).

CSV import via ``scripts/import_csv.py`` is an admin/dev path, **not** wired
into ``mise populate``. Files other than ``*.db`` are ignored by populate
(warned when present without a DB).

Loader tolerance (warnings, exit 0):
  - Extra/unknown columns or tables (ORM selects mapped columns only).
  - Unexpected ``statement_type`` / ``period`` / ``indicator_type`` /
    ``interval`` vocab (stored as free VARCHAR; no CHECK constraints).
  - Empty ``technical_indicators`` (production fixtures often have 0 rows).
  - Empty ``generic_api_responses`` (optional catch-all cache).
  - Invalid JSON in TEXT JSON fields (``_parse_json_field`` returns {}).

Seed-contract errors (exit 1):
  - Unreadable / non-SQLite file.
  - Missing required tables or required columns.
  - Column type mismatches vs the ORM.
  - NULL / empty identity-key cells.
  - Duplicate identity keys.
  - Zero rows in ``companies`` (offline symbol availability gate).
  - Child-table ``symbol`` values that do not resolve to ``companies.symbol``.

Exit 0 when clean (warnings allowed). Exit 1 on any error. Exit 2 on usage.
"""

from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Recommended vocab from FinancialStatement / TechnicalIndicator docstrings
# plus values written by download_fixtures (e.g. statement_type "balance").
RECOMMENDED_STATEMENT_TYPES = frozenset(
    {
        "income",
        "balance",
        "balance_sheet",
        "cash_flow",
        "income_ttm",
        "balance_sheet_ttm",
        "cash_flow_ttm",
        "key_metrics",
        "key_metrics_ttm",
        "ratios",
        "ratios_ttm",
        "growth",
        "income_growth",
        "balance_sheet_growth",
        "cash_flow_growth",
        "scores",
    }
)
RECOMMENDED_PERIODS = frozenset({"annual", "quarter"})
RECOMMENDED_INDICATOR_TYPES = frozenset(
    {
        "sma",
        "ema",
        "wma",
        "dema",
        "tema",
        "williams",
        "rsi",
        "adx",
        "standard_deviation",
    }
)
RECOMMENDED_INTERVALS = frozenset(
    {
        "1min",
        "5min",
        "15min",
        "30min",
        "1hour",
        "4hour",
        "1day",
    }
)

# Logical SQLite type affinities accepted for each ORM column kind.
TYPE_GROUPS = {
    "varchar": frozenset({"VARCHAR", "STRING", "TEXT", "CHAR", "NVARCHAR", "CHARACTER"}),
    "text": frozenset({"TEXT", "VARCHAR", "STRING", "CLOB", "CHAR"}),
    "date": frozenset({"DATE", "TEXT", "VARCHAR"}),  # SQLite date affinity is flexible
    "datetime": frozenset(
        {
            "DATETIME",
            "TIMESTAMP",
            "DATE",
            "TEXT",
            "VARCHAR",
        }
    ),
    "float": frozenset({"FLOAT", "REAL", "DOUBLE", "NUMERIC", "DECIMAL"}),
    "integer": frozenset({"INTEGER", "INT", "BIGINT", "SMALLINT", "TINYINT"}),
    "boolean": frozenset({"BOOLEAN", "BOOL", "INTEGER", "INT"}),
}


@dataclass(frozen=True)
class ColumnSpec:
    name: str
    kind: str
    required: bool = True  # must exist on the table
    identity: bool = False
    not_null: bool = False
    json_field: bool = False


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: tuple[ColumnSpec, ...]
    required: bool = True


def _cols(*specs: ColumnSpec) -> tuple[ColumnSpec, ...]:
    return specs


TABLES: dict[str, TableSpec] = {
    "companies": TableSpec(
        "companies",
        _cols(
            ColumnSpec("symbol", "varchar", identity=True, not_null=True),
            ColumnSpec("company_name", "varchar"),
            ColumnSpec("exchange", "varchar"),
            ColumnSpec("exchange_short_name", "varchar"),
            ColumnSpec("currency", "varchar"),
            ColumnSpec("country", "varchar"),
            ColumnSpec("sector", "varchar"),
            ColumnSpec("industry", "varchar"),
            ColumnSpec("cik", "varchar"),
            ColumnSpec("cusip", "varchar"),
            ColumnSpec("isin", "varchar"),
            ColumnSpec("description", "text"),
            ColumnSpec("website", "varchar"),
            ColumnSpec("logo", "varchar"),
            ColumnSpec("ceo", "varchar"),
            ColumnSpec("full_time_employees", "integer"),
            ColumnSpec("ipo_date", "varchar"),
            ColumnSpec("address", "varchar"),
            ColumnSpec("city", "varchar"),
            ColumnSpec("state", "varchar"),
            ColumnSpec("zip", "varchar"),
            ColumnSpec("phone", "varchar"),
            ColumnSpec("is_etf", "boolean"),
            ColumnSpec("is_actively_trading", "boolean"),
            ColumnSpec("profile_data", "text", json_field=True),
            ColumnSpec("created_at", "datetime", not_null=True),
            ColumnSpec("updated_at", "datetime", not_null=True),
        ),
    ),
    "stock_quotes": TableSpec(
        "stock_quotes",
        _cols(
            ColumnSpec("symbol", "varchar", identity=True, not_null=True),
            ColumnSpec("price", "float"),
            ColumnSpec("open", "float"),
            ColumnSpec("high", "float"),
            ColumnSpec("low", "float"),
            ColumnSpec("previous_close", "float"),
            ColumnSpec("change", "float"),
            ColumnSpec("change_percent", "float"),
            ColumnSpec("volume", "integer"),
            ColumnSpec("avg_volume", "integer"),
            ColumnSpec("market_cap", "integer"),
            ColumnSpec("pe_ratio", "float"),
            ColumnSpec("eps", "float"),
            ColumnSpec("fifty_two_week_high", "float"),
            ColumnSpec("fifty_two_week_low", "float"),
            ColumnSpec("shares_outstanding", "integer"),
            ColumnSpec("quote_data", "text", json_field=True),
            ColumnSpec("quote_timestamp", "datetime"),
            ColumnSpec("created_at", "datetime", not_null=True),
            ColumnSpec("updated_at", "datetime", not_null=True),
        ),
    ),
    "historical_prices": TableSpec(
        "historical_prices",
        _cols(
            ColumnSpec("id", "integer"),
            ColumnSpec("symbol", "varchar", identity=True, not_null=True),
            ColumnSpec("date", "date", identity=True, not_null=True),
            ColumnSpec("open", "float"),
            ColumnSpec("high", "float"),
            ColumnSpec("low", "float"),
            ColumnSpec("close", "float"),
            ColumnSpec("volume", "integer"),
            ColumnSpec("unadjusted_volume", "integer"),
            ColumnSpec("adj_open", "float"),
            ColumnSpec("adj_high", "float"),
            ColumnSpec("adj_low", "float"),
            ColumnSpec("adj_close", "float"),
            ColumnSpec("change", "float"),
            ColumnSpec("change_percent", "float"),
            ColumnSpec("vwap", "float"),
            ColumnSpec("label", "varchar"),
            ColumnSpec("change_over_time", "float"),
            ColumnSpec("price_data", "text", json_field=True),
            ColumnSpec("created_at", "datetime", not_null=True),
            ColumnSpec("updated_at", "datetime", not_null=True),
        ),
    ),
    "financial_statements": TableSpec(
        "financial_statements",
        _cols(
            ColumnSpec("id", "integer"),
            ColumnSpec("symbol", "varchar", identity=True, not_null=True),
            ColumnSpec("statement_type", "varchar", identity=True, not_null=True),
            ColumnSpec("period", "varchar", identity=True, not_null=True),
            ColumnSpec("fiscal_date", "date", identity=True, not_null=True),
            ColumnSpec("fiscal_year", "integer"),
            ColumnSpec("calendar_year", "integer"),
            ColumnSpec("reported_currency", "varchar"),
            ColumnSpec("filing_date", "date"),
            ColumnSpec("accepted_date", "varchar"),
            ColumnSpec("cik", "varchar"),
            ColumnSpec("statement_data", "text", not_null=True, json_field=True),
            ColumnSpec("created_at", "datetime", not_null=True),
            ColumnSpec("updated_at", "datetime", not_null=True),
        ),
    ),
    "technical_indicators": TableSpec(
        "technical_indicators",
        _cols(
            ColumnSpec("id", "integer"),
            ColumnSpec("symbol", "varchar", identity=True, not_null=True),
            ColumnSpec("indicator_type", "varchar", identity=True, not_null=True),
            ColumnSpec("period", "integer", identity=True, not_null=True),
            ColumnSpec("interval", "varchar", identity=True, not_null=True),
            ColumnSpec("date", "date", identity=True, not_null=True),
            ColumnSpec("value", "float"),
            ColumnSpec("open", "float"),
            ColumnSpec("high", "float"),
            ColumnSpec("low", "float"),
            ColumnSpec("close", "float"),
            ColumnSpec("volume", "integer"),
            ColumnSpec("indicator_data", "text", json_field=True),
            ColumnSpec("created_at", "datetime", not_null=True),
            ColumnSpec("updated_at", "datetime", not_null=True),
        ),
    ),
    "generic_api_responses": TableSpec(
        "generic_api_responses",
        _cols(
            ColumnSpec("id", "integer"),
            ColumnSpec("endpoint", "varchar", identity=True, not_null=True),
            ColumnSpec("params_hash", "varchar", identity=True, not_null=True),
            ColumnSpec("params_json", "text", json_field=True),
            ColumnSpec("symbol", "varchar"),
            ColumnSpec("response_data", "text", not_null=True, json_field=True),
            ColumnSpec("http_status", "integer"),
            ColumnSpec("created_at", "datetime", not_null=True),
            ColumnSpec("updated_at", "datetime", not_null=True),
        ),
    ),
}

CHILD_SYMBOL_TABLES = (
    "stock_quotes",
    "historical_prices",
    "financial_statements",
    "technical_indicators",
)


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)


def _normalize_type(raw: str) -> str:
    return raw.upper().split("(")[0].strip()


def _type_ok(kind: str, raw: str) -> bool:
    return _normalize_type(raw) in TYPE_GROUPS[kind]


def _list_db_files(seed_dir: Path) -> list[Path]:
    return sorted(p for p in seed_dir.iterdir() if p.is_file() and p.suffix == ".db")


def _table_columns(con: sqlite3.Connection, table: str) -> dict[str, str]:
    rows = con.execute(f"PRAGMA table_info({table})").fetchall()
    # cid, name, type, notnull, dflt_value, pk
    return {r[1]: r[2] or "TEXT" for r in rows}


def _existing_tables(con: sqlite3.Connection) -> set[str]:
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    return {r[0] for r in rows}


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def validate_sqlite_file(path: Path, report: Report) -> None:
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    except Exception as exc:  # noqa: BLE001 — surface open failures as seed errors
        report.err(f"{path.name}: not a readable SQLite database ({exc})")
        return

    try:
        present = _existing_tables(con)
        known = set(TABLES)
        for extra in sorted(present - known):
            report.warn(f"{path.name}: unknown table {extra!r} (ORM ignores; SQLite keeps it)")

        for tname, spec in TABLES.items():
            if tname not in present:
                if spec.required:
                    report.err(f"{path.name}: missing required table {tname!r}")
                continue
            cols = _table_columns(con, tname)
            known_cols = {c.name for c in spec.columns}
            for col in spec.columns:
                if col.name not in cols:
                    if col.required or col.identity or col.not_null:
                        report.err(f"{path.name}:{tname}: missing required column {col.name!r}")
                    continue
                if not _type_ok(col.kind, cols[col.name]):
                    report.err(
                        f"{path.name}:{tname}.{col.name}: expected {col.kind} type, "
                        f"got {cols[col.name]!r}"
                    )
            for unknown in sorted(set(cols) - known_cols):
                report.warn(
                    f"{path.name}:{tname}: unknown column {unknown!r} "
                    "(loader/ORM tolerates extra columns)"
                )

            identity = [c.name for c in spec.columns if c.identity]
            if identity and all(c in cols for c in identity):
                id_list = ", ".join(_quote_ident(c) for c in identity)
                null_sql = " OR ".join(
                    f"{_quote_ident(c)} IS NULL OR TRIM(CAST({_quote_ident(c)} AS TEXT)) = ''"
                    for c in identity
                )
                null_count = con.execute(
                    f"SELECT COUNT(*) FROM {_quote_ident(tname)} WHERE {null_sql}"
                ).fetchone()[0]
                if null_count:
                    report.err(
                        f"{path.name}:{tname}: {null_count} row(s) with NULL/empty identity "
                        f"({', '.join(identity)})"
                    )
                dup_rows = con.execute(
                    f"""
                    SELECT {id_list}, COUNT(*) AS n
                    FROM {_quote_ident(tname)}
                    GROUP BY {id_list}
                    HAVING COUNT(*) > 1
                    LIMIT 5
                    """
                ).fetchall()
                if dup_rows:
                    report.err(
                        f"{path.name}:{tname}: duplicate identity keys on "
                        f"({', '.join(identity)}); examples: {dup_rows!r}"
                    )

            # NOT NULL value checks for non-identity required cells
            for col in spec.columns:
                if not col.not_null or col.name not in cols or col.identity:
                    continue
                n = con.execute(
                    f"SELECT COUNT(*) FROM {_quote_ident(tname)} "
                    f"WHERE {_quote_ident(col.name)} IS NULL"
                ).fetchone()[0]
                if n:
                    report.err(
                        f"{path.name}:{tname}: {n} row(s) with NULL required column {col.name!r}"
                    )

            # JSON parse warnings (loader tolerates via _parse_json_field)
            for col in spec.columns:
                if not col.json_field or col.name not in cols:
                    continue
                rows = con.execute(
                    f"SELECT {_quote_ident(col.name)} FROM {_quote_ident(tname)} "
                    f"WHERE {_quote_ident(col.name)} IS NOT NULL "
                    f"AND TRIM(CAST({_quote_ident(col.name)} AS TEXT)) != '' "
                    f"LIMIT 50"
                ).fetchall()
                bad = 0
                for (raw,) in rows:
                    try:
                        json.loads(raw)
                    except (TypeError, json.JSONDecodeError):
                        bad += 1
                if bad:
                    report.warn(
                        f"{path.name}:{tname}.{col.name}: {bad} non-empty value(s) "
                        "failed JSON parse (offline _parse_json_field returns {{}})"
                    )

        # Content / FK / enum checks when companies exists
        if "companies" in present:
            company_cols = _table_columns(con, "companies")
            if "symbol" in company_cols:
                company_n = con.execute("SELECT COUNT(*) FROM companies").fetchone()[0]
                if company_n == 0:
                    report.err(
                        f"{path.name}: all-empty seed — companies has 0 rows "
                        "(offline OfflineProvider gates symbols on companies)"
                    )

                for tname in CHILD_SYMBOL_TABLES:
                    if tname not in present:
                        continue
                    cols = _table_columns(con, tname)
                    if "symbol" not in cols:
                        continue
                    orphans = con.execute(
                        f"""
                        SELECT DISTINCT symbol FROM {_quote_ident(tname)}
                        WHERE symbol IS NOT NULL
                          AND TRIM(CAST(symbol AS TEXT)) != ''
                          AND symbol NOT IN (SELECT symbol FROM companies)
                        LIMIT 5
                        """
                    ).fetchall()
                    if orphans:
                        report.err(
                            f"{path.name}:{tname}: symbol(s) do not resolve to "
                            f"companies.symbol; examples: {[r[0] for r in orphans]!r}"
                        )

                if "generic_api_responses" in present:
                    gcols = _table_columns(con, "generic_api_responses")
                    if "symbol" in gcols:
                        orphans = con.execute(
                            """
                            SELECT DISTINCT symbol FROM generic_api_responses
                            WHERE symbol IS NOT NULL
                              AND TRIM(CAST(symbol AS TEXT)) != ''
                              AND symbol NOT IN (SELECT symbol FROM companies)
                            LIMIT 5
                            """
                        ).fetchall()
                        if orphans:
                            report.err(
                                f"{path.name}:generic_api_responses: symbol(s) do not "
                                f"resolve to companies.symbol; examples: "
                                f"{[r[0] for r in orphans]!r}"
                            )
                    if "params_hash" in gcols:
                        hashes = con.execute(
                            """
                            SELECT params_hash FROM generic_api_responses
                            WHERE params_hash IS NOT NULL
                            """
                        ).fetchall()
                        bad_hash = sum(
                            1
                            for (h,) in hashes
                            if not (
                                isinstance(h, str)
                                and len(h) == 64
                                and all(c in "0123456789abcdefABCDEF" for c in h)
                            )
                        )
                        if bad_hash:
                            report.warn(
                                f"{path.name}:generic_api_responses: {bad_hash} "
                                "params_hash value(s) are not 64-char hex "
                                "(loader stores free VARCHAR; download_fixtures uses SHA256)"
                            )

                if "financial_statements" in present:
                    fs_cols = _table_columns(con, "financial_statements")
                    if "statement_type" in fs_cols:
                        for (stype,) in con.execute(
                            "SELECT DISTINCT statement_type FROM financial_statements"
                        ):
                            if stype not in RECOMMENDED_STATEMENT_TYPES:
                                report.warn(
                                    f"{path.name}:financial_statements: statement_type "
                                    f"{stype!r} not in recommended set "
                                    f"{sorted(RECOMMENDED_STATEMENT_TYPES)} "
                                    "(loader stores free VARCHAR)"
                                )
                    if "period" in fs_cols:
                        for (period,) in con.execute(
                            "SELECT DISTINCT period FROM financial_statements"
                        ):
                            if period not in RECOMMENDED_PERIODS:
                                report.warn(
                                    f"{path.name}:financial_statements: period "
                                    f"{period!r} not in recommended set "
                                    f"{sorted(RECOMMENDED_PERIODS)} "
                                    "(loader stores free VARCHAR)"
                                )

                if "technical_indicators" in present:
                    ti_n = con.execute("SELECT COUNT(*) FROM technical_indicators").fetchone()[0]
                    if ti_n == 0:
                        report.warn(
                            f"{path.name}:technical_indicators: empty table "
                            "(populate/offline tolerate missing indicator data)"
                        )
                    else:
                        ti_cols = _table_columns(con, "technical_indicators")
                        if "indicator_type" in ti_cols:
                            for (itype,) in con.execute(
                                "SELECT DISTINCT indicator_type FROM technical_indicators"
                            ):
                                if itype not in RECOMMENDED_INDICATOR_TYPES:
                                    report.warn(
                                        f"{path.name}:technical_indicators: "
                                        f"indicator_type {itype!r} not in recommended set "
                                        f"{sorted(RECOMMENDED_INDICATOR_TYPES)} "
                                        "(loader stores free VARCHAR)"
                                    )
                        if "interval" in ti_cols:
                            for (interval,) in con.execute(
                                "SELECT DISTINCT interval FROM technical_indicators"
                            ):
                                if interval not in RECOMMENDED_INTERVALS:
                                    report.warn(
                                        f"{path.name}:technical_indicators: "
                                        f"interval {interval!r} not in recommended set "
                                        f"{sorted(RECOMMENDED_INTERVALS)} "
                                        "(loader stores free VARCHAR)"
                                    )

                if "generic_api_responses" in present:
                    g_n = con.execute("SELECT COUNT(*) FROM generic_api_responses").fetchone()[0]
                    if g_n == 0:
                        report.warn(
                            f"{path.name}:generic_api_responses: empty table "
                            "(optional catch-all; offline falls back to other tables)"
                        )

                # Date parse check on historical_prices.date
                if "historical_prices" in present:
                    hp_cols = _table_columns(con, "historical_prices")
                    if "date" in hp_cols:
                        bad_dates = con.execute(
                            """
                            SELECT COUNT(*) FROM historical_prices
                            WHERE date IS NOT NULL
                              AND date(date) IS NULL
                            """
                        ).fetchone()[0]
                        if bad_dates:
                            report.err(
                                f"{path.name}:historical_prices: {bad_dates} row(s) with "
                                "unparseable date values"
                            )
    finally:
        con.close()


def validate_seed_dir(seed_dir: Path) -> Report:
    report = Report()
    if not seed_dir.is_dir():
        report.err(f"not a directory: {seed_dir}")
        return report

    dbs = _list_db_files(seed_dir)
    other = sorted(
        p.name
        for p in seed_dir.iterdir()
        if p.is_file() and p.suffix != ".db" and p.name not in {".gitkeep", ".gitignore"}
    )
    if len(dbs) == 0:
        if other:
            report.warn(
                f"{seed_dir}: non-DB files present ({', '.join(other)}) but populate only "
                "copies *.db — CSV/admin import_csv.py is not on the populate path"
            )
        report.err(
            f"{seed_dir}: no *.db file — seed-generation bundles must include "
            "exactly one SQLite DB (populate no-ops on zero files, but that is not a "
            "valid generated seed)"
        )
        return report
    if len(dbs) > 1:
        names = ", ".join(p.name for p in dbs)
        report.err(
            f"{seed_dir}: expected at most 1 .db file but found {len(dbs)} "
            f"({names}) — populate exits 1 on multiples"
        )
        return report

    if other:
        report.warn(
            f"{seed_dir}: ignoring non-DB files ({', '.join(other)}); populate only copies *.db"
        )

    validate_sqlite_file(dbs[0], report)
    return report


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("Usage: validate_sqlite_seed.py <seed_dir>", file=sys.stderr)
        return 2
    seed_dir = Path(argv[1])
    report = validate_seed_dir(seed_dir)

    for w in report.warnings:
        print(f"WARN: {w}")
    if report.errors:
        print(f"FAIL: {len(report.errors)} error(s) in {seed_dir}")
        for e in report.errors:
            print(f"  - {e}")
        return 1

    print(
        f"OK: validated {seed_dir}"
        + (f" ({len(report.warnings)} warning(s))" if report.warnings else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))

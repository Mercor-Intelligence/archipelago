"""DuckDB database for fixture storage."""

import json
import threading
from pathlib import Path

import duckdb
from loguru import logger
from utils.config import FIXTURES_DIR

DB_PATH = FIXTURES_DIR / "fixtures.duckdb"
SAMPLE_DATA_DIR = Path(__file__).parent.parent / "data"

_local = threading.local()
_init_lock = threading.Lock()
_schema_initialized = False


def get_connection() -> duckdb.DuckDBPyConnection:
    """Get or create a thread-local DuckDB connection."""
    global _schema_initialized
    conn = getattr(_local, "connection", None)
    if conn is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        conn = duckdb.connect(str(DB_PATH))
        _local.connection = conn

        with _init_lock:
            if not _schema_initialized:
                _init_schema(conn)
                _schema_initialized = True

    return conn


def _init_schema(conn: duckdb.DuckDBPyConnection) -> None:
    """Initialize database schema if tables don't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bonds (
            isin VARCHAR PRIMARY KEY,
            issuer_name VARCHAR,
            ticker VARCHAR,
            coupon DOUBLE,
            coupon_frequency VARCHAR,
            interest_type VARCHAR,
            maturity_date DATE,
            issue_date DATE,
            currency VARCHAR,
            country VARCHAR,
            issue_rating VARCHAR,
            issue_rating_group VARCHAR,
            issuer_rating VARCHAR,
            asset_class VARCHAR,
            sector VARCHAR
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS bond_reference (
            isin VARCHAR PRIMARY KEY,
            issuer_name VARCHAR,
            ticker VARCHAR,
            coupon DOUBLE,
            coupon_frequency VARCHAR,
            interest_type VARCHAR,
            maturity_date DATE,
            issue_date DATE,
            currency VARCHAR,
            country VARCHAR,
            issue_rating VARCHAR,
            issue_rating_group VARCHAR,
            issuer_rating VARCHAR,
            asset_class VARCHAR,
            sector VARCHAR,
            issuer_lei VARCHAR,
            issuer_country VARCHAR
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS bond_pricing (
            isin VARCHAR,
            pricing_date DATE,
            price DOUBLE,
            yield_to_maturity DOUBLE,
            duration DOUBLE,
            modified_duration DOUBLE,
            convexity DOUBLE,
            estimated_volume DOUBLE,
            PRIMARY KEY (isin, pricing_date)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS bond_cashflows (
            isin VARCHAR,
            payment_date DATE,
            payment_amount DOUBLE,
            payment_type VARCHAR,
            PRIMARY KEY (isin, payment_date, payment_type)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS inflation_factors (
            country_code VARCHAR,
            factor_date DATE,
            inflation_factor DOUBLE,
            PRIMARY KEY (country_code, factor_date)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS muni_bonds (
            isin VARCHAR PRIMARY KEY,
            issuer_name VARCHAR,
            coupon DOUBLE,
            maturity_date DATE,
            state VARCHAR,
            sector VARCHAR,
            source_of_repayment VARCHAR,
            is_insured BOOLEAN
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS muni_reference (
            isin VARCHAR PRIMARY KEY,
            issuer_name VARCHAR,
            coupon DOUBLE,
            coupon_type VARCHAR,
            coupon_frequency VARCHAR,
            maturity_date DATE,
            issue_date DATE,
            dated_date DATE,
            first_coupon_date DATE,
            state VARCHAR,
            sector VARCHAR,
            purpose VARCHAR,
            source_of_repayment VARCHAR,
            is_insured BOOLEAN,
            insurer VARCHAR,
            credit_enhancement VARCHAR,
            tax_status VARCHAR,
            callable BOOLEAN,
            call_date DATE,
            call_price DOUBLE,
            underwriters VARCHAR[]
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS muni_pricing (
            isin VARCHAR,
            trade_date DATE,
            price DOUBLE,
            yield_to_maturity DOUBLE,
            yield_to_call DOUBLE,
            trade_amount DOUBLE,
            trade_type VARCHAR,
            settlement_date DATE,
            PRIMARY KEY (isin, trade_date, trade_type)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS muni_cashflows (
            isin VARCHAR,
            payment_date DATE,
            payment_amount DOUBLE,
            payment_type VARCHAR,
            PRIMARY KEY (isin, payment_date, payment_type)
        )
    """)

    # Create indexes for common query patterns
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bonds_country ON bonds(country)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bonds_currency ON bonds(currency)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bonds_maturity ON bonds(maturity_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bond_pricing_isin ON bond_pricing(isin)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_bond_pricing_date ON bond_pricing(pricing_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_muni_bonds_state ON muni_bonds(state)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_muni_bonds_sector ON muni_bonds(sector)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_muni_pricing_isin ON muni_pricing(isin)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_muni_pricing_date ON muni_pricing(trade_date)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_inflation_country ON inflation_factors(country_code)"
    )

    logger.debug("Database schema initialized")
    _load_sample_data_if_empty(conn)


def _load_sample_data_if_empty(conn: duckdb.DuckDBPyConnection) -> None:
    """Load sample data from JSON files if database is empty."""
    bond_count = conn.execute("SELECT COUNT(*) FROM bonds").fetchone()[0]
    muni_count = conn.execute("SELECT COUNT(*) FROM muni_bonds").fetchone()[0]

    if bond_count > 0 or muni_count > 0:
        return

    if not SAMPLE_DATA_DIR.exists():
        return

    logger.info("Loading sample data from data/ directory")

    sample_files = {
        "bonds": (
            "sample_bonds.json",
            [
                "isin",
                "issuer_name",
                "ticker",
                "coupon",
                "coupon_frequency",
                "interest_type",
                "maturity_date",
                "issue_date",
                "currency",
                "country",
                "issue_rating",
                "issue_rating_group",
                "issuer_rating",
                "asset_class",
                "sector",
            ],
        ),
        "bond_reference": (
            "sample_bond_reference.json",
            [
                "isin",
                "issuer_name",
                "ticker",
                "coupon",
                "coupon_frequency",
                "interest_type",
                "maturity_date",
                "issue_date",
                "currency",
                "country",
                "issue_rating",
                "issue_rating_group",
                "issuer_rating",
                "asset_class",
                "sector",
                "issuer_lei",
                "issuer_country",
            ],
        ),
        "bond_pricing": (
            "sample_bond_pricing.json",
            [
                "isin",
                "pricing_date",
                "price",
                "yield_to_maturity",
                "duration",
                "modified_duration",
                "convexity",
                "estimated_volume",
            ],
        ),
        "bond_cashflows": (
            "sample_bond_cashflows.json",
            [
                "isin",
                "payment_date",
                "payment_amount",
                "payment_type",
            ],
        ),
        "muni_bonds": (
            "sample_muni_bonds.json",
            [
                "isin",
                "issuer_name",
                "coupon",
                "maturity_date",
                "state",
                "sector",
                "source_of_repayment",
                "is_insured",
            ],
        ),
        "muni_reference": (
            "sample_muni_reference.json",
            [
                "isin",
                "issuer_name",
                "coupon",
                "coupon_type",
                "coupon_frequency",
                "maturity_date",
                "issue_date",
                "dated_date",
                "first_coupon_date",
                "state",
                "sector",
                "purpose",
                "source_of_repayment",
                "is_insured",
                "insurer",
                "credit_enhancement",
                "tax_status",
                "callable",
                "call_date",
                "call_price",
                "underwriters",
            ],
        ),
        "muni_pricing": (
            "sample_muni_pricing.json",
            [
                "isin",
                "trade_date",
                "price",
                "yield_to_maturity",
                "yield_to_call",
                "trade_amount",
                "trade_type",
                "settlement_date",
            ],
        ),
        "muni_cashflows": (
            "sample_muni_cashflows.json",
            [
                "isin",
                "payment_date",
                "payment_amount",
                "payment_type",
            ],
        ),
        "inflation_factors": (
            "sample_inflation_factors.json",
            [
                "country_code",
                "factor_date",
                "inflation_factor",
            ],
        ),
    }

    for table, (filename, columns) in sample_files.items():
        filepath = SAMPLE_DATA_DIR / filename
        if not filepath.exists():
            continue

        data = json.loads(filepath.read_text())
        for record in data:
            placeholders = ", ".join(["?" for _ in columns])
            col_names = ", ".join(columns)
            values = [record.get(col) for col in columns]
            conn.execute(
                f"INSERT OR REPLACE INTO {table} ({col_names}) VALUES ({placeholders})",
                values,
            )

        logger.debug(f"Loaded {len(data)} records into {table}")


def close_connection() -> None:
    """Close the current thread's database connection."""
    conn = getattr(_local, "connection", None)
    if conn is not None:
        conn.close()
        _local.connection = None


def reset_database() -> None:
    """Drop all tables and reinitialize.  Use for testing or full refresh."""
    global _schema_initialized
    conn = get_connection()
    tables = [
        "bonds",
        "bond_reference",
        "bond_pricing",
        "bond_cashflows",
        "inflation_factors",
        "muni_bonds",
        "muni_reference",
        "muni_pricing",
        "muni_cashflows",
    ]
    with _init_lock:
        for table in tables:
            conn.execute(f"DROP TABLE IF EXISTS {table}")
        _schema_initialized = False
        _init_schema(conn)
        _schema_initialized = True
    logger.info("Database reset complete")


def get_db_path() -> Path:
    """Get the path to the database file."""
    return DB_PATH

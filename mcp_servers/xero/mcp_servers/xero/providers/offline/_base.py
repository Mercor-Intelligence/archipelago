"""Base functionality for offline provider."""

import asyncio
import threading

from loguru import logger

from mcp_servers.xero.config import Config
from mcp_servers.xero.db.session import init_db


class OfflineProviderBase:
    """
    Base class for offline provider with database access.

    Database starts empty - users upload CSV data via import_csv tool.
    """

    _db_initialized = False
    _init_lock = threading.Lock()

    def __init__(self):
        """Initialize offline provider and database."""
        logger.info("Initializing offline provider with database")

        # Initialize database tables if not already done
        if not OfflineProviderBase._db_initialized:
            with OfflineProviderBase._init_lock:
                if not OfflineProviderBase._db_initialized:
                    self._ensure_db_initialized()
                    OfflineProviderBase._db_initialized = True

    def _ensure_db_initialized(self) -> None:
        """Ensure database is initialized, handling both sync and async contexts."""
        try:
            try:
                asyncio.get_running_loop()
                logger.debug("Async context detected - initializing database in background thread")

                exception_holder = []

                def init_in_thread():
                    try:
                        _initialize_database_tables()
                    except Exception as e:
                        exception_holder.append(e)

                thread = threading.Thread(target=init_in_thread)
                thread.start()
                thread.join()

                if exception_holder:
                    raise exception_holder[0]
            except RuntimeError:
                _initialize_database_tables()
        except Exception as e:
            logger.error(f"Failed to initialize database: {e}")
            raise


_SAMPLE_BALANCE_SHEET_ACCOUNTS: list[dict[str, object]] = [
    {
        "AccountID": "562555f2-8cde-4ce9-8203-0363922537a4",
        "Code": "090",
        "Name": "Bank",
        "Status": "ACTIVE",
        "Type": "BANK",
        "Class": "ASSET",
        "CurrencyCode": "USD",
        "OpeningBalance": 50000.0,
    },
    {
        "AccountID": "a7f0e2d1-4c8b-4a3d-9e5f-1a2b3c4d5e6f",
        "Code": "310",
        "Name": "Accounts Receivable",
        "Status": "ACTIVE",
        "Type": "CURRENT",
        "Class": "ASSET",
        "CurrencyCode": "USD",
        "OpeningBalance": 25000.0,
    },
    {
        "AccountID": "b8e1f3d2-5c9a-4b4e-0f6a-2b3c4d5e6f7a",
        "Code": "140",
        "Name": "Inventory",
        "Status": "ACTIVE",
        "Type": "CURRENT",
        "Class": "ASSET",
        "CurrencyCode": "USD",
        "OpeningBalance": 10000.0,
    },
    {
        "AccountID": "c9f2e4d3-6d0b-4c5f-1a7b-3c4d5e6f7a8b",
        "Code": "150",
        "Name": "Prepaid Expenses",
        "Status": "ACTIVE",
        "Type": "CURRENT",
        "Class": "ASSET",
        "CurrencyCode": "USD",
        "OpeningBalance": 3000.0,
    },
    {
        "AccountID": "d0e3f5d4-7e1c-4d6a-2b8c-4d5e6f7a8b9c",
        "Code": "160",
        "Name": "Office Equipment",
        "Status": "ACTIVE",
        "Type": "FIXED",
        "Class": "ASSET",
        "CurrencyCode": "USD",
        "OpeningBalance": 15000.0,
    },
    {
        "AccountID": "e1f4e6d5-8f2d-4e7b-3c9d-5e6f7a8b9c0d",
        "Code": "170",
        "Name": "Furniture & Fixtures",
        "Status": "ACTIVE",
        "Type": "FIXED",
        "Class": "ASSET",
        "CurrencyCode": "USD",
        "OpeningBalance": 8000.0,
    },
    {
        "AccountID": "f2e5f7d6-9e3e-4f8c-4d0e-6f7a8b9c0d1e",
        "Code": "180",
        "Name": "Accumulated Depreciation",
        "Status": "ACTIVE",
        "Type": "FIXED",
        "Class": "ASSET",
        "CurrencyCode": "USD",
        "OpeningBalance": -4000.0,
    },
    {
        "AccountID": "a3e6f8d7-0f4f-5a9d-5e1f-7a8b9c0d1e2f",
        "Code": "200",
        "Name": "Accounts Payable",
        "Status": "ACTIVE",
        "Type": "CURRENT",
        "Class": "LIABILITY",
        "CurrencyCode": "USD",
        "OpeningBalance": -15000.0,
    },
    {
        "AccountID": "b4f7e9d8-1e5e-6b0e-6f2a-8b9c0d1e2f3a",
        "Code": "210",
        "Name": "Short-term Loan",
        "Status": "ACTIVE",
        "Type": "LIABILITY",
        "Class": "LIABILITY",
        "CurrencyCode": "USD",
        "OpeningBalance": -10000.0,
    },
    {
        "AccountID": "c5e8f0d9-2f6f-7c1f-7a3b-9c0d1e2f3a4b",
        "Code": "220",
        "Name": "Accrued Expenses",
        "Status": "ACTIVE",
        "Type": "LIABILITY",
        "Class": "LIABILITY",
        "CurrencyCode": "USD",
        "OpeningBalance": -3000.0,
    },
    {
        "AccountID": "d6f9e1d0-3e7e-8d2e-8b4c-0d1e2f3a4b5c",
        "Code": "230",
        "Name": "Long-term Loan",
        "Status": "ACTIVE",
        "Type": "LIABILITY",
        "Class": "LIABILITY",
        "CurrencyCode": "USD",
        "OpeningBalance": -19000.0,
    },
    {
        "AccountID": "g6f8f0d9-2f6f-7c1f-7a3b-9c0d1e2f3b5c",
        "Code": "300",
        "Name": "Common Stock",
        "Status": "ACTIVE",
        "Type": "EQUITY",
        "Class": "EQUITY",
        "CurrencyCode": "USD",
        "OpeningBalance": -10000.0,
    },
    {
        "AccountID": "h7f9g1d0-3e7e-8d2e-8b4c-0d1e2f3b4c5d",
        "Code": "320",
        "Name": "Retained Earnings",
        "Status": "ACTIVE",
        "Type": "EQUITY",
        "Class": "EQUITY",
        "CurrencyCode": "USD",
        "OpeningBalance": -50000.0,
    },
]


async def _seed_balance_sheet_accounts() -> None:
    """Seed the database with sample balance sheet accounts if empty."""
    from sqlalchemy import select

    from mcp_servers.xero.db.models import Account
    from mcp_servers.xero.db.session import async_session

    async with async_session() as session:
        async with session.begin():
            result = await session.execute(select(Account).limit(1))
            if result.scalar_one_or_none():
                return
            for account_data in _SAMPLE_BALANCE_SHEET_ACCOUNTS:
                session.add(Account.from_dict(account_data))


async def _ensure_required_columns() -> None:
    """Ensure any new columns exist on the persisted tables."""
    from sqlalchemy import text

    from mcp_servers.xero.db.session import async_session

    table_columns: dict[str, list[tuple[str, str]]] = {
        "accounts": [("opening_balance", "FLOAT DEFAULT 0.0")],
        "payments": [
            ("payment_type", "TEXT"),
            ("reference", "TEXT"),
            ("account_id", "TEXT"),
            ("currency_rate", "FLOAT"),
        ],
        "bank_transactions": [
            ("reference", "TEXT"),
            ("bank_account_id", "TEXT"),
            ("bank_account_code", "TEXT"),
            ("bank_account_name", "TEXT"),
            ("bank_account_currency", "TEXT"),
            ("currency_code", "TEXT"),
        ],
    }

    async with async_session() as session:
        async with session.begin():
            for table_name, columns in table_columns.items():
                result = await session.execute(text(f"PRAGMA table_info('{table_name}')"))
                existing_columns = {row["name"] for row in result.mappings()}

                for column_name, column_def in columns:
                    if column_name in existing_columns:
                        continue
                    await session.execute(
                        text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_def}")
                    )


def _should_seed_balance_sheet_accounts() -> bool:
    """Determine whether demo data should be seeded."""
    return Config().xero_seed_demo_data


def _seed_balance_sheet_accounts_if_enabled() -> bool:
    """Seed the balance sheet accounts if the feature flag is enabled."""
    if _should_seed_balance_sheet_accounts():
        asyncio.run(_seed_balance_sheet_accounts())
        return True

    logger.debug("Demo data seeding disabled (XERO_SEED_DEMO_DATA not enabled)")
    return False


def _initialize_database_tables() -> None:
    """Initialize the database schema and optionally seed demo data."""
    asyncio.run(init_db())
    asyncio.run(_ensure_required_columns())
    seeded = _seed_balance_sheet_accounts_if_enabled()

    if seeded:
        logger.info("Database initialized (demo data seeded, ready for CSV uploads)")
    else:
        logger.info("Database initialized (empty - ready for CSV uploads)")

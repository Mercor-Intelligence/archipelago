#!/usr/bin/env python3
"""
Setup script to load demo data from CSV templates into Xero MCP database.

Usage:
    uv run python mcp_servers/xero/templates/setup_demo_data.py

This will populate the empty database with sample data from the CSV templates.
"""

import asyncio
import csv
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(project_root))

from mcp_servers.xero.db.models import (  # noqa: E402
    Account,
    BankTransaction,
    Contact,
    Invoice,
    Payment,
)
from mcp_servers.xero.db.session import async_session, init_db  # noqa: E402


async def load_accounts(csv_path: Path):
    """Load accounts from CSV."""
    async with async_session() as session:
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            count = 0
            for row in reader:
                account = Account.from_dict(row)
                session.add(account)
                count += 1
            await session.commit()
        return count


async def load_contacts(csv_path: Path):
    """Load contacts from CSV."""
    async with async_session() as session:
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            count = 0
            for row in reader:
                contact = Contact.from_dict(row)
                session.add(contact)
                count += 1
            await session.commit()
        return count


async def load_invoices(csv_path: Path):
    """Load invoices from CSV."""
    async with async_session() as session:
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            count = 0
            for row in reader:
                invoice = Invoice.from_dict(row)
                session.add(invoice)
                count += 1
            await session.commit()
        return count


async def load_payments(csv_path: Path):
    """Load payments from CSV."""
    async with async_session() as session:
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            count = 0
            for row in reader:
                payment = Payment.from_dict(row)
                session.add(payment)
                count += 1
            await session.commit()
        return count


async def load_bank_transactions(csv_path: Path):
    """Load bank transactions from CSV."""
    async with async_session() as session:
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            count = 0
            for row in reader:
                bank_txn = BankTransaction.from_dict(row)
                session.add(bank_txn)
                count += 1
            await session.commit()
        return count


async def main():
    """Load all demo data from CSV templates."""
    print("🚀 Setting up Xero MCP demo data...\n")

    # Initialize database
    await init_db()
    print("✅ Database initialized\n")

    # Get template directory
    template_dir = Path(__file__).parent

    # Load data from templates
    loaders = [
        ("Accounts", template_dir / "accounts_template.csv", load_accounts),
        ("Contacts", template_dir / "contacts_template.csv", load_contacts),
        ("Invoices", template_dir / "invoices_template.csv", load_invoices),
        ("Payments", template_dir / "payments_template.csv", load_payments),
        (
            "Bank Transactions",
            template_dir / "bank_transactions_template.csv",
            load_bank_transactions,
        ),
    ]

    for name, csv_path, loader_func in loaders:
        if csv_path.exists():
            try:
                count = await loader_func(csv_path)
                print(f"✅ Loaded {count} {name}")
            except Exception as e:
                print(f"❌ Failed to load {name}: {e}")
        else:
            print(f"⚠️  Template not found: {csv_path.name}")

    print("\n🎉 Demo data setup complete!")
    print("\nYou can now:")
    print("  - Start the MCP server and see data in GET tools")
    print("  - Use xero.ResetState to clear all data")
    print("  - Upload your own CSV files via the GUI")


if __name__ == "__main__":
    asyncio.run(main())

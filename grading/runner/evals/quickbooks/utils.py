"""Utility functions for QuickBooks verifiers."""

import csv
import io
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Any, TypedDict

from loguru import logger

# ====================
# Date Parsing
# ====================


def parse_date(date_str: str | None) -> datetime | None:
    """Parse various date formats from QuickBooks data.

    Always returns an offset-aware datetime in UTC to ensure consistent comparisons.

    Supported formats:
    - ISO date: YYYY-MM-DD
    - ISO datetime: YYYY-MM-DDTHH:MM:SS
    - SQLite datetime: YYYY-MM-DD HH:MM:SS
    """
    if not date_str:
        return None

    # Try SQLite datetime format (YYYY-MM-DD HH:MM:SS)
    try:
        if " " in date_str and "T" not in date_str:
            naive_dt = datetime.strptime(date_str.split(".")[0], "%Y-%m-%d %H:%M:%S")
            return naive_dt.replace(tzinfo=UTC)
    except ValueError:
        pass

    # Try common date formats
    try:
        # Handle ISO format with optional time component
        date_part = date_str.split("T")[0] if "T" in date_str else date_str
        naive_dt = datetime.strptime(date_part, "%Y-%m-%d")
        return naive_dt.replace(tzinfo=UTC)
    except ValueError:
        pass

    return None


# Type definitions
class JournalEntryLine(TypedDict):
    """Structure for a journal entry line (debit or credit)."""

    account: str
    amount: Decimal
    description: str


class JournalEntry(TypedDict):
    """Structure for a complete journal entry."""

    id: str
    doc_number: str
    txn_date: str
    description: str
    debits: list[JournalEntryLine]
    credits: list[JournalEntryLine]


# ====================
# Fuzzy Matching
# ====================


def find_line_item_fuzzy(
    report_data: dict[str, Decimal], line_item_name: str
) -> Decimal | None:
    """Find line item with fuzzy matching (60% threshold).
    Handles: "Total Revenue" vs "Revenue - Total"
    """
    # Exact match
    if line_item_name in report_data:
        return report_data[line_item_name]

    # Case-insensitive
    for key, value in report_data.items():
        if key.lower() == line_item_name.lower():
            return value

    # Fuzzy (60% similarity - includes exact threshold matches)
    best_match, best_score = None, Decimal("0.6")
    for key, value in report_data.items():
        score = Decimal(
            str(SequenceMatcher(None, key.lower(), line_item_name.lower()).ratio())
        )
        # Use > to return first match on ties, but check threshold with >=
        if score >= Decimal("0.6") and (best_match is None or score > best_score):
            best_score, best_match = score, value

    if best_match:
        logger.info(f"Fuzzy matched '{line_item_name}' with score {best_score:.2f}")
    return best_match


def find_journal_entry_fuzzy(
    journal_entries: list[JournalEntry], je_description: str
) -> JournalEntry | None:
    """Find JE by doc_number or description (fuzzy 60%)."""
    # Exact matches
    for je in journal_entries:
        if (
            je.get("doc_number") == je_description
            or je.get("description") == je_description
        ):
            return je

    # Fuzzy (includes exact threshold matches)
    best_match, best_score = None, Decimal("0.6")
    for je in journal_entries:
        desc = je.get("description") or je.get("doc_number") or ""
        score = Decimal(
            str(SequenceMatcher(None, desc.lower(), je_description.lower()).ratio())
        )
        # Use > to return first match on ties, but check threshold with >=
        if score >= Decimal("0.6") and (best_match is None or score > best_score):
            best_score, best_match = score, je

    if best_match:
        logger.info(f"Fuzzy matched JE '{je_description}' with score {best_score:.2f}")
    return best_match


# ====================
# JE Verification
# ====================


def verify_je_lines(
    actual_lines: list[JournalEntryLine],
    expected_lines: list[dict[str, Any]],
    tolerance: Decimal,
) -> bool:
    """Verify JE lines match expected (fuzzy account + tolerance).

    Args:
        actual_lines: List of actual JE lines from database
        expected_lines: List of expected lines (flexible dict format from user input)
        tolerance: Acceptable variance in amounts
    """
    # Track which actual lines have been matched to prevent reuse
    matched_indices = set()

    for expected in expected_lines:
        exp_account = expected["account"]
        try:
            exp_amount = Decimal(str(expected["amount"]))
        except (ValueError, TypeError, InvalidOperation) as e:
            logger.warning(
                f"Invalid amount for account '{exp_account}': {expected['amount']} ({e})"
            )
            return False

        found = False
        for idx, actual in enumerate(actual_lines):
            # Skip already matched lines
            if idx in matched_indices:
                continue

            # Fuzzy account match (80%)
            account_match = (
                actual["account"].lower() == exp_account.lower()
                or SequenceMatcher(
                    None, actual["account"].lower(), exp_account.lower()
                ).ratio()
                > 0.8
            )
            actual_amount = Decimal(str(actual["amount"]))
            amount_match = abs(actual_amount - exp_amount) <= tolerance

            if account_match and amount_match:
                found = True
                matched_indices.add(idx)  # Mark this line as used
                break

        if not found:
            logger.warning(f"Expected JE line not found: {exp_account} ${exp_amount}")
            return False

    return True


# ====================
# Reference Artifacts
# ====================


async def parse_budget_csv_from_artifacts(
    budget_artifact_id: str, artifacts_to_reference: dict[str, bytes]
) -> dict[str, Decimal]:
    """Parse budget CSV from reference artifacts.
    Expected: Line Item,Budget | Total Revenue,225000
    Returns: {"Total Revenue": 225000, ...}
    """
    if budget_artifact_id not in artifacts_to_reference:
        logger.error(f"Budget artifact '{budget_artifact_id}' not found")
        return {}

    budget_text = artifacts_to_reference[budget_artifact_id].decode("utf-8")
    budget_data = {}

    for row in csv.DictReader(io.StringIO(budget_text)):
        line_item = row.get("Line Item") or row.get("line_item") or row.get("Account")
        budget_value = row.get("Budget") or row.get("budget") or row.get("Value")

        if line_item and budget_value:
            try:
                clean_value = budget_value.replace("$", "").replace(",", "").strip()
                budget_data[line_item] = Decimal(clean_value)
            except (ValueError, TypeError, InvalidOperation) as e:
                logger.warning(f"Could not parse budget for '{line_item}': {e}")

    logger.info(f"Parsed {len(budget_data)} budget line items")
    return budget_data

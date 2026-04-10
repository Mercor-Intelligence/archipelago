#!/usr/bin/env python3
"""
CSV Batch Import Script for Xero MCP Server

Scans a directory for CSV files and automatically imports them into the
database based on detected entity types.

IMPORTANT: Files are automatically sorted by dependency phase to prevent
foreign key violations. The script detects entity types from CSV headers
and imports foundation entities (Accounts, Contacts) before dependent
entities (Invoices, Payments, etc.). File naming does not affect import order.

See docs/CSV_IMPORT_GUIDE.md for the complete import order (19 entity types).

Usage:
    python scripts/import_csv_directory.py /path/to/csv/directory [--mode replace]

Supported Entity Types (19 total):
    Phase 1 - Foundation:
      - Accounts (AccountID)
      - Contacts (ContactID)
      - Asset Types (assetTypeId)

    Phase 2 - Core:
      - Assets (assetId)
      - Projects (projectId)
      - Budgets (BudgetID)

    Phase 3 - Transactional:
      - Invoices (InvoiceID)
      - Purchase Orders (PurchaseOrderID)
      - Quotes (QuoteID)
      - Credit Notes (CreditNoteID)
      - Bank Transactions (BankTransactionID)

    Phase 4 - Payments:
      - Payments (PaymentID)
      - Overpayments (OverpaymentID)
      - Prepayments (PrepaymentID)

    Phase 5 - Adjustments:
      - Bank Transfers (BankTransferID)
      - Journals (JournalID)

    Phase 6 - Supporting:
      - Time Entries (timeEntryId)
      - Files (Id)
      - Folders (Id)
      - Associations (Id)
"""

import argparse
import asyncio
import csv
import re
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from loguru import logger
from mcp_servers.xero.models import UploadCSVInput
from mcp_servers.xero.providers.offline._base import OfflineProviderBase
from mcp_servers.xero.tools.xero_tools import (
    set_provider,
    upload_accounts_csv,
    upload_asset_types_csv,
    upload_assets_csv,
    upload_associations_csv,
    upload_bank_transactions_csv,
    upload_bank_transfers_csv,
    upload_budgets_csv,
    upload_contacts_csv,
    upload_credit_notes_csv,
    upload_files_csv,
    upload_folders_csv,
    upload_invoices_csv,
    upload_journals_csv,
    upload_overpayments_csv,
    upload_payments_csv,
    upload_prepayments_csv,
    upload_projects_csv,
    upload_purchase_orders_csv,
    upload_quotes_csv,
    upload_time_entries_csv,
)

# Entity type detection mapping - ordered by dependency
# Files are processed alphabetically, so use numbered prefixes (01_, 02_, etc.)
# Each entity includes:
#   - id_field: Primary ID column name
#   - alt_id_fields: Alternative ID column names (snake_case variants)
#   - required_headers: Minimum headers for header-based detection fallback
ENTITY_MAPPINGS = {
    # Phase 1: Foundation Entities (No Dependencies)
    "AccountID": {
        "name": "Accounts",
        "upload_func": upload_accounts_csv,
        "id_field": "AccountID",
        "alt_id_fields": ["account_id", "Account_ID"],
        "required_headers": {"name", "type"},  # Minimum for accounts
        "phase": 1,
    },
    "ContactID": {
        "name": "Contacts",
        "upload_func": upload_contacts_csv,
        "id_field": "ContactID",
        "alt_id_fields": ["contact_id", "Contact_ID"],
        "required_headers": {"name", "emailaddress"},
        "phase": 1,
    },
    "assetTypeId": {
        "name": "Asset Types",
        "upload_func": upload_asset_types_csv,
        "id_field": "assetTypeId",
        "alt_id_fields": ["asset_type_id", "AssetTypeId"],
        "required_headers": {"assettypename", "bookdepreciationmethod"},
        "phase": 1,
    },
    # Phase 2: Core Entities (Depend on Phase 1)
    "assetId": {
        "name": "Assets",
        "upload_func": upload_assets_csv,
        "id_field": "assetId",
        "alt_id_fields": ["asset_id", "AssetId"],
        "required_headers": {"assetname", "assettypeid"},
        "phase": 2,
    },
    "projectId": {
        "name": "Projects",
        "upload_func": upload_projects_csv,
        "id_field": "projectId",
        "alt_id_fields": ["project_id", "ProjectId"],
        "required_headers": {"name", "contactid", "status"},
        "phase": 2,
    },
    "BudgetID": {
        "name": "Budgets",
        "upload_func": upload_budgets_csv,
        "id_field": "BudgetID",
        "alt_id_fields": ["budget_id", "Budget_ID"],
        "required_headers": {"description", "type"},
        "phase": 2,
    },
    # Phase 3: Transactional Entities (Depend on Phases 1-2)
    "InvoiceID": {
        "name": "Invoices",
        "upload_func": upload_invoices_csv,
        "id_field": "InvoiceID",
        "alt_id_fields": ["invoice_id", "Invoice_ID"],
        "required_headers": {"type", "date", "invoicenumber"},
        # Accept either flat contact ID columns or nested contact object columns.
        "required_any_of": [{"contactid", "contact"}],
        "phase": 3,
    },
    "PurchaseOrderID": {
        "name": "Purchase Orders",
        "upload_func": upload_purchase_orders_csv,
        "id_field": "PurchaseOrderID",
        "alt_id_fields": ["purchase_order_id", "Purchase_Order_ID"],
        "required_headers": {"contactid", "date", "status", "deliveryaddress"},
        "phase": 3,
    },
    "QuoteID": {
        "name": "Quotes",
        "upload_func": upload_quotes_csv,
        "id_field": "QuoteID",
        "alt_id_fields": ["quote_id", "Quote_ID"],
        "required_headers": {"contactid", "date", "status", "reference"},
        "phase": 3,
    },
    "CreditNoteID": {
        "name": "Credit Notes",
        "upload_func": upload_credit_notes_csv,
        "id_field": "CreditNoteID",
        "alt_id_fields": ["credit_note_id", "Credit_Note_ID"],
        "required_headers": {"type", "contactid", "date", "creditnotenumber"},
        "phase": 3,
    },
    "BankTransactionID": {
        "name": "Bank Transactions",
        "upload_func": upload_bank_transactions_csv,
        "id_field": "BankTransactionID",
        "alt_id_fields": ["bank_transaction_id", "Bank_Transaction_ID"],
        "required_headers": {"type", "date"},
        # Bank account details are optional in some task CSVs.
        # Require at least one contextual party/account column.
        "required_any_of": [{"bankaccountid", "bankaccount", "contact"}],
        "phase": 3,
    },
    # Phase 4: Payment Entities (Depend on Phase 3)
    "PaymentID": {
        "name": "Payments",
        "upload_func": upload_payments_csv,
        "id_field": "PaymentID",
        "alt_id_fields": ["payment_id", "Payment_ID"],
        "required_headers": {"invoiceid", "amount", "date"},
        "phase": 4,
    },
    "OverpaymentID": {
        "name": "Overpayments",
        "upload_func": upload_overpayments_csv,
        "id_field": "OverpaymentID",
        "alt_id_fields": ["overpayment_id", "Overpayment_ID"],
        "required_headers": {"type", "contactid", "date", "remainingcredit"},
        "phase": 4,
    },
    "PrepaymentID": {
        "name": "Prepayments",
        "upload_func": upload_prepayments_csv,
        "id_field": "PrepaymentID",
        "alt_id_fields": ["prepayment_id", "Prepayment_ID"],
        "required_headers": {"type", "contactid", "date", "total"},
        "phase": 4,
    },
    # Phase 5: Adjustment Entities
    "BankTransferID": {
        "name": "Bank Transfers",
        "upload_func": upload_bank_transfers_csv,
        "id_field": "BankTransferID",
        "alt_id_fields": ["bank_transfer_id", "Bank_Transfer_ID"],
        "required_headers": {"frombankaccountid", "tobankaccountid", "amount"},
        "phase": 5,
    },
    "JournalID": {
        "name": "Journals",
        "upload_func": upload_journals_csv,
        "id_field": "JournalID",
        "alt_id_fields": ["journal_id", "Journal_ID"],
        "required_headers": {"journaldate", "journalnumber"},
        "phase": 5,
    },
    # Phase 6: Supporting Entities
    "timeEntryId": {
        "name": "Time Entries",
        "upload_func": upload_time_entries_csv,
        "id_field": "timeEntryId",
        "alt_id_fields": ["time_entry_id", "TimeEntryId"],
        "required_headers": {"projectid", "userid", "duration"},
        "phase": 6,
    },
}

# Special handling for Files, Folders, and Associations (all use "Id" field)
# These are detected separately to avoid conflicts

# Some real-world datasets use nested/dotted references for FK fields.
# Treat these variants as acceptable equivalents during validation/detection.
REQUIRED_HEADER_ALIASES: dict[str, set[str]] = {
    "contactid": {"contactcontactid"},
    "bankaccountid": {"bankaccountaccountid", "accountid"},
}


def normalize_header(header: str) -> str:
    """Normalize a CSV header for matching.

    Args:
        header: Raw header string

    Returns:
        Normalized header (lowercase, no spaces or underscores)
    """
    return re.sub(r"[^a-z0-9]", "", header.lower())


def _tokenize_header(header: str) -> list[str]:
    """Split a header into normalized word tokens.

    Handles common CSV naming styles:
    - PascalCase: ContactID
    - snake_case: contact_id
    - dotted paths: Contact.ContactID
    - mixed styles: BankAccount.AccountID
    """
    if not header:
        return []

    # Break separators first.
    text = re.sub(r"[.\s/_-]+", " ", header.strip())
    # Split CamelCase boundaries.
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    text = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", text)
    return re.findall(r"[a-z0-9]+", text.lower())


def header_variants(header: str) -> set[str]:
    """Generate normalized variants for flexible header matching."""
    variants = set()
    if not header:
        return variants

    # Compact normalized form.
    variants.add(normalize_header(header))

    # Each dotted segment as standalone variant (e.g. Contact.ContactID -> ContactID).
    for segment in header.split("."):
        segment_norm = normalize_header(segment)
        if segment_norm:
            variants.add(segment_norm)

    # Token-based suffix variants to handle nested/expanded names robustly.
    tokens = _tokenize_header(header)
    if tokens:
        # Include suffixes from original token stream.
        for idx in range(len(tokens)):
            variants.add("".join(tokens[idx:]))

        # Also include suffixes from deduplicated adjacent tokens
        # (e.g., bank account account id -> bank account id).
        dedup_tokens = [tokens[0]]
        for token in tokens[1:]:
            if token != dedup_tokens[-1]:
                dedup_tokens.append(token)
        for idx in range(len(dedup_tokens)):
            variants.add("".join(dedup_tokens[idx:]))

    return {v for v in variants if v}


def required_header_candidates(required_header: str) -> set[str]:
    """Return all acceptable normalized candidates for a required header."""
    candidates = set(header_variants(required_header))
    candidates.update(REQUIRED_HEADER_ALIASES.get(normalize_header(required_header), set()))
    return candidates


def get_csv_headers(csv_path: Path) -> set[str]:
    """Read and return normalized headers from a CSV file.

    Args:
        csv_path: Path to CSV file

    Returns:
        Set of normalized header names
    """
    try:
        with open(csv_path, encoding="utf-8") as f:
            reader = csv.reader(f)
            headers = next(reader, [])
            # Generate robust variants for matching
            normalized_headers = set()
            for header in headers:
                normalized_headers.update(header_variants(header))
            return normalized_headers
    except Exception:
        return set()


def get_raw_csv_headers(csv_path: Path) -> list[str]:
    """Read and return raw (non-normalized) headers from a CSV file.

    Args:
        csv_path: Path to CSV file

    Returns:
        List of raw header names as they appear in the file
    """
    try:
        with open(csv_path, encoding="utf-8") as f:
            reader = csv.reader(f)
            return next(reader, [])
    except Exception:
        return []


def build_schema_error_message(csv_path: Path) -> str:
    """Build a detailed error message for unrecognized CSV files.

    Shows what headers were found, what entities are expected, and suggests
    the closest match if any headers partially overlap.
    """
    raw_headers = get_raw_csv_headers(csv_path)
    normalized_headers = set()
    for header in raw_headers:
        normalized_headers.update(header_variants(header))

    # Build summary of expected entities (show first 6 most common)
    expected_lines = []
    priority_entities = ["Accounts", "Contacts", "Invoices", "Payments", "Assets", "Projects"]
    shown = set()

    for id_field, mapping in ENTITY_MAPPINGS.items():
        name = mapping["name"]
        if name in priority_entities and name not in shown:
            required = mapping.get("required_headers", set())
            required_any_of = mapping.get("required_any_of", [])
            alt_ids = mapping.get("alt_id_fields", [])
            id_info = f"'{id_field}'" + (f" (or {alt_ids})" if alt_ids else "")

            requirement_parts = [f"required: {sorted(required)}"] if required else []
            if required_any_of:
                requirement_parts.append(f"required_any_of: {required_any_of}")

            expected_lines.append(
                f"    - {name}: ID field {id_info}, " + ", ".join(requirement_parts)
            )
            shown.add(name)

    # Find closest match by header overlap
    best_overlap = 0
    best_entity = None
    best_missing = set()
    for _id_field, mapping in ENTITY_MAPPINGS.items():
        required = mapping.get("required_headers", set())
        required_candidates = set()
        for required_header in required:
            required_candidates.update(required_header_candidates(required_header))

        overlap = len(required_candidates & normalized_headers)
        if overlap > best_overlap:
            best_overlap = overlap
            best_entity = mapping["name"]
            best_missing = required_candidates - normalized_headers

    lines = [
        f"SCHEMA ERROR: {csv_path.name}",
        f"  Headers found: {raw_headers}",
        "  Could not match to any known entity type.",
        "",
        "  Expected entities (examples):",
    ]
    lines.extend(expected_lines)
    lines.append(
        f"    ... ({len(ENTITY_MAPPINGS)} entity types total, see docs/CSV_IMPORT_GUIDE.md)"
    )

    if best_entity and best_overlap > 0:
        lines.append("")
        lines.append(f"  Closest match: {best_entity} ({best_overlap} required header(s) found)")
        if best_missing:
            lines.append(f"    Missing (normalized variants): {sorted(best_missing)}")

    return "\n".join(lines)


def detect_entity_type_by_headers(headers: set[str]) -> dict | None:
    """Detect entity type by matching required headers (fallback detection).

    This is used when ID field detection fails. It checks if the CSV contains
    the minimum required headers for each entity type.

    Args:
        headers: Set of normalized CSV headers

    Returns:
        Entity mapping dict or None if not recognized
    """
    if not headers:
        return None

    best_match = None
    best_score = 0

    for _id_field, mapping in ENTITY_MAPPINGS.items():
        required = mapping.get("required_headers", set())
        required_any_of = mapping.get("required_any_of", [])

        if not required and not required_any_of:
            continue

        # Required headers (all must match)
        required_candidates = set()
        for required_header in required:
            required_candidates.update(required_header_candidates(required_header))

        required_match = required_candidates.issubset(headers)

        # Required-any-of groups (at least one match per group)
        required_any_of_match = True
        matched_any_of_headers = 0
        for group in required_any_of:
            group_candidates = set()
            for option in group:
                group_candidates.update(required_header_candidates(option))
            if group_candidates.isdisjoint(headers):
                required_any_of_match = False
                break
            matched_any_of_headers += 1

        if required_match and required_any_of_match:
            # Score by total required constraints matched (more specific = better)
            score = len(required_candidates) + matched_any_of_headers
            if score > best_score:
                best_score = score
                best_match = mapping

    if best_match:
        logger.info(
            f"Detected {best_match['name']} using header-based fallback "
            f"(matched {best_score} required headers)"
        )
    return best_match


def detect_entity_type(csv_path: Path) -> dict | None:
    """
    Detect entity type by reading CSV headers.

    The detection prioritizes the primary ID field that appears FIRST in the CSV
    headers, since entity CSVs typically have their primary ID as the first column.
    This prevents misdetection when a CSV has foreign keys (e.g., an Invoices CSV
    with both InvoiceID and ContactID columns).

    Supports both PascalCase (e.g., InvoiceID) and snake_case (e.g., invoice_id).

    Args:
        csv_path: Path to CSV file

    Returns:
        Entity mapping dict or None if not recognized
    """
    try:
        with open(csv_path, encoding="utf-8") as f:
            # Read just the header line
            reader = csv.reader(f)
            headers = next(reader, [])

            # Find all potential matches and their positions in the headers
            # The entity whose primary ID appears earliest is the best match
            candidates = []

            for id_field, mapping in ENTITY_MAPPINGS.items():
                # Check primary ID field
                if id_field in headers:
                    position = headers.index(id_field)
                    candidates.append((position, id_field, mapping, "primary"))
                # Check alternative ID fields (snake_case, etc.)
                for alt_field in mapping.get("alt_id_fields", []):
                    if alt_field in headers:
                        position = headers.index(alt_field)
                        candidates.append((position, alt_field, mapping, "alt"))

            # Return the mapping whose ID field appears earliest in the headers
            if candidates:
                candidates.sort(key=lambda x: x[0])  # Sort by position
                best_match = candidates[0]
                if best_match[3] == "alt":
                    logger.info(
                        f"Detected {best_match[2]['name']} using alternate field '{best_match[1]}'"
                    )
                return best_match[2]

            # Special handling for Files, Folders, Associations (all use "Id")
            if "Id" in headers:
                # Use filename or other headers to differentiate
                filename_lower = csv_path.name.lower()
                if "file" in filename_lower and "folder" not in filename_lower:
                    return {
                        "name": "Files",
                        "upload_func": upload_files_csv,
                        "id_field": "Id",
                        "alt_id_fields": ["id"],
                        "phase": 6,
                    }
                elif "folder" in filename_lower:
                    return {
                        "name": "Folders",
                        "upload_func": upload_folders_csv,
                        "id_field": "Id",
                        "alt_id_fields": ["id"],
                        "phase": 6,
                    }
                elif "association" in filename_lower or "assoc" in filename_lower:
                    return {
                        "name": "Associations",
                        "upload_func": upload_associations_csv,
                        "id_field": "Id",
                        "alt_id_fields": ["id"],
                        "phase": 6,
                    }

                # If we can't determine from filename, check for other distinguishing headers
                if "FileName" in headers or "MimeType" in headers:
                    return {
                        "name": "Files",
                        "upload_func": upload_files_csv,
                        "id_field": "Id",
                        "alt_id_fields": ["id"],
                        "phase": 6,
                    }
                elif "Name" in headers and "IsInbox" in headers:
                    return {
                        "name": "Folders",
                        "upload_func": upload_folders_csv,
                        "id_field": "Id",
                        "alt_id_fields": ["id"],
                        "phase": 6,
                    }
                elif "FileId" in headers and "ObjectId" in headers:
                    return {
                        "name": "Associations",
                        "upload_func": upload_associations_csv,
                        "id_field": "Id",
                        "alt_id_fields": ["id"],
                        "phase": 6,
                    }

            # Phase 2: Try header-based fallback detection
            # Normalize headers for fallback detection (avoid reading file twice)
            normalized_headers = set()
            for header in headers:
                normalized_headers.update(header_variants(header))
            header_match = detect_entity_type_by_headers(normalized_headers)
            if header_match:
                return header_match

        logger.warning(
            f"Could not detect entity type for {csv_path.name} - no recognized ID field "
            f"or required headers. See docs/CSV_IMPORT_GUIDE.md for supported entity types."
        )
        return None

    except Exception as e:
        logger.error(f"Error reading {csv_path.name}: {e}")
        return None


def validate_csv_file(csv_path: Path, entity_mapping: dict) -> dict:
    """
    Validate a single CSV file without importing.

    Args:
        csv_path: Path to CSV file
        entity_mapping: Entity type mapping dict

    Returns:
        Validation result dict
    """
    phase = entity_mapping.get("phase", "?")
    logger.info(f"[Phase {phase}] Validating {csv_path.name} as {entity_mapping['name']}...")

    try:
        # Read CSV content and check structure
        with open(csv_path, encoding="utf-8") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames
            if not headers:
                return {
                    "file": csv_path.name,
                    "entity": entity_mapping["name"],
                    "phase": phase,
                    "success": False,
                    "message": "CSV has no headers",
                    "row_count": 0,
                }

            # Validate required headers with robust variant matching
            normalized_headers = set()
            for header in headers:
                if header:
                    normalized_headers.update(header_variants(header))

            required_headers = entity_mapping.get("required_headers", set())
            required_any_of = entity_mapping.get("required_any_of", [])
            missing_required = []
            for required in required_headers:
                candidates = required_header_candidates(required)
                if candidates.isdisjoint(normalized_headers):
                    missing_required.append(required)

            missing_required_any_of = []
            for group in required_any_of:
                group_candidates = set()
                for option in group:
                    group_candidates.update(required_header_candidates(option))
                if group_candidates.isdisjoint(normalized_headers):
                    missing_required_any_of.append(sorted(group))

            if missing_required:
                expected_headers = sorted(required_headers)
                return {
                    "file": csv_path.name,
                    "entity": entity_mapping["name"],
                    "phase": phase,
                    "success": False,
                    "message": (
                        f"Missing required columns (or header name mismatch): {sorted(missing_required)}. "
                        f"Expected required headers for this entity: {expected_headers}."
                    ),
                    "row_count": 0,
                }

            if missing_required_any_of:
                return {
                    "file": csv_path.name,
                    "entity": entity_mapping["name"],
                    "phase": phase,
                    "success": False,
                    "message": (
                        "Missing required columns (or header name mismatch): expected at least one "
                        f"column from each group {missing_required_any_of}."
                    ),
                    "row_count": 0,
                }

            # Count rows and validate basic structure
            row_count = 0
            for row in reader:
                row_count += 1
                # Check for empty rows
                if not any(v.strip() for v in row.values() if v):
                    continue

        logger.success(f"  ✓ Valid: {row_count} rows")
        return {
            "file": csv_path.name,
            "entity": entity_mapping["name"],
            "phase": phase,
            "success": True,
            "message": "Valid",
            "row_count": row_count,
        }

    except Exception as e:
        logger.error(f"  ✗ Error: {e}")
        return {
            "file": csv_path.name,
            "entity": entity_mapping["name"],
            "phase": phase,
            "success": False,
            "message": str(e),
            "row_count": 0,
        }


async def validate_directory(directory: Path) -> dict:
    """
    Validate all CSV files in a directory without importing.

    Args:
        directory: Directory containing CSV files

    Returns:
        Summary dict with validation results
    """
    if not directory.exists():
        raise ValueError(f"Directory does not exist: {directory}")

    if not directory.is_dir():
        raise ValueError(f"Not a directory: {directory}")

    csv_files = list(directory.glob("*.csv"))

    if not csv_files:
        logger.warning(f"No CSV files found in {directory}")
        return {"files_processed": 0, "results": []}

    logger.info(f"Found {len(csv_files)} CSV file(s) in {directory}")

    # Detect entity types for all files
    files_with_metadata = []
    skipped_files = []

    for csv_path in csv_files:
        entity_mapping = detect_entity_type(csv_path)

        if entity_mapping is None:
            error_msg = build_schema_error_message(csv_path)
            logger.error(error_msg)
            skipped_files.append(
                {
                    "file": csv_path.name,
                    "entity": "Unknown",
                    "phase": "?",
                    "success": False,
                    "message": "Could not detect entity type from CSV headers (see log for details)",
                    "row_count": 0,
                }
            )
            continue

        files_with_metadata.append(
            {
                "path": csv_path,
                "mapping": entity_mapping,
                "phase": entity_mapping.get("phase", 999),
                "name": csv_path.name,
            }
        )

    # Sort by phase
    files_with_metadata.sort(key=lambda x: (x["phase"], x["name"]))

    # Validate files
    results = []
    for file_meta in files_with_metadata:
        result = validate_csv_file(file_meta["path"], file_meta["mapping"])
        results.append(result)

    results.extend(skipped_files)

    return {
        "files_processed": len(results),
        "results": results,
    }


async def import_csv_file(csv_path: Path, merge_mode: str, entity_mapping: dict) -> dict:
    """
    Import a single CSV file.

    Args:
        csv_path: Path to CSV file
        merge_mode: Either "append" or "replace"
        entity_mapping: Entity type mapping dict

    Returns:
        Upload response dict
    """
    phase = entity_mapping.get("phase", "?")
    logger.info(f"[Phase {phase}] Importing {csv_path.name} as {entity_mapping['name']}...")

    try:
        # Read CSV content
        with open(csv_path, encoding="utf-8") as f:
            csv_content = f.read()

        # Create upload input
        upload_input = UploadCSVInput(
            csv_content=csv_content,
            merge_mode=merge_mode,
        )

        # Call the appropriate upload function
        result = await entity_mapping["upload_func"](upload_input)

        return {
            "file": csv_path.name,
            "entity": entity_mapping["name"],
            "phase": phase,
            "success": result.success,
            "message": result.message,
            "rows_added": result.rows_added,
            "rows_updated": result.rows_updated,
            "total_rows": result.total_rows,
        }

    except Exception as e:
        logger.error(f"Failed to import {csv_path.name}: {e}")
        return {
            "file": csv_path.name,
            "entity": entity_mapping["name"],
            "phase": phase,
            "success": False,
            "message": str(e),
            "rows_added": 0,
            "rows_updated": 0,
            "total_rows": 0,
        }


async def import_directory(directory: Path, merge_mode: str = "append") -> dict:
    """
    Import all CSV files from a directory.

    Files are automatically sorted by dependency phase to prevent foreign key
    violations. The script detects entity types from CSV headers and imports
    foundation entities (Accounts, Contacts) before dependent entities (Invoices,
    Payments, etc.).

    Args:
        directory: Directory containing CSV files
        merge_mode: Either "append" or "replace"

    Returns:
        Summary dict with results for all files
    """
    if not directory.exists():
        raise ValueError(f"Directory does not exist: {directory}")

    if not directory.is_dir():
        raise ValueError(f"Not a directory: {directory}")

    # Find all CSV files
    csv_files = list(directory.glob("*.csv"))

    if not csv_files:
        logger.warning(f"No CSV files found in {directory}")
        return {"files_processed": 0, "results": []}

    logger.info(f"Found {len(csv_files)} CSV file(s) in {directory}")

    # Initialize provider (needed for upload functions)
    provider = OfflineProviderBase()
    set_provider(provider)

    # Detect entity types for all files and sort by dependency phase
    files_with_metadata = []
    skipped_files = []

    for csv_path in csv_files:
        entity_mapping = detect_entity_type(csv_path)

        if entity_mapping is None:
            error_msg = build_schema_error_message(csv_path)
            logger.error(error_msg)
            skipped_files.append(
                {
                    "file": csv_path.name,
                    "entity": "Unknown",
                    "phase": "?",
                    "success": False,
                    "message": "Could not detect entity type from CSV headers (see log for details)",
                    "rows_added": 0,
                    "rows_updated": 0,
                    "total_rows": 0,
                }
            )
            continue

        files_with_metadata.append(
            {
                "path": csv_path,
                "mapping": entity_mapping,
                "phase": entity_mapping.get("phase", 999),
                "name": csv_path.name,
            }
        )

    # Sort by phase (foundation entities first), then alphabetically within phase
    files_with_metadata.sort(key=lambda x: (x["phase"], x["name"]))

    # Log the import order
    if files_with_metadata:
        logger.info("Import order (automatically sorted by dependency):")
        for idx, file_meta in enumerate(files_with_metadata, 1):
            phase = file_meta["phase"]
            entity_name = file_meta["mapping"]["name"]
            logger.info(f"  {idx}. [Phase {phase}] {file_meta['name']} → {entity_name}")

    # Process files in dependency order
    results = []
    for file_meta in files_with_metadata:
        result = await import_csv_file(file_meta["path"], merge_mode, file_meta["mapping"])
        results.append(result)

    # Add skipped files to results
    results.extend(skipped_files)

    return {
        "files_processed": len(results),
        "results": results,
    }


def print_summary(summary: dict) -> None:
    """Print a formatted summary of import results."""
    print("\n" + "=" * 80)
    print("CSV IMPORT SUMMARY")
    print("=" * 80)
    print(f"\nTotal files processed: {summary['files_processed']}\n")

    successful = [r for r in summary["results"] if r["success"]]
    failed = [r for r in summary["results"] if not r["success"]]

    if successful:
        print(f"✓ Successful imports: {len(successful)}")
        for result in successful:
            phase = result.get("phase", "?")
            print(f"  • [Phase {phase}] {result['file']} ({result['entity']})")
            print(
                f"    → {result['rows_added']} added, {result['rows_updated']} updated, "
                f"{result['total_rows']} total"
            )

    if failed:
        print(f"\n✗ Failed imports: {len(failed)}")
        for result in failed:
            phase = result.get("phase", "?")
            print(f"  • [Phase {phase}] {result['file']} ({result['entity']})")
            print(f"    → Error: {result['message']}")

    print("\n" + "=" * 80)
    print("\nℹ️  Files were automatically sorted by dependency phase")
    print("   For details, see: docs/CSV_IMPORT_GUIDE.md")
    print("=" * 80 + "\n")


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Batch import CSV files into Xero MCP server (19 entity types supported)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Import all CSVs from a directory (append mode)
  python scripts/import_csv_directory.py /path/to/csvs

  # Replace existing data with CSV data
  python scripts/import_csv_directory.py /path/to/csvs --mode replace

  # Import from the sample data directory
  python scripts/import_csv_directory.py mcp_servers/xero/data

AUTOMATIC DEPENDENCY ORDERING:
  Files are automatically sorted by dependency phase. The script detects
  entity types from CSV headers and imports foundation entities (Accounts,
  Contacts) before dependent entities (Invoices, Payments). File naming
  does not affect import order.

See docs/CSV_IMPORT_GUIDE.md for the complete dependency order (19 entity types).
        """,
    )

    parser.add_argument(
        "directory",
        type=Path,
        help="Directory containing CSV files to import",
    )

    parser.add_argument(
        "--mode",
        choices=["append", "replace"],
        default="append",
        help="Import mode: 'append' (default) appends/updates data, 'replace' overwrites existing data",
    )

    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose logging",
    )

    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Only validate CSV files, do not import into database",
    )

    args = parser.parse_args()

    # Configure logging
    if not args.verbose:
        logger.remove()
        logger.add(sys.stderr, level="INFO")

    try:
        if args.validate_only:
            # Validate only mode
            print("\n" + "=" * 80)
            print("CSV VALIDATION")
            print("=" * 80)
            print(f"Directory: {args.directory}")
            print("")

            summary = await validate_directory(args.directory)

            # Print validation summary
            print("\n" + "=" * 80)
            print("VALIDATION SUMMARY")
            print("=" * 80)
            print(f"\nTotal files validated: {summary['files_processed']}\n")

            successful = [r for r in summary["results"] if r["success"]]
            failed = [r for r in summary["results"] if not r["success"]]

            if successful:
                print(f"✓ Valid files: {len(successful)}")
                for result in successful:
                    phase = result.get("phase", "?")
                    print(f"  • [Phase {phase}] {result['file']} ({result['entity']})")
                    print(f"    → {result['row_count']} rows")

            if failed:
                print(f"\n✗ Invalid files: {len(failed)}")
                for result in failed:
                    phase = result.get("phase", "?")
                    print(f"  • [Phase {phase}] {result['file']} ({result['entity']})")
                    print(f"    → Error: {result['message']}")

            print("\n" + "=" * 80)

            if failed:
                print("\nVALIDATION FAILED - Fix errors before importing")
                return 1
            else:
                print("\nVALIDATION PASSED - All CSV files are valid")
                return 0

        # Run import
        summary = await import_directory(args.directory, merge_mode=args.mode)

        # Print results
        print_summary(summary)

        # Return exit code (any imports failed = error)
        failed_count = sum(1 for r in summary["results"] if not r["success"])
        return 1 if failed_count > 0 else 0

    except Exception as e:
        logger.error(f"Import failed: {e}")
        return 1

    finally:
        # Clean up database engine to prevent hanging on exit
        from mcp_servers.xero.db.session import engine

        await engine.dispose()


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)

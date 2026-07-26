"""HTML table extraction utilities for SEC filings.

Implements Tier 3 fallback for when XBRL data is incomplete or unavailable.
Uses BeautifulSoup to parse HTML tables and extract structured data.
"""

import re
import warnings
from typing import Literal

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from loguru import logger

# Suppress XML parsed as HTML warnings - we intentionally use HTML parser
# for flexibility with SEC filings which may contain mixed HTML/XML content
warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)


def extract_table_by_keyword(
    html_content: str,
    keyword: str,
    context_lines: int = 10,
) -> dict | None:
    """Search for and extract a table from HTML by keyword matching.

    Args:
        html_content: Raw HTML content to search
        keyword: Keyword to find table (e.g., 'Stock Option Activity', 'Debt Schedule')
        context_lines: Number of lines of context to search around tables

    Returns:
        Dictionary with table data or None if not found:
        {
            "found": bool,
            "table_data": dict,  # Structured table data
            "raw_html": str,     # Original table HTML
            "confidence": str,   # "high", "medium", or "low"
            "match_reason": str,
            "match_score": int,
            "tables_scanned": int,
        }
    """
    if not html_content or not keyword:
        logger.warning("Empty HTML content or keyword")
        return None

    try:
        soup = BeautifulSoup(html_content, "lxml")
    except Exception as e:
        logger.warning(f"Failed to parse HTML: {e}")
        return None

    tables = soup.find_all("table")
    if not tables:
        logger.debug("No tables found in HTML")
        return None

    keyword_lower = _clean_text(keyword).lower()
    best_match = None
    best_confidence = "low"
    best_score = 0
    tables_scanned = len(tables)

    for table in tables:
        # --- Table size heuristics: skip likely formatting/layout tables ---
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        # Skip very large tables that wrap the entire document
        if len(rows) > 500:
            logger.debug(f"Skipping oversized table ({len(rows)} rows)")
            continue
        # Skip tiny tables with only 1 real data column
        sample_cells = rows[0].find_all(["td", "th"])
        if len(sample_cells) < 2 and len(rows) < 4:
            continue

        confidence = "low"
        match_reason = None
        match_score = 0

        # Check 1: Caption or heading directly above table
        caption = table.find("caption")
        if caption:
            score = _keyword_match_score(keyword_lower, _match_text(caption))
            if score >= 2:
                confidence = "high"
                match_reason = "caption match"
                match_score = score

        # Check parent elements for headings
        if not match_reason:
            prev_siblings = list(table.find_previous_siblings(limit=3))
            for sibling in prev_siblings:
                if sibling.name in [
                    "h1",
                    "h2",
                    "h3",
                    "h4",
                    "h5",
                    "h6",
                    "strong",
                    "b",
                ]:
                    score = _keyword_match_score(
                        keyword_lower,
                        _match_text(sibling),
                    )
                    if score >= 2:
                        confidence = "high"
                        match_reason = "heading match"
                        match_score = score
                        break

        # Check 1b: Heading via find_previous (crosses wrapper divs).
        # For strong/b tags, skip matches inside a table cell to avoid
        # false-positive high-confidence from bold text in earlier tables.
        if not match_reason:
            for tag in [
                "h1",
                "h2",
                "h3",
                "h4",
                "h5",
                "h6",
                "strong",
                "b",
            ]:
                prev_heading = table.find_previous(tag)
                if prev_heading:
                    score = _keyword_match_score(
                        keyword_lower,
                        _match_text(prev_heading),
                    )
                    if score >= 2:
                        if tag in ("strong", "b") and prev_heading.find_parent(["td", "th"]):
                            continue
                        confidence = "high"
                        match_reason = "heading match"
                        match_score = score
                        break

        # Check 2: Table headers (th)
        if not match_reason:
            headers = table.find_all("th")
            best_header_score = 0
            for th in headers:
                score = _keyword_match_score(
                    keyword_lower,
                    _match_text(th),
                )
                best_header_score = max(best_header_score, score)
            if best_header_score >= 2:
                confidence = "medium"
                match_reason = "header match"
                match_score = best_header_score

        # Check 2b: Body cell match (td) — low confidence
        if not match_reason:
            for row_idx, row in enumerate(rows):
                tds = row.find_all("td")
                cells_to_check = tds if row_idx < 3 else tds[:1]
                for td in cells_to_check:
                    score = _keyword_match_score(
                        keyword_lower,
                        _match_text(td),
                    )
                    if score >= 2:
                        confidence = "low"
                        match_reason = "body cell match"
                        match_score = score
                        break
                if match_reason:
                    break

        # Check 3: Text context around table
        if not match_reason:
            context_text = ""
            prev_sibs = list(
                table.find_previous_siblings(limit=context_lines),
            )
            next_sibs = list(
                table.find_next_siblings(limit=context_lines),
            )

            for elem in prev_sibs + next_sibs:
                context_text += _match_text(elem) + " "

            score = _keyword_match_score(keyword_lower, context_text)
            if score >= 1:
                confidence = "low"
                match_reason = "context match"
                match_score = score

        if match_reason:
            logger.debug(f"Found table match: {match_reason} (confidence={confidence})")

            table_data = _parse_table_structure(table)

            if not table_data:
                continue

            current_rank = (_confidence_rank(confidence), match_score)
            best_rank = (_confidence_rank(best_confidence), best_score)

            if not best_match or current_rank > best_rank:
                column_data = _convert_to_column_oriented(table_data)

                best_match = {
                    "found": True,
                    "table_data": column_data,
                    "raw_html": str(table),
                    "confidence": confidence,
                    "match_reason": match_reason,
                    "match_score": match_score,
                    "tables_scanned": tables_scanned,
                }
                best_confidence = confidence
                best_score = match_score

                if confidence == "high":
                    break

    return best_match


def _convert_to_column_oriented(table_data: dict) -> dict:
    """Convert row-oriented table format to column-oriented for backward compatibility.

    Args:
        table_data: Dictionary with "headers" and "rows" keys

    Returns:
        Column-oriented dictionary mapping column names to lists of values
    """
    if not table_data or "headers" not in table_data or "rows" not in table_data:
        return table_data

    headers = table_data["headers"]
    rows = table_data["rows"]

    # Create column-oriented format
    column_dict = {}
    for header in headers:
        column_dict[header] = []

    # Populate columns from rows
    for row in rows:
        for header in headers:
            column_dict[header].append(row.get(header))

    return column_dict


def _confidence_rank(confidence: str) -> int:
    """Convert confidence string to numeric rank for comparison."""
    ranks = {"high": 3, "medium": 2, "low": 1}
    return ranks.get(confidence, 0)


def _parse_table_structure(table) -> dict | None:
    """Parse HTML table into structured dictionary.

    Args:
        table: BeautifulSoup table element

    Returns:
        Dictionary with "rows" (list of row dicts) and optional "headers" list
        Returns None if parsing fails or table is empty/invalid
    """
    try:
        rows = table.find_all("tr")
        if not rows:
            return None

        # Skip tables that are too small (likely formatting tables)
        if len(rows) < 2:
            logger.debug("Table has fewer than 2 rows, likely formatting table")
            return None

        # Find header row (usually first row with th elements)
        headers = []
        data_rows = []
        header_row_idx = -1

        for idx, row in enumerate(rows):
            th_elements = row.find_all("th")
            if th_elements and len(th_elements) > 1:  # Require at least 2 headers
                # This is a header row
                headers = [_clean_text(th.get_text()) for th in th_elements]
                header_row_idx = idx
                break

        # Extract data rows
        for idx, row in enumerate(rows):
            if idx <= header_row_idx:
                continue  # Skip header row(s)

            cells = row.find_all(["td", "th"])
            if cells and len(cells) > 1:  # Skip rows with single cell (likely formatting)
                row_data = [_clean_text(cell.get_text()) for cell in cells]
                # Only add row if it has some non-empty content
                if any(cell.strip() for cell in row_data):
                    data_rows.append(row_data)

        if not data_rows:
            logger.debug("No valid data rows found in table")
            return None

        # If we found headers, create column-oriented dict
        if headers:
            # Normalize headers - replace empty headers with column_{idx}
            normalized_headers = []
            for col_idx, header in enumerate(headers):
                if not header:
                    normalized_headers.append(f"column_{col_idx}")
                else:
                    normalized_headers.append(header)

            table_dict = {"headers": normalized_headers, "rows": []}

            for row_data in data_rows:
                # Pad or trim to match header count
                while len(row_data) < len(normalized_headers):
                    row_data.append("")
                row_data = row_data[: len(normalized_headers)]

                # Convert row to dict mapping headers to values
                row_dict = {}
                for col_idx, header in enumerate(normalized_headers):
                    value = row_data[col_idx] if col_idx < len(row_data) else ""
                    row_dict[header] = _parse_value(value)

                table_dict["rows"].append(row_dict)

            return table_dict

        # No headers found - return row-based structure
        else:
            # Infer number of columns from most common row length
            col_counts = {}
            for row in data_rows:
                count = len(row)
                col_counts[count] = col_counts.get(count, 0) + 1

            # Use most common column count
            num_cols = max(col_counts, key=col_counts.get) if col_counts else 0
            if num_cols == 0:
                return None

            # Generate column names
            headers = [f"column_{i}" for i in range(num_cols)]

            table_dict = {"headers": headers, "rows": []}

            for row_data in data_rows:
                # Pad or trim to match column count
                while len(row_data) < num_cols:
                    row_data.append("")
                row_data = row_data[:num_cols]

                # Convert to dict
                row_dict = {}
                for col_idx, header in enumerate(headers):
                    value = row_data[col_idx] if col_idx < len(row_data) else ""
                    row_dict[header] = _parse_value(value)

                table_dict["rows"].append(row_dict)

            logger.debug(
                f"Table parsed without explicit headers ({len(data_rows)} rows, {num_cols} columns)"
            )
            return table_dict

    except Exception as e:
        logger.warning(f"Failed to parse table structure: {e}")
        return None


def _keyword_match_score(keyword_lower: str, text: str) -> int:
    """Score how prominently a keyword matches within text.

    Used for ranking matches: higher score = keyword is more prominent in text.

    Returns:
        3 = exact/near-exact (keyword essentially IS the text)
        2 = word boundary match (keyword appears as complete word(s))
        1 = substring match (keyword inside other words, only for 4+ char keywords)
        0 = no match
    """
    if not keyword_lower or not text:
        return 0

    text_stripped = text.strip()
    if not text_stripped:
        return 0

    if text_stripped == keyword_lower:
        return 3

    if keyword_lower in text_stripped:
        ratio = len(keyword_lower) / len(text_stripped)
        if ratio >= 0.75:
            return 3

    pattern = r"\b" + re.escape(keyword_lower) + r"\b"
    if re.search(pattern, text):
        return 2

    if len(keyword_lower) >= 4 and keyword_lower in text:
        return 1

    return 0


def _match_text(element) -> str:
    """Extract text from a BeautifulSoup element, cleaned and lowercased for matching."""
    text = element.get_text()
    text = text.replace("\u200b", " ")  # Zero-width space → space (before _clean_text)
    return _clean_text(text).lower()


def _clean_text(text: str) -> str:
    """Clean and normalize text extracted from HTML.

    Args:
        text: Raw text from HTML element

    Returns:
        Cleaned text with normalized whitespace
    """
    if not text:
        return ""

    # Remove extra whitespace
    text = re.sub(r"\s+", " ", text)
    text = text.strip()

    # Remove common HTML artifacts
    text = text.replace("\xa0", " ")  # Non-breaking space
    text = text.replace("\u200b", "")  # Zero-width space

    return text


def _parse_value(value: str) -> int | float | str:
    """Try to parse string value as number.

    Args:
        value: String value from table cell

    Returns:
        Parsed number (int or float) if possible, otherwise original string
    """
    if not value or not isinstance(value, str):
        return value

    # Remove common formatting
    cleaned = value.replace(",", "").replace("$", "").replace("%", "").strip()

    # Handle parentheses as negative numbers
    is_negative = False
    if cleaned.startswith("(") and cleaned.endswith(")"):
        cleaned = cleaned[1:-1]
        is_negative = True

    # Try to parse as number
    try:
        # Try int first
        if "." not in cleaned:
            num = int(cleaned)
            return -num if is_negative else num
        # Otherwise float
        num = float(cleaned)
        return -num if is_negative else num
    except ValueError:
        # Not a number, return original
        return value


def extract_equity_compensation_table(html_content: str) -> dict | None:
    """Extract equity compensation table from HTML filing.

    Searches for common equity compensation table patterns:
    - Stock Option Activity
    - RSU Activity / Restricted Stock Unit Activity
    - PSU Activity / Performance Share Unit
    - ESPP Activity / Employee Stock Purchase Plan

    Args:
        html_content: Raw HTML content from SEC filing

    Returns:
        Dictionary with extracted equity compensation data:
        {
            "stock_options": {...},
            "rsus": {...},
            "psus": {...},
            "espp": {...}
        }
        Returns None if no tables found
    """
    result = {}

    # Search for stock option table
    keywords = {
        "stock_options": [
            "Stock Option Activity",
            "Options Outstanding",
            "Stock Options",
        ],
        "rsus": [
            "Restricted Stock Unit Activity",
            "RSU Activity",
            "Restricted Stock Units",
        ],
        "psus": [
            "Performance Share Unit",
            "PSU Activity",
            "Performance Stock Unit",
        ],
        "espp": [
            "Employee Stock Purchase Plan",
            "ESPP Activity",
        ],
    }

    for category, keyword_list in keywords.items():
        for keyword in keyword_list:
            table_result = extract_table_by_keyword(html_content, keyword)
            if table_result and table_result["found"]:
                # Map table columns to standard fields
                mapped_data = _map_equity_table_to_standard_fields(
                    table_result["table_data"],
                    category,
                )
                if mapped_data:
                    result[category] = {
                        **mapped_data,
                        "confidence": table_result["confidence"],
                    }
                    logger.info(
                        f"Found {category} table with {table_result['confidence']} confidence"
                    )
                    break  # Found table for this category

    return result if result else None


def _map_equity_table_to_standard_fields(
    table_data: dict,
    category: Literal["stock_options", "rsus", "psus", "espp"],
) -> dict | None:
    """Map HTML table columns to standard equity compensation fields.

    Args:
        table_data: Dictionary with "headers" and "rows" keys (row-oriented)
                    OR column-oriented format {"Column": [values]} for backward compat
        category: Type of equity compensation table

    Returns:
        Dictionary with standardized field names
    """
    if not table_data:
        return None

    # Handle both row-oriented and column-oriented formats
    if "headers" in table_data and "rows" in table_data:
        # Row-oriented format (new internal format)
        headers = table_data["headers"]
        rows = table_data["rows"]
    else:
        # Column-oriented format (backward compatibility)
        # Convert to row-oriented internally
        headers = list(table_data.keys())
        if not headers:
            return None

        # Get number of rows from first column
        first_column = table_data[headers[0]]
        num_rows = len(first_column) if isinstance(first_column, list) else 0

        # Build rows from columns
        rows = []
        for row_idx in range(num_rows):
            row = {}
            for header in headers:
                column_data = table_data[header]
                row[header] = column_data[row_idx] if isinstance(column_data, list) else column_data
            rows.append(row)

    # Define mapping patterns for each category
    field_patterns = {
        "stock_options": {
            "outstanding_beginning": [
                "outstanding.*beginning",
                "beginning.*balance",
                "balance.*beginning",
            ],
            "granted": ["granted", "options.*granted"],
            "exercised": ["exercised", "options.*exercised"],
            "forfeited": ["forfeited", "cancelled"],
            "expired": ["expired"],
            "outstanding_ending": [
                "outstanding.*ending",
                "ending.*balance",
                "balance.*ending",
            ],
            "exercisable_ending": ["exercisable", "vested.*exercisable"],
        },
        "rsus": {
            "unvested_beginning": [
                "unvested.*beginning",
                "beginning.*balance",
                "nonvested.*beginning",
            ],
            "granted": ["^granted$", "rsus.*granted"],
            "vested": ["^vested$", "rsus.*vested(?!.*unvested)"],
            "forfeited": ["^forfeited$", "cancelled"],
            "unvested_ending": [
                "unvested.*ending",
                "ending.*balance",
                "nonvested.*ending",
            ],
        },
        "psus": {
            "unvested_beginning": [
                "unvested.*beginning",
                "beginning.*balance",
                "outstanding.*beginning",
            ],
            "granted": ["granted", "psus.*granted"],
            "vested": ["vested", "psus.*vested"],
            "forfeited": ["forfeited", "cancelled"],
            "unvested_ending": [
                "unvested.*ending",
                "ending.*balance",
                "outstanding.*ending",
            ],
        },
        "espp": {
            "shares_available": ["shares.*available", "available"],
            "shares_purchased": ["purchased", "shares.*purchased"],
        },
    }

    if category not in field_patterns:
        return None

    patterns = field_patterns[category]
    result = {}

    # headers and rows are already set from format detection above
    # Try to match each field to a column header
    for field_name, pattern_list in patterns.items():
        found = False
        for header in headers:
            if found:
                break
            header_lower = header.lower()

            # Check if any pattern matches
            for pattern in pattern_list:
                if re.search(pattern, header_lower):
                    # Found matching column
                    # Take first non-empty value from rows
                    for row in rows:
                        val = row.get(header)
                        if val is not None and val != "" and val != "-":
                            result[field_name] = val
                            found = True
                            break
                    break

    return result if result else None


def extract_debt_schedule_table(html_content: str) -> dict | None:
    """Extract debt schedule table from HTML filing.

    Searches for common debt schedule table patterns:
    - Debt Schedule
    - Long-term Debt
    - Debt Maturities
    - Notes Payable

    Args:
        html_content: Raw HTML content from SEC filing

    Returns:
        Dictionary with extracted debt schedule data:
        {
            "debt_instruments": [
                {
                    "instrument_name": "Term Loan",
                    "current_portion": 0.0,
                    "noncurrent_portion": 150000000.0,
                    "total": 150000000.0,
                    "maturity_date": "2028-12-31",
                    "interest_rate": None
                }
            ],
            "total_current_debt": 0.0,
            "total_noncurrent_debt": 150000000.0,
            "total_debt": 150000000.0,
            "confidence": "high"
        }
        Returns None if no tables found
    """
    keywords = [
        "Debt Schedule",
        "Long-term Debt",
        "Debt Maturities",
        "Notes Payable",
        "Debt Instruments",
        "Long Term Debt",
        "Long-Term Borrowings",
        "Borrowings",
        "Debt Obligations",
        "Outstanding Indebtedness",
        "Credit Facilities",
        "Credit Facility",
        "Revolving Credit",
        "Unsecured Notes",
        "Unsecured Debt",
        "Senior Notes",
        "Senior Debt",
        "Convertible Notes",
        "Convertible Debt",
        "Summary of Debt",
        "Schedule of Debt",
        "Components of Debt",
        "Aggregate Maturities",
        "Future Maturities",
    ]

    best_match = None
    best_confidence = "low"

    for keyword in keywords:
        table_result = extract_table_by_keyword(html_content, keyword)
        if table_result and table_result["found"]:
            mapped_data = _map_debt_table_to_standard_fields(table_result["table_data"])

            if mapped_data and mapped_data.get("debt_instruments"):
                confidence_rank = _confidence_rank(table_result["confidence"])
                best_rank = _confidence_rank(best_confidence)

                if confidence_rank > best_rank:
                    best_match = {
                        **mapped_data,
                        "confidence": table_result["confidence"],
                    }
                    best_confidence = table_result["confidence"]

                    if table_result["confidence"] == "high":
                        break

    return best_match


def _map_debt_table_to_standard_fields(table_data: dict) -> dict | None:
    """Map HTML table columns to standard debt schedule fields.

    Args:
        table_data: Dictionary with "headers" and "rows" keys (row-oriented)
                    OR column-oriented format {"Column": [values]} for backward compat

    Returns:
        Dictionary with standardized debt schedule data
    """
    if not table_data:
        return None

    # Handle both row-oriented and column-oriented formats
    if "headers" in table_data and "rows" in table_data:
        # Row-oriented format (new internal format)
        headers = table_data["headers"]
        rows = table_data["rows"]
    else:
        # Column-oriented format (backward compatibility)
        # Convert to row-oriented internally
        headers = list(table_data.keys())
        if not headers:
            return None

        # Get number of rows from first column
        first_column = table_data[headers[0]]
        num_rows = len(first_column) if isinstance(first_column, list) else 0

        # Build rows from columns
        rows = []
        for row_idx in range(num_rows):
            row = {}
            for header in headers:
                column_data = table_data[header]
                row[header] = column_data[row_idx] if isinstance(column_data, list) else column_data
            rows.append(row)

    instruments = []
    total_current = 0.0
    total_noncurrent = 0.0

    instrument_patterns = [
        r"instrument",
        r"note",
        r"debt.*type",
        r"description",
        r"facility",
        r"loan",
        r"borrowing",
        r"obligation",
        r"credit",
        r"bond",
        r"debenture",
    ]

    current_patterns = [
        r"(?<!non)current",
        r"short.*term",
        r"due.*one.*year",
        r"maturit.*within",
    ]

    noncurrent_patterns = [
        r"noncurrent",
        r"non-current",
        r"long.*term",
        r"due.*after",
        r"maturit.*after",
    ]

    maturity_patterns = [
        r"maturit.*date",
        r"due.*date",
        r"expir.*date",
        r"maturity",
    ]

    interest_patterns = [
        r"interest.*rate",
        r"rate",
        r"coupon",
    ]

    # Find column headers that match patterns
    instrument_col = None
    for header in headers:
        header_lower = header.lower()
        for pattern in instrument_patterns:
            if re.search(pattern, header_lower):
                instrument_col = header
                break
        if instrument_col:
            break

    noncurrent_col = None
    for header in headers:
        header_lower = header.lower()
        for pattern in noncurrent_patterns:
            if re.search(pattern, header_lower):
                noncurrent_col = header
                break
        if noncurrent_col:
            break

    current_col = None
    for header in headers:
        if noncurrent_col and header == noncurrent_col:
            continue
        header_lower = header.lower()
        for pattern in current_patterns:
            if re.search(pattern, header_lower):
                current_col = header
                break
        if current_col:
            break

    maturity_col = None
    for header in headers:
        header_lower = header.lower()
        for pattern in maturity_patterns:
            if re.search(pattern, header_lower):
                maturity_col = header
                break
        if maturity_col:
            break

    interest_col = None
    for header in headers:
        header_lower = header.lower()
        for pattern in interest_patterns:
            if re.search(pattern, header_lower):
                interest_col = header
                break
        if interest_col:
            break

    # Process each row
    for row in rows:
        instrument_name = "Unknown Debt Instrument"
        if instrument_col:
            name_val = row.get(instrument_col)
            if name_val and isinstance(name_val, str) and name_val.strip():
                instrument_name = name_val.strip()

        invalid_names = ["instrument", "description", "debt type", "total"]
        if not instrument_name or instrument_name.lower() in invalid_names:
            continue

        current_portion = 0.0
        if current_col:
            current_val = row.get(current_col)
            if current_val is not None:
                if isinstance(current_val, int | float):
                    current_portion = float(current_val)
                elif isinstance(current_val, str):
                    parsed = _parse_value(current_val)
                    if isinstance(parsed, int | float):
                        current_portion = float(parsed)

        noncurrent_portion = 0.0
        if noncurrent_col:
            noncurrent_val = row.get(noncurrent_col)
            if noncurrent_val is not None:
                if isinstance(noncurrent_val, int | float):
                    noncurrent_portion = float(noncurrent_val)
                elif isinstance(noncurrent_val, str):
                    parsed = _parse_value(noncurrent_val)
                    if isinstance(parsed, int | float):
                        noncurrent_portion = float(parsed)

        # If both are 0, look for a generic total column
        if current_portion == 0.0 and noncurrent_portion == 0.0:
            for header in headers:
                header_lower = header.lower()
                if re.search(r"total|amount|balance", header_lower) and not re.search(
                    r"current|noncurrent|short|long", header_lower
                ):
                    total_val = row.get(header)
                    if total_val is not None:
                        if isinstance(total_val, int | float):
                            noncurrent_portion = float(total_val)
                        elif isinstance(total_val, str):
                            parsed = _parse_value(total_val)
                            if isinstance(parsed, int | float):
                                noncurrent_portion = float(parsed)
                    break

        maturity_date = None
        if maturity_col:
            maturity_val = row.get(maturity_col)
            if maturity_val and isinstance(maturity_val, str):
                maturity_date = _parse_date(maturity_val)

        interest_rate = None
        if interest_col:
            rate_val = row.get(interest_col)
            if rate_val is not None:
                if isinstance(rate_val, int | float):
                    interest_rate = float(rate_val)
                elif isinstance(rate_val, str):
                    parsed = _parse_value(rate_val)
                    if isinstance(parsed, int | float):
                        interest_rate = float(parsed)

        if current_portion > 0 or noncurrent_portion > 0:
            instruments.append(
                {
                    "instrument_name": instrument_name,
                    "current_portion": current_portion,
                    "noncurrent_portion": noncurrent_portion,
                    "total": current_portion + noncurrent_portion,
                    "maturity_date": maturity_date,
                    "interest_rate": interest_rate,
                }
            )
            total_current += current_portion
            total_noncurrent += noncurrent_portion

    if not instruments:
        return None

    return {
        "debt_instruments": instruments,
        "total_current_debt": total_current,
        "total_noncurrent_debt": total_noncurrent,
        "total_debt": total_current + total_noncurrent,
    }


def _parse_date(date_str: str) -> str | None:
    """Try to parse a date string into YYYY-MM-DD format.

    Args:
        date_str: Date string in various formats

    Returns:
        Date string in YYYY-MM-DD format or None if parsing fails
    """
    if not date_str or not isinstance(date_str, str):
        return None

    import re as re_module

    match = re_module.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", date_str)
    if match:
        month, day, year = match.groups()
        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"

    match = re_module.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", date_str)
    if match:
        year, month, day = match.groups()
        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"

    match = re_module.search(r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})", date_str)
    if match:
        month_name, day, year = match.groups()
        month_map = {
            "january": "01",
            "february": "02",
            "march": "03",
            "april": "04",
            "may": "05",
            "june": "06",
            "july": "07",
            "august": "08",
            "september": "09",
            "october": "10",
            "november": "11",
            "december": "12",
        }
        month_num = month_map.get(month_name.lower())
        if month_num:
            return f"{year}-{month_num}-{day.zfill(2)}"

    return None

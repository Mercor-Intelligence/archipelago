"""Diff computation and formatting for playground snapshots.

Compares normalized file contents and formats differences for LLM evaluation.
"""

import re
from typing import TypedDict

from .models import FileMismatch, NormalizedDiff


class DiffSeverityAnalysis(TypedDict):
    """Result of analyzing diff severity."""

    has_business_table_diffs: bool
    has_system_table_diffs: bool
    has_non_db_diffs: bool
    has_unparseable_db_diffs: bool  # DB files differ but no tables could be extracted
    business_tables: list[str]
    system_tables: list[str]
    severity: str  # "high", "medium", or "low"


# Preview size for LLM context (larger = more context but more tokens)
PREVIEW_SIZE = 5000

# Prompt size limits to avoid exceeding LLM context limits
MAX_PROMPT_SIZE = 50000  # ~12K tokens, safe for most models
MAX_MISMATCHES_SHOWN = 5  # Show at most 5 detailed mismatches
MAX_FILES_LISTED = 20  # Max files to list for missing/extra

# Database file extensions for SQL classification
DB_EXTENSIONS = {".db", ".sqlite", ".sqlite3", ".sql"}

# System/infrastructure tables that are less likely to be business-critical
# These patterns are common across many web frameworks and applications
#
# DESIGN PRINCIPLE: Err on the side of NOT matching (false positives are safer
# than false negatives). A false positive means we mark a system table as
# "business" → high severity (safe, just more conservative). A false negative
# means we mark a business table as "system" → low severity (dangerous, could
# miss important changes).
#
# Therefore, patterns should be:
# 1. Framework-specific prefixes (django_, auth_, alembic_, etc.)
# 2. Clear infrastructure suffixes (_log, _audit, _history)
# 3. Exact matches for unambiguous system tables
# 4. NOT generic suffixes like _session, _token that could be business data
SYSTEM_TABLE_PATTERNS = [
    # Generic system prefixes - very safe, internal/system tables
    r"^__",  # Double underscore prefix (e.g., __Auth, __Settings)
    # Note: We intentionally don't match ^_[a-z] because re.IGNORECASE would
    # make it match _Token, _Customer etc. Single underscore tables are ambiguous.
    # Framework-specific prefixes - safe, clearly infrastructure
    r"^auth_",  # Django auth tables (auth_user, auth_group, etc.)
    r"^oauth_",  # OAuth tables with underscore (oauth_token, oauth_session)
    r"^oauth2_",  # OAuth2 tables
    r"^django_",  # Django system tables
    r"^strapi_",  # Strapi system tables
    r"^alembic_",  # Alembic (Python) migrations
    r"^flyway_",  # Flyway (Java) migrations
    r"^schema_",  # Schema versioning
    r"^celery_",  # Celery task queue
    r"^login_",  # Login tracking (login_source, login_history)
    # Log/Audit tables - safe suffix patterns (clearly infrastructure)
    r"_log$",  # Tables ending in _log
    r"_logs$",  # Tables ending in _logs
    r"_history$",  # Tables ending in _history
    r"_audit$",  # Tables ending in _audit
    r"_changelog$",  # Tables ending in _changelog
    r"^log_",  # Tables starting with log_
    r"^audit_",  # Tables starting with audit_
    # Migration tables - exact matches and framework prefixes
    r"^migration[s]?$",  # Exact match: migration, migrations
    r"^django_migrations$",  # Django migrations
    r"^alembic_version$",  # Alembic version tracking
    r"^flyway_schema_history$",  # Flyway migrations
    # Cache/Queue tables - prefix patterns (clearly infrastructure)
    r"^cache_",  # Cache tables with underscore
    r"^queue_",  # Queue tables with underscore
    r"^qrtz_",  # Quartz scheduler tables
    r"^cron_",  # Cron job tables
    # Meta tables - safe suffix patterns
    r"_meta$",  # Tables ending in _meta
    r"^meta_",  # Tables starting with meta_
    # ERPNext-specific (Frappe framework) - exact matches
    r"^tabDocType$",
    r"^tabDocField$",
    r"^tabDocPerm$",
    r"^tabSingles$",
    r"^tabDefaultValue$",
    r"^tabVersion$",
    r"^tabComment$",
    r"^tabActivity Log$",
    r"^tabScheduled Job Log$",
    r"^tabError Log$",
    r"^tabAccess Log$",
    r"^tabView Log$",
    r"^tabSession",
    r"^tabFile$",
    # WordPress-specific - exact matches
    r"^wp_usermeta$",
    r"^wp_postmeta$",
    r"^wp_options$",
    r"^wp_sessions$",
    # Version tracking - exact match
    r"^version$",
]

# Compile patterns for efficiency
_SYSTEM_TABLE_RE = re.compile("|".join(SYSTEM_TABLE_PATTERNS), re.IGNORECASE)


def _is_system_table(table_name: str) -> bool:
    """Check if a table name matches system/infrastructure patterns."""
    return bool(_SYSTEM_TABLE_RE.search(table_name))


def _is_db_file(file_path: str) -> bool:
    """Check if a file path is a database file based on extension."""
    if "." not in file_path:
        return False
    ext = file_path[file_path.rfind(".") :].lower()
    return ext in DB_EXTENSIONS


def _extract_statements_by_table(sql_content: str) -> dict[str, list[str]]:
    """Extract SQL statements grouped by table name.

    Returns dict mapping table_name -> list of statements for that table.
    Handles quotes (", '), backticks (`), and IF NOT EXISTS.
    Also handles semicolons inside quoted string values.
    """
    statements_by_table: dict[str, list[str]] = {}

    # Pattern component that matches content until statement end, handling quoted strings.
    # This prevents semicolons inside string values from truncating the match.
    # - [^;'"`] matches any char except semicolon or quotes
    # - '[^']*' matches single-quoted strings (including semicolons inside)
    # - "[^"]*" matches double-quoted strings
    # - `[^`]*` matches backtick-quoted identifiers
    # Note: Doesn't handle escaped quotes (\') but those are rare in SQL dumps
    _STMT_BODY = r"(?:[^;'\"`]|'[^']*'|\"[^\"]*\"|`[^`]*`)*"

    # Match INSERT INTO with various quoting styles: ", ', `, or unquoted
    # Captures the full statement for comparison
    insert_pattern = re.compile(
        rf"(INSERT\s+INTO\s+[`\"']?(\w+)[`\"']?\s*{_STMT_BODY};?)",
        re.IGNORECASE | re.MULTILINE,
    )
    for match in insert_pattern.finditer(sql_content):
        statement = match.group(1).strip()
        table_name = match.group(2)
        if table_name not in statements_by_table:
            statements_by_table[table_name] = []
        statements_by_table[table_name].append(statement)

    # Match CREATE TABLE with IF NOT EXISTS and various quoting
    create_pattern = re.compile(
        rf"(CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?[`\"']?(\w+)[`\"']?\s*{_STMT_BODY};?)",
        re.IGNORECASE | re.MULTILINE,
    )
    for match in create_pattern.finditer(sql_content):
        statement = match.group(1).strip()
        table_name = match.group(2)
        if table_name not in statements_by_table:
            statements_by_table[table_name] = []
        statements_by_table[table_name].append(statement)

    return statements_by_table


def _classify_sql_differences(
    golden_content: str, agent_content: str
) -> dict[str, list[str]]:
    """Classify differences in SQL content by table type.

    Only returns tables where statements actually differ between golden and agent.
    """
    golden_statements = _extract_statements_by_table(golden_content)
    agent_statements = _extract_statements_by_table(agent_content)

    all_tables = set(golden_statements.keys()) | set(agent_statements.keys())
    business_tables = []
    system_tables = []

    for table in sorted(all_tables):
        golden_stmts = sorted(golden_statements.get(table, []))
        agent_stmts = sorted(agent_statements.get(table, []))

        # Only include tables where statements actually differ
        if golden_stmts != agent_stmts:
            if _is_system_table(table):
                system_tables.append(table)
            else:
                business_tables.append(table)

    return {
        "business_tables": business_tables,
        "system_tables": system_tables,
    }


def compute_normalized_diff(
    golden_files: dict[str, str],
    agent_files: dict[str, str],
) -> NormalizedDiff:
    """Compare normalized file contents.

    Args:
        golden_files: Dict of path -> normalized content from golden snapshot
        agent_files: Dict of path -> normalized content from agent snapshot

    Returns:
        NormalizedDiff with matches, mismatches, missing_in_agent, extra_in_agent
    """
    all_paths = set(golden_files.keys()) | set(agent_files.keys())
    diff = NormalizedDiff()

    for file_path in sorted(all_paths):
        golden_content = golden_files.get(file_path)
        agent_content = agent_files.get(file_path)

        if golden_content is None:
            diff.extra_in_agent.append(file_path)
        elif agent_content is None:
            diff.missing_in_agent.append(file_path)
        elif golden_content == agent_content:
            diff.matches.append(file_path)
        else:
            # Content differs - include snippet for LLM
            # For DB files, also store full content for accurate table classification
            is_db = _is_db_file(file_path)
            diff.mismatches.append(
                FileMismatch(
                    file_path=file_path,
                    golden_preview=golden_content[:PREVIEW_SIZE]
                    + ("..." if len(golden_content) > PREVIEW_SIZE else ""),
                    agent_preview=agent_content[:PREVIEW_SIZE]
                    + ("..." if len(agent_content) > PREVIEW_SIZE else ""),
                    golden_full=golden_content if is_db else None,
                    agent_full=agent_content if is_db else None,
                )
            )

    return diff


def has_differences(diff: NormalizedDiff) -> bool:
    """Check if diff contains any differences.

    Args:
        diff: Output from compute_normalized_diff

    Returns:
        True if there are mismatches, missing, or extra files
    """
    return (
        len(diff.mismatches) > 0
        or len(diff.missing_in_agent) > 0
        or len(diff.extra_in_agent) > 0
    )


def analyze_diff_severity(diff: NormalizedDiff) -> DiffSeverityAnalysis:
    """Analyze the severity and type of differences.

    Classifies differences to help determine if they're likely
    business-critical or just system/infrastructure noise.

    Args:
        diff: Output from compute_normalized_diff

    Returns:
        DiffSeverityAnalysis with severity classification and table lists.
    """
    result: DiffSeverityAnalysis = {
        "has_business_table_diffs": False,
        "has_system_table_diffs": False,
        "has_non_db_diffs": False,
        "has_unparseable_db_diffs": False,
        "business_tables": [],
        "system_tables": [],
        "severity": "low",
    }

    all_business_tables: set[str] = set()
    all_system_tables: set[str] = set()

    for mismatch in diff.mismatches:
        if _is_db_file(mismatch.file_path):
            # Classify SQL content using full content when available
            # (previews are truncated and may miss tables)
            golden = mismatch.golden_full or mismatch.golden_preview
            agent = mismatch.agent_full or mismatch.agent_preview
            classification = _classify_sql_differences(golden, agent)
            tables_found = (
                classification["business_tables"] + classification["system_tables"]
            )
            if not tables_found:
                # DB file differs but we couldn't extract any tables
                # (e.g., UPDATE/DELETE/ALTER statements, or non-standard SQL)
                result["has_unparseable_db_diffs"] = True
            all_business_tables.update(classification["business_tables"])
            all_system_tables.update(classification["system_tables"])
        else:
            # Non-database file difference
            result["has_non_db_diffs"] = True

    result["business_tables"] = sorted(all_business_tables)
    result["system_tables"] = sorted(all_system_tables)
    result["has_business_table_diffs"] = len(all_business_tables) > 0
    result["has_system_table_diffs"] = len(all_system_tables) > 0

    # Missing/extra files are always potentially significant
    if diff.missing_in_agent or diff.extra_in_agent:
        result["has_non_db_diffs"] = True

    # Determine overall severity
    if result["has_business_table_diffs"]:
        result["severity"] = "high"
    elif result["has_unparseable_db_diffs"]:
        # DB diffs we couldn't parse should be treated cautiously
        result["severity"] = "medium"
    elif result["has_non_db_diffs"]:
        result["severity"] = "medium"
    elif result["has_system_table_diffs"]:
        result["severity"] = "low"
    else:
        result["severity"] = "low"

    return result


def format_diff_for_prompt(diff: NormalizedDiff) -> str:
    """Format normalized diff as readable string for LLM.

    Intelligently limits output size to avoid exceeding LLM context limits:
    - Only shows count of matching files (not full list)
    - Limits number of missing/extra files listed
    - Limits number of detailed mismatches shown
    - Classifies SQL differences by table type (business vs system)
    - Applies overall size cap as final safety net

    Args:
        diff: Output from compute_normalized_diff

    Returns:
        Formatted string suitable for LLM prompt
    """
    lines = []

    # Summary section - always compact
    num_matches = len(diff.matches)
    num_missing = len(diff.missing_in_agent)
    num_extra = len(diff.extra_in_agent)
    num_mismatches = len(diff.mismatches)

    lines.append("=== SUMMARY ===")
    lines.append(f"Matching files: {num_matches}")
    lines.append(f"Content mismatches: {num_mismatches}")
    lines.append(f"Missing in agent: {num_missing}")
    lines.append(f"Extra in agent: {num_extra}")
    lines.append("")

    # Missing files - list up to MAX_FILES_LISTED
    if diff.missing_in_agent:
        missing = diff.missing_in_agent
        if len(missing) <= MAX_FILES_LISTED:
            lines.append(f"Missing in agent: {', '.join(missing)}")
        else:
            shown = missing[:MAX_FILES_LISTED]
            lines.append(
                f"Missing in agent (showing {MAX_FILES_LISTED} of {len(missing)}): "
                f"{', '.join(shown)}, ..."
            )

    # Extra files - list up to MAX_FILES_LISTED
    if diff.extra_in_agent:
        extra = diff.extra_in_agent
        if len(extra) <= MAX_FILES_LISTED:
            lines.append(f"Extra in agent: {', '.join(extra)}")
        else:
            shown = extra[:MAX_FILES_LISTED]
            lines.append(
                f"Extra in agent (showing {MAX_FILES_LISTED} of {len(extra)}): "
                f"{', '.join(shown)}, ..."
            )

    # Analyze and classify differences for database files
    # Note: This duplicates work done in analyze_diff_severity, but format_diff_for_prompt
    # is designed to be callable independently. Consider passing pre-computed analysis
    # if performance becomes an issue.
    all_business_tables: set[str] = set()
    all_system_tables: set[str] = set()

    for mismatch in diff.mismatches:
        if _is_db_file(mismatch.file_path):
            # Use full content when available (previews may miss tables)
            golden = mismatch.golden_full or mismatch.golden_preview
            agent = mismatch.agent_full or mismatch.agent_preview
            classification = _classify_sql_differences(golden, agent)
            all_business_tables.update(classification["business_tables"])
            all_system_tables.update(classification["system_tables"])

    # Add classification section if we found database differences
    if all_business_tables or all_system_tables:
        lines.append("\n=== TABLE CLASSIFICATION ===")
        if all_business_tables:
            lines.append(
                f"BUSINESS TABLES with differences: {', '.join(sorted(all_business_tables))}"
            )
            lines.append(
                "  ^ These contain domain/business data - differences are likely meaningful"
            )
        if all_system_tables:
            lines.append(
                f"SYSTEM TABLES with differences: {', '.join(sorted(all_system_tables))}"
            )
            lines.append(
                "  ^ These are infrastructure tables - differences may be trivial"
            )
        lines.append("")

    # Mismatches - show detailed content for up to MAX_MISMATCHES_SHOWN
    mismatches = diff.mismatches
    mismatches_to_show = mismatches[:MAX_MISMATCHES_SHOWN]

    if len(mismatches) > MAX_MISMATCHES_SHOWN:
        lines.append(
            f"\n=== CONTENT MISMATCHES (showing {MAX_MISMATCHES_SHOWN} of {len(mismatches)}) ==="
        )
        # List the files we're not showing details for
        not_shown = [m.file_path for m in mismatches[MAX_MISMATCHES_SHOWN:]]
        if len(not_shown) <= 10:
            lines.append(f"Additional mismatched files: {', '.join(not_shown)}")
        else:
            lines.append(
                f"Additional mismatched files: {', '.join(not_shown[:10])}, "
                f"... and {len(not_shown) - 10} more"
            )
    elif mismatches:
        lines.append("\n=== CONTENT MISMATCHES ===")

    # Adjust preview size based on number of mismatches to show
    # More mismatches = smaller previews to fit in context
    if len(mismatches_to_show) > 0:
        preview_budget = (MAX_PROMPT_SIZE - 2000) // len(mismatches_to_show) // 2
        effective_preview_size = min(PREVIEW_SIZE, max(500, preview_budget))
    else:
        effective_preview_size = PREVIEW_SIZE

    for mismatch in mismatches_to_show:
        lines.append(f"\n--- {mismatch.file_path} ---")
        lines.append("Golden (expected):")
        golden = mismatch.golden_preview
        if len(golden) > effective_preview_size:
            golden = golden[:effective_preview_size] + "... [truncated]"
        lines.append(golden)
        lines.append("\nAgent (actual):")
        agent = mismatch.agent_preview
        if len(agent) > effective_preview_size:
            agent = agent[:effective_preview_size] + "... [truncated]"
        lines.append(agent)

    result = "\n".join(lines)

    # Final safety net - hard truncate if still too large
    if len(result) > MAX_PROMPT_SIZE:
        result = result[:MAX_PROMPT_SIZE] + "\n\n... [output truncated due to size]"

    return result

import re
from typing import Any

from .base import BaseParser, TableMapping


class SQLInsertParser(BaseParser):
    """Parser for SQL INSERT statements (e.g., database dumps).

    Extracts data from INSERT statements in the form:
        INSERT INTO table_name (col1, col2) VALUES (val1, val2), (val3, val4);
    or:
        INSERT INTO table_name VALUES (val1, val2);
    or with schema qualification:
        INSERT INTO schema.table_name (col1, col2) VALUES (val1, val2);
        INSERT INTO [schema].[table_name] (col1, col2) VALUES (val1, val2);

    Supports both MySQL-style backslash escapes (\\', \\") and
    SQL-standard doubled-quote escapes ('', "").
    """

    def parse(self, content: str) -> dict[str, list[dict[str, Any]]]:
        """Parse SQL content and extract all INSERT statements.

        Args:
            content: Raw SQL text containing INSERT statements.

        Returns:
            Dictionary mapping table names to list of row dictionaries.
            Schema-qualified tables are stored as "schema.table" keys.
        """
        tables: dict[str, list[dict[str, Any]]] = {}

        # Split into statements using quote-aware logic (semicolons inside
        # quoted strings must not terminate the statement)
        for statement in self._split_statements(content):
            statement = statement.strip()
            if not statement.upper().startswith("INSERT"):
                continue

            # Match INSERT...VALUES (without the values part - we extract that
            # with quote-aware logic to handle semicolons inside strings)
            # Supports table names with spaces when backtick-quoted (e.g., `tabSales Order`)
            insert_header_pattern = re.compile(
                r"INSERT\s+INTO\s+"
                r"(?:(?:\[([^\]]+)\]|`([^`]+)`|'([^']+)'|\"([^\"]+)\"|(\w+))\.)?"  # Optional schema
                r"(?:\[([^\]]+)\]|`([^`]+)`|'([^']+)'|\"([^\"]+)\"|(\w+))\s*"  # Table name
                r"(?:\(([^)]+)\))?\s*VALUES\s*",
                re.IGNORECASE,
            )

            match = insert_header_pattern.match(statement)
            if not match:
                continue
            # Schema can be in groups 1-5 (different quoting styles)
            schema_name = (
                match.group(1)
                or match.group(2)
                or match.group(3)
                or match.group(4)
                or match.group(5)
            )
            # Table name can be in groups 6-10 (different quoting styles)
            table_name_raw = (
                match.group(6)
                or match.group(7)
                or match.group(8)
                or match.group(9)
                or match.group(10)
            )
            if not table_name_raw:
                continue
            table_name = table_name_raw.lower()
            if schema_name:
                table_name = f"{schema_name.lower()}.{table_name}"
            columns_str = match.group(11)

            # Extract values_str with quote-aware logic (semicolons in strings
            # must not terminate the VALUES clause)
            values_start = match.end()
            values_str = self._extract_values_until_semicolon(statement, values_start)

            # Parse column names if present
            columns: list[str] | None = None
            if columns_str:
                columns = [c.strip().strip("`'\"") for c in columns_str.split(",")]

            # Parse value tuples
            rows = self._parse_values(values_str, columns)

            if table_name not in tables:
                tables[table_name] = []
            tables[table_name].extend(rows)

        return tables

    def _split_statements(self, content: str) -> list[str]:
        """Split SQL content into statements, respecting quoted strings.

        Semicolons inside single- or double-quoted strings do not terminate
        the statement. Handles both backslash and doubled-quote escapes.
        Also handles PostgreSQL dollar-quoted strings ($$...$$, $tag$...$tag$).
        """
        statements: list[str] = []
        current: list[str] = []
        i = 0
        in_string = False
        string_char: str | None = None
        in_dollar_quote = False
        dollar_tag: str = ""

        while i < len(content):
            char = content[i]

            # Inside a dollar-quoted string (PostgreSQL: $$...$$ or $tag$...$tag$)
            if in_dollar_quote:
                current.append(char)
                if char == "$" and content[i : i + len(dollar_tag)] == dollar_tag:
                    # Found closing dollar-quote tag
                    for c in dollar_tag[1:]:
                        i += 1
                        current.append(c)
                    in_dollar_quote = False
                    dollar_tag = ""
                i += 1
                continue

            if in_string:
                current.append(char)
                if char == string_char and string_char is not None:
                    escape_type = self._get_quote_escape_type(content, i, string_char)
                    if escape_type is None:
                        in_string = False
                        string_char = None
                    elif escape_type == "doubled":
                        i += 1
                        if i < len(content):
                            current.append(content[i])
                i += 1
                continue

            # Check for PostgreSQL dollar-quote start: $$ or $tag$
            if char == "$":
                # Look ahead for dollar-quote tag: $<optional_tag>$
                j = i + 1
                while j < len(content) and (content[j].isalnum() or content[j] == "_"):
                    j += 1
                if j < len(content) and content[j] == "$":
                    # Found a dollar-quote tag like $$ or $function$
                    dollar_tag = content[i : j + 1]
                    in_dollar_quote = True
                    # Consume the entire opening tag so the body loop
                    # doesn't falsely match it as the closing tag
                    current.extend(dollar_tag)
                    i = j + 1
                    continue

            if char in ("'", '"'):
                in_string = True
                string_char = char
                current.append(char)
                i += 1
                continue

            if char == ";":
                stmt = "".join(current).strip()
                if stmt:
                    statements.append(stmt)
                current = []
                i += 1
                continue

            current.append(char)
            i += 1

        stmt = "".join(current).strip()
        if stmt:
            statements.append(stmt)

        return statements

    def _extract_values_until_semicolon(self, content: str, start: int) -> str:
        """Extract the VALUES clause from start until a semicolon outside quotes."""
        result: list[str] = []
        i = start
        in_string = False
        string_char: str | None = None

        while i < len(content):
            char = content[i]

            if in_string:
                result.append(char)
                if char == string_char and string_char is not None:
                    escape_type = self._get_quote_escape_type(content, i, string_char)
                    if escape_type is None:
                        in_string = False
                        string_char = None
                    elif escape_type == "doubled":
                        i += 1
                        if i < len(content):
                            result.append(content[i])
                i += 1
                continue

            if char == ";":
                return "".join(result).strip()

            if char in ("'", '"'):
                in_string = True
                string_char = char
                result.append(char)
                i += 1
                continue

            result.append(char)
            i += 1

        return "".join(result).strip()

    def apply_mapping(
        self, data: dict[str, list[dict[str, Any]]], mapping: TableMapping
    ) -> list[dict[str, Any]]:
        """Extract rows for the specified source table.

        Args:
            data: Parsed INSERT data (table_name -> rows).
            mapping: Must include source_table specifying which SQL table to use.

        Returns:
            List of row dictionaries from the specified source table.
        """
        if not mapping.source_table:
            return []

        source_table = mapping.source_table.lower()
        return data.get(source_table, [])

    def _parse_values(
        self, values_str: str, columns: list[str] | None
    ) -> list[dict[str, Any]]:
        """Parse VALUES clause into list of row dictionaries."""
        rows: list[dict[str, Any]] = []

        # Extract tuples using string-aware parsing to handle parens in quoted strings
        for tuple_str in self._extract_tuples(values_str):
            values = self._parse_tuple_values(tuple_str)

            if columns:
                row = dict(zip(columns, values, strict=False))
            else:
                # No column names - use numeric indices
                row = {f"col{i}": v for i, v in enumerate(values)}

            rows.append(row)

        return rows

    def _get_quote_escape_type(self, s: str, pos: int, quote_char: str) -> str | None:
        """Determine how a quote at position pos is escaped.

        Args:
            s: The string being parsed
            pos: Position of the quote character
            quote_char: The quote character (' or ")

        Returns:
            'backslash' if backslash-escaped, 'doubled' if doubled quote,
            or None if the quote is not escaped (ends the string).
        """
        if pos == 0:
            return None

        # Check for backslash escape: count preceding backslashes
        # An odd number of backslashes means the quote is escaped
        num_backslashes = 0
        check_pos = pos - 1
        while check_pos >= 0 and s[check_pos] == "\\":
            num_backslashes += 1
            check_pos -= 1

        if num_backslashes % 2 == 1:
            return "backslash"  # Backslash-escaped quote

        # Check for SQL-standard doubled quote: 'it''s' or "say ""hello"""
        # Look ahead to see if next char is also a quote
        if pos + 1 < len(s) and s[pos + 1] == quote_char:
            return "doubled"  # Doubled quote escape

        return None

    def _extract_tuples(self, values_str: str) -> list[str]:
        """Extract value tuples while respecting quoted strings.

        Handles cases like: ('hello (world)', 123), ('it''s', 456)
        where parentheses and quotes inside strings should not affect parsing.
        """
        tuples: list[str] = []
        current = ""
        depth = 0
        in_string = False
        string_char: str | None = None
        i = 0

        while i < len(values_str):
            char = values_str[i]

            if in_string:
                current += char
                if char == string_char and string_char is not None:
                    # Check if and how this quote is escaped
                    escape_type = self._get_quote_escape_type(
                        values_str, i, string_char
                    )
                    if escape_type is None:
                        # Quote is not escaped - ends the string
                        in_string = False
                        string_char = None
                    elif escape_type == "doubled":
                        # Doubled quote - consume the second quote too
                        i += 1
                        current += values_str[i]
                    # For backslash escapes, just continue (already added char)
            elif char in ("'", '"'):
                in_string = True
                string_char = char
                current += char
            elif char == "(":
                if depth == 0:
                    # Starting a new tuple
                    current = ""
                else:
                    current += char
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    # Completed a tuple
                    tuples.append(current)
                    current = ""
                else:
                    current += char
            elif depth > 0:
                current += char

            i += 1

        return tuples

    def _parse_tuple_values(self, tuple_str: str) -> list[Any]:
        """Parse a single value tuple into a list of values."""
        values: list[Any] = []
        current = ""
        in_string = False
        string_char: str | None = None
        depth = 0

        i = 0
        while i < len(tuple_str):
            char = tuple_str[i]

            if in_string:
                current += char
                if char == string_char and string_char is not None:
                    # Check if and how this quote is escaped
                    escape_type = self._get_quote_escape_type(tuple_str, i, string_char)
                    if escape_type is None:
                        # Quote is not escaped - ends the string
                        in_string = False
                        string_char = None
                    elif escape_type == "doubled":
                        # Doubled quote - consume the second quote too
                        i += 1
                        current += tuple_str[i]
                    # For backslash escapes, just continue (already added char)
            elif char in ("'", '"'):
                in_string = True
                string_char = char
                current += char
            elif char == "(":
                depth += 1
                current += char
            elif char == ")":
                depth -= 1
                current += char
            elif char == "," and depth == 0:
                values.append(self._parse_value(current.strip()))
                current = ""
            else:
                current += char

            i += 1

        if current.strip():
            values.append(self._parse_value(current.strip()))

        return values

    def _parse_value(self, value_str: str) -> Any:
        """Parse a single SQL value into a Python value."""
        if not value_str or value_str.upper() == "NULL":
            return None

        # Remove quotes from strings
        if (value_str.startswith("'") and value_str.endswith("'")) or (
            value_str.startswith('"') and value_str.endswith('"')
        ):
            quote_char = value_str[0]
            unquoted = value_str[1:-1]

            # Use single-pass replacement to correctly handle all escape sequences.
            # Order matters: we use a regex to handle everything in one pass to avoid
            # issues with sequences like \\' (escaped backslash followed by quote).
            unquoted = self._unescape_sql_string(unquoted, quote_char)
            return unquoted

        # Try to parse as number
        try:
            if "." in value_str:
                return float(value_str)
            return int(value_str)
        except ValueError:
            return value_str

    def _unescape_sql_string(self, s: str, quote_char: str) -> str:
        """Unescape a SQL string value using single-pass processing.

        Handles:
        - SQL-standard doubled quotes: '' -> ', "" -> "
        - MySQL backslash escapes: \\' -> ', \\" -> ", \\\\ -> \\
        - Common escape sequences: \\n -> newline, \\t -> tab, etc.

        Args:
            s: The string content (without outer quotes)
            quote_char: The quote character used (' or ")

        Returns:
            The unescaped string
        """
        result: list[str] = []
        i = 0

        while i < len(s):
            char = s[i]

            if char == "\\":
                # Backslash escape sequence
                if i + 1 < len(s):
                    next_char = s[i + 1]
                    if next_char == "\\":
                        result.append("\\")
                        i += 2
                        continue
                    elif next_char == "'":
                        result.append("'")
                        i += 2
                        continue
                    elif next_char == '"':
                        result.append('"')
                        i += 2
                        continue
                    elif next_char == "n":
                        result.append("\n")
                        i += 2
                        continue
                    elif next_char == "r":
                        result.append("\r")
                        i += 2
                        continue
                    elif next_char == "t":
                        result.append("\t")
                        i += 2
                        continue
                    elif next_char == "0":
                        result.append("\0")
                        i += 2
                        continue
                    elif next_char == "b":
                        result.append("\b")
                        i += 2
                        continue
                    elif next_char == "%":
                        # MySQL: \% is literal % (for LIKE patterns)
                        result.append("%")
                        i += 2
                        continue
                    elif next_char == "_":
                        # MySQL: \_ is literal _ (for LIKE patterns)
                        result.append("_")
                        i += 2
                        continue
                    # Unknown escape - keep both characters (MySQL behavior)
                    result.append(char)
                    i += 1
                else:
                    # Trailing backslash
                    result.append(char)
                    i += 1

            elif char == quote_char and i + 1 < len(s) and s[i + 1] == quote_char:
                # SQL-standard doubled quote: '' -> ' or "" -> "
                result.append(quote_char)
                i += 2

            else:
                result.append(char)
                i += 1

        return "".join(result)


class PostgreSQLCopyParser:
    """Parser for PostgreSQL pg_dump COPY format.

    Handles blocks of the form::

        COPY [schema.]table_name (col1, col2, ...) FROM stdin;
        value1\\tvalue2\\t...
        \\.

    NULL values are represented as ``\\N`` in COPY format.
    """

    _COPY_RE: re.Pattern[str] = re.compile(
        r"^COPY\s+"
        r'(?:(?:"[^"]+"|[\w]+)\.)?'  # optional schema (quoted or bare)
        r'("([^"]+)"|(\w+))\s+'  # table: quoted (group 2) or bare (group 3)
        r"\(([^)]+)\)\s+"  # column list (group 4)
        r"FROM\s+stdin\s*;",
        re.IGNORECASE,
    )

    _ESCAPE_MAP: dict[str, str] = {
        "\\": "\\",
        "t": "\t",
        "n": "\n",
        "r": "\r",
        "b": "\b",
        "f": "\f",
        "v": "\v",
        "0": "\0",
    }

    @classmethod
    def _unescape(cls, value: str) -> Any:
        """Decode PostgreSQL COPY escape sequences.

        Returns ``None`` for ``\\N`` (SQL NULL), otherwise returns the
        decoded string.
        """
        if value == "\\N":
            return None
        result: list[str] = []
        i = 0
        while i < len(value):
            if value[i] == "\\" and i + 1 < len(value):
                next_ch = value[i + 1]
                if next_ch in cls._ESCAPE_MAP:
                    result.append(cls._ESCAPE_MAP[next_ch])
                    i += 2
                else:
                    # Per PG COPY spec: unrecognized escape → the character itself
                    result.append(next_ch)
                    i += 2
            else:
                result.append(value[i])
                i += 1
        return "".join(result)

    def parse(self, content: str) -> dict[str, list[dict[str, Any]]]:
        """Parse a PostgreSQL COPY-format dump.

        Returns:
            Dictionary mapping table names (lowercase) to a list of row dicts.
        """
        tables: dict[str, list[dict[str, Any]]] = {}
        lines = content.splitlines()
        i = 0
        while i < len(lines):
            m = self._COPY_RE.match(lines[i].strip())
            if m:
                # group 2 = quoted table name, group 3 = bare table name
                table_name = (m.group(2) or m.group(3)).lower()
                columns = [c.strip().strip('"') for c in m.group(4).split(",")]
                rows: list[dict[str, Any]] = []
                i += 1
                while i < len(lines):
                    data_line = lines[i]
                    if data_line == "\\.":
                        i += 1
                        break
                    values = data_line.split("\t")
                    rows.append(
                        {
                            col: self._unescape(val)
                            for col, val in zip(columns, values, strict=False)
                        }
                    )
                    i += 1
                tables.setdefault(table_name, []).extend(rows)
            else:
                i += 1
        return tables


def detect_sql_format(sql_content: str) -> str:
    """Detect whether a SQL dump uses PostgreSQL COPY or MySQL INSERT format.

    Scans the full content for a ``COPY ... FROM stdin;`` line.

    Returns:
        ``'postgresql_copy'`` or ``'mysql_insert'``.
    """
    if re.search(
        r"^COPY\s+\S.*\s+FROM\s+stdin\s*;", sql_content, re.IGNORECASE | re.MULTILINE
    ):
        return "postgresql_copy"
    return "mysql_insert"


def parse_sql_dump(content: str) -> dict[str, list[dict[str, Any]]]:
    """Auto-detect the SQL dump format and parse with the right engine.

    Supports:
    - MySQL / MariaDB ``INSERT INTO ... VALUES`` dumps
    - PostgreSQL ``COPY ... FROM stdin`` dumps (default ``pg_dump`` output)

    Returns:
        ``{table_name: [row_dicts, ...]}`` — same shape regardless of format.
    """
    fmt = detect_sql_format(content)
    if fmt == "postgresql_copy":
        return PostgreSQLCopyParser().parse(content)
    return SQLInsertParser().parse(content)

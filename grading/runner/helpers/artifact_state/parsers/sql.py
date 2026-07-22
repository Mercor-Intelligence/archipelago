import io
import re
from collections.abc import Iterator
from typing import Any, Protocol

from .base import BaseParser, TableMapping


class ReadableTextStream(Protocol):
    """Minimal protocol for a readable text stream.

    Satisfied by ``TextIO``, ``io.StringIO``, ``io.TextIOWrapper``,
    and :class:`_PrefixedStream`.
    """

    def read(self, size: int = ..., /) -> str: ...
    def readline(self) -> str: ...
    def __iter__(self) -> Iterator[str]: ...
    def __next__(self) -> str: ...


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

    _INSERT_HEADER_RE: re.Pattern[str] = re.compile(
        r"\s*INSERT\s+INTO\s+"
        r"(?:(?:\[([^\]]+)\]|`([^`]+)`|'([^']+)'|\"([^\"]+)\"|(\w+))\.)?"  # Optional schema
        r"(?:\[([^\]]+)\]|`([^`]+)`|'([^']+)'|\"([^\"]+)\"|(\w+))\s*"  # Table name
        r"(?:\(([^)]+)\))?\s*VALUES\s*",
        re.IGNORECASE,
    )

    def iter_rows(self, content: str) -> Iterator[tuple[str, dict[str, Any]]]:
        """Stream-parse SQL content, yielding ``(table_name, row_dict)`` pairs.

        This avoids materializing all rows into memory at once, which is
        critical for multi-GB SQL dumps.

        Args:
            content: Raw SQL text containing INSERT statements.

        Yields:
            ``(table_name, row_dict)`` for each parsed row.
        """
        for stmt_start, stmt_end in self._split_statements(content):
            # Skip leading whitespace without copying
            pos = stmt_start
            while pos < stmt_end and content[pos] in (" ", "\t", "\n", "\r"):
                pos += 1

            # Quick prefix check — 6 chars, no copy
            if stmt_end - pos < 6 or content[pos : pos + 6].upper() != "INSERT":
                continue

            match = self._INSERT_HEADER_RE.match(content, pos=pos, endpos=stmt_end)
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
            if schema_name and schema_name.lower() != "public":
                table_name = f"{schema_name.lower()}.{table_name}"
            columns_str = match.group(11)

            values_start = match.end()
            values_end = stmt_end

            # Parse column names if present
            columns: list[str] | None = None
            if columns_str:
                columns = [c.strip().strip("`'\"") for c in columns_str.split(",")]

            # Yield individual rows instead of collecting them
            for tuple_str in self._extract_tuples(content, values_start, values_end):
                values = self._parse_tuple_values(tuple_str)
                if columns:
                    row = dict(zip(columns, values, strict=False))
                else:
                    row = {f"col{j}": v for j, v in enumerate(values)}
                yield (table_name, row)

    def parse(self, content: str) -> dict[str, list[dict[str, Any]]]:
        """Parse SQL content and extract all INSERT statements.

        Args:
            content: Raw SQL text containing INSERT statements.

        Returns:
            Dictionary mapping table names to list of row dictionaries.
            Schema-qualified tables are stored as "schema.table" keys.
        """
        tables: dict[str, list[dict[str, Any]]] = {}
        for table_name, row in self.iter_rows(content):
            tables.setdefault(table_name, []).append(row)
        return tables

    def _split_statements(self, content: str) -> Iterator[tuple[int, int]]:
        """Split SQL content into statements, respecting quoted strings.

        Yields (start, end) index pairs into *content* — no string copies.

        Semicolons inside single- or double-quoted strings do not terminate
        the statement. Handles both backslash and doubled-quote escapes.
        Also handles PostgreSQL dollar-quoted strings ($$...$$, $tag$...$tag$).
        """
        stmt_start = 0
        i = 0
        in_string = False
        string_char: str | None = None
        in_dollar_quote = False
        dollar_tag: str = ""

        while i < len(content):
            char = content[i]

            # Inside a dollar-quoted string (PostgreSQL: $$...$$ or $tag$...$tag$)
            if in_dollar_quote:
                if char == "$" and content[i : i + len(dollar_tag)] == dollar_tag:
                    # Found closing dollar-quote tag — skip past it
                    i += len(dollar_tag)
                    in_dollar_quote = False
                    dollar_tag = ""
                else:
                    i += 1
                continue

            if in_string:
                if char == string_char and string_char is not None:
                    escape_type = self._get_quote_escape_type(content, i, string_char)
                    if escape_type is None:
                        in_string = False
                        string_char = None
                    elif escape_type == "doubled":
                        i += 2
                        continue
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
                    i = j + 1
                    continue

            if char in ("'", '"'):
                in_string = True
                string_char = char
                i += 1
                continue

            if char == ";":
                yield (stmt_start, i)
                stmt_start = i + 1
                i += 1
                continue

            i += 1

        if stmt_start < len(content):
            yield (stmt_start, len(content))

    # Size of chunks read from the stream in :meth:`iter_rows_from_stream`.
    _STREAM_CHUNK_SIZE: int = 8 * 1024 * 1024  # 8 MB

    def iter_rows_from_stream(
        self, stream: ReadableTextStream
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        """Stream-parse SQL from a text stream, yielding ``(table, row)`` pairs.

        Reads fixed-size chunks from *stream* and processes characters
        incrementally.  At most one value-tuple's worth of text is buffered
        at any time, so memory usage is bounded regardless of how long an
        individual INSERT line is (``mysqldump --extended-insert`` emits all
        rows for a table on a single line that can exceed 10 GB).
        """
        buf = ""
        pos = 0
        eof = False

        def _refill() -> bool:
            nonlocal buf, eof
            if eof:
                return False
            chunk = stream.read(self._STREAM_CHUNK_SIZE)
            if not chunk:
                eof = True
                return False
            buf += chunk
            return True

        # Mutable container so _compact can adjust tuple_start across frames.
        ts = [0]  # ts[0] = tuple_start

        def _compact(up_to: int) -> None:
            """Discard ``buf[:up_to]`` and adjust *pos* and *tuple_start*.

            Only performs the physical copy when the dead region exceeds
            one chunk, keeping the amortised cost O(n) instead of the
            O(n²) that per-tuple slicing would cause.
            """
            nonlocal buf, pos
            if up_to > self._STREAM_CHUNK_SIZE:
                buf = buf[up_to:]
                pos -= up_to
                ts[0] = max(0, ts[0] - up_to)

        def _trim(up_to: int) -> None:
            """Unconditionally discard ``buf[:up_to]`` (used between statements)."""
            nonlocal buf, pos
            if up_to > 0:
                buf = buf[up_to:]
                pos -= up_to

        # Regex to locate block-comment boundaries (/* and */) efficiently.
        # Used during scanning to avoid matching INSERT headers inside
        # /*!50003 ... */ conditional comments (triggers, procedures).
        comment_token_re = re.compile(r"/\*|\*/")

        # Initial fill
        _refill()

        while True:
            # ---- SCANNING: find next INSERT ... VALUES header ----
            # Accumulate chunks until the regex finds a complete header.
            # Between INSERT statements the non-INSERT text (comments,
            # CREATE TABLE, etc.) is typically small, so the buffer stays
            # bounded.  After a match we trim aggressively.
            #
            # We track block-comment depth so that INSERT patterns inside
            # /*!50003 ... */ conditional comments are skipped.
            comment_depth = 0
            comment_scanned = pos  # how far we've tracked comment tokens
            match = None
            while True:
                match = self._INSERT_HEADER_RE.search(buf, pos)
                if match is None:
                    # Track comment depth through the entire unmatched region
                    for m in comment_token_re.finditer(buf, comment_scanned):
                        if m.group() == "/*":
                            comment_depth += 1
                        else:
                            comment_depth = max(0, comment_depth - 1)
                    comment_scanned = len(buf)
                    # Keep buffer bounded: discard already-searched text,
                    # keeping an overlap for headers split at the boundary.
                    overlap = min(4096, len(buf))
                    discard = len(buf) - overlap
                    if discard > 0:
                        _trim(discard)
                        comment_scanned = max(0, len(buf) - 1)
                    if not _refill():
                        break  # EOF
                    continue

                # Track comment depth up to the match position
                for m in comment_token_re.finditer(buf, comment_scanned, match.start()):
                    if m.group() == "/*":
                        comment_depth += 1
                    else:
                        comment_depth = max(0, comment_depth - 1)
                comment_scanned = match.start()

                if comment_depth > 0:
                    # INSERT is inside a block comment — skip it
                    pos = match.end()
                    comment_scanned = pos
                    match = None
                    continue
                break  # real match outside comments

            if match is None:
                break  # EOF, no more INSERT statements

            # Extract table / column metadata from the header.
            schema_name = (
                match.group(1)
                or match.group(2)
                or match.group(3)
                or match.group(4)
                or match.group(5)
            )
            table_name_raw = (
                match.group(6)
                or match.group(7)
                or match.group(8)
                or match.group(9)
                or match.group(10)
            )
            if not table_name_raw:
                pos = match.end()
                continue
            table_name = table_name_raw.lower()
            if schema_name and schema_name.lower() != "public":
                table_name = f"{schema_name.lower()}.{table_name}"

            columns_str = match.group(11)
            columns: list[str] | None = None
            if columns_str:
                columns = [c.strip().strip("`'\"") for c in columns_str.split(",")]

            # Advance past the header and trim everything before VALUES.
            pos = match.end()
            _trim(pos)
            # pos is now 0

            # ---- IN_VALUES: extract tuples character-by-character ----
            in_string = False
            string_char: str | None = None
            depth = 0
            ts[0] = 0

            while True:
                if pos >= len(buf):
                    if not _refill():
                        break
                    if pos >= len(buf):
                        break

                char = buf[pos]

                if in_string:
                    if char == string_char and string_char is not None:
                        # Ensure lookahead for doubled-quote detection.
                        if len(buf) - pos < 2 and not eof:
                            _refill()
                        escape = self._get_quote_escape_type(buf, pos, string_char)
                        if escape is None:
                            in_string = False
                            string_char = None
                        elif escape == "doubled":
                            pos += 2
                            continue
                    pos += 1
                    continue

                if char in ("'", '"'):
                    in_string = True
                    string_char = char
                    pos += 1
                    continue

                if char == "(":
                    if depth == 0:
                        ts[0] = pos + 1
                    depth += 1
                    pos += 1
                    continue

                if char == ")":
                    depth -= 1
                    if depth == 0:
                        # Complete tuple — parse and yield.
                        tuple_str = buf[ts[0] : pos]
                        values = self._parse_tuple_values(tuple_str)
                        if columns:
                            row = dict(zip(columns, values, strict=False))
                        else:
                            row = {f"col{j}": v for j, v in enumerate(values)}
                        yield (table_name, row)
                        pos += 1
                        _compact(pos)
                        # tuple_start will be set at next "("
                        continue
                    pos += 1
                    continue

                if char == ";" and depth == 0:
                    # End of INSERT statement — back to scanning.
                    pos += 1
                    _trim(pos)
                    break

                pos += 1

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

    def _extract_tuples(self, content: str, start: int, end: int) -> Iterator[str]:
        """Extract value tuples while respecting quoted strings.

        Operates on ``content[start:end]`` and yields each inner tuple
        as a small string (the content between the outer parentheses).

        Handles cases like: ('hello (world)', 123), ('it''s', 456)
        where parentheses and quotes inside strings should not affect parsing.
        """
        depth = 0
        in_string = False
        string_char: str | None = None
        tuple_start = start
        i = start

        while i < end:
            char = content[i]

            if in_string:
                if char == string_char and string_char is not None:
                    escape_type = self._get_quote_escape_type(content, i, string_char)
                    if escape_type is None:
                        in_string = False
                        string_char = None
                    elif escape_type == "doubled":
                        i += 2
                        continue
            elif char in ("'", '"'):
                in_string = True
                string_char = char
            elif char == "(":
                if depth == 0:
                    tuple_start = i + 1
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    yield content[tuple_start:i]

            i += 1

    def _parse_tuple_values(self, tuple_str: str) -> list[Any]:
        """Parse a single value tuple into a list of values.

        Uses index-based slicing instead of ``current += char`` to avoid
        O(n²) intermediate string copies on large tuples.
        """
        values: list[Any] = []
        value_start = 0
        in_string = False
        string_char: str | None = None
        depth = 0
        i = 0

        while i < len(tuple_str):
            char = tuple_str[i]

            if in_string:
                if char == string_char and string_char is not None:
                    escape_type = self._get_quote_escape_type(tuple_str, i, string_char)
                    if escape_type is None:
                        in_string = False
                        string_char = None
                    elif escape_type == "doubled":
                        i += 2
                        continue
            elif char in ("'", '"'):
                in_string = True
                string_char = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char == "," and depth == 0:
                values.append(self._parse_value(tuple_str[value_start:i].strip()))
                value_start = i + 1

            i += 1

        trailing = tuple_str[value_start:].strip()
        if trailing:
            values.append(self._parse_value(trailing))

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

    def iter_rows(self, content: str) -> Iterator[tuple[str, dict[str, Any]]]:
        """Stream-parse PostgreSQL COPY blocks, yielding ``(table, row)`` pairs.

        Yields:
            ``(table_name, row_dict)`` for each parsed data row.
        """
        lines = content.splitlines()
        i = 0
        while i < len(lines):
            m = self._COPY_RE.match(lines[i].strip())
            if m:
                table_name = (m.group(2) or m.group(3)).lower()
                columns = [c.strip().strip('"') for c in m.group(4).split(",")]
                i += 1
                while i < len(lines):
                    data_line = lines[i]
                    if data_line == "\\.":
                        i += 1
                        break
                    values = data_line.split("\t")
                    yield (
                        table_name,
                        {
                            col: self._unescape(val)
                            for col, val in zip(columns, values, strict=False)
                        },
                    )
                    i += 1
            else:
                i += 1

    def iter_rows_from_stream(
        self, stream: ReadableTextStream
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        """Stream-parse PostgreSQL COPY blocks from a text stream.

        Reads line-by-line, detecting ``COPY ... FROM stdin;`` headers and
        yielding ``(table_name, row_dict)`` pairs for each data row until
        the ``\\.`` terminator.
        """
        for line in stream:
            m = self._COPY_RE.match(line.strip())
            if not m:
                continue
            table_name = (m.group(2) or m.group(3)).lower()
            columns = [c.strip().strip('"') for c in m.group(4).split(",")]
            for data_line in stream:
                data_line = data_line.rstrip("\n\r")
                if data_line == "\\.":
                    break
                values = data_line.split("\t")
                yield (
                    table_name,
                    {
                        col: self._unescape(val)
                        for col, val in zip(columns, values, strict=False)
                    },
                )

    def parse(self, content: str) -> dict[str, list[dict[str, Any]]]:
        """Parse a PostgreSQL COPY-format dump.

        Returns:
            Dictionary mapping table names (lowercase) to a list of row dicts.
            Tables with COPY headers but no data rows appear as empty lists.
        """
        tables: dict[str, list[dict[str, Any]]] = {}
        # Pre-scan for COPY headers to ensure empty tables are registered
        lines = content.splitlines()
        for line in lines:
            m = self._COPY_RE.match(line.strip())
            if m:
                table_name = (m.group(2) or m.group(3)).lower()
                tables.setdefault(table_name, [])
        # Fill rows via streaming
        for table_name, row in self.iter_rows(content):
            tables.setdefault(table_name, []).append(row)
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


def iter_sql_dump(content: str) -> Iterator[tuple[str, dict[str, Any]]]:
    """Auto-detect format and stream-parse, yielding ``(table, row)`` pairs.

    Same auto-detection as :func:`parse_sql_dump` but avoids materializing
    all rows into a single dict, enabling constant-memory streaming for
    multi-GB dumps.
    """
    fmt = detect_sql_format(content)
    if fmt == "postgresql_copy":
        yield from PostgreSQLCopyParser().iter_rows(content)
    else:
        yield from SQLInsertParser().iter_rows(content)


class _PrefixedStream:
    """A TextIO-like wrapper that reads *prefix* text first, then *stream*.

    Supports both ``.read(n)`` (for chunk-based MySQL parsing) and
    ``__iter__`` / ``__next__`` line iteration (for PostgreSQL COPY parsing).

    This is a proper iterator (``__iter__`` returns ``self``), so nested
    ``for`` loops over the same object share a single read position.
    """

    def __init__(self, prefix: str, stream: ReadableTextStream) -> None:
        self._buf = io.StringIO(prefix)
        self._stream = stream
        self._switched = False

    def read(self, size: int = -1) -> str:
        if self._switched:
            return self._stream.read(size)
        data = self._buf.read(size)
        if size < 0:
            self._switched = True
            return data + self._stream.read(-1)
        if len(data) < size:
            self._switched = True
            return data + self._stream.read(size - len(data))
        return data

    def readline(self) -> str:
        if not self._switched:
            line = self._buf.readline()
            if line:
                # If the prefix was cut mid-line (no trailing newline),
                # join with the continuation from the underlying stream
                # so callers never see a split line.
                if not line.endswith("\n"):
                    self._switched = True
                    return line + self._stream.readline()
                return line
            self._switched = True
        return self._stream.readline()

    def __iter__(self) -> "_PrefixedStream":
        return self

    def __next__(self) -> str:
        line = self.readline()
        if not line:
            raise StopIteration
        return line


_FORMAT_PEEK_CHUNK = 64 * 1024  # read in 64 KB increments
_FORMAT_PEEK_MAX = 10 * 1024 * 1024  # stop peeking after 10 MB
_COPY_DETECT_RE = re.compile(
    r"^COPY\s+\S.*\s+FROM\s+stdin\s*;", re.IGNORECASE | re.MULTILINE
)
_INSERT_DETECT_RE = re.compile(r"^INSERT\s+INTO\s+", re.IGNORECASE | re.MULTILINE)


def detect_sql_format_from_stream(
    stream: ReadableTextStream,
) -> tuple[str, _PrefixedStream]:
    """Detect MySQL vs PostgreSQL format by peeking at the stream.

    Reads 64 KB chunks until a decisive ``COPY ... FROM stdin`` or
    ``INSERT INTO`` pattern is found, up to a 10 MB cap.  Never reads a
    full line (which for ``mysqldump --extended-insert`` could be
    multi-GB).  Falls back to ``mysql_insert`` at EOF or if the cap is
    reached without a match.

    Returns:
        ``(format_string, combined_stream)`` where *format_string* is
        ``'postgresql_copy'`` or ``'mysql_insert'``.
    """
    peeked = ""
    fmt = "mysql_insert"
    while len(peeked) < _FORMAT_PEEK_MAX:
        chunk = stream.read(_FORMAT_PEEK_CHUNK)
        if not chunk:
            break
        peeked += chunk
        if _COPY_DETECT_RE.search(peeked):
            fmt = "postgresql_copy"
            break
        if _INSERT_DETECT_RE.search(peeked):
            fmt = "mysql_insert"
            break

    return fmt, _PrefixedStream(peeked, stream)


def iter_sql_dump_from_stream(
    stream: ReadableTextStream,
) -> Iterator[tuple[str, dict[str, Any]]]:
    """Auto-detect format and stream-parse from a text stream.

    Unlike :func:`iter_sql_dump` which requires the full dump as a string,
    this reads from an arbitrary ``TextIO`` (e.g. a file opened inside a
    zip archive) and never loads the entire content into memory.
    """
    fmt, combined = detect_sql_format_from_stream(stream)
    if fmt == "postgresql_copy":
        yield from PostgreSQLCopyParser().iter_rows_from_stream(combined)
    else:
        yield from SQLInsertParser().iter_rows_from_stream(combined)

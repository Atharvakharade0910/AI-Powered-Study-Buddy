"""Small database compatibility layer for SQLite development and PostgreSQL production."""

from __future__ import annotations

import re
from typing import Any, Iterable


class CompatRow(dict):
    """Mapping row that also supports SQLite-style numeric indexing."""

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, int):
            return tuple(self.values())[key]
        return super().__getitem__(key)


def _translate_placeholders(statement: str) -> str:
    """Convert SQLite placeholders without altering SQL literals or comments."""
    translated: list[str] = []
    index = 0
    quote: str | None = None
    dollar_quote: str | None = None
    line_comment = False
    block_comment = False
    while index < len(statement):
        character = statement[index]
        next_character = statement[index + 1] if index + 1 < len(statement) else ""
        if dollar_quote:
            if statement.startswith(dollar_quote, index):
                translated.append(dollar_quote)
                index += len(dollar_quote)
                dollar_quote = None
                continue
            translated.append(character)
        elif line_comment:
            translated.append(character)
            if character in {"\n", "\r"}:
                line_comment = False
        elif block_comment:
            translated.append(character)
            if character == "*" and next_character == "/":
                translated.append(next_character)
                index += 1
                block_comment = False
        elif quote:
            translated.append(character)
            if character == quote:
                if next_character == quote:
                    translated.append(next_character)
                    index += 1
                else:
                    quote = None
        elif character in {"'", '"'}:
            quote = character
            translated.append(character)
        elif character == "$":
            delimiter_match = re.match(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$", statement[index:])
            if delimiter_match:
                dollar_quote = delimiter_match.group(0)
                translated.append(dollar_quote)
                index += len(dollar_quote)
                continue
            translated.append(character)
        elif character == "-" and next_character == "-":
            translated.extend((character, next_character))
            index += 1
            line_comment = True
        elif character == "/" and next_character == "*":
            translated.extend((character, next_character))
            index += 1
            block_comment = True
        elif character == "?":
            translated.append("%s")
        else:
            translated.append(character)
        index += 1
    return "".join(translated)


def _split_sql_statements(script: str) -> list[str]:
    """Split a SQL script on delimiters outside quoted, commented, or dollar-quoted content."""
    statements: list[str] = []
    statement: list[str] = []
    index = 0
    quote: str | None = None
    dollar_quote: str | None = None
    line_comment = False
    block_comment = False
    while index < len(script):
        character = script[index]
        next_character = script[index + 1] if index + 1 < len(script) else ""
        statement.append(character)
        if dollar_quote:
            if script.startswith(dollar_quote, index):
                statement.extend(dollar_quote[1:])
                index += len(dollar_quote) - 1
                dollar_quote = None
        elif line_comment:
            if character in {"\n", "\r"}:
                line_comment = False
        elif block_comment:
            if character == "*" and next_character == "/":
                statement.append(next_character)
                index += 1
                block_comment = False
        elif quote:
            if character == quote:
                if next_character == quote:
                    statement.append(next_character)
                    index += 1
                else:
                    quote = None
        elif character in {"'", '"'}:
            quote = character
        elif character == "$":
            delimiter_match = re.match(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$", script[index:])
            if delimiter_match:
                dollar_quote = delimiter_match.group(0)
                statement.extend(dollar_quote[1:])
                index += len(dollar_quote) - 1
        elif character == "-" and next_character == "-":
            statement.append(next_character)
            index += 1
            line_comment = True
        elif character == "/" and next_character == "*":
            statement.append(next_character)
            index += 1
            block_comment = True
        elif character == ";":
            completed_statement = "".join(statement[:-1]).strip()
            if completed_statement:
                statements.append(completed_statement)
            statement = []
        index += 1
    final_statement = "".join(statement).strip()
    if final_statement:
        statements.append(final_statement)
    return statements


def _translate_sql(sql: str) -> str:
    statement = sql.strip()
    pragma_match = re.fullmatch(r"PRAGMA\s+table_info\((\w+)\)", statement, flags=re.IGNORECASE)
    if pragma_match:
        table = pragma_match.group(1)
        return (
            "SELECT ordinal_position AS cid, column_name AS name, data_type AS type, "
            "0 AS notnull, column_default AS dflt_value, 0 AS pk "
            "FROM information_schema.columns WHERE table_schema = current_schema() "
            f"AND table_name = '{table}' ORDER BY ordinal_position"
        )
    if statement.upper().startswith("PRAGMA "):
        return "SELECT 1"
    statement = re.sub(r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b", "BIGSERIAL PRIMARY KEY", statement, flags=re.IGNORECASE)
    statement = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", statement, flags=re.IGNORECASE)
    if statement.upper().startswith("INSERT INTO") and "ON CONFLICT" not in statement.upper():
        statement += " ON CONFLICT DO NOTHING"
    statement = re.sub(r"json_array_length\(questions_json\)", "jsonb_array_length(questions_json::jsonb)", statement, flags=re.IGNORECASE)
    return _translate_placeholders(statement)


class PostgresCursor:
    def __init__(self, connection: "PostgresConnection", cursor: Any, insert_statement: bool = False):
        self.connection = connection
        self.cursor = cursor
        self.insert_statement = insert_statement

    @property
    def rowcount(self) -> int:
        return self.cursor.rowcount

    @property
    def lastrowid(self) -> int | None:
        if not self.insert_statement:
            return None
        row = self.connection.execute("SELECT lastval() AS id").fetchone()
        return row["id"] if row else None

    def fetchone(self) -> CompatRow | None:
        row = self.cursor.fetchone()
        return self.connection.wrap_row(row, self.cursor.description) if row else None

    def fetchall(self) -> list[CompatRow]:
        return [self.connection.wrap_row(row, self.cursor.description) for row in self.cursor.fetchall()]


class PostgresConnection:
    def __init__(self, url: str):
        try:
            import psycopg
        except ImportError as error:  # pragma: no cover - exercised only in a misconfigured production image
            raise RuntimeError("psycopg is required when DATABASE_URL uses PostgreSQL") from error
        self.connection = psycopg.connect(url)

    @staticmethod
    def wrap_row(row: Any, description: Any) -> CompatRow:
        if isinstance(row, dict):
            return CompatRow(row)
        return CompatRow({column.name: value for column, value in zip(description, row)})

    def execute(self, sql: str, params: Iterable[Any] | None = None) -> PostgresCursor:
        translated = _translate_sql(sql)
        cursor = self.connection.cursor()
        cursor.execute(translated, tuple(params) if params is not None else None)
        return PostgresCursor(self, cursor, translated.upper().startswith("INSERT INTO"))

    def executemany(self, sql: str, params: Iterable[Iterable[Any]]) -> PostgresCursor:
        translated = _translate_sql(sql)
        cursor = self.connection.cursor()
        cursor.executemany(translated, [tuple(row) for row in params])
        return PostgresCursor(self, cursor, translated.upper().startswith("INSERT INTO"))

    def executescript(self, script: str) -> None:
        for statement in _split_sql_statements(script):
            self.execute(statement)

    def __enter__(self) -> "PostgresConnection":
        return self

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        try:
            if exc_type:
                self.connection.rollback()
            else:
                self.connection.commit()
        finally:
            self.connection.close()

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
    return statement.replace("?", "%s")


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
        for statement in script.split(";"):
            if statement.strip():
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

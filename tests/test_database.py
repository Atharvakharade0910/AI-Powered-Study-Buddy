"""Regression checks for the SQLite-to-PostgreSQL compatibility layer."""

from database import _translate_sql


def test_postgres_translation_preserves_insert_conflict_semantics() -> None:
    statement = _translate_sql(
        "INSERT OR IGNORE INTO conversation_documents (conversation_id, document_id) VALUES (?, ?)"
    )

    assert statement == (
        "INSERT INTO conversation_documents (conversation_id, document_id) VALUES (%s, %s) "
        "ON CONFLICT DO NOTHING"
    )


def test_postgres_translation_preserves_explicit_conflict_update() -> None:
    statement = _translate_sql(
        "INSERT INTO learner_memories (user_id, memory_key, memory_value) VALUES (?, ?, ?) "
        "ON CONFLICT (user_id, memory_key) DO UPDATE SET memory_value = excluded.memory_value"
    )

    assert statement == (
        "INSERT INTO learner_memories (user_id, memory_key, memory_value) VALUES (%s, %s, %s) "
        "ON CONFLICT (user_id, memory_key) DO UPDATE SET memory_value = excluded.memory_value"
    )


def test_postgres_translation_converts_sqlite_specific_schema_expressions() -> None:
    statement = _translate_sql(
        "CREATE TABLE reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, count INTEGER "
        "CHECK(json_array_length(questions_json) > 0))"
    )

    assert "BIGSERIAL PRIMARY KEY" in statement
    assert "jsonb_array_length(questions_json::jsonb)" in statement


def test_postgres_translation_uses_information_schema_for_table_info() -> None:
    statement = _translate_sql("PRAGMA table_info(users)")

    assert "information_schema.columns" in statement
    assert "table_name = 'users'" in statement

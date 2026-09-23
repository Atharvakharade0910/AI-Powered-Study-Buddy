"""Regression checks for the SQLite-to-PostgreSQL compatibility layer."""

from database import _split_sql_statements, _translate_sql


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


def test_postgres_translation_keeps_question_marks_in_quoted_sql_content() -> None:
    statement = _translate_sql('SELECT \'why?\' AS prompt, "?" AS label WHERE id = ?')

    assert statement == 'SELECT \'why?\' AS prompt, "?" AS label WHERE id = %s'


def test_script_splitter_preserves_semicolons_in_quoted_sql_content() -> None:
    statements = _split_sql_statements(
        "INSERT INTO learner_memories (memory_value) VALUES ('remember; this'); "
        'INSERT INTO labels (name) VALUES ("semi;colon");'
    )

    assert statements == [
        "INSERT INTO learner_memories (memory_value) VALUES ('remember; this')",
        'INSERT INTO labels (name) VALUES ("semi;colon")',
    ]


def test_script_splitter_preserves_semicolons_in_sql_comments() -> None:
    statements = _split_sql_statements(
        "-- migration note; still the same statement\n"
        "CREATE TABLE labels (id INTEGER); "
        "/* a block comment; with a delimiter */ "
        "INSERT INTO labels (id) VALUES (1);"
    )

    assert statements == [
        "-- migration note; still the same statement\nCREATE TABLE labels (id INTEGER)",
        "/* a block comment; with a delimiter */ INSERT INTO labels (id) VALUES (1)",
    ]

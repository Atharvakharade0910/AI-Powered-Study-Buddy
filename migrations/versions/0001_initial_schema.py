"""Create the PostgreSQL schema used by Study Buddy."""

from alembic import op

revision = "0001_initial_schema"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE IF NOT EXISTS users (id BIGSERIAL PRIMARY KEY, identifier TEXT NOT NULL UNIQUE, identifier_type TEXT NOT NULL CHECK(identifier_type IN ('email','phone')), password_hash TEXT NOT NULL, full_name TEXT, age INTEGER, standard TEXT, board TEXT, age_range TEXT, phone TEXT, phone_verified INTEGER NOT NULL DEFAULT 0, is_admin INTEGER NOT NULL DEFAULT 0, onboarding_completed INTEGER NOT NULL DEFAULT 0, learning_profile_completed INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS registration_challenges (id BIGSERIAL PRIMARY KEY, token TEXT NOT NULL UNIQUE, phone TEXT NOT NULL, code_hash TEXT NOT NULL, payload_json TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, resend_count INTEGER NOT NULL DEFAULT 0, last_sent_at TEXT, dev_code TEXT, expires_at TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS sessions (id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, token_hash TEXT NOT NULL UNIQUE, expires_at TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS study_items (id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, title TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'note', created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS conversations (id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, title TEXT NOT NULL, mode TEXT NOT NULL CHECK(mode IN ('general','document','voice')), created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS chat_messages (id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, conversation_id BIGINT REFERENCES conversations(id) ON DELETE CASCADE, role TEXT NOT NULL CHECK(role IN ('user','assistant')), message TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS voice_messages (id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, role TEXT NOT NULL CHECK(role IN ('user','assistant')), message TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS documents (id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, filename TEXT NOT NULL, content TEXT NOT NULL, storage_key TEXT, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS quizzes (id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, title TEXT NOT NULL, topic TEXT NOT NULL DEFAULT 'Document study', document_id BIGINT REFERENCES documents(id) ON DELETE SET NULL, questions_json TEXT NOT NULL, score INTEGER, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS review_cards (id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, prompt TEXT NOT NULL, answer TEXT NOT NULL, due_at TEXT NOT NULL, interval_days INTEGER NOT NULL DEFAULT 1, repetitions INTEGER NOT NULL DEFAULT 0, completed_at TEXT, topic TEXT NOT NULL DEFAULT 'Document study', created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS conversation_documents (conversation_id BIGINT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE, document_id BIGINT NOT NULL REFERENCES documents(id) ON DELETE CASCADE, created_at TEXT NOT NULL, PRIMARY KEY (conversation_id, document_id));
    CREATE TABLE IF NOT EXISTS quiz_attempts (id BIGSERIAL PRIMARY KEY, quiz_id BIGINT NOT NULL REFERENCES quizzes(id) ON DELETE CASCADE, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, answer_hash TEXT NOT NULL, score INTEGER NOT NULL, result_json TEXT NOT NULL, review_cards_created INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, UNIQUE (quiz_id, answer_hash));
    CREATE TABLE IF NOT EXISTS learning_events (id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE, event_type TEXT NOT NULL, topic TEXT NOT NULL, score DOUBLE PRECISION, metadata_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_conversations_user_updated ON conversations(user_id, updated_at DESC);
    CREATE INDEX IF NOT EXISTS idx_chat_messages_conversation ON chat_messages(conversation_id, id);
    CREATE INDEX IF NOT EXISTS idx_learning_events_user_topic ON learning_events(user_id, topic, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_conversation_documents_document ON conversation_documents(document_id);
    """)


def downgrade() -> None:
    for table in ("learning_events", "quiz_attempts", "conversation_documents", "review_cards", "quizzes", "documents", "voice_messages", "chat_messages", "conversations", "study_items", "sessions", "registration_challenges", "users"):
        op.execute(f"DROP TABLE IF EXISTS {table} CASCADE")

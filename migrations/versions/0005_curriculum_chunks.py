"""Store source-backed curriculum chunks for board-aware tutoring."""

from alembic import op

revision = "0005_curriculum_chunks"
down_revision = "0004_state_board"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """CREATE TABLE IF NOT EXISTS curriculum_chunks (
            id BIGSERIAL PRIMARY KEY,
            board TEXT NOT NULL,
            state TEXT,
            standard TEXT NOT NULL,
            subject TEXT NOT NULL,
            chapter TEXT,
            content TEXT NOT NULL,
            source_url TEXT NOT NULL,
            source_title TEXT,
            academic_year TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(board, state, standard, subject, chapter, source_url)
        )"""
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_curriculum_lookup ON curriculum_chunks(board, state, standard, subject)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS curriculum_chunks")

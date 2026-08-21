"""Add stream, topic, version, and review metadata to curriculum records."""

from alembic import op

revision = "0006_curriculum_review_fields"
down_revision = "0005_curriculum_chunks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS stream TEXT")
    op.execute("ALTER TABLE curriculum_chunks ADD COLUMN IF NOT EXISTS stream TEXT")
    op.execute("ALTER TABLE curriculum_chunks ADD COLUMN IF NOT EXISTS topic TEXT")
    op.execute("ALTER TABLE curriculum_chunks ADD COLUMN IF NOT EXISTS review_status TEXT NOT NULL DEFAULT 'pending'")
    op.execute("ALTER TABLE curriculum_chunks ADD COLUMN IF NOT EXISTS content_hash TEXT")
    op.execute("ALTER TABLE curriculum_chunks ADD COLUMN IF NOT EXISTS reviewed_at TEXT")
    op.execute("CREATE INDEX IF NOT EXISTS idx_curriculum_review ON curriculum_chunks(review_status, academic_year)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_curriculum_review")
    op.execute("ALTER TABLE curriculum_chunks DROP COLUMN IF EXISTS reviewed_at")
    op.execute("ALTER TABLE curriculum_chunks DROP COLUMN IF EXISTS content_hash")
    op.execute("ALTER TABLE curriculum_chunks DROP COLUMN IF EXISTS review_status")
    op.execute("ALTER TABLE curriculum_chunks DROP COLUMN IF EXISTS topic")
    op.execute("ALTER TABLE curriculum_chunks DROP COLUMN IF EXISTS stream")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS stream")

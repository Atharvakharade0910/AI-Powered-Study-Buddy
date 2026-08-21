"""Track whether a student has completed first-login learning setup."""

from alembic import op

revision = "0003_learning_profile_completion"
down_revision = "0002_learning_profile"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS learning_profile_completed INTEGER NOT NULL DEFAULT 0")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS learning_profile_completed")

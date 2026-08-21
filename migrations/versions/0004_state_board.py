"""Persist the state for State Board learners."""

from alembic import op

revision = "0004_state_board"
down_revision = "0003_learning_profile_completion"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS state TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS state")

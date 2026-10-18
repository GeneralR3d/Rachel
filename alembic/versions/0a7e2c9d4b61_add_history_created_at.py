"""align history timestamps with the existing ORM

Revision ID: 0a7e2c9d4b61
Revises: f9c6a1d4b3e8
"""

from alembic import op

revision = "0a7e2c9d4b61"
down_revision = "f9c6a1d4b3e8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Some deployments already aligned this older ORM field manually. Keep
    # their timestamps; otherwise existing rows receive migration time.
    op.execute(
        "ALTER TABLE history ADD COLUMN IF NOT EXISTS created_at "
        "TIMESTAMPTZ NOT NULL DEFAULT now()"
    )


def downgrade() -> None:
    op.drop_column("history", "created_at")

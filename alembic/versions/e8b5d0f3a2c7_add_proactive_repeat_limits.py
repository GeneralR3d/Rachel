"""add proactive repeat limits

Revision ID: e8b5d0f3a2c7
Revises: d7a4c9e2f1b6
"""

from alembic import op
import sqlalchemy as sa

revision = "e8b5d0f3a2c7"
down_revision = "d7a4c9e2f1b6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("proactive_schedules", sa.Column("repeat_times", sa.BigInteger(), nullable=True))
    op.add_column("proactive_schedules", sa.Column("repeat_completed", sa.BigInteger(), nullable=False, server_default="0"))
    op.create_check_constraint("ck_proactive_repeat_times", "proactive_schedules", "repeat_times IS NULL OR repeat_times > 0")
    op.create_check_constraint("ck_proactive_repeat_completed", "proactive_schedules", "repeat_completed >= 0")


def downgrade() -> None:
    op.drop_constraint("ck_proactive_repeat_completed", "proactive_schedules", type_="check")
    op.drop_constraint("ck_proactive_repeat_times", "proactive_schedules", type_="check")
    op.drop_column("proactive_schedules", "repeat_completed")
    op.drop_column("proactive_schedules", "repeat_times")

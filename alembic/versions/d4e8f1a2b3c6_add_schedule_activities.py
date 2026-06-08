"""add_schedule_activities

Revision ID: d4e8f1a2b3c6
Revises: b2c3d4e5f6a7
Create Date: 2026-06-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4e8f1a2b3c6"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "schedule_activities",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("day_of_week", sa.Integer(), nullable=False),
        sa.Column("start_hour", sa.Integer(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("location", sa.Text(), nullable=False),
        sa.Column("duration_hours", sa.Integer(), nullable=False),
        sa.Column("companions", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("interesting_event", sa.Text(), nullable=False),
        sa.UniqueConstraint("day_of_week", "start_hour", name="uq_schedule_day_start"),
    )


def downgrade() -> None:
    op.drop_table("schedule_activities")

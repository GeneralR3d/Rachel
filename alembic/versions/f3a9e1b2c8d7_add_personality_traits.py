"""add_personality_traits

Revision ID: f3a9e1b2c8d7
Revises: c17982a24b3a
Create Date: 2026-06-05 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f3a9e1b2c8d7"
down_revision: Union[str, None] = "c17982a24b3a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "personality_traits",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("name", sa.Text(), nullable=False, unique=True),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("low_prompt", sa.Text(), nullable=False),
        sa.Column("medium_prompt", sa.Text(), nullable=False),
        sa.Column("high_prompt", sa.Text(), nullable=False),
        sa.Column("current_value", sa.Text(), nullable=False, server_default="medium"),
    )


def downgrade() -> None:
    op.drop_table("personality_traits")

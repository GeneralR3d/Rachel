"""add_user_facts_preferences

Revision ID: a7c1f2e9b3d4
Revises: e5f9a2b4c7d8
Create Date: 2026-06-10 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7c1f2e9b3d4"
down_revision: Union[str, None] = "e5f9a2b4c7d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "user_facts_preferences",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("facts", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("user_facts_preferences")

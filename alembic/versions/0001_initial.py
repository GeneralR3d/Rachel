"""initial schema: system_prompt, summary, history

Revision ID: 0001
Revises:
Create Date: 2026-06-03

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "system_prompt",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "summary",
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("chat_id"),
    )
    op.create_table(
        "history",
        sa.Column(
            "message_id",
            sa.BigInteger(),
            sa.Identity(always=False),
            nullable=False,
        ),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("sender", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("message_id"),
    )
    op.create_index("ix_history_chat_id", "history", ["chat_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_history_chat_id", table_name="history")
    op.drop_table("history")
    op.drop_table("summary")
    op.drop_table("system_prompt")

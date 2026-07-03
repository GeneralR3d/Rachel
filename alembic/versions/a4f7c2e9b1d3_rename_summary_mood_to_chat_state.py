"""rename summary_mood to chat_state and add last_processed_message_id

Renames the per-chat summary_mood table to chat_state (it now also holds the
memory-pipeline watermark) and adds the last_processed_message_id column. Also
gives summary a "" server default so a chat_state row can be inserted
watermark-first, before any summary exists.

Revision ID: a4f7c2e9b1d3
Revises: f1d2c3b4a5e6
Create Date: 2026-07-03 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a4f7c2e9b1d3"
down_revision: Union[str, None] = "f1d2c3b4a5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.rename_table("summary_mood", "chat_state")
    op.alter_column("chat_state", "summary", server_default="")
    op.add_column(
        "chat_state",
        sa.Column(
            "last_processed_message_id",
            sa.BigInteger(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("chat_state", "last_processed_message_id")
    op.alter_column("chat_state", "summary", server_default=None)
    op.rename_table("chat_state", "summary_mood")

"""add summarizer_system_prompt to system_prompt

Revision ID: a1b2c3d4e5f6
Revises: 8569e903f7f4
Create Date: 2026-06-07 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, None] = "8569e903f7f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "system_prompt",
        sa.Column("summarizer_system_prompt", sa.Text(), nullable=False, server_default=""),
    )


def downgrade() -> None:
    op.drop_column("system_prompt", "summarizer_system_prompt")

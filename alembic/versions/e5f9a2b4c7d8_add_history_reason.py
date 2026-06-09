"""add_history_reason

Revision ID: e5f9a2b4c7d8
Revises: d4e8f1a2b3c6
Create Date: 2026-06-09 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e5f9a2b4c7d8"
down_revision: Union[str, None] = "d4e8f1a2b3c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("history", sa.Column("reason", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("history", "reason")

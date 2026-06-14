"""rename summary table to summary_mood and add mood column

Revision ID: b8d3f6a1c2e9
Revises: a7c1f2e9b3d4
Create Date: 2026-06-14 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "b8d3f6a1c2e9"
down_revision: Union[str, None] = "a7c1f2e9b3d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.rename_table("summary", "summary_mood")
    op.add_column(
        "summary_mood",
        sa.Column("mood", sa.Text(), nullable=False, server_default="default"),
    )


def downgrade() -> None:
    op.drop_column("summary_mood", "mood")
    op.rename_table("summary_mood", "summary")

"""drop the unused system_prompts table

Revision ID: f9c6a1d4b3e8
Revises: e8b5d0f3a2c7

System prompts now come directly from app.prompts. Downgrade recreates the
previous schema, but cannot recover prompt values deleted by the upgrade.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f9c6a1d4b3e8"
down_revision: Union[str, None] = "e8b5d0f3a2c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_table("system_prompts")


def downgrade() -> None:
    op.create_table(
        "system_prompts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("responder_system_prompt", sa.Text(), nullable=False),
        sa.Column("summarizer_system_prompt", sa.Text(), nullable=False, server_default=""),
        sa.PrimaryKeyConstraint("id"),
    )

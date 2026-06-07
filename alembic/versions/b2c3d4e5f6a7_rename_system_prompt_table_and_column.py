"""rename system_prompt table/column to system_prompts/responder_system_prompt

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-07 00:00:00.000001

"""
from typing import Sequence, Union

from alembic import op


revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.rename_table("system_prompt", "system_prompts")
    op.alter_column("system_prompts", "system_prompt", new_column_name="responder_system_prompt")


def downgrade() -> None:
    op.alter_column("system_prompts", "responder_system_prompt", new_column_name="system_prompt")
    op.rename_table("system_prompts", "system_prompt")

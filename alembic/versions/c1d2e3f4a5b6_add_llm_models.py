"""add_llm_models

Revision ID: c1d2e3f4a5b6
Revises: a4f7c2e9b1d3
Create Date: 2026-07-26 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, None] = "a4f7c2e9b1d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "llm_models",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("model_string", sa.Text(), nullable=False),
        sa.UniqueConstraint("model_string", name="uq_llm_models_model_string"),
    )
    op.create_table(
        "active_models",
        sa.Column("role", sa.Text(), primary_key=True),
        sa.Column("model_string", sa.Text(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("active_models")
    op.drop_table("llm_models")

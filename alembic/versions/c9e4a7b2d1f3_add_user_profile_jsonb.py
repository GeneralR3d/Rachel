"""add_user_profile_jsonb

Adds a structured, fixed-slot ``profile`` JSONB column to
``user_facts_preferences``. The field set lives in code
(app.prompts.USER_PROFILE_FIELDS) — this is just a flexible bag of slots
maintained by the userfacts profile pipeline.

Revision ID: c9e4a7b2d1f3
Revises: b8d3f6a1c2e9
Create Date: 2026-06-16 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "c9e4a7b2d1f3"
down_revision: Union[str, None] = "b8d3f6a1c2e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "user_facts_preferences",
        sa.Column(
            "profile",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("user_facts_preferences", "profile")

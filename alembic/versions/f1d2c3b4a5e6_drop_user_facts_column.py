"""Drop user_facts_preferences.facts — free-form facts now live in Graphiti/Neo4j.

Existing facts data is intentionally discarded (per-user memory is rebuilt by
the Graphiti pipeline as conversations happen); the fixed-slot `profile` JSONB
column stays in Postgres.

Revision ID: f1d2c3b4a5e6
Revises: c9e4a7b2d1f3
Create Date: 2026-07-02
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1d2c3b4a5e6"
down_revision: Union[str, None] = "c9e4a7b2d1f3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_column("user_facts_preferences", "facts")


def downgrade() -> None:
    # Data is not recoverable — the column comes back empty.
    op.add_column(
        "user_facts_preferences",
        sa.Column("facts", sa.Text(), nullable=False, server_default=""),
    )

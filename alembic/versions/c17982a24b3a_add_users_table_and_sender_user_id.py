"""add_users_table_and_sender_user_id

Revision ID: c17982a24b3a
Revises: 39525caf8b08
Create Date: 2026-06-04 16:13:44.036158

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c17982a24b3a'
down_revision: Union[str, None] = '39525caf8b08'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("telegram_user_id", sa.BigInteger(), nullable=False),
        sa.Column("first_name", sa.Text(), nullable=True),
        sa.Column("last_name", sa.Text(), nullable=True),
        sa.Column("username", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("telegram_user_id"),
    )

    # Add sender_user_id with a default of 0 for any pre-existing rows.
    op.add_column(
        "history",
        sa.Column("sender_user_id", sa.BigInteger(), nullable=False, server_default="0"),
    )
    # Remove the server default now that existing rows are covered.
    op.alter_column("history", "sender_user_id", server_default=None)

    op.drop_column("history", "sender")


def downgrade() -> None:
    op.add_column(
        "history",
        sa.Column("sender", sa.Text(), nullable=False, server_default="unknown"),
    )
    op.alter_column("history", "sender", server_default=None)

    op.drop_column("history", "sender_user_id")

    op.drop_table("users")

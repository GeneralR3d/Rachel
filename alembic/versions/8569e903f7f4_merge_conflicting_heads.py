"""merge conflicting heads

Revision ID: 8569e903f7f4
Revises: 84176aa2e975, f3a9e1b2c8d7
Create Date: 2026-06-05 13:50:29.834900

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8569e903f7f4'
down_revision: Union[str, None] = ('84176aa2e975', 'f3a9e1b2c8d7')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass

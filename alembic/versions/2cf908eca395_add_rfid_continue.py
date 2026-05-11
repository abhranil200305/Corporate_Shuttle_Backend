"""Add RFID continue

Revision ID: 2cf908eca395
Revises: 337e5209ecbc
Create Date: 2026-05-11 12:27:06.421016

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2cf908eca395'
down_revision: Union[str, Sequence[str], None] = '337e5209ecbc'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass

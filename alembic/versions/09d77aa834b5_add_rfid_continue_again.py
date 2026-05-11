"""Add RFID continue again

Revision ID: 09d77aa834b5
Revises: 2cf908eca395
Create Date: 2026-05-11 12:30:35.798229

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '09d77aa834b5'
down_revision: Union[str, Sequence[str], None] = '2cf908eca395'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass

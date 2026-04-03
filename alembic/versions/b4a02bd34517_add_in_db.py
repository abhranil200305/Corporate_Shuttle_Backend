"""add cancellation reason

Revision ID: b4a02bd34517
Revises: 12b12d793771
Create Date: 2026-04-03 17:04:49.255500
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'b4a02bd34517'
down_revision: Union[str, Sequence[str], None] = '12b12d793771'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add the cancellation_reason column
    op.add_column('scheduled_trips', sa.Column('cancellation_reason', sa.Text(), nullable=True))
    # Do NOT alter the status column; keep it as VARCHAR


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('scheduled_trips', 'cancellation_reason')
    # Status column unchanged
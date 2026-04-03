"""add emergency part

Revision ID: 12b12d793771
Revises: 656ccc31775b
Create Date: 2026-04-03 16:50:19.934801
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '12b12d793771'
down_revision: Union[str, Sequence[str], None] = '656ccc31775b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Add a new column for premature end reason
    op.add_column('scheduled_trips', sa.Column('premature_end_reason', sa.Text(), nullable=True))
    # No enum changes, status column stays as VARCHAR


def downgrade() -> None:
    """Downgrade schema."""
    # Remove the premature end column
    op.drop_column('scheduled_trips', 'premature_end_reason')
    # Status column unchanged
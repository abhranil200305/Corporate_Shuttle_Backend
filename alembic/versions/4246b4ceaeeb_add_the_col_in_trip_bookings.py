"""add the col in trip_bookings

Revision ID: 4246b4ceaeeb
Revises: 9573a17c23c6
Create Date: 2026-04-10 15:39:08.654871
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '4246b4ceaeeb'
down_revision: Union[str, Sequence[str], None] = '9573a17c23c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    # ✅ ONLY THIS IS NEEDED
    op.add_column(
        'trip_bookings',
        sa.Column('otp', sa.String(length=10), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""

    # ✅ Reverse of above
    op.drop_column('trip_bookings', 'otp')
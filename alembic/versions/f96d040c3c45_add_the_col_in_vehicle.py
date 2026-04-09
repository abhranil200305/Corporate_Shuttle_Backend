"""add the col in vehicle

Revision ID: f96d040c3c45
Revises: 00f307950147
Create Date: 2026-04-09 17:03:48.876097

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f96d040c3c45'
down_revision: Union[str, Sequence[str], None] = '00f307950147'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    # ✅ Add inspection_created_at
    op.add_column(
        'vehicles',
        sa.Column('inspection_created_at', sa.DateTime(timezone=True), nullable=True)
    )

    # ✅ Create enum first (important for Postgres)
    inspection_status_enum = sa.Enum(
        'pending',
        'approved',
        'rejected',
        name='vehicle_inspection_status'
    )
    inspection_status_enum.create(op.get_bind(), checkfirst=True)

    # ✅ Add column using enum
    op.add_column(
        'vehicles',
        sa.Column('inspection_status', inspection_status_enum, nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""

    # ✅ Drop columns
    op.drop_column('vehicles', 'inspection_status')
    op.drop_column('vehicles', 'inspection_created_at')

    # ✅ Drop enum safely
    sa.Enum(name='vehicle_inspection_status').drop(op.get_bind(), checkfirst=True)
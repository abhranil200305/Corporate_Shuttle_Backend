"""add a tables col all

Revision ID: 702d47d801bd
Revises: 4dbeecae7583
Create Date: 2026-04-13 11:04:44.952010

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '702d47d801bd'
down_revision: Union[str, Sequence[str], None] = '4dbeecae7583'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    # -----------------------------
    # 1. Add new column
    # -----------------------------
    op.add_column(
        'scheduled_trips',
        sa.Column(
            'emergency_stop_request_status',
            sa.Enum(
                'approved',
                'rejected',
                'pending',
                name='emergency_stop_request_status',
                native_enum=False,
                create_constraint=True
            ),
            nullable=True
        )
    )

    # -----------------------------
    # 2. DROP existing constraint FIRST
    # -----------------------------
    op.execute(
        "ALTER TABLE scheduled_trips DROP CONSTRAINT IF EXISTS scheduled_trip_status"
    )

    # -----------------------------
    # 3. ALTER column with new enum
    # -----------------------------
    op.alter_column(
        'scheduled_trips',
        'status',
        existing_type=sa.VARCHAR(length=20),
        type_=sa.Enum(
            'scheduled',
            'in_progress',
            'completed',
            'cancelled',
            'premature_end',
            'premature_end_requested',
            name='scheduled_trip_status',
            native_enum=False,
            create_constraint=True
        ),
        existing_nullable=False
    )

    # -----------------------------
    # 4. Vehicles changes
    # -----------------------------
    op.add_column(
        'vehicles',
        sa.Column('inspection_reason', sa.Text(), nullable=True)
    )

    # DROP old enum constraint (VERY IMPORTANT)
    op.execute(
        "ALTER TABLE vehicles DROP CONSTRAINT IF EXISTS vehicle_inspection_status"
    )

    op.alter_column(
        'vehicles',
        'inspection_status',
        existing_type=postgresql.ENUM(
            'pending',
            'approved',
            'rejected',
            name='vehicle_inspection_status'
        ),
        type_=sa.Enum(
            'pending',
            'approved',
            'rejected',
            name='vehicle_inspection_status',
            native_enum=False,
            create_constraint=True
        ),
        existing_nullable=True
    )


def downgrade() -> None:
    """Downgrade schema."""

    # -----------------------------
    # Vehicles revert
    # -----------------------------
    op.execute(
        "ALTER TABLE vehicles DROP CONSTRAINT IF EXISTS vehicle_inspection_status"
    )

    op.alter_column(
        'vehicles',
        'inspection_status',
        existing_type=sa.Enum(
            'pending',
            'approved',
            'rejected',
            name='vehicle_inspection_status',
            native_enum=False,
            create_constraint=True
        ),
        type_=postgresql.ENUM(
            'pending',
            'approved',
            'rejected',
            name='vehicle_inspection_status'
        ),
        existing_nullable=True
    )

    op.drop_column('vehicles', 'inspection_reason')

    # -----------------------------
    # Scheduled trips revert
    # -----------------------------
    op.execute(
        "ALTER TABLE scheduled_trips DROP CONSTRAINT IF EXISTS scheduled_trip_status"
    )

    op.alter_column(
        'scheduled_trips',
        'status',
        existing_type=sa.Enum(
            'scheduled',
            'in_progress',
            'completed',
            'cancelled',
            'premature_end',
            'premature_end_requested',
            name='scheduled_trip_status',
            native_enum=False,
            create_constraint=True
        ),
        type_=sa.VARCHAR(length=20),
        existing_nullable=False
    )

    op.drop_column('scheduled_trips', 'emergency_stop_request_status')
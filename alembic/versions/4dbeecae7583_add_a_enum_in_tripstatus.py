"""add a enum in tripstatus

Revision ID: 4dbeecae7583
Revises: 4246b4ceaeeb
Create Date: 2026-04-13

"""

from typing import Sequence, Union
from alembic import op

# revision identifiers
revision: str = '4dbeecae7583'
down_revision: Union[str, Sequence[str], None] = '4246b4ceaeeb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 🔥 STEP 1: Drop old constraint
    op.execute("""
        ALTER TABLE scheduled_trips
        DROP CONSTRAINT IF EXISTS scheduled_trip_status
    """)

    # 🔥 STEP 2: Add new constraint with 'per'
    op.execute("""
        ALTER TABLE scheduled_trips
        ADD CONSTRAINT scheduled_trip_status
        CHECK (status IN (
            'scheduled',
            'in_progress',
            'completed',
            'cancelled',
            'premature_end',
            'per'
        ))
    """)


def downgrade() -> None:
    # 🔥 rollback to old enum

    op.execute("""
        ALTER TABLE scheduled_trips
        DROP CONSTRAINT IF EXISTS scheduled_trip_status
    """)

    op.execute("""
        ALTER TABLE scheduled_trips
        ADD CONSTRAINT scheduled_trip_status
        CHECK (status IN (
            'scheduled',
            'in_progress',
            'completed',
            'cancelled',
            'premature_end'
        ))
    """)
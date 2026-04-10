"""add the col in driver_profile

Revision ID: 9573a17c23c6
Revises: 22147adad968
Create Date: 2026-04-10
"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

# revision identifiers
revision: str = '9573a17c23c6'
down_revision: Union[str, Sequence[str], None] = '22147adad968'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    # ✅ Add column to driver_profiles
    op.add_column(
        'driver_profiles',
        sa.Column('duration_payable_days', sa.Integer(), nullable=True)
    )

    # ✅ Optional constraint (recommended)
    op.create_check_constraint(
        "ck_driver_profiles_duration_positive",
        "driver_profiles",
        "duration_payable_days IS NULL OR duration_payable_days > 0"
    )

    # ✅ Remove from vehicles (since you moved it)
    op.drop_column('vehicles', 'duration_payable_days')


def downgrade() -> None:
    """Downgrade schema."""

    # ✅ Add back to vehicles
    op.add_column(
        'vehicles',
        sa.Column('duration_payable_days', sa.Integer(), nullable=True)
    )

    # remove constraint from driver_profiles
    op.drop_constraint(
        "ck_driver_profiles_duration_positive",
        "driver_profiles",
        type_="check"
    )

    # remove column from driver_profiles
    op.drop_column('driver_profiles', 'duration_payable_days')
"""add the col in vehicle"""

from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = '22147adad968'
down_revision: Union[str, Sequence[str], None] = 'cf0fdc3746c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    # ✅ Step 1: Add column as nullable
    op.add_column(
        'vehicles',
        sa.Column('duration_payable_days', sa.Integer(), nullable=True)
    )

    # ✅ Step 2: Fill existing rows (set default value, e.g., 30 days)
    op.execute(
        "UPDATE vehicles SET duration_payable_days = 30"
    )

    # ✅ Step 3: Make column NOT NULL
    op.alter_column(
        'vehicles',
        'duration_payable_days',
        nullable=False
    )

    # ✅ Step 4: Add constraint
    op.create_check_constraint(
        "ck_vehicles_duration_positive",
        "vehicles",
        "duration_payable_days > 0"
    )


def downgrade() -> None:
    """Downgrade schema."""

    op.drop_constraint(
        "ck_vehicles_duration_positive",
        "vehicles",
        type_="check"
    )

    op.drop_column('vehicles', 'duration_payable_days')
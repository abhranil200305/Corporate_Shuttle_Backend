"""add the col in vehicle

Revision ID: cf0fdc3746c3
Revises: f96d040c3c45
Create Date: 2026-04-10 11:52:31.509316
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'cf0fdc3746c3'
down_revision: Union[str, Sequence[str], None] = 'f96d040c3c45'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    # ✅ Only add your new column (this is all you need)
    op.add_column(
        'vehicles',
        sa.Column('inspection_reviewed_at', sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""

    # ✅ Remove the column on downgrade
    op.drop_column('vehicles', 'inspection_reviewed_at')
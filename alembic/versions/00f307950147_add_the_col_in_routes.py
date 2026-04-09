"""add the col in routes

Revision ID: 00f307950147
Revises: d51d69524c5c
Create Date: 2026-04-09 15:55:45.682748
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '00f307950147'
down_revision: Union[str, Sequence[str], None] = 'd51d69524c5c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # ✅ Only add the new column
    op.add_column(
        'routes',
        sa.Column('has_ac', sa.Boolean(), nullable=True)
    )


def downgrade() -> None:
    """Downgrade schema."""
    # ✅ Only drop the column
    op.drop_column('routes', 'has_ac')
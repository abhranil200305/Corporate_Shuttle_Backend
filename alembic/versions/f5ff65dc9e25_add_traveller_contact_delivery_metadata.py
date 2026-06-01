"""add traveller contact delivery metadata

Revision ID: f5ff65dc9e25
Revises: b642f8d3ff00
Create Date: 2026-06-01 14:08:45.880978

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f5ff65dc9e25'
down_revision: Union[str, Sequence[str], None] = 'b642f8d3ff00'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "traveller_contact_notifications",
        sa.Column("delivered_channel", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "traveller_contact_notifications",
        sa.Column(
            "delivery_attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
    )
    op.add_column(
        "traveller_contact_notifications",
        sa.Column("delivery_retry_after", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_check_constraint(
        "ck_traveller_contact_notifications_attempt_nonnegative",
        "traveller_contact_notifications",
        "delivery_attempt_count >= 0",
    )

    op.create_index(
        "ix_traveller_contact_notifications_retry",
        "traveller_contact_notifications",
        ["status", "delivery_retry_after"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_traveller_contact_notifications_retry",
        table_name="traveller_contact_notifications",
    )

    op.drop_constraint(
        "ck_traveller_contact_notifications_attempt_nonnegative",
        "traveller_contact_notifications",
        type_="check",
    )

    op.drop_column("traveller_contact_notifications", "delivery_retry_after")
    op.drop_column("traveller_contact_notifications", "delivery_attempt_count")
    op.drop_column("traveller_contact_notifications", "delivered_channel")
 
"""enable multi seat booking sessions

Revision ID: 6ef107bdc21a
Revises: 8b8743a25900
Create Date: 2026-06-01 11:00:28.096806

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6ef107bdc21a'
down_revision: Union[str, Sequence[str], None] = '8b8743a25900'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index(
        "uq_trip_bookings_passenger_trip_active",
        table_name="trip_bookings",
    )
 
 
def downgrade() -> None:
    op.create_index(
        "uq_trip_bookings_passenger_trip_active",
        "trip_bookings",
        ["passenger_user_id", "scheduled_trip_id"],
        unique=True,
        postgresql_where=sa.text(
            "booking_status IN ('pending_payment', 'booked', 'boarded')"
        ),
    )
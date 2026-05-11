"""Add RFID reserved seat check constraints

Revision ID: 337e5209ecbc
Revises: 190644f9b03a
Create Date: 2026-05-11 11:45:35.764326

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '337e5209ecbc'
down_revision: Union[str, Sequence[str], None] = '190644f9b03a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_check_constraint(
        "ck_vehicles_default_rfid_reserved_nonnegative",
        "vehicles",
        "default_rfid_reserved_seat_count >= 0",
    )

    op.create_check_constraint(
        "ck_vehicles_default_rfid_reserved_not_above_seat_count",
        "vehicles",
        "default_rfid_reserved_seat_count <= seat_count",
    )

    op.create_check_constraint(
        "ck_scheduled_trips_rfid_reserved_nonnegative",
        "scheduled_trips",
        "rfid_reserved_seat_count >= 0",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_scheduled_trips_rfid_reserved_nonnegative",
        "scheduled_trips",
        type_="check",
    )

    op.drop_constraint(
        "ck_vehicles_default_rfid_reserved_not_above_seat_count",
        "vehicles",
        type_="check",
    )

    op.drop_constraint(
        "ck_vehicles_default_rfid_reserved_nonnegative",
        "vehicles",
        type_="check",
    )
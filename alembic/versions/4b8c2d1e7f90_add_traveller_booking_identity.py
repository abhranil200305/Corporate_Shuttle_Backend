"""add traveller booking identity

Revision ID: 4b8c2d1e7f90
Revises: 956c140b3a8f
Create Date: 2026-07-06

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "4b8c2d1e7f90"
down_revision: str | Sequence[str] | None = "956c140b3a8f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "trip_bookings",
        sa.Column(
            "traveller_identity_key",
            sa.String(length=255),
            nullable=True,
        ),
    )

    # Legacy single-seat bookings were self-only. Booking-session rows retain
    # profile identity where possible; old ad-hoc guests receive a row-local
    # identity because their historical phone format cannot be normalized and
    # hashed safely inside a portable migration.
    op.execute(
        """
        UPDATE trip_bookings AS tb
        SET traveller_identity_key = CASE
            WHEN tb.traveller_profile_id IS NOT NULL
                 AND EXISTS (
                     SELECT 1
                     FROM passenger_traveller_profiles AS ptp
                     WHERE ptp.id = tb.traveller_profile_id
                       AND ptp.is_self = true
                 )
                THEN 'self:' || COALESCE(
                    tb.booked_by_user_id,
                    tb.passenger_user_id
                )
            WHEN tb.traveller_profile_id IS NOT NULL
                THEN 'profile:' || tb.traveller_profile_id
            WHEN LOWER(TRIM(COALESCE(
                tb.traveller_relationship_label_snapshot,
                ''
            ))) = 'self'
                THEN 'self:' || COALESCE(
                    tb.booked_by_user_id,
                    tb.passenger_user_id
                )
            WHEN tb.booking_session_id IS NULL
                THEN 'self:' || COALESCE(
                    tb.booked_by_user_id,
                    tb.passenger_user_id
                )
            ELSE 'legacy:' || tb.id
        END
        """
    )

    op.alter_column(
        "trip_bookings",
        "traveller_identity_key",
        existing_type=sa.String(length=255),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_trip_bookings_traveller_identity_nonempty",
        "trip_bookings",
        "traveller_identity_key <> ''",
    )
    op.create_index(
        "ix_trip_bookings_traveller_identity_status",
        "trip_bookings",
        ["traveller_identity_key", "booking_status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_trip_bookings_traveller_identity_status",
        table_name="trip_bookings",
    )
    op.drop_constraint(
        "ck_trip_bookings_traveller_identity_nonempty",
        "trip_bookings",
        type_="check",
    )
    op.drop_column("trip_bookings", "traveller_identity_key")

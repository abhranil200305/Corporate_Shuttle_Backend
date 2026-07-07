"""add booking session shared otp

Revision ID: 8f6a1c2d9b30
Revises: 4b8c2d1e7f90
Create Date: 2026-07-07

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "8f6a1c2d9b30"
down_revision: str | Sequence[str] | None = "4b8c2d1e7f90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "booking_sessions",
        sa.Column("otp", sa.String(length=10), nullable=True),
    )

    # Existing booking-session seats previously had independent booking-level
    # OTPs. Promote the first child booking OTP to the session, then mirror
    # that credential back to every child row so older API consumers that still
    # read trip_bookings.otp see the shared session credential.
    op.execute(
        """
        UPDATE booking_sessions AS bs
        SET otp = (
            SELECT tb.otp
            FROM trip_bookings AS tb
            WHERE tb.booking_session_id = bs.id
              AND tb.otp IS NOT NULL
            ORDER BY tb.created_at ASC, tb.id ASC
            LIMIT 1
        )
        WHERE bs.otp IS NULL
        """
    )

    op.execute(
        """
        UPDATE trip_bookings AS tb
        SET otp = bs.otp
        FROM booking_sessions AS bs
        WHERE tb.booking_session_id = bs.id
          AND bs.otp IS NOT NULL
        """
    )

    op.create_index(
        "ix_booking_sessions_trip_otp_active",
        "booking_sessions",
        ["scheduled_trip_id", "otp"],
        unique=True,
        postgresql_where=sa.text(
            "otp IS NOT NULL AND status IN ('pending_payment', 'confirmed')"
        ),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_booking_sessions_trip_otp_active",
        table_name="booking_sessions",
    )
    op.drop_column("booking_sessions", "otp")

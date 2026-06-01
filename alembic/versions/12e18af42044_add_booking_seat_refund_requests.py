"""add booking seat refund requests

Revision ID: 12e18af42044
Revises: f5ff65dc9e25
Create Date: 2026-06-01 14:10:11.706352

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "12e18af42044"
down_revision: Union[str, Sequence[str], None] = "f5ff65dc9e25"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


BOOKING_SEAT_REFUND_REQUEST_STATUS = postgresql.ENUM(
    "pending",
    "processing",
    "succeeded",
    "failed",
    "skipped",
    name="booking_seat_refund_request_status",
    create_type=False,
)


def upgrade() -> None:
    # Create enum only if it doesn't already exist
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_type
                WHERE typname = 'booking_seat_refund_request_status'
            ) THEN
                CREATE TYPE booking_seat_refund_request_status AS ENUM (
                    'pending',
                    'processing',
                    'succeeded',
                    'failed',
                    'skipped'
                );
            END IF;
        END
        $$;
        """
    )

    op.create_table(
        "booking_seat_refund_requests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("booking_session_id", sa.String(length=36), nullable=False),
        sa.Column("booking_id", sa.String(length=36), nullable=False),
        sa.Column(
            "booking_session_payment_id",
            sa.String(length=36),
            nullable=False,
        ),
        sa.Column("owner_user_id", sa.String(length=36), nullable=False),
        sa.Column("amount", sa.Numeric(10, 2), nullable=False),

        sa.Column(
            "status",
            BOOKING_SEAT_REFUND_REQUEST_STATUS,
            nullable=False,
            server_default=sa.text("'pending'"),
        ),

        sa.Column("razorpay_refund_id", sa.String(length=64), nullable=True),
        sa.Column("provider_response_json", sa.Text(), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),

        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),

        sa.Column(
            "retry_after",
            sa.DateTime(timezone=True),
            nullable=True,
        ),

        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),

        sa.Column(
            "processed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),

        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),

        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),

        sa.CheckConstraint(
            "amount > 0",
            name="ck_booking_seat_refund_requests_amount_positive",
        ),

        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_booking_seat_refund_requests_attempt_nonnegative",
        ),

        sa.ForeignKeyConstraint(
            ["booking_session_id"],
            ["booking_sessions.id"],
            ondelete="RESTRICT",
        ),

        sa.ForeignKeyConstraint(
            ["booking_id"],
            ["trip_bookings.id"],
            ondelete="RESTRICT",
        ),

        sa.ForeignKeyConstraint(
            ["booking_session_payment_id"],
            ["booking_session_payments.id"],
            ondelete="RESTRICT",
        ),

        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
        ),

        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(
        "ix_booking_seat_refund_requests_status_retry",
        "booking_seat_refund_requests",
        ["status", "retry_after"],
        unique=False,
    )

    op.create_index(
        "ix_booking_seat_refund_requests_booking",
        "booking_seat_refund_requests",
        ["booking_id"],
        unique=False,
    )

    op.create_index(
        "ix_booking_seat_refund_requests_session",
        "booking_seat_refund_requests",
        ["booking_session_id"],
        unique=False,
    )

    op.create_index(
        "ix_booking_seat_refund_requests_payment",
        "booking_seat_refund_requests",
        ["booking_session_payment_id"],
        unique=False,
    )

    op.create_index(
        "ix_booking_seat_refund_requests_owner",
        "booking_seat_refund_requests",
        ["owner_user_id"],
        unique=False,
    )

    op.create_index(
        "ix_booking_seat_refund_requests_razorpay_refund",
        "booking_seat_refund_requests",
        ["razorpay_refund_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_booking_seat_refund_requests_razorpay_refund",
        table_name="booking_seat_refund_requests",
    )

    op.drop_index(
        "ix_booking_seat_refund_requests_owner",
        table_name="booking_seat_refund_requests",
    )

    op.drop_index(
        "ix_booking_seat_refund_requests_payment",
        table_name="booking_seat_refund_requests",
    )

    op.drop_index(
        "ix_booking_seat_refund_requests_session",
        table_name="booking_seat_refund_requests",
    )

    op.drop_index(
        "ix_booking_seat_refund_requests_booking",
        table_name="booking_seat_refund_requests",
    )

    op.drop_index(
        "ix_booking_seat_refund_requests_status_retry",
        table_name="booking_seat_refund_requests",
    )

    op.drop_table("booking_seat_refund_requests")

    op.execute(
        """
        DROP TYPE IF EXISTS booking_seat_refund_request_status;
        """
    )
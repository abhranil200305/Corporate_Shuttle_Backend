"""add cancellation metadata

Revision ID: f2a4c6d8e901
Revises: 8d7f4c2a9b31
Create Date: 2026-07-14 00:00:00.000000
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "f2a4c6d8e901"
down_revision: str | Sequence[str] | None = "8d7f4c2a9b31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _add_actor_columns(table_name: str) -> None:
    op.add_column(
        table_name,
        sa.Column("cancellation_source", sa.String(length=32), nullable=True),
    )
    op.add_column(
        table_name,
        sa.Column("cancelled_by_user_id", sa.String(length=36), nullable=True),
    )
    op.create_foreign_key(
        f"fk_{table_name}_cancelled_by_user_id_users",
        table_name,
        "users",
        ["cancelled_by_user_id"],
        ["id"],
        ondelete="SET NULL",
    )


def upgrade() -> None:
    op.add_column(
        "scheduled_trips",
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
    )
    _add_actor_columns("scheduled_trips")

    for table_name in ("booking_sessions", "trip_bookings"):
        op.add_column(
            table_name,
            sa.Column("cancellation_reason", sa.Text(), nullable=True),
        )
        _add_actor_columns(table_name)

    # Preserve useful legacy context without inventing an actor identity.
    op.execute(
        """
        UPDATE scheduled_trips
        SET
            cancelled_at = COALESCE(actual_end_at, updated_at),
            cancellation_source = 'legacy'
        WHERE status IN ('cancelled', 'premature_end')
        """
    )
    op.execute(
        """
        UPDATE trip_bookings AS tb
        SET
            cancelled_at = COALESCE(tb.cancelled_at, st.cancelled_at, tb.updated_at),
            cancellation_reason = COALESCE(
                st.cancellation_reason,
                st.premature_end_reason,
                'Cancellation reason was not recorded.'
            ),
            cancellation_source = 'legacy'
        FROM scheduled_trips AS st
        WHERE
            tb.scheduled_trip_id = st.id
            AND tb.booking_status = 'cancelled'
        """
    )
    op.execute(
        """
        UPDATE booking_sessions
        SET
            cancelled_at = COALESCE(cancelled_at, updated_at),
            cancellation_reason = 'Cancellation reason was not recorded.',
            cancellation_source = 'legacy'
        WHERE status = 'cancelled'
        """
    )


def _drop_actor_columns(table_name: str) -> None:
    op.drop_constraint(
        f"fk_{table_name}_cancelled_by_user_id_users",
        table_name,
        type_="foreignkey",
    )
    op.drop_column(table_name, "cancelled_by_user_id")
    op.drop_column(table_name, "cancellation_source")


def downgrade() -> None:
    for table_name in ("trip_bookings", "booking_sessions"):
        _drop_actor_columns(table_name)
        op.drop_column(table_name, "cancellation_reason")

    _drop_actor_columns("scheduled_trips")
    op.drop_column("scheduled_trips", "cancelled_at")

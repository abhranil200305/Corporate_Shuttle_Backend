"""deduplicate traveller contact notifications

Revision ID: c7e9a2b4d601
Revises: a1c3e5f7b902
Create Date: 2026-07-17 00:00:00.000000
"""

from typing import Sequence

from alembic import op


revision: str = "c7e9a2b4d601"
down_revision: str | Sequence[str] | None = "a1c3e5f7b902"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Keep the most useful row for historical duplicates: a sent delivery
    # wins, followed by a pending/retryable row, then the earliest record.
    op.execute(
        """
        WITH ranked AS (
            SELECT
                id,
                ROW_NUMBER() OVER (
                    PARTITION BY booking_id, event_type
                    ORDER BY
                        CASE status
                            WHEN 'sent' THEN 0
                            WHEN 'pending' THEN 1
                            WHEN 'failed' THEN 2
                            WHEN 'skipped' THEN 3
                            ELSE 4
                        END,
                        created_at ASC,
                        id ASC
                ) AS duplicate_rank
            FROM traveller_contact_notifications
        )
        DELETE FROM traveller_contact_notifications AS notification
        USING ranked
        WHERE
            notification.id = ranked.id
            AND ranked.duplicate_rank > 1
        """
    )
    op.create_unique_constraint(
        "uq_traveller_contact_notifications_booking_event",
        "traveller_contact_notifications",
        ["booking_id", "event_type"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_traveller_contact_notifications_booking_event",
        "traveller_contact_notifications",
        type_="unique",
    )

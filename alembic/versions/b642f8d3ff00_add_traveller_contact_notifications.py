"""add traveller contact notifications

Revision ID: b642f8d3ff00
Revises: 6ef107bdc21a
Create Date: 2026-06-01 12:36:08.590971
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = "b642f8d3ff00"
down_revision: Union[str, Sequence[str], None] = "6ef107bdc21a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


ENUM_NAME = "traveller_contact_notification_status"


def upgrade() -> None:
    # Create enum only if it does not exist
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_type
                WHERE typname = '{ENUM_NAME}'
            ) THEN
                CREATE TYPE {ENUM_NAME} AS ENUM (
                    'pending',
                    'sent',
                    'failed',
                    'skipped'
                );
            END IF;
        END
        $$;
        """
    )

    status_enum = postgresql.ENUM(
        "pending",
        "sent",
        "failed",
        "skipped",
        name=ENUM_NAME,
        create_type=False,
    )

    op.create_table(
        "traveller_contact_notifications",

        sa.Column("id", sa.String(36), nullable=False),

        sa.Column(
            "booking_session_id",
            sa.String(36),
            nullable=False,
        ),

        sa.Column(
            "booking_id",
            sa.String(36),
            nullable=False,
        ),

        sa.Column(
            "owner_user_id",
            sa.String(36),
            nullable=False,
        ),

        sa.Column(
            "traveller_profile_id",
            sa.String(36),
            nullable=True,
        ),

        sa.Column(
            "traveller_name_snapshot",
            sa.String(120),
            nullable=True,
        ),

        sa.Column(
            "traveller_phone_snapshot",
            sa.String(20),
            nullable=True,
        ),

        sa.Column(
            "traveller_email_snapshot",
            sa.String(255),
            nullable=True,
        ),

        sa.Column(
            "channel",
            sa.String(20),
            nullable=False,
        ),

        sa.Column(
            "event_type",
            sa.String(80),
            nullable=False,
        ),

        sa.Column(
            "title",
            sa.String(255),
            nullable=False,
        ),

        sa.Column(
            "message",
            sa.Text(),
            nullable=False,
        ),

        sa.Column(
            "payload_json",
            sa.Text(),
            nullable=True,
        ),

        sa.Column(
            "status",
            status_enum,
            nullable=False,
            server_default=sa.text("'pending'"),
        ),

        sa.Column(
            "provider_message_id",
            sa.String(120),
            nullable=True,
        ),

        sa.Column(
            "failure_reason",
            sa.Text(),
            nullable=True,
        ),

        sa.Column(
            "sent_at",
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
            "channel <> ''",
            name="ck_traveller_contact_notifications_channel_nonempty",
        ),

        sa.CheckConstraint(
            "event_type <> ''",
            name="ck_traveller_contact_notifications_event_type_nonempty",
        ),

        sa.CheckConstraint(
            "title <> ''",
            name="ck_traveller_contact_notifications_title_nonempty",
        ),

        sa.CheckConstraint(
            "message <> ''",
            name="ck_traveller_contact_notifications_message_nonempty",
        ),

        sa.ForeignKeyConstraint(
            ["booking_session_id"],
            ["booking_sessions.id"],
            ondelete="CASCADE",
        ),

        sa.ForeignKeyConstraint(
            ["booking_id"],
            ["trip_bookings.id"],
            ondelete="CASCADE",
        ),

        sa.ForeignKeyConstraint(
            ["owner_user_id"],
            ["users.id"],
            ondelete="RESTRICT",
        ),

        sa.ForeignKeyConstraint(
            ["traveller_profile_id"],
            ["passenger_traveller_profiles.id"],
            ondelete="SET NULL",
        ),

        sa.PrimaryKeyConstraint("id"),
    )

    op.create_index(
        "ix_traveller_contact_notifications_status_created",
        "traveller_contact_notifications",
        ["status", "created_at"],
    )

    op.create_index(
        "ix_traveller_contact_notifications_booking_session",
        "traveller_contact_notifications",
        ["booking_session_id"],
    )

    op.create_index(
        "ix_traveller_contact_notifications_booking",
        "traveller_contact_notifications",
        ["booking_id"],
    )

    op.create_index(
        "ix_traveller_contact_notifications_owner",
        "traveller_contact_notifications",
        ["owner_user_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_traveller_contact_notifications_owner",
        table_name="traveller_contact_notifications",
    )

    op.drop_index(
        "ix_traveller_contact_notifications_booking",
        table_name="traveller_contact_notifications",
    )

    op.drop_index(
        "ix_traveller_contact_notifications_booking_session",
        table_name="traveller_contact_notifications",
    )

    op.drop_index(
        "ix_traveller_contact_notifications_status_created",
        table_name="traveller_contact_notifications",
    )

    op.drop_table("traveller_contact_notifications")

    op.execute(
        f"""
        DROP TYPE IF EXISTS {ENUM_NAME};
        """
    )
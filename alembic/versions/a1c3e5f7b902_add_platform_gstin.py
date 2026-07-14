"""add platform GSTIN

Revision ID: a1c3e5f7b902
Revises: f2a4c6d8e901
Create Date: 2026-07-14 00:00:00.000000
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "a1c3e5f7b902"
down_revision: str | Sequence[str] | None = "f2a4c6d8e901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    columns = (
        sa.Column("gstin", sa.String(length=15), nullable=True),
        sa.Column("gst_legal_name", sa.String(length=200), nullable=True),
        sa.Column("gst_trade_name", sa.String(length=200), nullable=True),
        sa.Column("gst_registered_address", sa.Text(), nullable=True),
        sa.Column("gst_state_name", sa.String(length=100), nullable=True),
        sa.Column("gst_state_code", sa.String(length=2), nullable=True),
        sa.Column("gst_postal_code", sa.String(length=6), nullable=True),
        sa.Column("gst_sac_code", sa.String(length=8), nullable=True),
        sa.Column("gst_service_description", sa.String(length=255), nullable=True),
        sa.Column(
            "gst_default_place_of_supply",
            sa.String(length=100),
            nullable=True,
        ),
        sa.Column(
            "gst_default_place_of_supply_state_code",
            sa.String(length=2),
            nullable=True,
        ),
        sa.Column("gst_reverse_charge_applicable", sa.Boolean(), nullable=True),
    )
    for column in columns:
        op.add_column("platform_settings", column)

    op.create_table(
        "invoice_email_deliveries",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("delivery_key", sa.String(length=80), nullable=False),
        sa.Column("booking_id", sa.String(length=36), nullable=False),
        sa.Column("recipient_email", sa.String(length=255), nullable=True),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("retry_after", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("message_id", sa.String(length=120), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'sent', 'failed', 'skipped')",
            name="ck_invoice_email_deliveries_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0",
            name="ck_invoice_email_deliveries_attempt_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["booking_id"], ["trip_bookings.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("delivery_key"),
        sa.UniqueConstraint("booking_id"),
    )
    op.create_index(
        "ix_invoice_email_deliveries_retry",
        "invoice_email_deliveries",
        ["status", "retry_after"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_invoice_email_deliveries_retry",
        table_name="invoice_email_deliveries",
    )
    op.drop_table("invoice_email_deliveries")

    for column_name in (
        "gst_reverse_charge_applicable",
        "gst_default_place_of_supply_state_code",
        "gst_default_place_of_supply",
        "gst_service_description",
        "gst_sac_code",
        "gst_postal_code",
        "gst_state_code",
        "gst_state_name",
        "gst_registered_address",
        "gst_trade_name",
        "gst_legal_name",
        "gstin",
    ):
        op.drop_column("platform_settings", column_name)

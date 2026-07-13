"""add gst settings and snapshots

Revision ID: 8d7f4c2a9b31
Revises: 4b8c2d1e7f90
Create Date: 2026-07-09 00:00:00.000000
"""

from typing import Sequence

import sqlalchemy as sa
from alembic import op


revision: str = "8d7f4c2a9b31"
down_revision: str | Sequence[str] | None = "4b8c2d1e7f90"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _money_column() -> sa.Column:
    return sa.Column(
        sa.Numeric(10, 2),
        nullable=False,
        server_default=sa.text("0.00"),
    )


def _rate_column(default: str = "0.00") -> sa.Column:
    return sa.Column(
        sa.Numeric(5, 2),
        nullable=False,
        server_default=sa.text(default),
    )


def upgrade() -> None:
    op.add_column(
        "platform_settings",
        sa.Column(
            "gst_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "gst_cgst_rate_percent",
            sa.Numeric(5, 2),
            nullable=False,
            server_default=sa.text("2.50"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "gst_sgst_rate_percent",
            sa.Numeric(5, 2),
            nullable=False,
            server_default=sa.text("2.50"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "gst_igst_rate_percent",
            sa.Numeric(5, 2),
            nullable=False,
            server_default=sa.text("0.00"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "gst_apply_on_ac_routes_only",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column(
        "platform_settings",
        sa.Column(
            "gst_inclusive_pricing",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.create_check_constraint(
        "ck_platform_settings_gst_cgst_rate_range",
        "platform_settings",
        "gst_cgst_rate_percent >= 0 AND gst_cgst_rate_percent <= 100",
    )
    op.create_check_constraint(
        "ck_platform_settings_gst_sgst_rate_range",
        "platform_settings",
        "gst_sgst_rate_percent >= 0 AND gst_sgst_rate_percent <= 100",
    )
    op.create_check_constraint(
        "ck_platform_settings_gst_igst_rate_range",
        "platform_settings",
        "gst_igst_rate_percent >= 0 AND gst_igst_rate_percent <= 100",
    )

    for table_name in ("trip_bookings", "rfid_trip_rides"):
        op.add_column(table_name, sa.Column("taxable_amount", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0.00")))
        op.add_column(table_name, sa.Column("cgst_rate_percent_snapshot", sa.Numeric(5, 2), nullable=False, server_default=sa.text("0.00")))
        op.add_column(table_name, sa.Column("cgst_amount", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0.00")))
        op.add_column(table_name, sa.Column("sgst_rate_percent_snapshot", sa.Numeric(5, 2), nullable=False, server_default=sa.text("0.00")))
        op.add_column(table_name, sa.Column("sgst_amount", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0.00")))
        op.add_column(table_name, sa.Column("igst_rate_percent_snapshot", sa.Numeric(5, 2), nullable=False, server_default=sa.text("0.00")))
        op.add_column(table_name, sa.Column("igst_amount", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0.00")))
        op.add_column(table_name, sa.Column("total_tax_amount", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0.00")))
        op.add_column(table_name, sa.Column("gst_enabled_snapshot", sa.Boolean(), nullable=False, server_default=sa.text("true")))
        op.add_column(table_name, sa.Column("gst_inclusive_snapshot", sa.Boolean(), nullable=False, server_default=sa.text("true")))

    op.add_column("booking_sessions", sa.Column("total_taxable_amount", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0.00")))
    op.add_column("booking_sessions", sa.Column("total_cgst_amount", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0.00")))
    op.add_column("booking_sessions", sa.Column("total_sgst_amount", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0.00")))
    op.add_column("booking_sessions", sa.Column("total_igst_amount", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0.00")))
    op.add_column("booking_sessions", sa.Column("total_tax_amount", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0.00")))
    op.add_column("booking_sessions", sa.Column("gst_enabled_snapshot", sa.Boolean(), nullable=False, server_default=sa.text("true")))
    op.add_column("booking_sessions", sa.Column("gst_inclusive_snapshot", sa.Boolean(), nullable=False, server_default=sa.text("true")))
    op.add_column("booking_sessions", sa.Column("cgst_rate_percent_snapshot", sa.Numeric(5, 2), nullable=False, server_default=sa.text("0.00")))
    op.add_column("booking_sessions", sa.Column("sgst_rate_percent_snapshot", sa.Numeric(5, 2), nullable=False, server_default=sa.text("0.00")))
    op.add_column("booking_sessions", sa.Column("igst_rate_percent_snapshot", sa.Numeric(5, 2), nullable=False, server_default=sa.text("0.00")))

    for table_name in ("booking_payments", "booking_session_payments"):
        op.add_column(table_name, sa.Column("taxable_amount", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0.00")))
        op.add_column(table_name, sa.Column("cgst_amount", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0.00")))
        op.add_column(table_name, sa.Column("sgst_amount", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0.00")))
        op.add_column(table_name, sa.Column("igst_amount", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0.00")))
        op.add_column(table_name, sa.Column("total_tax_amount", sa.Numeric(10, 2), nullable=False, server_default=sa.text("0.00")))

    op.execute(
        """
        UPDATE trip_bookings AS tb
        SET
            taxable_amount = CASE
                WHEN COALESCE(r.has_ac, false) THEN ROUND((tb.fare_amount / 1.05)::numeric, 2)
                ELSE tb.fare_amount
            END,
            cgst_rate_percent_snapshot = CASE WHEN COALESCE(r.has_ac, false) THEN 2.50 ELSE 0.00 END,
            sgst_rate_percent_snapshot = CASE WHEN COALESCE(r.has_ac, false) THEN 2.50 ELSE 0.00 END,
            igst_rate_percent_snapshot = 0.00,
            cgst_amount = CASE
                WHEN COALESCE(r.has_ac, false) THEN ROUND(((tb.fare_amount / 1.05) * 0.025)::numeric, 2)
                ELSE 0.00
            END,
            sgst_amount = CASE
                WHEN COALESCE(r.has_ac, false) THEN ROUND(((tb.fare_amount / 1.05) * 0.025)::numeric, 2)
                ELSE 0.00
            END,
            igst_amount = 0.00,
            total_tax_amount = CASE
                WHEN COALESCE(r.has_ac, false)
                THEN ROUND(((tb.fare_amount / 1.05) * 0.025)::numeric, 2) + ROUND(((tb.fare_amount / 1.05) * 0.025)::numeric, 2)
                ELSE 0.00
            END,
            gst_enabled_snapshot = COALESCE(r.has_ac, false),
            gst_inclusive_snapshot = true
        FROM routes AS r
        WHERE r.id = tb.route_id
        """
    )
    op.execute(
        """
        UPDATE booking_payments AS bp
        SET
            taxable_amount = tb.taxable_amount,
            cgst_amount = tb.cgst_amount,
            sgst_amount = tb.sgst_amount,
            igst_amount = tb.igst_amount,
            total_tax_amount = tb.total_tax_amount
        FROM trip_bookings AS tb
        WHERE tb.id = bp.booking_id
        """
    )
    op.execute(
        """
        UPDATE booking_sessions AS bs
        SET
            total_taxable_amount = totals.taxable_amount,
            total_cgst_amount = totals.cgst_amount,
            total_sgst_amount = totals.sgst_amount,
            total_igst_amount = totals.igst_amount,
            total_tax_amount = totals.total_tax_amount,
            gst_enabled_snapshot = totals.gst_enabled_snapshot,
            gst_inclusive_snapshot = totals.gst_inclusive_snapshot,
            cgst_rate_percent_snapshot = totals.cgst_rate_percent_snapshot,
            sgst_rate_percent_snapshot = totals.sgst_rate_percent_snapshot,
            igst_rate_percent_snapshot = totals.igst_rate_percent_snapshot
        FROM (
            SELECT
                booking_session_id,
                SUM(taxable_amount) AS taxable_amount,
                SUM(cgst_amount) AS cgst_amount,
                SUM(sgst_amount) AS sgst_amount,
                SUM(igst_amount) AS igst_amount,
                SUM(total_tax_amount) AS total_tax_amount,
                BOOL_OR(gst_enabled_snapshot) AS gst_enabled_snapshot,
                BOOL_OR(gst_inclusive_snapshot) AS gst_inclusive_snapshot,
                MAX(cgst_rate_percent_snapshot) AS cgst_rate_percent_snapshot,
                MAX(sgst_rate_percent_snapshot) AS sgst_rate_percent_snapshot,
                MAX(igst_rate_percent_snapshot) AS igst_rate_percent_snapshot
            FROM trip_bookings
            WHERE booking_session_id IS NOT NULL
            GROUP BY booking_session_id
        ) AS totals
        WHERE totals.booking_session_id = bs.id
        """
    )
    op.execute(
        """
        UPDATE booking_session_payments AS bsp
        SET
            taxable_amount = bs.total_taxable_amount,
            cgst_amount = bs.total_cgst_amount,
            sgst_amount = bs.total_sgst_amount,
            igst_amount = bs.total_igst_amount,
            total_tax_amount = bs.total_tax_amount
        FROM booking_sessions AS bs
        WHERE bs.id = bsp.booking_session_id
        """
    )
    op.execute(
        """
        UPDATE rfid_trip_rides AS rr
        SET
            taxable_amount = CASE
                WHEN COALESCE(r.has_ac, false) THEN ROUND((rr.fare_amount / 1.05)::numeric, 2)
                ELSE rr.fare_amount
            END,
            cgst_rate_percent_snapshot = CASE WHEN COALESCE(r.has_ac, false) THEN 2.50 ELSE 0.00 END,
            sgst_rate_percent_snapshot = CASE WHEN COALESCE(r.has_ac, false) THEN 2.50 ELSE 0.00 END,
            igst_rate_percent_snapshot = 0.00,
            cgst_amount = CASE
                WHEN COALESCE(r.has_ac, false) THEN ROUND(((rr.fare_amount / 1.05) * 0.025)::numeric, 2)
                ELSE 0.00
            END,
            sgst_amount = CASE
                WHEN COALESCE(r.has_ac, false) THEN ROUND(((rr.fare_amount / 1.05) * 0.025)::numeric, 2)
                ELSE 0.00
            END,
            igst_amount = 0.00,
            total_tax_amount = CASE
                WHEN COALESCE(r.has_ac, false)
                THEN ROUND(((rr.fare_amount / 1.05) * 0.025)::numeric, 2) + ROUND(((rr.fare_amount / 1.05) * 0.025)::numeric, 2)
                ELSE 0.00
            END,
            gst_enabled_snapshot = COALESCE(r.has_ac, false),
            gst_inclusive_snapshot = true
        FROM routes AS r
        WHERE r.id = rr.route_id
        """
    )

    op.create_check_constraint(
        "ck_booking_sessions_total_taxable_nonnegative",
        "booking_sessions",
        "total_taxable_amount >= 0",
    )
    op.create_check_constraint(
        "ck_booking_sessions_tax_components_nonnegative",
        "booking_sessions",
        "total_cgst_amount >= 0 AND total_sgst_amount >= 0 AND total_igst_amount >= 0",
    )
    op.create_check_constraint(
        "ck_booking_sessions_total_tax_nonnegative",
        "booking_sessions",
        "total_tax_amount >= 0",
    )
    op.create_check_constraint(
        "ck_booking_sessions_gst_rate_snapshot_range",
        "booking_sessions",
        "cgst_rate_percent_snapshot >= 0 AND cgst_rate_percent_snapshot <= 100 "
        "AND sgst_rate_percent_snapshot >= 0 AND sgst_rate_percent_snapshot <= 100 "
        "AND igst_rate_percent_snapshot >= 0 AND igst_rate_percent_snapshot <= 100",
    )

    for table_name, prefix in (
        ("trip_bookings", "ck_trip_bookings"),
        ("rfid_trip_rides", "ck_rfid_trip_rides"),
    ):
        op.create_check_constraint(
            f"{prefix}_taxable_amount_nonnegative",
            table_name,
            "taxable_amount >= 0",
        )
        op.create_check_constraint(
            f"{prefix}_tax_components_nonnegative",
            table_name,
            "cgst_amount >= 0 AND sgst_amount >= 0 AND igst_amount >= 0",
        )
        op.create_check_constraint(
            f"{prefix}_total_tax_nonnegative",
            table_name,
            "total_tax_amount >= 0",
        )
        op.create_check_constraint(
            f"{prefix}_gst_rate_snapshot_range",
            table_name,
            "cgst_rate_percent_snapshot >= 0 AND cgst_rate_percent_snapshot <= 100 "
            "AND sgst_rate_percent_snapshot >= 0 AND sgst_rate_percent_snapshot <= 100 "
            "AND igst_rate_percent_snapshot >= 0 AND igst_rate_percent_snapshot <= 100",
        )

    for table_name, prefix in (
        ("booking_payments", "ck_booking_payments"),
        ("booking_session_payments", "ck_booking_session_payments"),
    ):
        op.create_check_constraint(
            f"{prefix}_taxable_nonnegative",
            table_name,
            "taxable_amount >= 0",
        )
        op.create_check_constraint(
            f"{prefix}_tax_components_nonnegative",
            table_name,
            "cgst_amount >= 0 AND sgst_amount >= 0 AND igst_amount >= 0",
        )
        op.create_check_constraint(
            f"{prefix}_total_tax_nonnegative",
            table_name,
            "total_tax_amount >= 0",
        )


def downgrade() -> None:
    for table_name, prefix in (
        ("booking_payments", "ck_booking_payments"),
        ("booking_session_payments", "ck_booking_session_payments"),
    ):
        op.drop_constraint(f"{prefix}_total_tax_nonnegative", table_name, type_="check")
        op.drop_constraint(f"{prefix}_tax_components_nonnegative", table_name, type_="check")
        op.drop_constraint(f"{prefix}_taxable_nonnegative", table_name, type_="check")

    for table_name, prefix in (
        ("trip_bookings", "ck_trip_bookings"),
        ("rfid_trip_rides", "ck_rfid_trip_rides"),
    ):
        op.drop_constraint(f"{prefix}_gst_rate_snapshot_range", table_name, type_="check")
        op.drop_constraint(f"{prefix}_total_tax_nonnegative", table_name, type_="check")
        op.drop_constraint(f"{prefix}_tax_components_nonnegative", table_name, type_="check")
        op.drop_constraint(f"{prefix}_taxable_amount_nonnegative", table_name, type_="check")

    op.drop_constraint(
        "ck_booking_sessions_gst_rate_snapshot_range",
        "booking_sessions",
        type_="check",
    )
    op.drop_constraint(
        "ck_booking_sessions_total_tax_nonnegative",
        "booking_sessions",
        type_="check",
    )
    op.drop_constraint(
        "ck_booking_sessions_tax_components_nonnegative",
        "booking_sessions",
        type_="check",
    )
    op.drop_constraint(
        "ck_booking_sessions_total_taxable_nonnegative",
        "booking_sessions",
        type_="check",
    )

    for table_name in ("booking_payments", "booking_session_payments"):
        for column_name in (
            "total_tax_amount",
            "igst_amount",
            "sgst_amount",
            "cgst_amount",
            "taxable_amount",
        ):
            op.drop_column(table_name, column_name)

    for column_name in (
        "igst_rate_percent_snapshot",
        "sgst_rate_percent_snapshot",
        "cgst_rate_percent_snapshot",
        "gst_inclusive_snapshot",
        "gst_enabled_snapshot",
        "total_tax_amount",
        "total_igst_amount",
        "total_sgst_amount",
        "total_cgst_amount",
        "total_taxable_amount",
    ):
        op.drop_column("booking_sessions", column_name)

    for table_name in ("trip_bookings", "rfid_trip_rides"):
        for column_name in (
            "gst_inclusive_snapshot",
            "gst_enabled_snapshot",
            "total_tax_amount",
            "igst_amount",
            "igst_rate_percent_snapshot",
            "sgst_amount",
            "sgst_rate_percent_snapshot",
            "cgst_amount",
            "cgst_rate_percent_snapshot",
            "taxable_amount",
        ):
            op.drop_column(table_name, column_name)

    op.drop_constraint(
        "ck_platform_settings_gst_igst_rate_range",
        "platform_settings",
        type_="check",
    )
    op.drop_constraint(
        "ck_platform_settings_gst_sgst_rate_range",
        "platform_settings",
        type_="check",
    )
    op.drop_constraint(
        "ck_platform_settings_gst_cgst_rate_range",
        "platform_settings",
        type_="check",
    )
    for column_name in (
        "gst_inclusive_pricing",
        "gst_apply_on_ac_routes_only",
        "gst_igst_rate_percent",
        "gst_sgst_rate_percent",
        "gst_cgst_rate_percent",
        "gst_enabled",
    ):
        op.drop_column("platform_settings", column_name)

from decimal import Decimal
from types import SimpleNamespace
import unittest

from app.admin.logic.service import AdminService


class AdminTransactionAuditTests(unittest.TestCase):
    def test_gst_inclusive_booking_uses_taxable_amount(self):
        booking = SimpleNamespace(
            fare_amount=Decimal("30.00"),
            taxable_amount=Decimal("28.57"),
            commission_amount=Decimal("2.86"),
            driver_payout_amount=Decimal("25.71"),
        )

        self.assertTrue(AdminService.is_booking_payout_audit_correct(booking))

    def test_incorrect_driver_payout_fails_audit(self):
        booking = SimpleNamespace(
            fare_amount=Decimal("30.00"),
            taxable_amount=Decimal("28.57"),
            commission_amount=Decimal("2.86"),
            driver_payout_amount=Decimal("27.14"),
        )

        self.assertFalse(AdminService.is_booking_payout_audit_correct(booking))

    def test_legacy_booking_without_taxable_snapshot_uses_fare(self):
        booking = SimpleNamespace(
            fare_amount=Decimal("30.00"),
            taxable_amount=Decimal("0.00"),
            commission_amount=Decimal("3.00"),
            driver_payout_amount=Decimal("27.00"),
        )

        self.assertTrue(AdminService.is_booking_payout_audit_correct(booking))


if __name__ == "__main__":
    unittest.main()

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
import unittest

from app.admin.logic.service import AdminService
from app.admin.rfid_service import AdminRFIDService
from app.passenger.service import PassengerService


class CancellationMetadataTests(unittest.TestCase):
    def test_setting_metadata_preserves_existing_cancellation_timestamp(self):
        original_time = datetime(2026, 7, 14, 8, 30, tzinfo=timezone.utc)
        record = SimpleNamespace(cancelled_at=original_time)

        occurred_at = PassengerService._set_cancellation_metadata(
            record,
            reason="  Cancelled by passenger.  ",
            source="passenger",
            cancelled_by_user_id="user-1",
            cancelled_at=datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(
            occurred_at,
            datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc),
        )
        self.assertEqual(record.cancelled_at, original_time)
        self.assertEqual(record.cancellation_reason, "Cancelled by passenger.")
        self.assertEqual(record.cancellation_source, "passenger")
        self.assertEqual(record.cancelled_by_user_id, "user-1")

    def test_premature_end_reason_is_propagated(self):
        cancelled_at = datetime(2026, 7, 14, 8, 30, tzinfo=timezone.utc)
        record = SimpleNamespace(
            cancelled_at=cancelled_at,
            cancellation_reason=None,
            premature_end_reason="Vehicle became unsafe.",
            cancellation_source="driver",
            cancelled_by_user_id="driver-1",
        )

        expected = {
            "cancelled_at": cancelled_at,
            "reason": "Vehicle became unsafe.",
            "source": "driver",
            "cancelled_by_user_id": "driver-1",
        }

        self.assertEqual(
            PassengerService._serialize_cancellation_metadata(record), expected
        )
        self.assertEqual(AdminService.serialize_cancellation_metadata(record), expected)

    def test_non_cancelled_record_has_no_metadata(self):
        record = SimpleNamespace(
            cancelled_at=None,
            cancellation_reason=None,
            cancellation_source=None,
            cancelled_by_user_id=None,
        )

        self.assertIsNone(
            PassengerService._serialize_cancellation_metadata(record)
        )


class RFIDTransactionDirectionTests(unittest.TestCase):
    def test_amount_and_hold_deltas_map_to_admin_directions(self):
        cases = (
            ("10.00", "0.00", "credit"),
            ("-10.00", "0.00", "debit"),
            ("0.00", "10.00", "hold"),
            ("0.00", "-10.00", "hold_release"),
            ("0.00", "0.00", "neutral"),
        )

        for amount_delta, held_delta, expected in cases:
            with self.subTest(expected=expected):
                entry = SimpleNamespace(
                    amount_delta=Decimal(amount_delta),
                    held_delta=Decimal(held_delta),
                )
                self.assertEqual(
                    AdminRFIDService._rfid_transaction_direction(entry),
                    expected,
                )


if __name__ == "__main__":
    unittest.main()

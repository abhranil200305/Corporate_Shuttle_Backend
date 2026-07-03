from __future__ import annotations

import unittest

from pydantic import ValidationError

from app.admin.rfid_schemas import (
    RFIDCardBulkRegisterRequest,
    RFIDCardRegisterRequest,
)
from app.rfid.scan_schemas import RFIDScanRequest


class RFIDCardUIDValidationTests(unittest.TestCase):
    def test_registration_accepts_boundary_lengths(self) -> None:
        for card_uid in ("A" * 8, "B" * 24):
            with self.subTest(card_uid_length=len(card_uid)):
                payload = RFIDCardRegisterRequest(card_uid=card_uid)
                self.assertEqual(payload.card_uid, card_uid)

    def test_registration_rejects_out_of_range_lengths(self) -> None:
        for card_uid in ("A" * 7, "B" * 25):
            with self.subTest(card_uid_length=len(card_uid)):
                with self.assertRaises(ValidationError):
                    RFIDCardRegisterRequest(card_uid=card_uid)

    def test_scan_uses_same_uid_length_limits(self) -> None:
        RFIDScanRequest(device_serial_number="reader-1", card_uid="A" * 8)
        RFIDScanRequest(device_serial_number="reader-1", card_uid="B" * 24)

        for card_uid in ("A" * 7, "B" * 25):
            with self.subTest(card_uid_length=len(card_uid)):
                with self.assertRaises(ValidationError):
                    RFIDScanRequest(
                        device_serial_number="reader-1",
                        card_uid=card_uid,
                    )

    def test_bulk_registration_validates_each_uid_after_trimming(self) -> None:
        payload = RFIDCardBulkRegisterRequest(
            card_uids=["  ABCD1234  ", "B" * 24],
        )
        self.assertEqual(payload.card_uids, ["ABCD1234", "B" * 24])

        for card_uid in ("  ABC1234  ", f"  {'C' * 25}  "):
            with self.subTest(card_uid=card_uid):
                with self.assertRaises(ValidationError):
                    RFIDCardBulkRegisterRequest(card_uids=[card_uid])


if __name__ == "__main__":
    unittest.main()

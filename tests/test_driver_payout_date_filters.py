from datetime import datetime, timezone
import unittest

from fastapi import HTTPException

from app.driver.trips.payout_details import IST, _build_trip_window_utc


class DriverPayoutDateFilterTests(unittest.TestCase):
    def test_year_only_range_includes_complete_calendar_year(self) -> None:
        start_utc, end_utc = _build_trip_window_utc(
            from_month=None,
            from_year=2026,
            to_month=None,
            to_year=2026,
        )

        self.assertEqual(
            start_utc,
            datetime(2026, 1, 1, tzinfo=IST).astimezone(timezone.utc),
        )
        self.assertEqual(
            end_utc,
            datetime(2027, 1, 1, tzinfo=IST).astimezone(timezone.utc),
        )

    def test_year_only_boundary_can_be_used_independently(self) -> None:
        start_utc, end_utc = _build_trip_window_utc(
            from_month=None,
            from_year=2026,
            to_month=None,
            to_year=None,
        )
        self.assertEqual(
            start_utc,
            datetime(2026, 1, 1, tzinfo=IST).astimezone(timezone.utc),
        )
        self.assertIsNone(end_utc)

        start_utc, end_utc = _build_trip_window_utc(
            from_month=None,
            from_year=None,
            to_month=None,
            to_year=2026,
        )
        self.assertIsNone(start_utc)
        self.assertEqual(
            end_utc,
            datetime(2027, 1, 1, tzinfo=IST).astimezone(timezone.utc),
        )

    def test_month_and_year_range_remains_supported(self) -> None:
        start_utc, end_utc = _build_trip_window_utc(
            from_month=3,
            from_year=2026,
            to_month=5,
            to_year=2026,
        )
        self.assertEqual(
            start_utc,
            datetime(2026, 3, 1, tzinfo=IST).astimezone(timezone.utc),
        )
        self.assertEqual(
            end_utc,
            datetime(2026, 6, 1, tzinfo=IST).astimezone(timezone.utc),
        )

    def test_month_without_year_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            _build_trip_window_utc(
                from_month=3,
                from_year=None,
                to_month=None,
                to_year=None,
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(
            raised.exception.detail,
            "from_year is required when from_month is provided",
        )

    def test_reversed_year_range_is_rejected(self) -> None:
        with self.assertRaises(HTTPException) as raised:
            _build_trip_window_utc(
                from_month=None,
                from_year=2027,
                to_month=None,
                to_year=2026,
            )

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(
            raised.exception.detail,
            "from period must be before or equal to to period",
        )


if __name__ == "__main__":
    unittest.main()

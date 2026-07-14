import unittest

from pydantic import ValidationError

from app.passenger.schemas import (
    BookingSessionGuestTravellerRequest,
    PassengerTravellerProfileCreateRequest,
    PassengerTravellerProfileUpdateRequest,
)


class TravellerContactValidationTests(unittest.TestCase):
    def test_create_normalizes_indian_mobile_and_email_domain(self) -> None:
        payload = PassengerTravellerProfileCreateRequest(
            full_name="Traveller",
            phone="+91 98765-43210",
            email="Traveller@EXAMPLE.COM",
        )

        self.assertEqual(payload.phone, "+919876543210")
        self.assertEqual(payload.email, "Traveller@example.com")

    def test_local_indian_mobile_formats_are_supported(self) -> None:
        values = (
            "9876543210",
            "09876543210",
            "91 98765 43210",
            "+91 (98765) 43210",
        )

        for value in values:
            with self.subTest(value=value):
                payload = BookingSessionGuestTravellerRequest(
                    full_name="Guest",
                    phone=value,
                )
                self.assertEqual(payload.phone, "+919876543210")

    def test_prefixed_international_number_is_supported(self) -> None:
        payload = BookingSessionGuestTravellerRequest(
            full_name="International Guest",
            phone="+1 (415) 555-2671",
        )

        self.assertEqual(payload.phone, "+14155552671")

    def test_update_validates_only_provided_contact_fields(self) -> None:
        payload = PassengerTravellerProfileUpdateRequest(
            phone="98765 43210",
        )

        self.assertEqual(payload.phone, "+919876543210")
        self.assertIsNone(payload.email)

    def test_invalid_indian_mobile_is_rejected(self) -> None:
        invalid_values = (
            "1234567890",
            "98765",
            "+91 58765 43210",
            "98765-ABCDE",
        )

        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    BookingSessionGuestTravellerRequest(
                        full_name="Guest",
                        phone=value,
                    )

    def test_international_number_requires_plus_prefix(self) -> None:
        with self.assertRaises(ValidationError):
            BookingSessionGuestTravellerRequest(
                full_name="Guest",
                phone="14155552671",
            )

    def test_invalid_email_is_rejected(self) -> None:
        invalid_values = (
            "missing-at.example.com",
            "user@",
            "user @example.com",
            "@example.com",
        )

        for value in invalid_values:
            with self.subTest(value=value):
                with self.assertRaises(ValidationError):
                    PassengerTravellerProfileCreateRequest(
                        full_name="Traveller",
                        phone="9876543210",
                        email=value,
                    )

    def test_optional_email_remains_optional(self) -> None:
        payload = BookingSessionGuestTravellerRequest(
            full_name="Guest",
            phone="9876543210",
            email=None,
        )

        self.assertIsNone(payload.email)


if __name__ == "__main__":
    unittest.main()

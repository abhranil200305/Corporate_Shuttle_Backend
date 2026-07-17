import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import HTTPException
from pydantic import ValidationError

from app.db.schema import UserRole
from app.passenger.schemas import (
    BookingSessionGuestTravellerRequest,
    PassengerTravellerProfileCreateRequest,
    PassengerTravellerProfileUpdateRequest,
)
from app.passenger.service import PassengerService


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


class PermanentTravellerServiceValidationTests(
    unittest.IsolatedAsyncioTestCase
):
    async def test_service_revalidates_and_normalizes_permanent_profile(self) -> None:
        result = MagicMock()
        result.scalar_one_or_none.return_value = None
        db = MagicMock()
        db.execute = AsyncMock(return_value=result)
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        service = PassengerService(db)
        user = SimpleNamespace(
            id="passenger-1",
            email="owner@example.com",
            role=UserRole.PASSENGER,
        )
        # model_construct simulates any internal caller that bypasses the API's
        # Pydantic request validation.
        payload = PassengerTravellerProfileCreateRequest.model_construct(
            full_name=" Saved Traveller ",
            phone="+91 98765-43210",
            email="Traveller@EXAMPLE.COM",
            relationship_label="Guest",
            is_self=False,
        )

        response = await service.create_traveller_profile(user, payload)

        profile = db.add.call_args.args[0]
        self.assertEqual(profile.phone, "+919876543210")
        self.assertEqual(profile.email, "Traveller@example.com")
        self.assertEqual(
            response["profile"]["phone"],
            "+919876543210",
        )

    async def test_service_rejects_invalid_permanent_profile_contact(self) -> None:
        service = PassengerService(MagicMock())
        user = SimpleNamespace(
            id="passenger-1",
            email="owner@example.com",
            role=UserRole.PASSENGER,
        )
        payload = PassengerTravellerProfileCreateRequest.model_construct(
            full_name="Saved Traveller",
            phone="not-a-phone",
            email="not-an-email",
            relationship_label=None,
            is_self=False,
        )

        with self.assertRaises(HTTPException) as raised:
            await service.create_traveller_profile(user, payload)

        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(
            raised.exception.detail["error"],
            "invalid_traveller_phone",
        )


if __name__ == "__main__":
    unittest.main()

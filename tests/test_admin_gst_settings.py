import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

from app.admin.logic.service import AdminService
from app.admin.structs.dto import GSTSettingsUpdate


class AdminGSTSettingsValidationTests(unittest.TestCase):
    def test_gstin_is_normalized(self) -> None:
        payload = GSTSettingsUpdate(gstin=" 27abcde1234f1z5 ")

        self.assertEqual(payload.gstin, "27ABCDE1234F1Z5")
        self.assertIn("gstin", payload.model_fields_set)

    def test_gstin_can_be_explicitly_cleared(self) -> None:
        payload = GSTSettingsUpdate(gstin=None)

        self.assertIsNone(payload.gstin)
        self.assertIn("gstin", payload.model_fields_set)

    def test_invalid_gstin_is_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            GSTSettingsUpdate(gstin="invalid")

    def test_invoice_profile_fields_are_normalized(self) -> None:
        payload = GSTSettingsUpdate(
            gst_legal_name="  TransEV Private Limited  ",
            gst_state_code="19",
            gst_postal_code="700001",
            gst_sac_code="996411",
            gst_default_place_of_supply_state_code="19",
        )

        self.assertEqual(payload.gst_legal_name, "TransEV Private Limited")
        self.assertEqual(payload.gst_state_code, "19")
        self.assertEqual(payload.gst_postal_code, "700001")
        self.assertEqual(payload.gst_sac_code, "996411")

    def test_invalid_invoice_profile_codes_are_rejected(self) -> None:
        for field_name, value in (
            ("gst_state_code", "WB"),
            ("gst_postal_code", "123"),
            ("gst_sac_code", "transport"),
        ):
            with self.subTest(field_name=field_name):
                with self.assertRaises(ValidationError):
                    GSTSettingsUpdate(**{field_name: value})

    def test_gst_settings_response_uses_environment_fallback(self) -> None:
        service = AdminService.__new__(AdminService)

        with patch.dict("os.environ", {"GSTIN": "27ABCDE1234F1Z5"}, clear=False):
            response = service._serialize_gst_settings(None)

        self.assertEqual(response["gstin"], "27ABCDE1234F1Z5")

    def test_gst_settings_response_prefers_persisted_gstin(self) -> None:
        service = AdminService.__new__(AdminService)
        settings = SimpleNamespace(
            settings_key="default",
            gstin="29ABCDE1234F1Z3",
            gst_legal_name="TransEV Private Limited",
            gst_trade_name="TransEV",
            gst_registered_address="1 Example Road, Kolkata",
            gst_state_name="West Bengal",
            gst_state_code="19",
            gst_postal_code="700001",
            gst_sac_code="996411",
            gst_service_description="Passenger transportation service",
            gst_default_place_of_supply="West Bengal",
            gst_default_place_of_supply_state_code="19",
            gst_reverse_charge_applicable=False,
            gst_enabled=True,
            gst_cgst_rate_percent=Decimal("2.50"),
            gst_sgst_rate_percent=Decimal("2.50"),
            gst_igst_rate_percent=Decimal("0.00"),
            gst_apply_on_ac_routes_only=True,
            gst_inclusive_pricing=True,
            created_at=None,
            updated_at=None,
        )

        response = service._serialize_gst_settings(settings)

        self.assertEqual(response["gstin"], "29ABCDE1234F1Z3")


if __name__ == "__main__":
    unittest.main()

import os
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from app.tax import (
    GSTConfig,
    gst_config_from_env,
    gst_config_from_settings,
    gst_invoice_profile_from_settings,
    gstin_from_env,
    gstin_from_settings,
    gst_settings_kwargs_from_env,
    normalize_gstin,
)


GST_ENV_NAMES = (
    "GSTIN",
    "GST_LEGAL_NAME",
    "GST_TRADE_NAME",
    "GST_REGISTERED_ADDRESS",
    "GST_STATE_NAME",
    "GST_STATE_CODE",
    "GST_POSTAL_CODE",
    "GST_SAC_CODE",
    "GST_SERVICE_DESCRIPTION",
    "GST_DEFAULT_PLACE_OF_SUPPLY",
    "GST_DEFAULT_PLACE_OF_SUPPLY_STATE_CODE",
    "GST_REVERSE_CHARGE_APPLICABLE",
    "GST_ENABLED",
    "GST_CGST_RATE_PERCENT",
    "GST_SGST_RATE_PERCENT",
    "GST_IGST_RATE_PERCENT",
    "GST_APPLY_ON_AC_ROUTES_ONLY",
    "GST_INCLUSIVE_PRICING",
)


def gst_environment(**values: str):
    environment = {
        key: value
        for key, value in os.environ.items()
        if key not in GST_ENV_NAMES
    }
    environment.update(values)
    return patch.dict(os.environ, environment, clear=True)


class GSTEnvironmentConfigTests(unittest.TestCase):
    def test_built_in_defaults_are_used_when_values_are_absent(self) -> None:
        with gst_environment():
            self.assertEqual(gst_config_from_env(), GSTConfig())
            self.assertEqual(gst_config_from_settings(None), GSTConfig())

    def test_missing_settings_use_environment_values(self) -> None:
        with gst_environment(
            GST_ENABLED="no",
            GST_CGST_RATE_PERCENT="6.125",
            GST_SGST_RATE_PERCENT="6.124",
            GST_IGST_RATE_PERCENT="12",
            GST_APPLY_ON_AC_ROUTES_ONLY="off",
            GST_INCLUSIVE_PRICING="0",
            GSTIN="27abcde1234f1z5",
            GST_LEGAL_NAME="TransEV Private Limited",
            GST_TRADE_NAME="TransEV",
            GST_REGISTERED_ADDRESS="1 Example Road, Kolkata",
            GST_STATE_NAME="West Bengal",
            GST_STATE_CODE="19",
            GST_POSTAL_CODE="700001",
            GST_SAC_CODE="996411",
            GST_SERVICE_DESCRIPTION="Passenger transportation service",
            GST_DEFAULT_PLACE_OF_SUPPLY="West Bengal",
            GST_DEFAULT_PLACE_OF_SUPPLY_STATE_CODE="19",
            GST_REVERSE_CHARGE_APPLICABLE="false",
        ):
            config = gst_config_from_settings(None)

            self.assertEqual(
                config,
                GSTConfig(
                    enabled=False,
                    cgst_rate_percent=Decimal("6.13"),
                    sgst_rate_percent=Decimal("6.12"),
                    igst_rate_percent=Decimal("12.00"),
                    apply_on_ac_routes_only=False,
                    inclusive_pricing=False,
                ),
            )
            self.assertEqual(
                gst_settings_kwargs_from_env(),
                {
                    "gstin": "27ABCDE1234F1Z5",
                    "gst_legal_name": "TransEV Private Limited",
                    "gst_trade_name": "TransEV",
                    "gst_registered_address": "1 Example Road, Kolkata",
                    "gst_state_name": "West Bengal",
                    "gst_state_code": "19",
                    "gst_postal_code": "700001",
                    "gst_sac_code": "996411",
                    "gst_service_description": "Passenger transportation service",
                    "gst_default_place_of_supply": "West Bengal",
                    "gst_default_place_of_supply_state_code": "19",
                    "gst_reverse_charge_applicable": False,
                    "gst_enabled": False,
                    "gst_cgst_rate_percent": Decimal("6.13"),
                    "gst_sgst_rate_percent": Decimal("6.12"),
                    "gst_igst_rate_percent": Decimal("12.00"),
                    "gst_apply_on_ac_routes_only": False,
                    "gst_inclusive_pricing": False,
                },
            )

            invoice_profile = gst_invoice_profile_from_settings(None)
            self.assertEqual(invoice_profile["legal_name"], "TransEV Private Limited")
            self.assertEqual(invoice_profile["state_code"], "19")
            self.assertEqual(invoice_profile["postal_code"], "700001")
            self.assertEqual(invoice_profile["sac_code"], "996411")
            self.assertFalse(invoice_profile["reverse_charge_applicable"])

    def test_gstin_uses_environment_and_normalizes_case(self) -> None:
        with gst_environment(GSTIN=" 27abcde1234f1z5 "):
            self.assertEqual(gstin_from_env(), "27ABCDE1234F1Z5")
            self.assertEqual(gstin_from_settings(None), "27ABCDE1234F1Z5")

    def test_persisted_gstin_overrides_environment(self) -> None:
        settings = SimpleNamespace(gstin="29ABCDE1234F1Z3")

        with gst_environment(GSTIN="27ABCDE1234F1Z5"):
            self.assertEqual(gstin_from_settings(settings), "29ABCDE1234F1Z3")

    def test_fourteen_character_organisation_identifier_is_accepted(self) -> None:
        self.assertEqual(
            normalize_gstin(" ab12cd34ef56gh "),
            "AB12CD34EF56GH",
        )

        with gst_environment(GSTIN="ab12cd34ef56gh"):
            self.assertEqual(gstin_from_env(), "AB12CD34EF56GH")

    def test_invalid_gstin_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "14-character alphanumeric"):
            normalize_gstin("not-a-gstin")

        with gst_environment(GSTIN="not-a-gstin"):
            with self.assertRaisesRegex(RuntimeError, "14-character alphanumeric"):
                gstin_from_env()

    def test_persisted_settings_override_environment(self) -> None:
        settings = SimpleNamespace(
            gst_enabled=True,
            gst_cgst_rate_percent=Decimal("1.25"),
            gst_sgst_rate_percent=Decimal("1.25"),
            gst_igst_rate_percent=Decimal("0.00"),
            gst_apply_on_ac_routes_only=True,
            gst_inclusive_pricing=True,
        )

        with gst_environment(GST_ENABLED="not-a-valid-boolean"):
            self.assertEqual(
                gst_config_from_settings(settings),
                GSTConfig(
                    enabled=True,
                    cgst_rate_percent=Decimal("1.25"),
                    sgst_rate_percent=Decimal("1.25"),
                    igst_rate_percent=Decimal("0.00"),
                    apply_on_ac_routes_only=True,
                    inclusive_pricing=True,
                ),
            )

    def test_invalid_boolean_fails_explicitly(self) -> None:
        with gst_environment(GST_ENABLED="sometimes"):
            with self.assertRaisesRegex(RuntimeError, "GST_ENABLED must be one of"):
                gst_config_from_env()

    def test_invalid_decimal_fails_explicitly(self) -> None:
        with gst_environment(GST_CGST_RATE_PERCENT="not-a-rate"):
            with self.assertRaisesRegex(
                RuntimeError,
                "GST_CGST_RATE_PERCENT must be a decimal percentage",
            ):
                gst_config_from_env()

    def test_out_of_range_rate_fails_explicitly(self) -> None:
        with gst_environment(GST_SGST_RATE_PERCENT="100.01"):
            with self.assertRaisesRegex(
                RuntimeError,
                "GST_SGST_RATE_PERCENT must be between 0 and 100",
            ):
                gst_config_from_env()


if __name__ == "__main__":
    unittest.main()

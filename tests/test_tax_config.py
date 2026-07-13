import os
import unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

from app.tax import (
    GSTConfig,
    gst_config_from_env,
    gst_config_from_settings,
    gst_settings_kwargs_from_env,
)


GST_ENV_NAMES = (
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
                    "gst_enabled": False,
                    "gst_cgst_rate_percent": Decimal("6.13"),
                    "gst_sgst_rate_percent": Decimal("6.12"),
                    "gst_igst_rate_percent": Decimal("12.00"),
                    "gst_apply_on_ac_routes_only": False,
                    "gst_inclusive_pricing": False,
                },
            )

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

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any

from dotenv import load_dotenv


load_dotenv()


MONEY_QUANT = Decimal("0.01")
PERCENT_QUANT = Decimal("0.01")
TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
FALSE_ENV_VALUES = frozenset({"0", "false", "no", "off"})


def money(value: Decimal | int | str | None) -> Decimal:
    if value is None:
        value = Decimal("0.00")
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)


def percent(value: Decimal | int | str | None) -> Decimal:
    if value is None:
        value = Decimal("0.00")
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    return value.quantize(PERCENT_QUANT, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class GSTConfig:
    enabled: bool = True
    cgst_rate_percent: Decimal = Decimal("2.50")
    sgst_rate_percent: Decimal = Decimal("2.50")
    igst_rate_percent: Decimal = Decimal("0.00")
    apply_on_ac_routes_only: bool = True
    inclusive_pricing: bool = True


def _bool_from_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default

    normalized = raw.strip().lower()
    if normalized in TRUE_ENV_VALUES:
        return True
    if normalized in FALSE_ENV_VALUES:
        return False
    raise RuntimeError(
        f"{name} must be one of: 1, 0, true, false, yes, no, on, off."
    )


def _percent_from_env(name: str, default: Decimal) -> Decimal:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return percent(default)

    try:
        value = Decimal(raw.strip())
    except (InvalidOperation, ValueError) as exc:
        raise RuntimeError(f"{name} must be a decimal percentage.") from exc

    if not value.is_finite() or value < Decimal("0") or value > Decimal("100"):
        raise RuntimeError(f"{name} must be between 0 and 100.")
    return percent(value)


def gst_config_from_env() -> GSTConfig:
    """Return the fallback GST configuration loaded from the environment."""
    defaults = GSTConfig()
    return GSTConfig(
        enabled=_bool_from_env("GST_ENABLED", defaults.enabled),
        cgst_rate_percent=_percent_from_env(
            "GST_CGST_RATE_PERCENT", defaults.cgst_rate_percent
        ),
        sgst_rate_percent=_percent_from_env(
            "GST_SGST_RATE_PERCENT", defaults.sgst_rate_percent
        ),
        igst_rate_percent=_percent_from_env(
            "GST_IGST_RATE_PERCENT", defaults.igst_rate_percent
        ),
        apply_on_ac_routes_only=_bool_from_env(
            "GST_APPLY_ON_AC_ROUTES_ONLY", defaults.apply_on_ac_routes_only
        ),
        inclusive_pricing=_bool_from_env(
            "GST_INCLUSIVE_PRICING", defaults.inclusive_pricing
        ),
    )


def gst_settings_kwargs_from_env() -> dict[str, bool | Decimal]:
    """Build ORM column values for a new environment-seeded settings row."""
    config = gst_config_from_env()
    return {
        "gst_enabled": config.enabled,
        "gst_cgst_rate_percent": config.cgst_rate_percent,
        "gst_sgst_rate_percent": config.sgst_rate_percent,
        "gst_igst_rate_percent": config.igst_rate_percent,
        "gst_apply_on_ac_routes_only": config.apply_on_ac_routes_only,
        "gst_inclusive_pricing": config.inclusive_pricing,
    }


@dataclass(frozen=True)
class GSTBreakdown:
    gross_amount: Decimal
    taxable_amount: Decimal
    cgst_rate_percent: Decimal
    cgst_amount: Decimal
    sgst_rate_percent: Decimal
    sgst_amount: Decimal
    igst_rate_percent: Decimal
    igst_amount: Decimal
    total_tax_amount: Decimal
    total_rate_percent: Decimal
    divisor_used: Decimal
    rounding_adjustment: Decimal
    gst_enabled: bool
    gst_applicable: bool
    gst_inclusive: bool

    def multiplied(self, count: int) -> "GSTBreakdown":
        multiplier = Decimal(count)
        return GSTBreakdown(
            gross_amount=money(self.gross_amount * multiplier),
            taxable_amount=money(self.taxable_amount * multiplier),
            cgst_rate_percent=self.cgst_rate_percent,
            cgst_amount=money(self.cgst_amount * multiplier),
            sgst_rate_percent=self.sgst_rate_percent,
            sgst_amount=money(self.sgst_amount * multiplier),
            igst_rate_percent=self.igst_rate_percent,
            igst_amount=money(self.igst_amount * multiplier),
            total_tax_amount=money(self.total_tax_amount * multiplier),
            total_rate_percent=self.total_rate_percent,
            divisor_used=self.divisor_used,
            rounding_adjustment=money(self.rounding_adjustment * multiplier),
            gst_enabled=self.gst_enabled,
            gst_applicable=self.gst_applicable,
            gst_inclusive=self.gst_inclusive,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "gross_amount": self.gross_amount,
            "taxable_amount": self.taxable_amount,
            "cgst_rate_percent": self.cgst_rate_percent,
            "cgst_amount": self.cgst_amount,
            "sgst_rate_percent": self.sgst_rate_percent,
            "sgst_amount": self.sgst_amount,
            "igst_rate_percent": self.igst_rate_percent,
            "igst_amount": self.igst_amount,
            "total_tax_amount": self.total_tax_amount,
            "total_rate_percent": self.total_rate_percent,
            "divisor_used": self.divisor_used,
            "rounding_adjustment": self.rounding_adjustment,
            "gst_enabled": self.gst_enabled,
            "gst_applicable": self.gst_applicable,
            "gst_inclusive": self.gst_inclusive,
        }


def build_gst_breakdown(
    amount: Decimal | int | str,
    *,
    is_ac: bool | None,
    config: GSTConfig,
) -> GSTBreakdown:
    configured_amount = money(amount)
    cgst_rate = percent(config.cgst_rate_percent)
    sgst_rate = percent(config.sgst_rate_percent)
    igst_rate = percent(config.igst_rate_percent)
    total_rate = percent(cgst_rate + sgst_rate + igst_rate)

    gst_applicable = bool(config.enabled) and total_rate > Decimal("0.00")
    if config.apply_on_ac_routes_only and not bool(is_ac):
        gst_applicable = False

    if not gst_applicable:
        return GSTBreakdown(
            gross_amount=configured_amount,
            taxable_amount=configured_amount,
            cgst_rate_percent=Decimal("0.00"),
            cgst_amount=Decimal("0.00"),
            sgst_rate_percent=Decimal("0.00"),
            sgst_amount=Decimal("0.00"),
            igst_rate_percent=Decimal("0.00"),
            igst_amount=Decimal("0.00"),
            total_tax_amount=Decimal("0.00"),
            total_rate_percent=Decimal("0.00"),
            divisor_used=Decimal("1.00"),
            rounding_adjustment=Decimal("0.00"),
            gst_enabled=bool(config.enabled),
            gst_applicable=False,
            gst_inclusive=bool(config.inclusive_pricing),
        )

    divisor = Decimal("1.00") + (total_rate / Decimal("100"))
    if config.inclusive_pricing:
        gross_amount = configured_amount
        taxable_amount = money(gross_amount / divisor)
    else:
        taxable_amount = configured_amount
        gross_amount = money(
            taxable_amount + ((taxable_amount * total_rate) / Decimal("100"))
        )

    cgst_amount = money((taxable_amount * cgst_rate) / Decimal("100"))
    sgst_amount = money((taxable_amount * sgst_rate) / Decimal("100"))
    igst_amount = money((taxable_amount * igst_rate) / Decimal("100"))
    total_tax_amount = money(cgst_amount + sgst_amount + igst_amount)
    recomputed_gross_amount = money(taxable_amount + total_tax_amount)

    return GSTBreakdown(
        gross_amount=gross_amount,
        taxable_amount=taxable_amount,
        cgst_rate_percent=cgst_rate,
        cgst_amount=cgst_amount,
        sgst_rate_percent=sgst_rate,
        sgst_amount=sgst_amount,
        igst_rate_percent=igst_rate,
        igst_amount=igst_amount,
        total_tax_amount=total_tax_amount,
        total_rate_percent=total_rate,
        divisor_used=divisor.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP),
        rounding_adjustment=money(gross_amount - recomputed_gross_amount),
        gst_enabled=bool(config.enabled),
        gst_applicable=True,
        gst_inclusive=bool(config.inclusive_pricing),
    )


def gst_config_from_settings(settings: Any | None) -> GSTConfig:
    if settings is None:
        return gst_config_from_env()

    fallback = GSTConfig()

    return GSTConfig(
        enabled=bool(getattr(settings, "gst_enabled", fallback.enabled)),
        cgst_rate_percent=percent(
            getattr(
                settings,
                "gst_cgst_rate_percent",
                fallback.cgst_rate_percent,
            )
        ),
        sgst_rate_percent=percent(
            getattr(
                settings,
                "gst_sgst_rate_percent",
                fallback.sgst_rate_percent,
            )
        ),
        igst_rate_percent=percent(
            getattr(
                settings,
                "gst_igst_rate_percent",
                fallback.igst_rate_percent,
            )
        ),
        apply_on_ac_routes_only=bool(
            getattr(
                settings,
                "gst_apply_on_ac_routes_only",
                fallback.apply_on_ac_routes_only,
            )
        ),
        inclusive_pricing=bool(
            getattr(
                settings,
                "gst_inclusive_pricing",
                fallback.inclusive_pricing,
            )
        ),
    )

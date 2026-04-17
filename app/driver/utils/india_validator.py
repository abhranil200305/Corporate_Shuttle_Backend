#app/driver/utils/india_validator.py
import re


class IndiaDocumentFormatValidator:
    """
    Format-only validation for common Indian Driving Licence (DL) numbers
    and Vehicle Registration / RC numbers.
    """

    @staticmethod
    def is_valid_dl_number(value: str) -> bool:
        if not isinstance(value, str):
            return False

        raw = value.strip().upper()
        if not raw:
            return False

        # Normalize
        normalized = re.sub(r"[^A-Z0-9]", "", raw)

        # Modern DL pattern
        modern_pattern = re.compile(r"^[A-Z]{2}\d{2}\d{4}\d{7}$")
        if modern_pattern.fullmatch(normalized):
            return True

        # Legacy formats
        legacy_pattern = re.compile(
            r"^[A-Z]{2}[- ]?\d{2}(?:[-/ ]?[A-Z]+)?[-/ ]?\d{2,4}[-/ ]?\d{1,7}$"
        )

        return legacy_pattern.fullmatch(raw) is not None

    @staticmethod
    def is_valid_rc_number(value: str) -> bool:
        if not isinstance(value, str):
            return False

        raw = value.strip().upper()
        if not raw:
            return False

        # Normalize
        normalized = re.sub(r"[\s-]", "", raw)

        # Standard RC pattern
        standard_pattern = re.compile(r"^[A-Z]{2}\d{1,2}[A-Z]{0,3}\d{4}$")

        # BH series
        bh_pattern = re.compile(r"^\d{2}BH\d{4}[A-HJ-NP-Z]{1,2}$")

        return (
            standard_pattern.fullmatch(normalized) is not None
            or bh_pattern.fullmatch(normalized) is not None
        )
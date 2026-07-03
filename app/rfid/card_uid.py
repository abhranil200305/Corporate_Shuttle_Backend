from __future__ import annotations

from typing import Annotated, Any

from pydantic import BeforeValidator, Field

RFID_CARD_UID_MIN_LENGTH = 8
RFID_CARD_UID_MAX_LENGTH = 24


def _strip_card_uid(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip()
    return value


RFIDCardUID = Annotated[
    str,
    BeforeValidator(_strip_card_uid),
    Field(
        min_length=RFID_CARD_UID_MIN_LENGTH,
        max_length=RFID_CARD_UID_MAX_LENGTH,
    ),
]

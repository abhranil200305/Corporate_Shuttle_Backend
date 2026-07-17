from __future__ import annotations

from io import BytesIO

import qrcode
from qrcode.constants import ERROR_CORRECT_M


def generate_booking_qr_png(qr_token: str) -> bytes:
    qr = qrcode.QRCode(
        version=None,
        error_correction=ERROR_CORRECT_M,
        box_size=8,
        border=4,
    )
    qr.add_data(qr_token)
    qr.make(fit=True)
    image = qr.make_image(fill_color="black", back_color="white")

    output = BytesIO()
    image.save(output, format="PNG", optimize=True)
    return output.getvalue()

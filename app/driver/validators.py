import re
from fastapi import HTTPException
from datetime import datetime, timezone

# -----------------------------
# Aadhaar Validation
# -----------------------------
def validate_aadhaar(aadhaar: str) -> str:
    aadhaar = aadhaar.strip()

    if not re.fullmatch(r"\d{12}", aadhaar):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_aadhaar",
                "message": "Aadhaar must be exactly 12 digits.",
            },
        )

    return aadhaar


# -----------------------------
# PAN Validation
# -----------------------------
def validate_pan(pan: str) -> str:
    pan = pan.strip().upper()

    if not re.fullmatch(r"^[A-Z]{5}[0-9]{4}[A-Z]$", pan):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_pan",
                "message": "PAN must be in format ABCDE1234F.",
            },
        )

    return pan


# -----------------------------
# IFSC Validation (bonus, useful for bank verification)
# -----------------------------
def validate_ifsc(ifsc: str) -> str:
    ifsc = ifsc.strip().upper()

    if not re.fullmatch(r"^[A-Z]{4}0[A-Z0-9]{6}$", ifsc):
        raise HTTPException(
            status_code=400,
            detail={
                "error": "invalid_ifsc",
                "message": "Invalid IFSC code format.",
            },
        )

    return ifsc

# ---------------------------
# Vehicle Registration Number Validation
# ---------------------------
def validate_registration_number(reg_no: str) -> str:
    reg_no = reg_no.strip().upper()

    # Example India-style validation (you can adjust rules)
    pattern = r"^[A-Z]{2}[0-9]{2}[A-Z]{1,2}[0-9]{4}$"

    if not re.match(pattern, reg_no):
        raise HTTPException(
            status_code=400,
            detail="Invalid registration number format"
        )

    return reg_no



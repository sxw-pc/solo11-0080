"""DISPLAY character/zoned decimal and blank-policy decoding."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from ..model import Field
from .charset import CharsetDecodeFailure, decode_bytes
from .failure import FieldDecodeError
from .packed import decode_comp3

BLANK_POLICIES = ("null", "zero", "error")

def blank_value(field: Field, policy: str) -> Any:
    if policy == "null":
        return None
    if policy == "zero":
        return Decimal(0).scaleb(-field.scale) if field.is_numeric else ""
    raise FieldDecodeError("DE003", f"field {field.name} is blank and blank=error")

def _display_number(field: Field, text: str) -> Decimal:
    if field.signed:
        if not field.separate:
            raise FieldDecodeError("DE001", f"signed DISPLAY {field.name} uses unsupported overpunch")
        sign, digits = (text[:1], text[1:]) if field.sign == "LEADING" else (text[-1:], text[:-1])
        if sign not in {"+", "-"}:
            raise FieldDecodeError("DE001", f"signed DISPLAY {field.name} has invalid sign {sign!r}")
    else:
        sign, digits = "+", text
    if not digits or not digits.isascii() or not digits.isdigit():
        raise FieldDecodeError("DE001", f"field {field.name} contains non-digit DISPLAY data")
    value = Decimal(digits).scaleb(-field.scale)
    return value.copy_negate() if sign == "-" else value

def decode_field(field: Field, raw: bytes, charset: str, blank_policy: str) -> Any:
    if blank_policy not in BLANK_POLICIES:
        raise ValueError(f"invalid blank policy {blank_policy!r}")
    if field.usage == "COMP-3":
        return decode_comp3(raw, field.digits, field.scale)
    try:
        text = decode_bytes(raw, charset)
    except CharsetDecodeFailure as exc:
        raise FieldDecodeError("RD001", exc.message, {
            "byte_offset": exc.byte_offset, "relative_offset": exc.relative_offset,
        }) from exc
    if text.strip() == "":
        return blank_value(field, blank_policy)
    if field.is_numeric:
        return _display_number(field, text)
    return text.rstrip(" ")

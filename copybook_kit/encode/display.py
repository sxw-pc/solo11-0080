"""DISPLAY character and zoned-decimal encoding; inverse of decode.display."""

from decimal import Decimal

from ..model import Field
from .failure import FieldEncodeError
from .packed import scaled_integer

def encode_text(field: Field, value: str, charset: str) -> bytes:
    try:
        raw = value.encode(charset)
        padding = " ".encode(charset)
    except UnicodeEncodeError as exc:
        raise FieldEncodeError(f"{charset} cannot encode {field.name}: {exc}",
                               {"value": value}) from exc
    if len(raw) > field.length:
        raise FieldEncodeError(
            f"{field.name} value needs {len(raw)} bytes but PIC allows {field.length}",
            {"value": value, "length": field.length})
    return raw + padding * (field.length - len(raw))

def encode_display_number(field: Field, value: Decimal, charset: str) -> bytes:
    int_scaled = scaled_integer(value, field.scale, field.name)
    if int_scaled < 0 and not field.signed:
        raise FieldEncodeError(f"unsigned {field.name} cannot hold negative value {value}",
                               {"value": str(value)})
    digits = str(abs(int_scaled))
    if len(digits) > field.digits:
        raise FieldEncodeError(f"value {value} overflows {field.digits} digits of {field.name}",
                               {"value": str(value), "digits": field.digits})
    text = digits.zfill(field.digits)
    if field.signed:
        if not field.separate:
            raise FieldEncodeError(f"signed DISPLAY {field.name} uses unsupported overpunch")
        sign = "-" if int_scaled < 0 else "+"
        text = sign + text if field.sign == "LEADING" else text + sign
    try:
        return text.encode(charset)
    except UnicodeEncodeError as exc:  # digits and +/- are ASCII; defensive only
        raise FieldEncodeError(f"{charset} cannot encode {field.name}: {exc}",
                               {"value": text}) from exc

"""COMP-3 packed decimal encoding; the exact inverse of decode.packed."""

from decimal import Decimal

from .failure import FieldEncodeError

def scaled_integer(value: Decimal, scale: int, name: str) -> int:
    """Return ``value * 10**scale`` as an int, refusing non-declared fractions."""
    if not value.is_finite():
        raise FieldEncodeError(f"{name} value {value} is not a finite decimal",
                               {"value": str(value)})
    scaled = value.scaleb(scale)
    integral = scaled.to_integral_value()
    if scaled != integral:
        raise FieldEncodeError(
            f"{name} value {value} cannot be represented at declared scale {scale}",
            {"value": str(value), "scale": scale})
    return int(integral)

def encode_comp3(value: Decimal, digits: int, scale: int, signed: bool) -> bytes:
    int_scaled = scaled_integer(value, scale, "COMP-3 field")
    if int_scaled < 0 and not signed:
        raise FieldEncodeError(f"unsigned COMP-3 field cannot hold negative value {value}",
                               {"value": str(value)})
    text = str(abs(int_scaled))
    if len(text) > digits:
        raise FieldEncodeError(f"value {value} overflows {digits} COMP-3 digits",
                               {"value": str(value), "digits": digits})
    nibbles = [int(char) for char in text.zfill(digits)]
    nibbles.append(0x0D if int_scaled < 0 else (0x0C if signed else 0x0F))
    if len(nibbles) % 2:
        nibbles.insert(0, 0)
    return bytes((nibbles[index] << 4) | nibbles[index + 1]
                 for index in range(0, len(nibbles), 2))

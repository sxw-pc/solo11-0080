"""COMP-3 packed decimal decoding without binary floating point."""

from __future__ import annotations

from decimal import Decimal

from .failure import FieldDecodeError

POSITIVE_SIGNS = {0x0C, 0x0F}
NEGATIVE_SIGNS = {0x0D}

def packed_length(digits: int) -> int:
    return (digits + 2) // 2

def decode_comp3(data: bytes, digits: int, scale: int) -> Decimal:
    if not data:
        raise FieldDecodeError("DE002", "empty COMP-3 field")
    nibbles = [nibble for byte in data for nibble in (byte >> 4, byte & 0x0F)]
    sign, digit_nibbles = nibbles[-1], nibbles[:-1]
    if sign not in POSITIVE_SIGNS | NEGATIVE_SIGNS:
        raise FieldDecodeError("DE002", f"invalid COMP-3 sign nibble 0x{sign:X}", {"sign_nibble": f"{sign:X}"})
    if len(digit_nibbles) < digits:
        raise FieldDecodeError("DE002", f"COMP-3 field is shorter than PIC requires ({len(digit_nibbles)} < {digits})")
    padding = len(digit_nibbles) - digits
    if any(value != 0 for value in digit_nibbles[:padding]):
        raise FieldDecodeError("DE002", "non-zero padding nibble in COMP-3 field")
    if any(value > 9 for value in digit_nibbles[padding:]):
        raise FieldDecodeError("DE002", "non-decimal digit nibble in COMP-3 field")
    value = Decimal("".join(str(value) for value in digit_nibbles[padding:])).scaleb(-scale)
    return value.copy_negate() if sign in NEGATIVE_SIGNS else value

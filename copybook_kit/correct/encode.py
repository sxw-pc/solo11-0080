"""Encode approved values into contract byte representations (CR004/CR007).

The encoder is the exact inverse of :mod:`copybook_kit.decode`: DISPLAY characters
are charset-encoded and space-padded, DISPLAY numerics are zero-filled with an
optional separate sign, COMP-3 is packed with a canonical C/D sign nibble, and
amounts keep their declared scale through :class:`decimal.Decimal`.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from ..decode.display import blank_value, decode_field
from ..decode.failure import FieldDecodeError
from ..model import Field

class EncodeError(Exception):
    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.message, self.detail = message, detail or {}

def is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")

def decimal_value(value: Any, field: Field) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise EncodeError(f"value for {field.name} must be a string or integer, "
                          f"not {type(value).__name__}")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value.strip())
        except InvalidOperation as exc:
            raise EncodeError(f"value {value!r} for {field.name} is not numeric") from exc
    raise EncodeError(f"value for {field.name} must be a string or integer, "
                      f"not {type(value).__name__}")

def _scaled_integer(field: Field, value: Any) -> int:
    number = decimal_value(value, field)
    scaled = number.scaleb(field.scale)
    if scaled != scaled.to_integral_value():
        raise EncodeError(
            f"value {number} cannot be represented with the declared scale {field.scale}",
            {"value": str(number), "scale": field.scale})
    return int(scaled)

def _digits_text(field: Field, scaled: int) -> str:
    text = str(abs(scaled))
    if len(text) > field.digits:
        raise EncodeError(
            f"value overflows the {field.digits} declared digits of {field.name}",
            {"digits": field.digits, "needed": len(text)})
    return text.zfill(field.digits)

def _encode_comp3(field: Field, value: Any) -> bytes:
    scaled = _scaled_integer(field, value)
    nibbles = [int(char) for char in _digits_text(field, scaled)]
    nibbles.append(0x0D if scaled < 0 else 0x0C)
    if len(nibbles) % 2:
        nibbles.insert(0, 0)
    packed = bytes((nibbles[index] << 4) | nibbles[index + 1]
                   for index in range(0, len(nibbles), 2))
    if len(packed) != field.length:
        raise EncodeError(f"packed length {len(packed)} does not match the storage width "
                          f"{field.length} of {field.name}")
    return packed

def _encode_display_number(field: Field, value: Any, charset: str) -> bytes:
    scaled = _scaled_integer(field, value)
    if not field.signed and scaled < 0:
        raise EncodeError(f"unsigned field {field.name} cannot hold a negative value")
    body = _digits_text(field, scaled)
    if field.signed:
        if not field.separate:
            raise EncodeError(f"signed overpunch DISPLAY is not supported for {field.name}")
        sign = "-" if scaled < 0 else "+"
        text = sign + body if field.sign == "LEADING" else body + sign
    else:
        text = body
    try:
        return text.encode(charset)
    except UnicodeEncodeError as exc:
        raise EncodeError(f"{charset} cannot encode the value for {field.name}: {exc}") from exc

def encode_field(field: Field, value: Any, charset: str) -> bytes:
    """Return the canonical storage bytes for ``value`` under the field's PIC/usage."""
    if is_blank(value):
        if field.usage == "COMP-3":
            raise EncodeError(f"blank cannot be represented in COMP-3 field {field.name}")
        return b" " * field.length
    if field.usage == "COMP-3":
        return _encode_comp3(field, value)
    if field.is_numeric:
        return _encode_display_number(field, value, charset)
    if not isinstance(value, str):
        raise EncodeError(f"character field {field.name} expects a string value, "
                          f"not {type(value).__name__}")
    if value != value.rstrip(" "):
        raise EncodeError(f"trailing spaces cannot be represented in {field.name}; "
                          "padding is added automatically")
    try:
        encoded = value.encode(charset)
    except UnicodeEncodeError as exc:
        raise EncodeError(f"{charset} cannot encode the value for {field.name}: {exc}") from exc
    if len(encoded) > field.length:
        raise EncodeError(
            f"value needs {len(encoded)} bytes but {field.name} stores {field.length}",
            {"length": field.length, "needed": len(encoded)})
    return encoded.ljust(field.length, b" ")

def expected_value(field: Field, value: Any, blank_policy: str) -> Any:
    """Business value an approved manifest value must decode back to."""
    if is_blank(value):
        try:
            return blank_value(field, blank_policy)
        except FieldDecodeError as exc:
            raise EncodeError(f"blank is not allowed for {field.name} under the contract's "
                              f"blank={blank_policy} policy") from exc
    if field.usage == "COMP-3" or field.is_numeric:
        return decimal_value(value, field)
    return value

def proof_decode(field: Field, encoded: bytes, charset: str, blank_policy: str) -> Any:
    try:
        return decode_field(field, encoded, charset, blank_policy)
    except FieldDecodeError as exc:
        raise EncodeError(f"encoded bytes fail the contract decode: {exc.message}",
                          dict(exc.detail)) from exc

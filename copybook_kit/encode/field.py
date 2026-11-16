"""Encode one approved value into a field's declared storage bytes."""

from decimal import Decimal, InvalidOperation
from typing import Any

from ..model import Field
from .display import encode_display_number, encode_text
from .failure import FieldEncodeError
from .packed import encode_comp3

def parse_manifest_decimal(value: Any, name: str) -> Decimal:
    if not isinstance(value, str):
        raise FieldEncodeError(f"{name} value must be a JSON string", {"value": str(value)})
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise FieldEncodeError(f"{name} value {value!r} is not a decimal number",
                               {"value": value}) from exc

def encode_value(field: Field, value: Any, charset: str) -> bytes:
    """Encode a decoded-typed value (Decimal for numerics, str for characters)."""
    if field.usage == "COMP-3" or field.is_numeric:
        if not isinstance(value, Decimal):
            raise FieldEncodeError(f"{field.name} needs a decimal value", {"value": str(value)})
        if field.usage == "COMP-3":
            return encode_comp3(value, field.digits, field.scale, field.signed)
        return encode_display_number(field, value, charset)
    if not isinstance(value, str):
        raise FieldEncodeError(f"{field.name} needs a string value", {"value": str(value)})
    return encode_text(field, value, charset)

def encode_manifest_value(field: Field, value: Any, charset: str) -> bytes:
    """Encode a manifest-supplied JSON string into the field's storage."""
    if field.usage == "COMP-3" or field.is_numeric:
        return encode_value(field, parse_manifest_decimal(value, field.name), charset)
    return encode_value(field, value, charset)

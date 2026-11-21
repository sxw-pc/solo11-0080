"""Character-set decoding with byte-accurate failure positions."""

from __future__ import annotations

SUPPORTED_CHARSETS = ("utf-8", "latin-1", "cp037")
ALIASES = {"utf8": "utf-8", "latin1": "latin-1", "iso-8859-1": "latin-1", "ebcdic-cp-us": "cp037"}

class CharsetDecodeFailure(Exception):
    def __init__(self, message: str, byte_offset: int, relative_offset: int):
        super().__init__(message)
        self.message, self.byte_offset, self.relative_offset = message, byte_offset, relative_offset

def normalize_charset(value: str) -> str:
    normalized = ALIASES.get(value.strip().lower().replace("_", "-"), value.strip().lower().replace("_", "-"))
    if normalized not in SUPPORTED_CHARSETS:
        raise ValueError(f"unsupported charset {value!r}; choose one of {', '.join(SUPPORTED_CHARSETS)}")
    return normalized

def decode_bytes(data: bytes, charset: str, byte_offset: int = 0) -> str:
    try:
        return data.decode(charset, errors="strict")
    except UnicodeDecodeError as exc:
        raise CharsetDecodeFailure(
            f"{charset} cannot decode bytes at relative position {exc.start}",
            byte_offset=byte_offset + exc.start,
            relative_offset=exc.start,
        ) from exc

"""Encode-side failures; the correction layer maps them to CE004 with context."""

class FieldEncodeError(Exception):
    """Raised when a value cannot be stored in a field's declared bytes."""

    def __init__(self, message: str, detail: dict | None = None):
        super().__init__(message)
        self.message, self.detail = message, detail or {}

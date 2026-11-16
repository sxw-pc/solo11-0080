"""Stable error catalog. Expected failures never use a bare ``Exception``."""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any

CODE_CATALOG = {
    "CB001": "unsupported or malformed PIC", "CB002": "invalid OCCURS count",
    "CB003": "REDEFINES target not found", "CB004": "invalid level hierarchy",
    "CB005": "redefinition larger than its target", "CB006": "record length is zero",
    "CB007": "unsupported clause or clause syntax", "CB008": "invalid fixed-format source line",
    "CB009": "record length exceeds 32760 bytes", "RD001": "charset decode failure",
    "RD002": "truncated record at end of file", "RD003": "empty input file",
    "RD004": "max-records guard reached", "DE001": "invalid DISPLAY numeric field",
    "DE002": "invalid COMP-3 packed decimal", "DE003": "blank value is not allowed",
    "R001_REQUIRED": "required field is empty", "R002_ENUM": "value is outside the declared enum",
    "R003_RANGE": "value is outside the declared range", "R004_DATE": "value does not match date_format",
    "R005_PATTERN": "value does not match pattern", "R006_CHECK_DIGIT": "check digit is invalid",
    "R007_SCALE": "decimal scale exceeds the declared maximum", "R008_UNIQUE": "duplicate unique key",
    "R009_CROSS_FIELD": "cross-field comparison failed",
    "R010_RECORD_TYPE": "record type is not declared in the contract",
    "FL001": "trailer count mismatch or trailer missing", "FL002": "trailer amount mismatch",
    "FL003": "data record count below min_records", "FL004": "data record count above max_records",
    "FL005": "header placement is invalid", "RC001": "rules contract schema error",
    "RC002": "unsafe or invalid regular expression", "RC003": "invalid rule value",
    "RC004": "rule references an unknown field",
    "LD001": "an incomplete run with this run key already exists", "LD002": "run id not found",
    "LD003": "run key conflicts with an existing run", "IO001": "filesystem access failed",
    "CLI001": "invalid command-line argument",
    "CE001": "correction manifest is invalid",
    "CE002": "correction source run, input or record verification failed",
    "CE003": "correction leaf path cannot be uniquely located",
    "CE004": "correction value cannot be encoded into its field storage",
    "CE005": "corrections conflict on shared byte ranges",
    "CE006": "REDEFINES branch declaration is missing or inconsistent",
    "CE007": "correction review or publish failed",
}
FATAL_FILE_CODES = frozenset({"RD002", "RD003", "FL001", "FL002", "FL003", "FL004", "FL005"})

@dataclass
class Diagnostic:
    code: str
    message: str
    severity: str = "error"
    line: int | None = None
    column: int | None = None
    field: str | None = None
    byte_offset: int | None = None
    record_no: int | None = None
    detail: dict[str, Any] = dc_field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.code not in CODE_CATALOG:
            raise ValueError(f"unknown diagnostic code: {self.code}")
        if self.severity not in {"error", "warning", "info"}:
            raise ValueError(f"invalid diagnostic severity: {self.severity}")

    def to_dict(self) -> dict[str, Any]:
        payload = {"code": self.code, "message": self.message, "severity": self.severity}
        for name in ("line", "column", "field", "byte_offset", "record_no"):
            if (value := getattr(self, name)) is not None:
                payload[name] = value
        if self.detail:
            payload["detail"] = self.detail
        return payload

class CopybookKitError(Exception):
    code = "CLI001"

    def __init__(self, message: str, *, code: str | None = None, detail: dict | None = None):
        super().__init__(message)
        self.message, self.detail = message, detail or {}
        if code is not None:
            self.code = code
        if self.code not in CODE_CATALOG:
            raise ValueError(f"unknown diagnostic code: {self.code}")

    def diagnostic(self) -> Diagnostic:
        return Diagnostic(self.code, self.message, detail=self.detail)

    def to_dict(self) -> dict[str, Any]:
        return self.diagnostic().to_dict()

class CompileError(CopybookKitError):
    code = "CB007"

    def __init__(self, diagnostics: list[Diagnostic]):
        self.diagnostics = list(diagnostics)
        super().__init__("; ".join(f"{d.code}: {d.message}" for d in self.diagnostics) or "copybook failed")

    def to_dict(self) -> dict[str, Any]:
        return {"ok": False, "errors": [item.to_dict() for item in self.diagnostics]}

class RulesError(CopybookKitError):
    def __init__(self, message: str, *, code: str = "RC001"):
        super().__init__(message, code=code)

class LedgerError(CopybookKitError):
    code = "LD003"

class InputError(CopybookKitError):
    code = "IO001"

class CorrectionError(CopybookKitError):
    def __init__(self, message: str, *, code: str = "CE001", detail: dict | None = None):
        super().__init__(message, code=code, detail=detail)

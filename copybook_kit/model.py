"""Core data models shared by compilation, decoding, rules and runs."""

from __future__ import annotations

from dataclasses import dataclass, field as dc_field
from typing import Any, Iterator

from .errors import Diagnostic
from .util import json_safe, sha256_bytes

@dataclass
class Field:
    name: str; level: int
    pic: str | None = None; usage: str = "DISPLAY"; occurs: int = 1
    redefines: str | None = None; sign: str = "NONE"; separate: bool = False
    offset: int = 0; length: int = 0; total_length: int = 0
    digits: int = 0; scale: int = 0; category: str = "GROUP"; signed: bool = False
    children: list["Field"] = dc_field(default_factory=list)

    @property
    def is_group(self) -> bool:
        return self.category == "GROUP"

    @property
    def is_numeric(self) -> bool:
        return self.category == "NUMERIC"

    def to_dict(self) -> dict[str, Any]:
        keys = ("name", "level", "pic", "usage", "occurs", "redefines", "sign", "separate",
                "offset", "length", "total_length", "digits", "scale", "category")
        payload = {key: getattr(self, key) for key in keys}
        if self.children:
            payload["children"] = [child.to_dict() for child in self.children]
        return payload

@dataclass(frozen=True)
class LeafOccurrence:
    field: Field
    path: str
    offset: int
    length: int

def walk_leaves(fields: list[Field], prefix: str = "", base_shift: int = 0) -> Iterator[LeafOccurrence]:
    for item in fields:
        indexed = item.occurs > 1
        for index in range(item.occurs):
            path = f"{prefix}{item.name}{f'[{index}]' if indexed else ''}"
            shift = base_shift + index * item.length
            if item.is_group:
                yield from walk_leaves(item.children, f"{path}.", shift)
            else:
                yield LeafOccurrence(item, path, item.offset + shift, item.length)

def walk_groups(fields: list[Field]) -> Iterator[Field]:
    for item in fields:
        yield item
        yield from walk_groups(item.children)

@dataclass
class Layout:
    fields: list[Field]
    record_length: int
    errors: list[Diagnostic] = dc_field(default_factory=list)
    warnings: list[Diagnostic] = dc_field(default_factory=list)

    @property
    def valid(self) -> bool:
        return not self.errors and self.record_length > 0

    def to_dict(self) -> dict[str, Any]:
        return {"record_length": self.record_length, "fields": [item.to_dict() for item in self.fields],
                "warnings": [item.to_dict() for item in self.warnings],
                "errors": [item.to_dict() for item in self.errors]}

    def leaves(self) -> list[LeafOccurrence]:
        return list(walk_leaves(self.fields))

    def find_field(self, name: str) -> Field | None:
        for leaf in self.leaves():
            if leaf.field.name == name:
                return leaf.field
        return next((item for item in walk_groups(self.fields) if item.name == name), None)

    def elementary_names(self) -> set[str]:
        return {leaf.field.name for leaf in self.leaves()}

@dataclass(frozen=True)
class RawRecord:
    record_no: int
    byte_offset: int
    data: bytes

@dataclass
class Record:
    record_no: int
    byte_offset: int
    raw: bytes
    record_type: str | None = None
    values: dict[str, Any] = dc_field(default_factory=dict)
    flat: dict[str, Any] = dc_field(default_factory=dict)
    evidence: dict[str, str] = dc_field(default_factory=dict)
    errors: list[Diagnostic] = dc_field(default_factory=list)
    hits: list["RuleHit"] = dc_field(default_factory=list)

    @property
    def raw_sha256(self) -> str:
        return sha256_bytes(self.raw)

    @property
    def accepted(self) -> bool:
        return not self.errors and not self.hits

@dataclass(frozen=True)
class RuleHit:
    record_no: int
    code: str
    message: str
    scope: str
    field: str | None = None
    detail: dict[str, Any] = dc_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {key: getattr(self, key) for key in ("record_no", "code", "message", "scope")}
        if self.field is not None:
            payload["field"] = self.field
        if self.detail:
            payload["detail"] = json_safe(self.detail)
        return payload

@dataclass
class FileIssue:
    code: str
    message: str
    severity: str = "error"
    byte_offset: int | None = None
    record_no: int | None = None
    detail: dict[str, Any] = dc_field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = {"code": self.code, "message": self.message, "severity": self.severity}
        for name in ("byte_offset", "record_no"):
            if (value := getattr(self, name)) is not None:
                payload[name] = value
        if self.detail:
            payload["detail"] = json_safe(self.detail)
        return payload

@dataclass
class RunResult:
    run_id: str
    run_key: str
    status: str
    generated_at: str
    counts: dict[str, int]
    outputs: dict[str, str]
    file_issues: list[FileIssue] = dc_field(default_factory=list)
    reject_codes: dict[str, int] = dc_field(default_factory=dict)
    input_meta: dict[str, Any] = dc_field(default_factory=dict)
    contract_meta: dict[str, Any] = dc_field(default_factory=dict)
    parent_run_id: str | None = None
    exit_code: int = 0
    idempotent: bool = False

    def to_dict(self) -> dict[str, Any]:
        return json_safe({
            "schema_version": 1, "run_id": self.run_id, "run_key": self.run_key,
            "status": self.status, "generated_at": self.generated_at,
            "input": self.input_meta, "contracts": self.contract_meta,
            "counts": dict(sorted(self.counts.items())),
            "file_issues": [issue.to_dict() for issue in self.file_issues],
            "reject_codes": dict(sorted(self.reject_codes.items())),
            "outputs": dict(sorted(self.outputs.items())),
            "parent_run_id": self.parent_run_id, "exit_code": self.exit_code,
        })

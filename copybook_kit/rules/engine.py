"""Record/file rule orchestration on top of field checks."""

from __future__ import annotations

from decimal import Decimal

from ..model import FileIssue, Record, RuleHit
from ..util import decimal_text
from .checks import (ValidationState, cross_hit, first_value, scalar_text, unique_checks,
                     validate_field, values_for)
from .schema import RuleSet

def assign_record_type(record: Record, rules: RuleSet) -> None:
    if rules.record_type is None:
        record.record_type = None
        return
    value = first_value(record, rules.record_type.field)
    record.record_type = scalar_text(value) if value is not None else None

class RuleEngine:
    def __init__(self, rules: RuleSet) -> None:
        self.rules = rules

    def _declared_types(self) -> set[str]:
        if self.rules.record_type is None:
            return set()
        return {value for value in (self.rules.record_type.data, self.rules.record_type.header,
                                    self.rules.record_type.trailer) if value is not None}

    def validate_record(self, record: Record, state: ValidationState) -> list[RuleHit]:
        if self.rules.record_type is not None:
            if record.record_type not in self._declared_types():
                return [RuleHit(record.record_no, "R010_RECORD_TYPE",
                                f"undeclared record type {record.record_type!r}", "record")]
            if record.record_type != self.rules.record_type.data:
                return []
        hits: list[RuleHit] = []
        for rule in self.rules.fields.values():
            for path, value in values_for(record, rule.name):
                hits.extend(validate_field(record, path, value, rule))
        for rule in self.rules.record_rules:
            if rule.kind == "cross_field":
                hit = cross_hit(record, rule)
                if hit is not None:
                    hits.append(hit)
        return hits or unique_checks(record, self.rules, state)

    def _check_positions(self, records: list[Record]) -> list[FileIssue]:
        issues: list[FileIssue] = []
        if self.rules.record_type is None:
            return issues
        marker = self.rules.record_type
        if marker.header:
            if not records or records[0].record_type != marker.header:
                issues.append(FileIssue("FL005", f"first record is not the declared header {marker.header!r}",
                                        byte_offset=records[0].byte_offset if records else 0))
            for record in records[1:]:
                if record.record_type == marker.header:
                    issues.append(FileIssue("FL005", "header record appears after the first record",
                                            byte_offset=record.byte_offset, record_no=record.record_no))
        if marker.trailer:
            if not records or records[-1].record_type != marker.trailer:
                issues.append(FileIssue("FL001", f"last record is not the declared trailer {marker.trailer!r}",
                                        byte_offset=records[-1].byte_offset if records else 0))
            for record in records[:-1]:
                if record.record_type == marker.trailer:
                    issues.append(FileIssue("FL005", "trailer record appears before the last record",
                                            byte_offset=record.byte_offset, record_no=record.record_no))
        return issues

    def _check_trailer(self, records: list[Record], data_records: list[Record]) -> list[FileIssue]:
        if self.rules.trailer is None or self.rules.record_type is None or self.rules.record_type.trailer is None:
            return []
        trailers = [record for record in records if record.record_type == self.rules.record_type.trailer]
        trailer = trailers[-1] if trailers else None
        if trailer is None:
            return []
        issues: list[FileIssue] = []
        if self.rules.trailer.count_field:
            try:
                declared_count = int(scalar_text(first_value(trailer, self.rules.trailer.count_field)))
            except (TypeError, ValueError):
                declared_count = None
            if declared_count != len(data_records):
                issues.append(FileIssue("FL001", f"trailer count {declared_count!r} != {len(data_records)} "
                                        "data records", byte_offset=trailer.byte_offset, record_no=trailer.record_no))
        if self.rules.trailer.amount_field:
            total, missing = Decimal(0), False
            for record in data_records:
                value = first_value(record, self.rules.trailer.amount_field)
                if isinstance(value, Decimal):
                    total += value
                else:
                    missing = True
            declared = first_value(trailer, self.rules.trailer.amount_field)
            if missing or not isinstance(declared, Decimal) or total != declared:
                issues.append(FileIssue(
                    "FL002", f"trailer amount {scalar_text(declared)!r} != computed {decimal_text(total)}",
                    byte_offset=trailer.byte_offset, record_no=trailer.record_no,
                    detail={"computed": decimal_text(total), "missing_amount_values": missing}))
        return issues

    def validate_file(self, records: list[Record]) -> list[FileIssue]:
        rules = self.rules
        data_records = [record for record in records
                        if rules.record_type is None or record.record_type == rules.record_type.data]
        issues = self._check_positions(records)
        if rules.min_records is not None and len(data_records) < rules.min_records:
            issues.append(FileIssue("FL003", f"{len(data_records)} data records is below min_records "
                                    f"{rules.min_records}"))
        if rules.max_records is not None and len(data_records) > rules.max_records:
            issues.append(FileIssue("FL004", f"{len(data_records)} data records is above max_records "
                                    f"{rules.max_records}"))
        issues.extend(self._check_trailer(records, data_records))
        return issues

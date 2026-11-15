"""Fixed-width record splitting with precise byte offsets."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..model import FileIssue, RawRecord

@dataclass
class ReadResult:
    records: list[RawRecord] = field(default_factory=list)
    file_issues: list[FileIssue] = field(default_factory=list)
    total_bytes: int = 0
    trailing_bytes: bytes = b""
    full_record_count: int = 0

    @property
    def truncated(self) -> bool:
        return bool(self.trailing_bytes)

def read_records(data: bytes, record_length: int, *, max_records: int = 0,
                 recfm: str = "F") -> ReadResult:
    """Split on the layout length. F and FB are identical; max_records reads a prefix."""
    if record_length <= 0:
        raise ValueError("record_length must be positive")
    if recfm not in {"F", "FB"}:
        raise ValueError("recfm must be F or FB")
    if max_records < 0:
        raise ValueError("max_records must be non-negative")
    result = ReadResult(total_bytes=len(data))
    if not data:
        result.file_issues.append(FileIssue("RD003", "input file is empty", byte_offset=0))
        return result
    full_count = len(data) // record_length
    result.full_record_count = full_count
    process_count = min(full_count, max_records if max_records > 0 else full_count)
    result.records = [RawRecord(index + 1, index * record_length,
                                data[index * record_length:(index + 1) * record_length])
                      for index in range(process_count)]
    if max_records > 0 and full_count > max_records:
        result.file_issues.append(FileIssue(
            "RD004", f"max-records guard stopped after {max_records} records; "
            f"{full_count - max_records} complete records remain unread", severity="warning",
            byte_offset=max_records * record_length, record_no=max_records + 1,
            detail={"remaining_records": full_count - max_records}))
        return result
    remaining = data[full_count * record_length:]
    if remaining:
        result.trailing_bytes = remaining
        result.file_issues.append(FileIssue(
            "RD002", f"file ends with {len(remaining)} bytes, shorter than the "
            f"{record_length}-byte record length", byte_offset=full_count * record_length,
            record_no=full_count + 1, detail={"trailing_length": len(remaining)}))
    return result

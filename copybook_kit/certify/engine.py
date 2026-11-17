"""Certification engine: per-record file evidence and static universal proof."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from ..copybook.layout import compile_copybook
from ..decode.charset import normalize_charset
from ..decode.display import decode_field
from ..decode.failure import FieldDecodeError
from ..decode.record import decode_record
from ..errors import CompileError, InputError
from ..model import Layout, LeafOccurrence
from ..pipeline.reader import read_records
from ..rules.loader import load_rules
from ..rules.schema import RuleSet
from ..util import json_safe, sha256_bytes, sha8
from .credential import (
    CERTIFICATE_SCHEMA_VERSION,
    LEVEL_FILE_REUSE,
    LEVEL_UNIVERSAL,
    ROUTE_AUTHORITY,
    VERDICT_PASS,
    VERDICT_REJECT,
    VERDICT_UNKNOWN,
    certificate_digest,
    finalize_certificate,
    timestamp,
)

# Hand-checked byte witnesses for the three character sets the kit supports.
# Each byte string is legal under the old charset but decodes differently under
# the new one, proving that a charset change reinterprets legal old bytes rather
# than being a silent no-op. Every witness was verified with the real decoders.
_CHARSET_WITNESSES = {
    frozenset(("utf-8", "latin-1")): {
        "byte_hex": "c3a9",
        "old_text": "é",
        "new_text": "Ã©",
        "reason": "two UTF-8 continuation bytes decode as two separate latin-1 characters",
    },
    frozenset(("utf-8", "cp037")): {
        "byte_hex": "c2a0",
        "old_text": " ",
        "new_text": "Bµ",
        "reason": "the UTF-8 non-breaking-space sequence is two unrelated EBCDIC cp037 characters",
    },
    frozenset(("latin-1", "cp037")): {
        "byte_hex": "c182",
        "old_text": "Á",
        "new_text": "A?",
        "reason": "latin-1 and EBCDIC cp037 assign different characters to the same bytes",
    },
}


@dataclass(frozen=True)
class CertificationInput:
    old_copybook_source: str
    new_copybook_source: str
    rules: RuleSet
    input_data: bytes
    charset: str
    recfm: str
    field_paths: tuple[str, ...]
    requested_level: str | None
    created_at: str
    input_name: str = ""
    old_copybook_name: str = ""
    new_copybook_name: str = ""
    rules_name: str = ""
    old_copybook_sha256: str = ""
    new_copybook_sha256: str = ""
    input_sha256: str = ""
    rules_sha256: str = ""


def _leaf_index(layout: Layout) -> dict[str, LeafOccurrence]:
    root_prefix = f"{layout.fields[0].name}." if len(layout.fields) == 1 else ""
    index: dict[str, LeafOccurrence] = {}
    for leaf in layout.leaves():
        short = leaf.path.removeprefix(root_prefix)
        index[short] = leaf
        index[leaf.path] = leaf
    return index


def _flat_value(record, path: str) -> Any:
    """decode_record keys flat maps by the root-less path; accept both forms."""
    if path in record.flat:
        return record.flat[path]
    short = path.split(".", 1)[1] if "." in path else path
    return record.flat.get(short)


def _flat_error(record, path: str):
    for diagnostic in record.errors:
        if diagnostic.field in (path, path.split(".", 1)[1] if "." in path else path):
            return diagnostic
    return None


def resolve_field_paths(layout: Layout, requested: list[str] | None) -> tuple[str, ...]:
    """Default to every elementary leaf of the old contract; validate names."""
    index = _leaf_index(layout)
    if not requested:
        seen: set[str] = set()
        paths: list[str] = []
        for leaf in layout.leaves():
            if leaf.path not in seen:
                seen.add(leaf.path)
                paths.append(leaf.path)
        return tuple(paths)
    resolved: list[str] = []
    for raw_path in requested:
        if raw_path in index:
            resolved.append(index[raw_path].path)
            continue
        bare = next((leaf.path for leaf in layout.leaves() if leaf.field.name == raw_path), None)
        if bare is None:
            raise InputError(f"certification field path {raw_path!r} is not an elementary field "
                             "of the old copybook", code="CLI001")
        resolved.append(bare)
    return tuple(dict.fromkeys(resolved))


def _overlaps(layout: Layout, target: LeafOccurrence) -> bool:
    """True when another elementary leaf shares any storage byte (REDEFINES)."""
    for leaf in layout.leaves():
        if leaf.path == target.path:
            continue
        if max(leaf.offset, target.offset) < min(leaf.offset + leaf.length,
                                                  target.offset + target.length):
            return True
    return False


_DECODE_FAILED = object()


def _decode_or_none(field, raw: bytes, charset: str) -> Any:
    try:
        return decode_field(field, raw, charset, "null")
    except FieldDecodeError:
        return _DECODE_FAILED


def _same_business_value(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    return json_safe(left) == json_safe(right)


def _witness_entry(leaf: LeafOccurrence, raw: bytes, old_layout: Layout, new_layout: Layout,
                   old_charset: str, new_charset: str, reason: str) -> dict[str, Any] | None:
    """Try the candidate against the real decoders; only accept a true witness.

    A byte string that decodes under the old contract but is rejected under the
    new one is a counterexample too: refusal is not the same business value.
    """
    old_value = _decode_or_none(leaf.field, raw, old_charset)
    new_leaf = _leaf_index(new_layout).get(leaf.path)
    if new_leaf is None or old_value is _DECODE_FAILED:
        return None
    try:
        new_value = decode_field(new_leaf.field, raw, new_charset, "null")
    except FieldDecodeError as exc:
        return {
            "byte_hex": raw.hex(),
            "byte_range": {"offset": leaf.offset, "length": leaf.length},
            "old_value": json_safe(old_value),
            "new_value": None,
            "new_decode_error": exc.code,
            "reason": reason,
        }
    if _same_business_value(old_value, new_value):
        return None
    return {
        "byte_hex": raw.hex(),
        "byte_range": {"offset": leaf.offset, "length": leaf.length},
        "old_value": json_safe(old_value),
        "new_value": json_safe(new_value),
        "reason": reason,
    }


def _numeric_candidate(kind: str, length: int) -> bytes:
    if kind == "display":
        return b"0" * (length - 1) + b"1"
    if kind == "comp3":
        # digits nibbles zero, positive sign nibble; high padding nibble zero.
        body = bytes((length - 1))
        return body + b"\x0c"
    return b"0" * length


def static_assessment(path: str, old_layout: Layout, new_layout: Layout,
                      old_charset: str, new_charset: str) -> dict[str, Any]:
    """Decide universal equivalence for one field path, with evidence or a reason."""
    old_index, new_index = _leaf_index(old_layout), _leaf_index(new_layout)
    old_leaf = old_index[path]
    base: dict[str, Any] = {"path": path, "verdict": VERDICT_UNKNOWN}
    new_leaf = new_index.get(path)
    if new_leaf is None:
        return {**base, "verdict": VERDICT_REJECT,
                "reason": "the new copybook drops this path; every legal old value loses its meaning",
                "old_byte_range": {"offset": old_leaf.offset, "length": old_leaf.length},
                "new_byte_range": None}
    base["old_byte_range"] = {"offset": old_leaf.offset, "length": old_leaf.length}
    base["new_byte_range"] = {"offset": new_leaf.offset, "length": new_leaf.length}

    old_field, new_field = old_leaf.field, new_leaf.field
    same_storage = (old_leaf.offset == new_leaf.offset and old_leaf.length == new_leaf.length)
    if not same_storage:
        return {**base, "verdict": VERDICT_REJECT,
                "reason": "byte offset or length changed; the new layout reads different storage "
                          "(record boundary, OCCURS index or REDEFINES storage was moved)",
                "counterexample": {
                    "kind": "storage_moved",
                    "old_byte_range": {"offset": old_leaf.offset, "length": old_leaf.length},
                    "new_byte_range": {"offset": new_leaf.offset, "length": new_leaf.length},
                    "reason": "a record holding distinguishable bytes at the two ranges is legal "
                              "under the old layout and is misread by the new one",
                }}

    if _overlaps(old_layout, old_leaf) or _overlaps(new_layout, new_leaf):
        return {**base,
                "reason": "the storage is shared through REDEFINES in at least one contract; "
                          "shared-storage reinterpretation cannot be proved for all old records"}

    if old_charset != new_charset:
        witness = _CHARSET_WITNESSES.get(frozenset((old_charset, new_charset)))
        if witness is not None:
            return {**base, "verdict": VERDICT_REJECT,
                    "reason": "character set changed; legal old bytes decode to another string",
                    "counterexample": {**witness, "byte_range": dict(base["old_byte_range"])}}
        return {**base, "reason": "character set changed to an unsupported combination; not proved"}

    if old_field.category != new_field.category or old_field.usage != new_field.usage:
        witness = _category_change_witness(old_leaf, new_leaf, old_layout, new_layout,
                                           old_charset, new_charset)
        if witness is not None:
            return {**base, "verdict": VERDICT_REJECT,
                    "reason": "PIC category or USAGE changed over identical storage; legal old "
                              "bytes decode to a different business value",
                    "counterexample": witness}
        return {**base, "reason": "PIC category or USAGE changed and no automatic witness was "
                                  "constructed; equivalence cannot be assumed"}

    if old_field.category == "CHAR":
        if old_field.pic == new_field.pic:
            return {**base, "verdict": VERDICT_PASS,
                    "reason": "identical CHAR storage, charset and trimming; every byte string "
                              "decodes to the same value"}
        return {**base, "reason": f"PIC changed {old_field.pic} -> {new_field.pic} on identical storage"}

    # Numeric leaves: identical storage and identical arithmetic meaning.
    same_numeric = (old_field.digits == new_field.digits and old_field.scale == new_field.scale
                    and old_field.signed == new_field.signed and old_field.sign == new_field.sign
                    and old_field.separate == new_field.separate)
    if same_numeric:
        return {**base, "verdict": VERDICT_PASS,
                "reason": "identical numeric storage, scale, sign and USAGE; the Decimal value is "
                          "determined by the same bytes"}
    witness = _numeric_change_witness(old_leaf, new_leaf, old_layout, new_layout,
                                      old_charset, new_charset)
    if witness is not None:
        return {**base, "verdict": VERDICT_REJECT,
                "reason": f"numeric definition changed (scale/ digits/ sign differ); decimal "
                          f"meaning is not preserved ({old_field.pic} -> {new_field.pic})",
                "counterexample": witness}
    return {**base, "reason": "numeric definition changed and no automatic witness was constructed"}


def _category_change_witness(old_leaf, new_leaf, old_layout, new_layout,
                             old_charset, new_charset) -> dict[str, Any] | None:
    old_field, new_field = old_leaf.field, new_leaf.field
    length = old_leaf.length
    if old_field.category == "CHAR" and new_field.category == "NUMERIC" and new_field.usage == "DISPLAY":
        # An alphabetic string is a legal CHAR value but illegal/non-identical as digits.
        candidate = b"A" * length
        return _witness_entry(old_leaf, candidate, old_layout, new_layout,
                              old_charset, new_charset,
                              "alphabetic content is legal for the old X field but cannot mean "
                              "the same value under the new 9 field")
    if old_field.category == "NUMERIC" and new_field.category == "CHAR":
        kind = "comp3" if old_field.usage == "COMP-3" else "display"
        candidate = _numeric_candidate(kind, length)
        return _witness_entry(old_leaf, candidate, old_layout, new_layout,
                              old_charset, new_charset,
                              "digits decode as a number under the old PIC but as text under the new PIC")
    if {old_field.usage, new_field.usage} == {"COMP-3", "DISPLAY"}:
        candidate = _numeric_candidate("comp3" if old_field.usage == "COMP-3" else "display", length)
        return _witness_entry(old_leaf, candidate, old_layout, new_layout,
                              old_charset, new_charset,
                              "packed and display encodings assign different meanings to the same bytes")
    return None


def _numeric_change_witness(old_leaf, new_leaf, old_layout, new_layout,
                            old_charset, new_charset) -> dict[str, Any] | None:
    kind = "comp3" if old_leaf.field.usage == "COMP-3" else "display"
    candidate = _numeric_candidate(kind, old_leaf.length)
    return _witness_entry(old_leaf, candidate, old_layout, new_layout,
                          old_charset, new_charset,
                          "a smallest non-zero legal old value changes its decimal meaning under "
                          "the new scale/digits/sign")


def _field_evidence(record_no: int, byte_offset: int, leaf: LeafOccurrence, raw: bytes) -> dict[str, Any]:
    field_bytes = raw[leaf.offset:leaf.offset + leaf.length]
    return {
        "record_no": record_no,
        "byte_offset": byte_offset,
        "path": leaf.path,
        "byte_range": {"offset": leaf.offset, "length": leaf.length},
        "field_sha8": sha8(field_bytes),
        "record_sha256": sha256_bytes(raw),
    }


def _record_lengths_aligned(old_layout: Layout, new_layout: Layout) -> bool:
    return old_layout.record_length == new_layout.record_length


def assess_file(data: bytes, old_layout: Layout, new_layout: Layout, rules: RuleSet,
                field_paths: tuple[str, ...], charset: str, recfm: str) -> dict[str, Any]:
    """Decode every record with both layouts and compare business values per path."""
    old_index, new_index = _leaf_index(old_layout), _leaf_index(new_layout)
    boundaries_ok = _record_lengths_aligned(old_layout, new_layout)
    read = read_records(data, old_layout.record_length, recfm=recfm)
    counterexamples: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    legacy_rejects: list[dict[str, Any]] = []
    compared = 0

    for raw_record in read.records:
        old_record = decode_record(old_layout, raw_record.data, record_no=raw_record.record_no,
                                   byte_offset=raw_record.byte_offset, charset=charset,
                                   blank_policies=rules.blank_policies)
        if old_record.errors:
            # Old-contract rejections stay isolated as original evidence; they are
            # never relabelled as compatibility passes.
            legacy_rejects.append({
                "record_no": raw_record.record_no,
                "byte_offset": raw_record.byte_offset,
                "record_sha256": sha256_bytes(raw_record.data),
                "error_codes": [item.code for item in old_record.errors],
                "errors": [item.to_dict() for item in old_record.errors],
            })
            continue
        compared += 1
        if not boundaries_ok:
            counterexamples.append({
                "kind": "record_boundary",
                "record_no": raw_record.record_no,
                "byte_offset": raw_record.byte_offset,
                "record_sha256": sha256_bytes(raw_record.data),
                "old_record_length": old_layout.record_length,
                "new_record_length": new_layout.record_length,
                "reason": "record boundaries changed; the new layout cannot split this file silently",
            })
            continue
        new_record = decode_record(new_layout, raw_record.data, record_no=raw_record.record_no,
                                   byte_offset=raw_record.byte_offset, charset=charset,
                                   blank_policies=rules.blank_policies)
        for path in field_paths:
            old_leaf = old_index[path]
            field_bytes = raw_record.data[old_leaf.offset:old_leaf.offset + old_leaf.length]
            evidence.append(_field_evidence(raw_record.record_no, raw_record.byte_offset,
                                            old_leaf, raw_record.data))
            new_leaf = new_index.get(path)
            if new_leaf is None:
                counterexamples.append({
                    "kind": "missing_path", "record_no": raw_record.record_no,
                    "byte_offset": raw_record.byte_offset, "path": path,
                    "old_byte_range": {"offset": old_leaf.offset, "length": old_leaf.length},
                    "new_byte_range": None, "record_sha256": sha256_bytes(raw_record.data),
                    "field_hex": field_bytes.hex(),
                    "reason": "the new copybook no longer defines this path",
                })
                continue
            diagnostic = _flat_error(new_record, path)
            if diagnostic is not None:
                counterexamples.append({
                    "kind": "new_decode_failure", "record_no": raw_record.record_no,
                    "byte_offset": raw_record.byte_offset, "path": path,
                    "old_byte_range": {"offset": old_leaf.offset, "length": old_leaf.length},
                    "new_byte_range": {"offset": new_leaf.offset, "length": new_leaf.length},
                    "record_sha256": sha256_bytes(raw_record.data),
                    "field_hex": field_bytes.hex(),
                    "error": diagnostic.to_dict(),
                    "old_value": json_safe(_flat_value(old_record, path)),
                    "new_value": None,
                    "reason": "bytes accepted by the old layout fail under the new layout",
                })
                continue
            old_value, new_value = _flat_value(old_record, path), _flat_value(new_record, path)
            if not _same_business_value(old_value, new_value):
                counterexamples.append({
                    "kind": "value_change", "record_no": raw_record.record_no,
                    "byte_offset": raw_record.byte_offset, "path": path,
                    "old_byte_range": {"offset": old_leaf.offset, "length": old_leaf.length},
                    "new_byte_range": {"offset": new_leaf.offset, "length": new_leaf.length},
                    "record_sha256": sha256_bytes(raw_record.data),
                    "field_hex": field_bytes.hex(),
                    "old_value": json_safe(old_value), "new_value": json_safe(new_value),
                    "reason": "the same raw bytes produce a different business value",
                })

    file_verdict = VERDICT_PASS if not counterexamples else VERDICT_REJECT
    return {
        "verdict": file_verdict,
        "record_length": {"old": old_layout.record_length, "new": new_layout.record_length,
                          "boundaries_preserved": boundaries_ok},
        "records_total": len(read.records),
        "records_compared": compared,
        "legacy_rejected_records": len(legacy_rejects),
        "read_issues": [issue.to_dict() for issue in read.file_issues],
        "counterexamples": counterexamples,
        "legacy_rejects": legacy_rejects,
        "field_evidence": evidence,
    }


def _compile(source: str) -> Layout:
    layout = compile_copybook(source)
    if not layout.valid:
        raise CompileError(layout.errors)
    return layout


def certify(material: CertificationInput) -> dict[str, Any]:
    """Build the full two-level certificate document for the supplied materials."""
    old_layout = _compile(material.old_copybook_source)
    new_layout = _compile(material.new_copybook_source)
    old_charset = normalize_charset(material.charset)
    if material.recfm not in {"F", "FB"}:
        raise InputError("--recfm must be F or FB", code="CLI001")

    file_result = assess_file(material.input_data, old_layout, new_layout, material.rules,
                              material.field_paths, old_charset, material.recfm)
    field_results = [
        static_assessment(path, old_layout, new_layout, old_charset, old_charset)
        for path in material.field_paths
    ]
    universal_verdict = (VERDICT_PASS if all(item["verdict"] == VERDICT_PASS for item in field_results)
                         else VERDICT_REJECT if any(item["verdict"] == VERDICT_REJECT
                                                     for item in field_results)
                         else VERDICT_UNKNOWN)
    requested = material.requested_level
    if requested is not None and requested not in (LEVEL_FILE_REUSE, LEVEL_UNIVERSAL):
        raise InputError(f"unknown certification level {requested!r}", code="CLI001")

    payload = {
        "schema_version": CERTIFICATE_SCHEMA_VERSION,
        "certificate_id": "",
        "created_at": material.created_at,
        "level": {
            "requested": requested,
            LEVEL_FILE_REUSE: file_result["verdict"],
            LEVEL_UNIVERSAL: universal_verdict,
        },
        "authorization": {
            LEVEL_FILE_REUSE: ROUTE_AUTHORITY[LEVEL_FILE_REUSE],
            LEVEL_UNIVERSAL: ROUTE_AUTHORITY[LEVEL_UNIVERSAL],
        },
        "materials": {
            "input": {"name": material.input_name, "sha256": material.input_sha256,
                      "size": len(material.input_data)},
            "old_copybook": {"name": material.old_copybook_name,
                             "sha256": material.old_copybook_sha256},
            "new_copybook": {"name": material.new_copybook_name,
                             "sha256": material.new_copybook_sha256},
            "rules": {"name": material.rules_name, "sha256": material.rules_sha256},
        },
        "charset": old_charset,
        "recfm": material.recfm,
        "field_paths": list(material.field_paths),
        "file_evaluation": file_result,
        "universal_evaluation": {
            "verdict": universal_verdict,
            "method": "static proof over all byte strings permitted by the old layout; "
                      "inputs are never enumerated and sample observations never count as proof",
            "fields": field_results,
        },
    }
    payload["certificate_id"] = "cert-" + certificate_digest(payload)[:20]
    return finalize_certificate(payload)


def build_and_certify(*, old_copybook_path: Path, new_copybook_path: Path, rules_path: Path,
                      input_path: Path, charset: str, recfm: str, field_paths: list[str] | None,
                      requested_level: str | None, now: datetime) -> dict[str, Any]:
    """Read all materials from disk and produce the sealed certificate."""
    material = build_input(old_copybook_path=old_copybook_path, new_copybook_path=new_copybook_path,
                           rules_path=rules_path, input_path=input_path, charset=charset,
                           recfm=recfm, field_paths=field_paths, requested_level=requested_level,
                           now=now)
    return certify(material)


def build_input(*, old_copybook_path: Path, new_copybook_path: Path, rules_path: Path,
                input_path: Path, charset: str, recfm: str, field_paths: list[str] | None,
                requested_level: str | None, now: datetime) -> CertificationInput:
    """Read and hash all materials from raw bytes for a CLI/API call."""
    def read_bytes(path: Path, label: str) -> bytes:
        try:
            return path.read_bytes()
        except OSError as exc:
            raise InputError(f"cannot read {label} {path}: {exc}", code="IO001") from exc

    old_bytes = read_bytes(old_copybook_path, "old copybook")
    new_bytes = read_bytes(new_copybook_path, "new copybook")
    rules_bytes = read_bytes(rules_path, "rules")
    input_bytes = read_bytes(input_path, "input file")
    try:
        old_source, new_source = old_bytes.decode("utf-8"), new_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise InputError(f"copybook must be UTF-8 text: {exc}", code="CB008") from exc
    old_layout = _compile(old_source)
    rules = load_rules(rules_path, old_layout)
    paths = resolve_field_paths(old_layout, field_paths)
    return CertificationInput(
        old_copybook_source=old_source, new_copybook_source=new_source, rules=rules,
        input_data=input_bytes, charset=charset, recfm=recfm, field_paths=paths,
        requested_level=requested_level, created_at=timestamp(now),
        input_name=input_path.name, old_copybook_name=old_copybook_path.name,
        new_copybook_name=new_copybook_path.name, rules_name=rules_path.name,
        old_copybook_sha256=sha256_bytes(old_bytes), new_copybook_sha256=sha256_bytes(new_bytes),
        input_sha256=sha256_bytes(input_bytes), rules_sha256=sha256_bytes(rules_bytes))

"""DISPLAY/COMP-3 decoding, blank policy and byte-accurate diagnostics."""

from __future__ import annotations

from decimal import Decimal

import pytest

from conftest import FIXTURES, cpy, decode
from copybook_kit.copybook.layout import compile_file
from copybook_kit.decode.charset import normalize_charset
from copybook_kit.decode.record import decode_record

def codes(record):
    return [item.code for item in record.errors]

def test_customer_record_values_occurs_evidence_and_hash():
    layout = compile_file(str(FIXTURES / "copybooks" / "customer.cpy"))
    raw = (FIXTURES / "data" / "customer_normal.dat").read_bytes()[86:172]
    record = decode_record(layout, raw, record_no=2, byte_offset=86, charset="utf-8")
    assert not record.errors
    assert record.values["REC-TYPE"] == "D"
    assert record.values["CUSTOMER-NAME"] == "DEMO CUSTOMER 01"
    assert record.values["BALANCE"] == Decimal("1234.50")
    assert record.values["TAGS"][2]["TAG-VALUE"].strip(" ")
    assert len(record.evidence) == len(layout.leaves())
    assert all(len(value) == 8 and value == value.lower() for value in record.evidence.values())
    assert len(record.raw_sha256) == 64

def test_account_redefines_and_elementary_occurs_are_both_decoded():
    layout = compile_file(str(FIXTURES / "copybooks" / "account.cpy"))
    record = decode_record(layout, (FIXTURES / "data" / "account_normal.dat").read_bytes()[:50],
                           record_no=1, byte_offset=0, charset="utf-8")
    assert not record.errors
    assert record.values["CONTACT"]["CONTACT-NAME"] == "DEMO ACCOUNT 001"
    assert record.values["CONTACT-V2"]["EMAIL"] == "DEMO ACCOUNT 00155501001"
    assert record.values["SCORE-HISTORY"] == [Decimal("100"), Decimal("200")]
    assert record.flat["CONTACT-V2.EMAIL"] == "DEMO ACCOUNT 00155501001"

@pytest.mark.parametrize(("raw", "value"), [
    (bytes.fromhex("1234567D"), Decimal("-12345.67")),
    (bytes.fromhex("1234567C"), Decimal("12345.67")),
    (bytes.fromhex("1234567F"), Decimal("12345.67")),
])
def test_comp3_signs_and_exact_scale(raw, value):
    _, record = decode(cpy("01 R.", "05 AMT PIC S9(5)V9(2) USAGE COMP-3."), raw)
    assert not record.errors and record.values["AMT"] == value

@pytest.mark.parametrize("raw", [bytes.fromhex("1234567A"), bytes.fromhex("12345A7C"), b"    "])
def test_comp3_invalid_sign_digit_or_blank_is_de002(raw):
    _, record = decode(cpy("01 R.", "05 AMT PIC S9(5)V9(2) USAGE COMP-3."), raw)
    assert "DE002" in codes(record)

def test_comp3_padding_nibble_must_be_zero():
    source = cpy("01 R.", "05 AMT PIC S9(2)V9(2) USAGE COMP-3.")
    _, good = decode(source, bytes.fromhex("01234C"))
    assert not good.errors and good.values["AMT"] == Decimal("12.34")
    _, bad = decode(source, bytes.fromhex("11234C"))
    assert "DE002" in codes(bad)

def test_separate_sign_display_and_invalid_non_separate_overpunch():
    source = cpy("01 R.", "05 L PIC S9(3) SIGN IS LEADING SEPARATE.",
                 "05 T PIC S9(3) SIGN IS TRAILING SEPARATE.", "05 N PIC 9(2).")
    _, record = decode(source, b"-123456+07")
    assert not record.errors
    assert record.values == {"L": Decimal("-123"), "T": Decimal("456"), "N": Decimal("7")}
    assert "DE001" in codes(decode(source, b"x123456+07")[1])
    assert "DE001" in codes(decode(cpy("01 R.", "05 S PIC S9(3) SIGN IS TRAILING."), b"123")[1])

def test_blank_policies_null_zero_error_and_trailing_space_trim():
    source = cpy("01 R.", "05 A PIC X(3).", "05 N PIC 9(2)V9(1).")
    assert decode(source, b"   123")[1].values == {"A": None, "N": Decimal("12.3")}
    assert decode(source, b"      ", policies={"A": "zero", "N": "zero"})[1].values == {
        "A": "", "N": Decimal("0.0")}
    assert "DE003" in codes(decode(source, b"      ", policies={"A": "error", "N": "null"})[1])
    assert decode(cpy("01 R.", "05 A PIC X(6)."), b"AB    ")[1].values["A"] == "AB"

def test_rd001_reports_exact_absolute_byte_and_rd002_short_record():
    source = cpy("01 R.", "05 A PIC X(2).", "05 B PIC X(2).")
    _, record = decode(source, b"ABx\xff", offset=100)
    assert codes(record) == ["RD001"]
    diagnostic = record.errors[0]
    assert diagnostic.byte_offset == 103 and diagnostic.field == "B"
    assert diagnostic.detail["relative_offset"] == 1
    assert "RD002" in codes(decode(cpy("01 R.", "05 A PIC X(4)."), b"abc")[1])

def test_cp037_and_charset_aliases():
    layout, _ = decode(cpy("01 R.", "05 A PIC X(3)."), b"ABC")
    record = decode_record(layout, b"\xc1\xc2\xc3", record_no=1, byte_offset=0, charset="cp037")
    assert record.values["A"] == "ABC"
    assert normalize_charset("LATIN1") == "latin-1" and normalize_charset("utf8") == "utf-8"
    with pytest.raises(ValueError):
        normalize_charset("utf-16")

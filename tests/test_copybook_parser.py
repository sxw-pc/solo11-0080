"""Fixed-format lexing, PIC subset and explicit rejection diagnostics."""

from __future__ import annotations

import pytest

from conftest import cpy
from copybook_kit.copybook.layout import compile_copybook

def codes(source: str) -> set[str]:
    return {item.code for item in compile_copybook(source).errors}

def test_comments_sequence_area_continuation_and_columns_73_plus():
    ignored = "       05 C PIC X(4).".ljust(72) + "THIS IS BEYOND COLUMN 72"
    source = ("      * SYNTHETIC COMMENT\n      / PAGE EJECT\n       01 R.\n       05 A\n"
              "      -       PIC X(2).\n       05 B PIC X(3).\n" + ignored + "\n")
    layout = compile_copybook(source)
    assert layout.valid, layout.to_dict()
    assert [field.name for field in layout.fields[0].children] == ["A", "B", "C"]
    assert layout.record_length == 9

@pytest.mark.parametrize(("statement", "expected"), [
    ("05 A PIC 9(3)V99.", "CB001"), ("05 A PIC X(0).", "CB001"),
    ("05 A PIC X(2) OCCURS 0 TIMES.", "CB002"),
    ("05 A PIC X(2) OCCURS NOPE TIMES.", "CB002"),
    ("05 A PIC X(2).\n       05 B PIC X(2) REDEFINES NOPE.", "CB003"),
    ("05 A PIC X(2).\n       05 B PIC X(3) REDEFINES A.", "CB005"),
    ("05 G.", "CB006"), ("05 A PIC X(2) COPY OTHER.", "CB007"),
    ("05 A PIC X(2) USAGE COMP-1.", "CB007"),
    ("05 A PIC X(2) OCCURS 2 DEPENDING ON B.", "CB007"),
    ("05 A PIC X(2).\n       05 B PIC X(2) SIGN IS LEADING.", "CB007"),
    ("05 A PIC 9(2) SIGN IS TRAILING SEPARATE USAGE COMP-3.", "CB007"),
])
def test_stable_rejection_codes(statement, expected):
    assert expected in codes(cpy("01 R.", *statement.splitlines()))

def test_hierarchy_and_fixed_format_failures():
    assert "CB004" in codes(cpy("05 A PIC X(2)."))
    assert "CB004" in codes(cpy("01 R.", "01 S.")) and "CB004" in codes(cpy("1 R."))
    assert "CB008" in codes("01 R.\n") and "CB008" in codes(cpy("01 R.", "05\tA PIC X(1)."))
    assert "CB008" in codes("      X 05 A PIC X(1).\n")
    assert "CB008" in codes("      -       05 A PIC X(1).\n")

def test_record_length_limit():
    layout = compile_copybook(cpy("01 R.", "05 A PIC X(32761)."))
    assert "CB009" in {item.code for item in layout.errors} and layout.record_length == 32761

def test_supported_pic_sign_occurs_and_comp3_layout():
    layout = compile_copybook(cpy(
        "01 R.", "05 A PIC X(3).", "05 B PIC 9(2)V9(1).",
        "05 C PIC S9(2) SIGN IS TRAILING SEPARATE.", "05 D PIC S9(3) USAGE COMP-3.",
        "05 E OCCURS 2 TIMES PIC 9(2)."))
    assert layout.valid, layout.to_dict()
    fields = {leaf.field.name: leaf.field for leaf in layout.leaves()}
    assert fields["C"].sign == "TRAILING" and fields["C"].separate
    assert fields["D"].usage == "COMP-3" and fields["D"].length == 2
    assert layout.record_length == 3 + 3 + 3 + 2 + 4

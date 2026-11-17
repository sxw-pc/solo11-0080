"""Byte layout, OCCURS offsets and REDEFINES overlap."""

from __future__ import annotations

from conftest import FIXTURES, cpy
from copybook_kit.copybook.layout import compile_copybook, compile_file

def leaf_map(layout):
    prefix = layout.fields[0].name + "."
    return {leaf.path.removeprefix(prefix): leaf for leaf in layout.leaves()}

def test_customer_fixture_offsets_and_length():
    layout = compile_file(str(FIXTURES / "copybooks" / "customer.cpy"))
    assert layout.valid and layout.record_length == 86
    leaves = leaf_map(layout)
    assert (leaves["REC-TYPE"].offset, leaves["REC-TYPE"].length) == (0, 1)
    assert (leaves["BALANCE"].offset, leaves["BALANCE"].length) == (41, 5)
    assert (leaves["TAGS[0].TAG-VALUE"].offset, leaves["TAGS[2].TAG-VALUE"].offset) == (48, 64)
    assert leaves["TAGS[1].TAG-VALUE"].offset == 56
    tags = next(item for item in layout.fields[0].children if item.name == "TAGS")
    assert tags.length == 8 and tags.total_length == 24 and tags.occurs == 3
    assert layout.elementary_names() >= {"BALANCE", "TAG-VALUE", "CONTROL-NO"}
    assert layout.find_field("TAGS").is_group and layout.find_field("BALANCE").scale == 2

def test_account_redefines_and_occurs_fixture():
    layout = compile_file(str(FIXTURES / "copybooks" / "account.cpy"))
    assert layout.valid and layout.record_length == 50
    leaves = leaf_map(layout)
    assert leaves["ACCOUNT-KEY.REGION"].offset == 1
    assert leaves["ACCOUNT-KEY.ACCOUNT-NO"].offset == 3
    assert leaves["CONTACT.CONTACT-NAME"].offset == 11
    assert leaves["CONTACT-V2.EMAIL"].offset == 11
    assert leaves["AMOUNT"].offset == 38 and leaves["AMOUNT"].length == 4
    assert [leaves[f"SCORE-HISTORY[{index}]"].offset for index in range(2)] == [42, 46]
    contact, contact_v2 = layout.find_field("CONTACT"), layout.find_field("CONTACT-V2")
    assert contact.offset == contact_v2.offset == 11
    assert contact.total_length == contact_v2.total_length == 24

def test_balance_fixture_comp3_and_display_scale():
    layout = compile_file(str(FIXTURES / "copybooks" / "balance.cpy"))
    assert layout.valid and layout.record_length == 64
    leaves = leaf_map(layout)
    assert (leaves["AMOUNT"].offset, leaves["AMOUNT"].length) == (13, 6)
    assert (leaves["RATE"].offset, leaves["RATE"].length) == (19, 7)
    assert leaves["RATE"].field.scale == 4
    assert leaves["AMOUNT"].field.to_dict()["usage"] == "COMP-3"

def test_redefine_must_fit_and_nested_occurs_offsets():
    layout = compile_copybook(cpy(
        "01 R.", "05 BASE PIC X(4).", "05 ALT REDEFINES BASE PIC X(4).",
        "05 PAIR OCCURS 2 TIMES.", "10 X PIC X(2).", "10 Y PIC 9(2)."))
    assert layout.valid, layout.to_dict()
    leaves = leaf_map(layout)
    assert leaves["ALT"].offset == leaves["BASE"].offset == 0
    assert leaves["PAIR[0].X"].offset == 4 and leaves["PAIR[1].X"].offset == 8
    assert leaves["PAIR[1].Y"].offset == 10 and layout.record_length == 12

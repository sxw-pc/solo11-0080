#!/usr/bin/env python3
"""Deterministic synthetic fixture generator (no network, fixed seed)."""

from __future__ import annotations

import argparse
import hashlib
import random
from decimal import Decimal
from pathlib import Path

CUSTOMER_LENGTH, ACCOUNT_LENGTH, BALANCE_LENGTH = 86, 50, 64
WEIGHTS = [2, 3, 4, 5, 6]
TAGS = [f"TAG{index:02d}VAL" for index in range(1, 10)]

def pack_comp3(value: str | Decimal, digits: int, scale: int) -> bytes:
    scaled = int(Decimal(str(value)).scaleb(scale))
    sign = 0xD if scaled < 0 else 0xC
    text = str(abs(scaled)).zfill(digits)
    if len(text) > digits:
        raise ValueError(f"{value} does not fit {digits} COMP-3 digits")
    nibbles = [int(char) for char in text] + [sign]
    if len(nibbles) % 2:
        nibbles.insert(0, 0)
    return bytes((nibbles[i] << 4) | nibbles[i + 1] for i in range(0, len(nibbles), 2))

def check_digit(base: str) -> str:
    expected = (11 - sum(int(d) * w for d, w in zip(base, WEIGHTS)) % 11) % 11
    if expected >= 10:
        raise ValueError("base has no single-digit mod-11 check digit")
    return str(expected)

def control_numbers(count: int) -> list[str]:
    result, candidate = [], 10000
    while len(result) < count:
        base = f"{candidate:05d}"
        try:
            result.append(base + check_digit(base))
        except ValueError:
            pass
        candidate += 1
    return result

def put(buffer: bytearray, offset: int, text: str, width: int) -> None:
    raw = text.encode("ascii")
    if len(raw) > width:
        raise ValueError(f"{text!r} exceeds {width} bytes")
    buffer[offset:offset + width] = raw.ljust(width, b" ")

def put_number(buffer: bytearray, offset: int, value: str, width: int) -> None:
    if not value.isdigit() or len(value) > width:
        raise ValueError(f"{value!r} is not a {width}-digit number")
    put(buffer, offset, value.zfill(width), width)

def customer_record(sequence: int, *, name: str, open_date: str, status: str, balance: str,
                    renew_date: str, tag_count: int, customer_id: str | None = None,
                    controls: list[str] | None = None, tag_pool: list[str] | None = None) -> bytes:
    buffer = bytearray(b" " * CUSTOMER_LENGTH)
    put(buffer, 0, "D", 1)
    put(buffer, 1, customer_id or f"CUST{100001 + sequence:06d}", 10)
    put(buffer, 11, name, 20)
    put_number(buffer, 31, open_date, 8)
    put(buffer, 39, status, 2)
    buffer[41:46] = pack_comp3(balance, 9, 2)
    put_number(buffer, 46, str(tag_count), 2)
    pool = tag_pool or TAGS
    for index in range(3):
        put(buffer, 48 + index * 8,
            pool[(sequence + index) % len(pool)] if index < tag_count else "", 8)
    put_number(buffer, 72, renew_date, 8)
    put(buffer, 80, (controls or control_numbers(sequence + 1))[sequence], 6)
    return bytes(buffer)

def customer_file(records: list[bytes], balances: list[str]) -> bytes:
    header, trailer = bytearray(b" " * CUSTOMER_LENGTH), bytearray(b" " * CUSTOMER_LENGTH)
    header[0:1], header[41:46] = b"H", pack_comp3("0", 9, 2)
    trailer[0:1] = b"T"
    put_number(trailer, 31, str(len(records)), 8)
    trailer[41:46] = pack_comp3(sum((Decimal(v) for v in balances), Decimal(0)), 9, 2)
    return bytes(header) + b"".join(records) + bytes(trailer)

def account_record(sequence: int, region: str, score: int, amount: str, history: list[str]) -> bytes:
    buffer = bytearray(b" " * ACCOUNT_LENGTH)
    put(buffer, 0, "D", 1)
    put(buffer, 1, region, 2)
    put_number(buffer, 3, f"{sequence:08d}", 8)
    put(buffer, 11, f"DEMO ACCOUNT {sequence:03d}", 16)
    put_number(buffer, 27, f"55501{sequence:03d}", 8)
    put_number(buffer, 35, str(score), 3)
    buffer[38:42] = pack_comp3(amount, 7, 2)
    put_number(buffer, 42, history[0], 4)
    put_number(buffer, 46, history[1], 4)
    return bytes(buffer)

def balance_record(sequence: int, amount: str, rate: str, book_date: str, memo: str) -> bytes:
    buffer = bytearray(b" " * BALANCE_LENGTH)
    put(buffer, 0, "D", 1)
    put(buffer, 1, f"ACCT{sequence:08d}", 12)
    buffer[13:19] = pack_comp3(amount, 11, 2)
    put_number(buffer, 19, rate.replace(".", "").zfill(7), 7)
    put_number(buffer, 26, book_date, 8)
    put(buffer, 34, memo, 30)
    return bytes(buffer)

def generate(out: Path, seed: int) -> dict[str, str]:
    out.mkdir(parents=True, exist_ok=True)
    controls, tags = control_numbers(40), TAGS[:]
    random.Random(seed).shuffle(tags)
    normal_specs = [
        ("DEMO CUSTOMER 01", "20260105", "A", "1234.50", "20261005", 3),
        ("DEMO CUSTOMER 02", "20260210", "I", "-250.75", "20261110", 2),
        ("DEMO CUSTOMER 03", "20260315", "A", "0.00", "20261215", 1),
        ("DEMO CUSTOMER 04", "20260420", "A", "999999.99", "20261231", 3),
        ("DEMO CUSTOMER 05", "20260525", "I", "42.00", "20261225", 2),
    ]
    normal_records = [customer_record(index, name=name, open_date=open_date, status=status,
                                      balance=balance, renew_date=renew, tag_count=tag_count,
                                      controls=controls, tag_pool=tags)
                      for index, (name, open_date, status, balance, renew, tag_count)
                      in enumerate(normal_specs)]
    normal = customer_file(normal_records, [spec[3] for spec in normal_specs])
    reject_specs = [
        ("DEMO REJECT BASE", "20260101", "A", "10.00", "20261201", 1, None),
        ("DEMO REJECT STATUS", "20260202", "X", "20.00", "20261202", 1, None),
        ("DEMO REJECT DUP", "20260303", "A", "30.00", "20261203", 1, "CUST100011"),
        ("", "20260404", "A", "40.00", "20261204", 1, None),
        ("DEMO REJECT DATE", "20261301", "A", "50.00", "20261205", 1, None),
    ]
    reject_records = [customer_record(10 + index, name=name, open_date=open_date, status=status,
                                      balance=balance, renew_date=renew, tag_count=tag_count,
                                      customer_id=override, controls=controls, tag_pool=tags)
                      for index, (name, open_date, status, balance, renew, tag_count, override)
                      in enumerate(reject_specs)]
    accounts = b"".join([account_record(1, "NA", 85, "1200.25", ["0100", "0200"]),
                         account_record(2, "EU", 72, "-88.50", ["0050", "0060"]),
                         account_record(3, "AP", 99, "0.00", ["0999", "0001"])])
    balances = b"".join([
        balance_record(1, "1234.56", "012.3456", "20260601", "SYNTHETIC OPENING BALANCE"),
        balance_record(2, "-99.99", "000.5000", "20260602", "SYNTHETIC ADJUSTMENT")])
    outputs = {"customer_normal.dat": normal,
               "customer_rejects.dat": customer_file(reject_records, [spec[3] for spec in reject_specs]),
               "customer_truncated.dat": normal[:-12], "account_normal.dat": accounts,
               "balance_normal.dat": balances, "empty.dat": b""}
    for name, data in outputs.items():
        (out / name).write_bytes(data)
    return {name: hashlib.sha256(data).hexdigest() for name, data in outputs.items()}

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("fixtures"), help="fixtures root")
    parser.add_argument("--seed", type=int, default=20260101)
    args = parser.parse_args()
    hashes = generate(args.out / "data", args.seed)
    for name in sorted(hashes):
        print(f"{name}\t{hashes[name]}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

"""Shared deterministic fixtures and helpers; no network is used."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from copybook_kit.copybook.layout import compile_copybook
from copybook_kit.decode.record import decode_record
from copybook_kit.pipeline.options import RunOptions
from copybook_kit.pipeline.runner import run

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)

def cpy(*statements: str) -> str:
    """Wrap statements in fixed-format columns 8-72."""
    return "\n".join("       " + statement for statement in statements) + "\n"

def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))

def decode(source: str, raw: bytes, *, offset: int = 0, policies: dict[str, str] | None = None):
    layout = compile_copybook(source)
    assert layout.valid, layout.to_dict()
    return layout, decode_record(layout, raw, record_no=1, byte_offset=offset,
                                 charset="utf-8", blank_policies=policies)

@pytest.fixture
def run_fixture(tmp_path):
    def execute(data: str = "customer_normal.dat", copybook: str = "customer.cpy",
                rules: str = "customer_rules.yaml", **kwargs):
        return run(RunOptions(
            input_path=FIXTURES / "data" / data,
            copybook_path=FIXTURES / "copybooks" / copybook,
            rules_path=FIXTURES / "rules" / rules,
            out_root=tmp_path / "runs", db_path=tmp_path / "ledger.sqlite", now=NOW, **kwargs))

    return execute

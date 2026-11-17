"""CLI surface for certify-copybook and certificate-gated runs."""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from conftest import cpy
from copybook_kit.certify import LEVEL_FILE_REUSE, build_and_certify
from copybook_kit.util import write_json_atomic
import copybook_kit.cli as cli

NOW = "2026-01-01T00:00:00Z"
RULES = "version: 1\nname: cli-rules\nfields: {}\n"


def invoke(capsys, args):
    code = cli.main(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture
def pair(tmp_path):
    old = tmp_path / "old.cpy"
    new = tmp_path / "new.cpy"
    rules = tmp_path / "rules.yaml"
    data = tmp_path / "data.dat"
    old.write_text(cpy("01 R.", "05 CODE PIC X(4)."), encoding="utf-8")
    new.write_text(cpy("01 R.", "05 CODE PIC 9(4)."), encoding="utf-8")
    rules.write_text(RULES, encoding="utf-8")
    data.write_bytes(b"1234" * 2)
    cert = build_and_certify(old_copybook_path=old, new_copybook_path=new, rules_path=rules,
                             input_path=data, charset="utf-8", recfm="F", field_paths=None,
                             requested_level=LEVEL_FILE_REUSE,
                             now=datetime(2026, 1, 1, tzinfo=timezone.utc))
    cert_path = tmp_path / "cert.json"
    write_json_atomic(cert_path, cert)
    return tmp_path, old, new, rules, data, cert_path, cert


def test_certify_command_writes_json_and_exit_codes(pair, tmp_path, capsys):
    _, old, new, rules, data, _, _ = pair
    output = tmp_path / "out" / "certificate.json"
    code, out, err = invoke(capsys, [
        "certify-copybook", "--old-copybook", str(old), "--new-copybook", str(new),
        "--rules", str(rules), "--input", str(data), "--required-level", "file-reuse",
        "--output", str(output), "--now", NOW])
    assert code == 0 and err == ""
    payload = json.loads(out)
    assert payload["level"]["file-reuse"] == "pass" and payload["level"]["universal"] == "reject"
    assert json.loads(output.read_text(encoding="utf-8"))["digest"] == payload["digest"]
    assert payload["authorization"]["file-reuse"]["scope"] == "this input file only"

    code, out, _ = invoke(capsys, [
        "certify-copybook", "--old-copybook", str(old), "--new-copybook", str(new),
        "--rules", str(rules), "--input", str(data), "--required-level", "universal",
        "--now", NOW])
    assert code == 2 and json.loads(out)["level"]["universal"] == "reject"


def test_certify_field_option_limits_paths(pair, capsys):
    _, old, new, rules, data, _, _ = pair
    code, out, _ = invoke(capsys, [
        "certify-copybook", "--old-copybook", str(old), "--new-copybook", str(new),
        "--rules", str(rules), "--input", str(data), "--field", "CODE", "--now", NOW])
    assert code == 0
    payload = json.loads(out)
    assert payload["field_paths"] == ["R.CODE"]


def test_gated_run_blocked_then_allowed(pair, capsys):
    tmp_path, old, new, rules, data, cert_path, cert = pair
    common = ["run", "--input", str(data), "--copybook", str(new), "--rules", str(rules),
              "--certificate", str(cert_path), "--old-copybook", str(old),
              "--db", str(tmp_path / "db.sqlite"), "--out", str(tmp_path / "runs"),
              "--now", NOW]
    code, _, err = invoke(capsys, common + ["--required-level", "universal"])
    assert code == 2 and json.loads(err)["code"] == "CF002"
    code, out, _ = invoke(capsys, common + ["--required-level", "file-reuse"])
    assert code == 0
    result = json.loads(out)
    assert result["status"] == "completed"
    assert result["contracts"]["certificate"]["digest"] == cert["digest"]
    assert result["outputs"]["certificate"].endswith("certificate.json")


def test_unknown_field_path_fails_with_cli_error(pair, capsys):
    _, old, new, rules, data, _, _ = pair
    code, _, err = invoke(capsys, [
        "certify-copybook", "--old-copybook", str(old), "--new-copybook", str(new),
        "--rules", str(rules), "--input", str(data), "--field", "GHOST", "--now", NOW])
    assert code == 2 and json.loads(err)["code"] == "CLI001"

"""CLI commands, stable error JSON and loopback-only serve binding."""

from __future__ import annotations

import json
import subprocess
import sys

import pytest

import copybook_kit.cli as cli
from conftest import FIXTURES, ROOT, cpy

def invoke(capsys, args):
    code = cli.main(args)
    captured = capsys.readouterr()
    return code, captured.out, captured.err

def test_validate_json_success_and_contract_failure(tmp_path, capsys):
    code, out, _ = invoke(capsys, ["validate", "--copybook", str(FIXTURES / "copybooks" / "customer.cpy")])
    payload = json.loads(out)
    assert code == 0 and payload["ok"] is True and payload["valid"] is True
    assert payload["record_length"] == 86 and len(payload["fields"]) == 1
    bad = tmp_path / "bad.cpy"
    bad.write_text(cpy("01 R.", "05 A PIC 9(2)V99."), encoding="utf-8")
    code, out, _ = invoke(capsys, ["validate", "--copybook", str(bad)])
    assert code == 2 and json.loads(out)["ok"] is False
    assert "CB001" in {item["code"] for item in json.loads(out)["errors"]}

def test_run_profile_reprocess_and_failed_exit_codes(tmp_path, capsys):
    db, out_root = str(tmp_path / "ledger.sqlite"), str(tmp_path / "runs")
    common = ["--copybook", str(FIXTURES / "copybooks" / "customer.cpy"),
              "--out", out_root, "--db", db, "--now", "2026-01-01T00:00:00Z"]
    normal_args = ["run", "--input", str(FIXTURES / "data" / "customer_normal.dat"),
                   "--rules", str(FIXTURES / "rules" / "customer_rules.yaml"), *common]
    code, out, _ = invoke(capsys, normal_args)
    normal = json.loads(out)
    assert code == 0 and normal["status"] == "completed" and normal["idempotent"] is False
    profile_path = tmp_path / "profile.json"
    code, out, _ = invoke(capsys, ["profile", "--run-id", normal["run_id"], "--db", db,
                                   "--output", str(profile_path)])
    assert code == 0 and out == "" and json.loads(profile_path.read_text(encoding="utf-8"))["field_count"]
    code, out, _ = invoke(capsys, ["reprocess", "--run-id", normal["run_id"],
                                   "--rules", str(FIXTURES / "rules" / "negative_rules.yaml"),
                                   "--db", db, "--now", "2026-01-01T00:00:00Z"])
    child = json.loads(out)
    assert code == 3 and child["status"] == "failed" and child["parent_run_id"] == normal["run_id"]
    code, out, _ = invoke(capsys, ["run", "--input", str(FIXTURES / "data" / "customer_truncated.dat"),
                                   "--rules", str(FIXTURES / "rules" / "customer_rules.yaml"), *common])
    assert code == 3 and json.loads(out)["status"] == "failed"

def test_cli_errors_and_loopback_serve(monkeypatch, tmp_path, capsys):
    code, _, err = invoke(capsys, ["run", "--input", str(FIXTURES / "data" / "customer_normal.dat"),
                                   "--copybook", str(FIXTURES / "copybooks" / "customer.cpy"),
                                   "--rules", str(FIXTURES / "rules" / "customer_rules.yaml"),
                                   "--now", "not-a-time"])
    assert code == 2 and json.loads(err)["code"] == "CLI001"
    with pytest.raises(SystemExit) as exited:
        cli.main(["--unknown-command"])
    assert exited.value.code == 2 and json.loads(capsys.readouterr().err)["code"] == "CLI001"
    captured = {}

    class FakeApp:
        def run(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(cli, "create_app", lambda db: FakeApp())
    code, out, _ = invoke(capsys, ["serve", "--db", str(tmp_path / "db.sqlite"), "--port", "5001"])
    assert code == 0 and captured["host"] == "127.0.0.1" and captured["port"] == 5001
    assert captured["debug"] is False and "127.0.0.1" in out
    code, _, err = invoke(capsys, ["serve", "--db", str(tmp_path / "db.sqlite"), "--port", "0"])
    assert code == 2 and json.loads(err)["code"] == "CLI001"

def test_module_entry_point_runs_validate():
    result = subprocess.run([sys.executable, "-m", "copybook_kit.cli", "validate", "--copybook",
                             str(FIXTURES / "copybooks" / "balance.cpy")],
                            cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0 and json.loads(result.stdout)["record_length"] == 64
    assert result.stderr == ""

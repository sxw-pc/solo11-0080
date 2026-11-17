"""Portable evidence packs: packing, byte/replay contracts, hostile archives."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import tarfile
from pathlib import Path

import pytest
import yaml

import copybook_kit.cli as cli
from conftest import FIXTURES, NOW
from copybook_kit.errors import LedgerError
from copybook_kit.pipeline import portable_pack
from copybook_kit.pipeline.options import RunOptions
from copybook_kit.pipeline.portable_pack import PackError, pack_run, verify_pack
from copybook_kit.pipeline.runner import run
from copybook_kit.util import sha256_bytes

STRICT_RULES = FIXTURES / "rules" / "customer_rules.yaml"


def make_run(root: Path, data: str = "customer_rejects.dat", *, rules_path: Path | None = None,
             input_path: Path | None = None):
    return run(RunOptions(
        input_path=input_path or FIXTURES / "data" / data,
        copybook_path=FIXTURES / "copybooks" / "customer.cpy",
        rules_path=rules_path or STRICT_RULES,
        out_root=root / "runs", db_path=root / "ledger.sqlite", now=NOW))


def pack_of(root: Path, result, name: str = "pack.tar.gz"):
    out = root / "packs" / name
    info = pack_run(result.run_id, root / "ledger.sqlite", out)
    return out, info


def unpack(pack: Path) -> dict[str, bytes]:
    with tarfile.open(pack, "r:gz") as tar:
        return {info.name: tar.extractfile(info).read()
                for info in tar.getmembers() if info.isreg()}


def repack(root: Path, members: dict[str, bytes], name: str = "doctored.tar.gz"):
    """Rebuild a self-consistent pack after doctoring member bytes."""
    manifest = json.loads(members.pop("manifest.json"))
    by_path = {entry["path"]: entry for entry in manifest["members"]}
    for path, data in members.items():
        entry = by_path[path]
        entry["sha256"] = hashlib.sha256(data).hexdigest()
        entry["length"] = len(data)
    manifest["members"] = list(by_path.values())
    members["manifest.json"] = portable_pack._manifest_bytes(manifest)
    out = root / name
    portable_pack._write_pack(out, list(members.items()), max_pack_bytes=1 << 30)
    return out, sha256_bytes(members["manifest.json"])


def simple_manifest(member_entries) -> dict:
    return {
        "schema_version": 1, "kind": portable_pack.PACK_KIND, "run_id": "run-x",
        "run_key": "k", "status": "completed", "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z", "parameters": {}, "counts": {},
        "members": member_entries,
    }


def manifest_entry(path: str, data: bytes, role: str = "input") -> dict:
    return {"path": path, "role": role, "run_id": "run-x", "source": path,
            "length": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def test_pack_roundtrip_bytes_and_replay_in_a_new_root(tmp_path):
    host_a = tmp_path / "hostA"
    result = make_run(host_a)
    pack, info = pack_of(host_a, result)
    assert info["ok"] and info["manifest_sha256"] and info["members"] > 0
    # the pack travels to a different root; only pack bytes may be used there
    host_b = tmp_path / "elsewhere" / "deeper"
    host_b.mkdir(parents=True)
    moved = host_b / "evidence.tar.gz"
    moved.write_bytes(pack.read_bytes())
    before = sorted(p.name for p in host_b.iterdir())
    bytes_verdict = verify_pack(moved, contract="bytes",
                                manifest_sha256=info["manifest_sha256"])
    assert bytes_verdict["ok"] and bytes_verdict["contract"] == "bytes"
    assert bytes_verdict["claims"] == "byte_handover_only"
    assert "replay" not in bytes_verdict
    replay_verdict = verify_pack(moved, contract="replay")
    assert replay_verdict["ok"], replay_verdict["violations"]
    assert replay_verdict["contract"] == "replay"
    assert replay_verdict["claims"] == "semantic_replay"
    assert replay_verdict["bytes"]["ok"]
    assert replay_verdict["replay"]["records_compared"] == 7
    assert replay_verdict["replay"]["first_difference"] is None
    assert replay_verdict["replay"]["summary_differences"] == []
    # verdicts never leak old-host absolute paths, and verification writes nothing
    assert str(tmp_path) not in json.dumps(bytes_verdict)
    assert str(tmp_path) not in json.dumps(replay_verdict)
    assert sorted(p.name for p in host_b.iterdir()) == before


def test_pack_is_deterministic_and_covers_all_roles(tmp_path):
    result = make_run(tmp_path)
    ledger_bytes = (tmp_path / "ledger.sqlite").read_bytes()
    first, info1 = pack_of(tmp_path, result, "a.tar.gz")
    second, info2 = pack_of(tmp_path, result, "b.tar.gz")
    assert info1["manifest_sha256"] == info2["manifest_sha256"]
    assert first.read_bytes() == second.read_bytes()
    # packing is read-only: the operator ledger is untouched
    assert (tmp_path / "ledger.sqlite").read_bytes() == ledger_bytes
    manifest = json.loads(unpack(first)["manifest.json"])
    paths = [entry["path"] for entry in manifest["members"]]
    assert paths == sorted(paths)
    assert all(entry["run_id"] == result.run_id for entry in manifest["members"])
    roles = {entry["role"] for entry in manifest["members"]}
    assert roles >= {"input", "copybook", "rules", "summary", "layout", "accepted",
                     "rejects", "profile", "report", "html", "reject_raw",
                     "ledger_run", "ledger_rejects", "ledger_file_issues",
                     "ledger_checkpoint", "ledger_events"}
    evidence = sorted(entry["source"] for entry in manifest["members"]
                      if entry["role"] == "reject_raw")
    assert evidence == [f"rejects/record-{no:08d}.bin" for no in (3, 4, 5, 6)]
    for entry in manifest["members"]:
        assert entry["length"] >= 0 and len(entry["sha256"]) == 64


def test_pack_requires_a_finished_known_run(tmp_path):
    result = make_run(tmp_path)
    with pytest.raises(LedgerError):
        pack_run("run-does-not-exist", tmp_path / "ledger.sqlite", tmp_path / "x.tar.gz")
    connection = sqlite3.connect(tmp_path / "ledger.sqlite")
    with connection:
        connection.execute("UPDATE runs SET status='running' WHERE run_id=?",
                           (result.run_id,))
    connection.close()
    with pytest.raises(PackError):
        pack_run(result.run_id, tmp_path / "ledger.sqlite", tmp_path / "x.tar.gz")
    assert not (tmp_path / "x.tar.gz").exists()


def test_pack_detects_ledger_count_tampering(tmp_path):
    result = make_run(tmp_path)
    connection = sqlite3.connect(tmp_path / "ledger.sqlite")
    with connection:
        connection.execute(
            "UPDATE runs SET accepted_records=accepted_records+1 WHERE run_id=?",
            (result.run_id,))
    connection.close()
    with pytest.raises(PackError):
        pack_run(result.run_id, tmp_path / "ledger.sqlite", tmp_path / "x.tar.gz")
    assert not (tmp_path / "x.tar.gz").exists()


def test_pack_detects_truncated_input(tmp_path):
    source = tmp_path / "input.dat"
    source.write_bytes((FIXTURES / "data" / "customer_normal.dat").read_bytes())
    result = make_run(tmp_path, input_path=source)
    source.write_bytes(source.read_bytes()[:-10])  # truncate after the run
    with pytest.raises(PackError):
        pack_run(result.run_id, tmp_path / "ledger.sqlite", tmp_path / "x.tar.gz")
    assert not (tmp_path / "x.tar.gz").exists()


def test_pack_detects_source_mutation_during_packing(tmp_path, monkeypatch):
    rules_copy = tmp_path / "contracts" / "customer_rules.yaml"
    rules_copy.parent.mkdir(parents=True)
    rules_copy.write_bytes(STRICT_RULES.read_bytes())
    result = make_run(tmp_path, rules_path=rules_copy)
    original = portable_pack._read_bytes
    tampered = {"done": False}

    def sneaky(path):
        data = original(path)
        if Path(path) == rules_copy and not tampered["done"]:
            tampered["done"] = True
            rules_copy.write_bytes(data + b"\n# rewritten while packing\n")
        return data

    monkeypatch.setattr(portable_pack, "_read_bytes", sneaky)
    out = tmp_path / "packs" / "x.tar.gz"
    with pytest.raises(PackError):
        pack_run(result.run_id, tmp_path / "ledger.sqlite", out)
    assert tampered["done"]
    assert not out.exists()
    assert list((tmp_path / "packs").iterdir()) == []  # no half-published pack


def test_pack_budget_leaves_no_pack(tmp_path):
    result = make_run(tmp_path)
    out = tmp_path / "packs" / "small.tar.gz"
    with pytest.raises(PackError) as excinfo:
        pack_run(result.run_id, tmp_path / "ledger.sqlite", out, max_pack_bytes=128)
    assert excinfo.value.code == "PK003"
    assert not out.exists()
    assert list((tmp_path / "packs").iterdir()) == []


def test_failed_run_pack_roundtrip_with_tail_evidence(tmp_path):
    result = make_run(tmp_path, data="customer_truncated.dat")
    assert result.status == "failed"
    pack, info = pack_of(tmp_path, result)
    manifest = json.loads(unpack(pack)["manifest.json"])
    evidence = [entry["source"] for entry in manifest["members"]
                if entry["role"] == "reject_raw"]
    assert evidence == ["rejects/truncated-tail.bin"]
    assert verify_pack(pack, contract="bytes",
                       manifest_sha256=info["manifest_sha256"])["ok"]
    verdict = verify_pack(pack, contract="replay")
    assert verdict["ok"], verdict["violations"]


@pytest.mark.parametrize("bad_name,needle", [
    ("/abs.bin", "absolute member path"),
    ("members/../escape.bin", "escapes the pack root"),
    ("C:/windows/win.ini", "unsafe ':'"),
    ("\\\\server\\share\\x.bin", "absolute member path"),
])
def test_verify_rejects_unsafe_member_paths(tmp_path, bad_name, needle):
    manifest = portable_pack._manifest_bytes(simple_manifest([]))
    pack = tmp_path / "bad.tar.gz"
    portable_pack._write_pack(pack, [("manifest.json", manifest), (bad_name, b"x")],
                              max_pack_bytes=1 << 30)
    verdict = verify_pack(pack, contract="bytes", manifest_sha256=sha256_bytes(manifest))
    assert not verdict["ok"] and verdict["stage"] == "archive"
    assert any(needle in text for text in verdict["violations"])


def test_verify_rejects_duplicate_and_casefold_paths(tmp_path):
    manifest = portable_pack._manifest_bytes(simple_manifest([]))
    dup = tmp_path / "dup.tar.gz"
    portable_pack._write_pack(dup, [("manifest.json", manifest),
                                    ("members/./a.bin", b"x"), ("members/a.bin", b"y")],
                              max_pack_bytes=1 << 30)
    verdict = verify_pack(dup, contract="bytes", manifest_sha256=sha256_bytes(manifest))
    assert not verdict["ok"]
    assert any("duplicate normalized" in text for text in verdict["violations"])
    case = tmp_path / "case.tar.gz"
    portable_pack._write_pack(case, [("manifest.json", manifest),
                                     ("members/A.bin", b"x"), ("members/a.bin", b"y")],
                              max_pack_bytes=1 << 30)
    verdict = verify_pack(case, contract="bytes", manifest_sha256=sha256_bytes(manifest))
    assert not verdict["ok"]
    assert any("case-fold" in text for text in verdict["violations"])


def test_verify_rejects_links(tmp_path):
    manifest = portable_pack._manifest_bytes(simple_manifest([]))
    for name, link_type in (("sym.tar.gz", tarfile.SYMTYPE), ("hard.tar.gz", tarfile.LNKTYPE)):
        pack = tmp_path / name
        with tarfile.open(pack, "w:gz") as tar:
            tar.addfile(portable_pack._tar_info("manifest.json", len(manifest)),
                        io.BytesIO(manifest))
            link = tarfile.TarInfo("members/evil")
            link.type = link_type
            link.linkname = "/etc/passwd"
            tar.addfile(link)
        verdict = verify_pack(pack, contract="bytes", manifest_sha256=sha256_bytes(manifest))
        assert not verdict["ok"]
        assert any("not a regular file" in text for text in verdict["violations"])


def test_verify_rejects_missing_extra_and_mismatched_members(tmp_path):
    data = b"hello"
    manifest = portable_pack._manifest_bytes(
        simple_manifest([manifest_entry("members/input/x.dat", data)]))
    sha = sha256_bytes(manifest)
    missing = tmp_path / "missing.tar.gz"
    portable_pack._write_pack(missing, [("manifest.json", manifest)], max_pack_bytes=1 << 30)
    verdict = verify_pack(missing, contract="bytes", manifest_sha256=sha)
    assert not verdict["ok"]
    assert any("missing member" in text for text in verdict["violations"])
    extra = tmp_path / "extra.tar.gz"
    portable_pack._write_pack(extra, [("manifest.json", manifest),
                                      ("members/input/x.dat", data),
                                      ("members/artifacts/summary.json", b"{}")],
                              max_pack_bytes=1 << 30)
    verdict = verify_pack(extra, contract="bytes", manifest_sha256=sha)
    assert not verdict["ok"]
    assert any("not listed in the manifest" in text for text in verdict["violations"])
    mismatched = tmp_path / "mismatched.tar.gz"
    portable_pack._write_pack(mismatched, [("manifest.json", manifest),
                                           ("members/input/x.dat", b"tampered")],
                              max_pack_bytes=1 << 30)
    verdict = verify_pack(mismatched, contract="bytes", manifest_sha256=sha)
    assert not verdict["ok"]
    assert any("does not match" in text for text in verdict["violations"])


def test_verify_rejects_wrong_manifest_hash(tmp_path):
    result = make_run(tmp_path)
    pack, _ = pack_of(tmp_path, result)
    verdict = verify_pack(pack, contract="bytes", manifest_sha256="0" * 64)
    assert not verdict["ok"] and verdict["stage"] == "manifest"
    assert any("does not match the approved" in text for text in verdict["violations"])


def test_verify_enforces_unpack_budgets(tmp_path):
    result = make_run(tmp_path)
    pack, info = pack_of(tmp_path, result)
    sha = info["manifest_sha256"]
    verdict = verify_pack(pack, contract="bytes", manifest_sha256=sha, max_members=2)
    assert not verdict["ok"]
    assert any("member count exceeds" in text for text in verdict["violations"])
    verdict = verify_pack(pack, contract="bytes", manifest_sha256=sha, max_member_bytes=8)
    assert not verdict["ok"]
    assert any("per-member budget" in text for text in verdict["violations"])
    verdict = verify_pack(pack, contract="bytes", manifest_sha256=sha, max_total_bytes=16)
    assert not verdict["ok"]
    assert any("total uncompressed budget" in text for text in verdict["violations"])


def test_verify_rejects_over_budget_decompression(tmp_path):
    big = b"\0" * (2 * 1024 * 1024)
    manifest = portable_pack._manifest_bytes(
        simple_manifest([manifest_entry("members/input/zeros.bin", big)]))
    pack = tmp_path / "bomb.tar.gz"
    portable_pack._write_pack(pack, [("manifest.json", manifest),
                                     ("members/input/zeros.bin", big)],
                              max_pack_bytes=1 << 30)
    assert pack.stat().st_size < 100_000  # compresses far below the unpacked size
    verdict = verify_pack(pack, contract="bytes",
                          manifest_sha256=sha256_bytes(manifest),
                          max_total_bytes=1024 * 1024)
    assert not verdict["ok"]
    assert any("total uncompressed budget" in text for text in verdict["violations"])


def test_verify_rejects_reject_raw_file_pointing_outside_pack(tmp_path):
    result = make_run(tmp_path)
    pack, _ = pack_of(tmp_path, result)
    members = unpack(pack)
    rows = json.loads(members["members/ledger/rejects.json"])
    rows[0]["raw_file"] = "../../outside.bin"
    members["members/ledger/rejects.json"] = json.dumps(rows).encode()
    doctored, sha = repack(tmp_path, members)
    verdict = verify_pack(doctored, contract="bytes", manifest_sha256=sha)
    assert not verdict["ok"]
    assert any("unsafe" in text for text in verdict["violations"])
    members = unpack(pack)
    rows = json.loads(members["members/ledger/rejects.json"])
    rows[0]["raw_file"] = "rejects/record-99999999.bin"
    members["members/ledger/rejects.json"] = json.dumps(rows).encode()
    doctored, sha = repack(tmp_path, members, name="doctored2.tar.gz")
    verdict = verify_pack(doctored, contract="bytes", manifest_sha256=sha)
    assert not verdict["ok"]
    assert any("is not in the pack" in text for text in verdict["violations"])


def lenient_rules(root: Path) -> Path:
    data = yaml.safe_load(STRICT_RULES.read_text(encoding="utf-8"))
    del data["fields"]["STATUS"]["enum"]
    target = root / "lenient" / "customer_rules.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return target


def test_contracts_disagree_on_self_consistent_but_wrong_pack(tmp_path):
    """A self-consistent pack that misjudged a record: bytes pass, replay fails."""
    strict = STRICT_RULES.read_bytes()
    result = make_run(tmp_path, rules_path=lenient_rules(tmp_path))
    assert result.counts["accepted_records"] == 4  # STATUS-enum record now accepted
    pack, _ = pack_of(tmp_path, result)
    members = unpack(pack)
    # swap in the strict rules and make every hash self-consistent again
    members["members/rules/customer_rules.yaml"] = strict
    strict_sha = hashlib.sha256(strict).hexdigest()
    run_snapshot = json.loads(members["members/ledger/run.json"])
    run_snapshot["rules_sha256"] = strict_sha
    members["members/ledger/run.json"] = json.dumps(run_snapshot).encode()
    summary = json.loads(members["members/artifacts/summary.json"])
    summary["contracts"]["rules"]["sha256"] = strict_sha
    members["members/artifacts/summary.json"] = json.dumps(summary).encode()
    doctored, sha = repack(tmp_path, members)
    bytes_verdict = verify_pack(doctored, contract="bytes", manifest_sha256=sha)
    assert bytes_verdict["ok"], bytes_verdict["violations"]
    replay_verdict = verify_pack(doctored, contract="replay")
    assert not replay_verdict["ok"]
    difference = replay_verdict["replay"]["first_difference"]
    assert difference is not None
    assert difference["record_no"] == 3 and difference["byte_offset"] == 172
    assert difference["pack"]["decision"] == "accepted"
    assert difference["replay"]["decision"] == "rejected"
    assert difference["replay"]["codes"] == ["R002_ENUM"]
    keys = {item["key"] for item in replay_verdict["replay"]["summary_differences"]}
    assert "counts" in keys
    # the two contracts cannot impersonate each other
    assert bytes_verdict["claims"] == "byte_handover_only"
    assert "replay" not in bytes_verdict
    assert replay_verdict["claims"] == "semantic_replay"
    assert replay_verdict["bytes"]["ok"]


def test_truncated_input_member_diverges_only_in_replay(tmp_path):
    result = make_run(tmp_path, data="customer_normal.dat")
    pack, _ = pack_of(tmp_path, result)
    members = unpack(pack)
    truncated = members["members/input/customer_normal.dat"][:-10]
    members["members/input/customer_normal.dat"] = truncated
    digest = hashlib.sha256(truncated).hexdigest()
    run_snapshot = json.loads(members["members/ledger/run.json"])
    run_snapshot["input_sha256"] = digest
    run_snapshot["input_size"] = len(truncated)
    members["members/ledger/run.json"] = json.dumps(run_snapshot).encode()
    summary = json.loads(members["members/artifacts/summary.json"])
    summary["input"]["sha256"] = digest
    summary["input"]["size"] = len(truncated)
    members["members/artifacts/summary.json"] = json.dumps(summary).encode()
    doctored, sha = repack(tmp_path, members)
    bytes_verdict = verify_pack(doctored, contract="bytes", manifest_sha256=sha)
    assert bytes_verdict["ok"], bytes_verdict["violations"]
    replay_verdict = verify_pack(doctored, contract="replay")
    assert not replay_verdict["ok"]
    difference = replay_verdict["replay"]["first_difference"]
    assert difference is not None
    assert difference["record_no"] == 7 and difference["byte_offset"] == 516
    assert difference["pack"]["decision"] == "accepted"
    assert difference["replay"] is None
    keys = {item["key"] for item in replay_verdict["replay"]["summary_differences"]}
    assert {"status", "counts", "file_issues"} <= keys


@pytest.mark.parametrize("name,expected", [
    ("members/input/x.dat", "members/input/x.dat"),
    ("members\\input\\x.dat", "members/input/x.dat"),
    ("members/./input//x.dat", "members/input/x.dat"),
])
def test_normalize_member_path_canonicalizes(name, expected):
    assert portable_pack._normalize_member_path(name) == expected


@pytest.mark.parametrize("name", ["", ".", "/abs", "members/../x", "..", "C:/x", "a:b",
                                  "//unc/x"])
def test_normalize_member_path_rejects(name):
    with pytest.raises(portable_pack._Reject):
        portable_pack._normalize_member_path(name)


def test_pack_verifies_under_a_different_path_separator(tmp_path):
    result = make_run(tmp_path)
    pack, info = pack_of(tmp_path, result)
    renamed = [(name.replace("/", "\\"), data) for name, data in unpack(pack).items()]
    weird = tmp_path / "weird.tar.gz"
    portable_pack._write_pack(weird, renamed, max_pack_bytes=1 << 30)
    verdict = verify_pack(weird, contract="bytes",
                          manifest_sha256=info["manifest_sha256"])
    assert verdict["ok"], verdict["violations"]


def test_cli_pack_run_and_verify_pack(tmp_path, capsys):
    def invoke(args):
        code = cli.main(args)
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    db = str(tmp_path / "ledger.sqlite")
    code, out, _ = invoke([
        "run", "--input", str(FIXTURES / "data" / "customer_rejects.dat"),
        "--copybook", str(FIXTURES / "copybooks" / "customer.cpy"),
        "--rules", str(STRICT_RULES),
        "--out", str(tmp_path / "runs"), "--db", db, "--now", "2026-01-01T00:00:00Z"])
    assert code == 0
    run_id = json.loads(out)["run_id"]
    pack = tmp_path / "packs" / "run.tar.gz"
    code, out, _ = invoke(["pack-run", "--run-id", run_id, "--db", db, "--out", str(pack)])
    assert code == 0
    info = json.loads(out)
    assert info["ok"] and info["manifest_sha256"] and pack.is_file()
    code, out, _ = invoke(["verify-pack", "--pack", str(pack), "--contract", "bytes",
                           "--manifest-sha256", info["manifest_sha256"]])
    assert code == 0 and json.loads(out)["ok"]
    code, out, _ = invoke(["verify-pack", "--pack", str(pack), "--contract", "replay"])
    assert code == 0 and json.loads(out)["ok"]
    code, out, _ = invoke(["verify-pack", "--pack", str(pack), "--contract", "bytes",
                           "--manifest-sha256", "0" * 64])
    assert code == 3 and not json.loads(out)["ok"]
    code, _, err = invoke(["verify-pack", "--pack", str(pack), "--contract", "bytes"])
    assert code == 2 and json.loads(err)["ok"] is False
    code, _, err = invoke(["pack-run", "--run-id", "run-nope", "--db", db,
                           "--out", str(tmp_path / "y.tar.gz")])
    assert code == 2 and json.loads(err)["code"] == "LD002"

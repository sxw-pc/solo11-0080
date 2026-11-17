"""Command-line entry point for copybook-kit."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .copybook.layout import compile_file
from .demo import run_demo
from .errors import CompileError, CopybookKitError, InputError, LedgerError
from .pipeline.options import RunOptions
from .pipeline.portable_pack import (
    DEFAULT_MAX_MEMBER_BYTES,
    DEFAULT_MAX_MEMBERS,
    DEFAULT_MAX_PACK_BYTES,
    DEFAULT_MAX_TOTAL_BYTES,
    pack_run,
    verify_pack,
)
from .pipeline.runner import resume_run, run
from .store.ledger import Ledger
from .util import parse_now, pretty_json
from .web.app import create_app

DEFAULT_DB = Path("build/ledger.sqlite")
DEFAULT_OUT = Path("build/runs")

class CliParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        print(json.dumps({"ok": False, "code": "CLI001", "message": message},
                         ensure_ascii=False, sort_keys=True), file=sys.stderr)
        raise SystemExit(2)

def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite ledger path")
    parser.add_argument("--now", help="injected ISO-8601 timestamp, e.g. 2026-01-01T00:00:00Z")

def _print_result(result) -> int:
    payload = result.to_dict()
    payload["idempotent"] = result.idempotent
    print(pretty_json(payload), end="")
    return int(result.exit_code)

def command_validate(args: argparse.Namespace) -> int:
    try:
        layout = compile_file(str(args.copybook))
    except OSError as exc:
        raise InputError(f"cannot read copybook {args.copybook}: {exc}", code="IO001") from exc
    payload = layout.to_dict()
    payload["ok"] = payload["valid"] = layout.valid
    print(pretty_json(payload), end="")
    return 0 if layout.valid else 2

def command_run(args: argparse.Namespace) -> int:
    return _print_result(run(RunOptions(
        input_path=args.input, copybook_path=args.copybook, rules_path=args.rules,
        out_root=args.out, db_path=args.db, now=parse_now(args.now), charset=args.charset,
        recfm=args.recfm, max_records=args.max_records, force=args.force)))

def command_resume(args: argparse.Namespace) -> int:
    return _print_result(resume_run(args.run_id, args.db, parse_now(args.now)))

def command_reprocess(args: argparse.Namespace) -> int:
    parent = Ledger(args.db).get_run(args.run_id)
    if parent is None:
        raise LedgerError(f"parent run {args.run_id} not found", code="LD002")
    return _print_result(run(RunOptions(
        input_path=Path(parent["input_path"]), copybook_path=Path(parent["copybook_path"]),
        rules_path=args.rules, out_root=args.out or Path(parent["out_dir"]).parent,
        db_path=args.db, now=parse_now(args.now), charset=parent["charset"],
        recfm=parent["recfm"], max_records=int(parent["max_records"]),
        parent_run_id=parent["run_id"], kind="reprocess",
        notes=f"reprocess of {parent['run_id']}")))

def command_profile(args: argparse.Namespace) -> int:
    run_row = Ledger(args.db).get_run(args.run_id)
    if run_row is None:
        raise LedgerError(f"run {args.run_id} not found", code="LD002")
    path_text = run_row.get("profile_path")
    if not path_text or not Path(path_text).is_file():
        raise InputError(f"run {args.run_id} has no profile artifact", code="IO001")
    text = Path(path_text).read_text(encoding="utf-8")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
    else:
        print(text, end="" if text.endswith("\n") else "\n")
    return 0

def command_serve(args: argparse.Namespace) -> int:
    if not 1 <= args.port <= 65535:
        raise InputError("--port must be between 1 and 65535", code="CLI001")
    print(f"copybook-kit read-only console: http://127.0.0.1:{args.port}/")
    create_app(args.db).run(host="127.0.0.1", port=args.port, debug=False, use_reloader=False)
    return 0

def command_pack_run(args: argparse.Namespace) -> int:
    result = pack_run(args.run_id, args.db, args.out, max_pack_bytes=args.max_pack_bytes)
    print(pretty_json(result), end="")
    return 0

def command_verify_pack(args: argparse.Namespace) -> int:
    verdict = verify_pack(args.pack, contract=args.contract,
                          manifest_sha256=args.manifest_sha256,
                          max_members=args.max_members,
                          max_member_bytes=args.max_member_bytes,
                          max_total_bytes=args.max_total_bytes)
    print(pretty_json(verdict), end="")
    return 0 if verdict["ok"] else 3

def command_demo(args: argparse.Namespace) -> int:
    return run_demo(args.fixtures, args.out, parse_now(args.now or "2026-01-01T00:00:00Z"))

def build_parser() -> CliParser:
    parser = CliParser(prog="copybook-kit", description="定长契勘台：定长 copybook 契约摄入工具")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="compile a copybook and print its layout JSON")
    validate.add_argument("--copybook", type=Path, required=True)
    validate.set_defaults(func=command_validate)
    ingest = sub.add_parser("run", help="ingest one fixed-width file")
    ingest.add_argument("--input", type=Path, required=True)
    ingest.add_argument("--copybook", type=Path, required=True)
    ingest.add_argument("--rules", type=Path, required=True)
    ingest.add_argument("--out", type=Path, default=DEFAULT_OUT, help="run artifact root")
    ingest.add_argument("--charset", default="utf-8", choices=["utf-8", "latin-1", "cp037"])
    ingest.add_argument("--recfm", default="F", choices=["F", "FB"])
    ingest.add_argument("--max-records", type=int, default=0, help="0 disables the guard")
    ingest.add_argument("--force", action="store_true", help="replace an existing run key")
    _add_common(ingest)
    ingest.set_defaults(func=command_run)
    resume = sub.add_parser("resume", help="resume an interrupted run")
    resume.add_argument("--run-id", required=True)
    _add_common(resume)
    resume.set_defaults(func=command_resume)
    reprocess = sub.add_parser("reprocess", help="re-run a parent input with corrected rules")
    reprocess.add_argument("--run-id", required=True)
    reprocess.add_argument("--rules", type=Path, required=True)
    reprocess.add_argument("--out", type=Path, help="new run artifact root")
    _add_common(reprocess)
    reprocess.set_defaults(func=command_reprocess)
    profile = sub.add_parser("profile", help="print or copy a run's field profile JSON")
    profile.add_argument("--run-id", required=True)
    profile.add_argument("--db", type=Path, default=DEFAULT_DB)
    profile.add_argument("--output", type=Path)
    profile.set_defaults(func=command_profile)
    pack = sub.add_parser("pack-run", help="build a self-contained evidence pack for a finished run")
    pack.add_argument("--run-id", required=True)
    pack.add_argument("--db", type=Path, default=DEFAULT_DB)
    pack.add_argument("--out", type=Path, required=True, help="pack output path (.tar.gz)")
    pack.add_argument("--max-pack-bytes", type=int, default=DEFAULT_MAX_PACK_BYTES,
                      help="compressed pack budget; nothing is published when exceeded")
    pack.set_defaults(func=command_pack_run)
    verify = sub.add_parser("verify-pack", help="verify a run evidence pack offline")
    verify.add_argument("--pack", type=Path, required=True)
    verify.add_argument("--contract", required=True, choices=["bytes", "replay"],
                        help="bytes: handover matches the approved manifest; "
                             "replay: also recompute the run from packed bytes")
    verify.add_argument("--manifest-sha256",
                        help="independently preserved manifest hash (required for bytes)")
    verify.add_argument("--max-members", type=int, default=DEFAULT_MAX_MEMBERS)
    verify.add_argument("--max-member-bytes", type=int, default=DEFAULT_MAX_MEMBER_BYTES)
    verify.add_argument("--max-total-bytes", type=int, default=DEFAULT_MAX_TOTAL_BYTES)
    verify.set_defaults(func=command_verify_pack)
    serve = sub.add_parser("serve", help="start the read-only console on 127.0.0.1")
    serve.add_argument("--db", type=Path, default=DEFAULT_DB)
    serve.add_argument("--port", type=int, default=5000)
    serve.set_defaults(func=command_serve)
    demo = sub.add_parser("demo", help="run the complete offline fixture demo")
    demo.add_argument("--fixtures", type=Path, default=Path("fixtures"))
    demo.add_argument("--out", type=Path, default=Path("build/demo"))
    demo.add_argument("--now", help="defaults to 2026-01-01T00:00:00Z for byte stability")
    demo.set_defaults(func=command_demo)
    return parser

def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except CompileError as exc:
        payload = {"ok": False, "code": exc.code, "message": exc.message,
                   "errors": [item.to_dict() for item in exc.diagnostics]}
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2), file=sys.stderr)
        return 2
    except CopybookKitError as exc:
        print(pretty_json({"ok": False, **exc.to_dict()}), end="", file=sys.stderr)
        return 2
    except OSError as exc:
        print(pretty_json({"ok": False, "code": "IO001", "message": str(exc)}), end="", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(pretty_json({"ok": False, "code": "CLI001", "message": str(exc)}), end="", file=sys.stderr)
        return 2

if __name__ == "__main__":
    raise SystemExit(main())

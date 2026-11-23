"""Offline demo driver used by ``make demo`` and the CLI demo command."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from .errors import CopybookKitError
from .pipeline.options import RunOptions
from .pipeline.runner import run

@dataclass(frozen=True)
class Scenario:
    label: str
    data: str
    copybook: str
    rules: str
    expect_failure: bool = False

SCENARIOS = [
    Scenario("正常客户文件", "customer_normal.dat", "customer.cpy", "customer_rules.yaml"),
    Scenario("含拒绝记录", "customer_rejects.dat", "customer.cpy", "customer_rules.yaml"),
    Scenario("截断文件（预期失败可见）", "customer_truncated.dat", "customer.cpy",
             "customer_rules.yaml", True),
    Scenario("必然失败负例（min_records）", "customer_normal.dat", "customer.cpy",
             "negative_rules.yaml", True),
    Scenario("REDEFINES 账户文件", "account_normal.dat", "account.cpy", "account_rules.yaml"),
    Scenario("COMP-3 余额文件", "balance_normal.dat", "balance.cpy", "balance_rules.yaml"),
]

def _ensure_fixtures(fixtures: Path) -> None:
    required = [fixtures / "data" / scenario.data for scenario in SCENARIOS]
    if all(path.is_file() for path in required):
        return
    generator = fixtures / "generator.py"
    if not generator.is_file():
        raise CopybookKitError(f"fixtures are incomplete and generator is missing: {generator}",
                               code="IO001")
    subprocess.run([sys.executable, str(generator), "--out", str(fixtures)],
                   check=True, stdout=subprocess.DEVNULL)

def run_demo(fixtures: Path, out: Path, now: datetime) -> int:
    _ensure_fixtures(fixtures)
    out.mkdir(parents=True, exist_ok=True)
    db_path, run_root = out / "ledger.sqlite", out / "runs"
    print("定长契勘台 DEMO")
    print(f"fixtures={fixtures} out={out} now={now.isoformat().replace('+00:00', 'Z')}")
    unexpected = False
    for scenario in SCENARIOS:
        options = RunOptions(fixtures / "data" / scenario.data,
                             fixtures / "copybooks" / scenario.copybook,
                             fixtures / "rules" / scenario.rules, run_root, db_path, now)
        try:
            result = run(options)
        except CopybookKitError as exc:
            print(f"[ERROR] {scenario.label}: {exc.code} {exc.message}")
            unexpected = True
            continue
        failed_as_declared = (result.exit_code != 0) == scenario.expect_failure
        unexpected |= not failed_as_declared
        counts = result.counts
        print(f"[{'OK' if failed_as_declared else 'UNEXPECTED'}] {scenario.label}: "
              f"status={result.status} records={counts.get('total_records', 0)} "
              f"accepted={counts.get('accepted_records', 0)} "
              f"rejected={counts.get('rejected_records', 0)} run={result.run_id}")
        print(f"       report: {run_root / result.run_id / result.outputs.get('html', 'report.html')}")
    print(f"ledger: {db_path}")
    if unexpected:
        print("DEMO finished with unexpected outcomes", file=sys.stderr)
        return 2
    print("DEMO OK")
    return 0

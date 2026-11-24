# 定长契勘台（copybook-kit）

一个离线、可复现的定长主机文件契约工具：把 copybook 子集编译成带字节偏移的布局，
按布局切分与解码记录，执行声明式字段/记录/文件规则，并把每次摄入写入幂等台账。
它回答三个问题：**这份文件按契约怎么切、哪些内容无效、结果能否原样复现**。

仓库只使用合成 fixture：`DEMO-*` 机构与单号、虚构客户、固定种子生成的数据；
运行期不发起任何网络请求，控制台只监听 `127.0.0.1`。

## 快速开始

```bash
python -m pip install -e .
make demo          # 六个场景走完 编译 -> 解码 -> 校验 -> 台账 -> 报告
pytest -q          # 合成回归，含黄金样例、逐字节复现与离线计息检查
python -m copybook_kit.cli serve --port 5000
```

要求 Python 3.10+；核心使用标准库，Flask 只用于只读控制台，PyYAML 只用于规则文件。

## DEMO 场景

| 场景 | 数据 | 契约 | 预期 |
| --- | --- | --- | --- |
| 正常客户文件 | `customer_normal.dat` | `customer.cpy` | `completed`，7 条全部接收 |
| 含拒绝记录 | `customer_rejects.dat` | `customer.cpy` | `completed_with_rejects`，4 条隔离 |
| 截断文件 | `customer_truncated.dat` | `customer.cpy` | `failed`，RD002 + FL001 |
| 必然失败负例 | `customer_normal.dat` | `negative_rules.yaml` | `failed`，FL003 |
| OCCURS/REDEFINES | `account_normal.dat` | `account.cpy` | `completed` |
| COMP-3 余额 | `balance_normal.dat` | `balance.cpy` | `completed` |

每条拒绝都保留原记录与绝对字节偏移；台账记录父/子血缘，`reprocess` 可基于同一输入
换规则重跑，`resume` 可重放中断运行。

## CLI

```bash
copybook-kit validate --copybook fixtures/copybooks/customer.cpy
copybook-kit run --input fixtures/data/customer_normal.dat \
  --copybook fixtures/copybooks/customer.cpy \
  --rules fixtures/rules/customer_rules.yaml \
  --db build/ledger.sqlite --out build/runs --now 2026-01-01T00:00:00Z
copybook-kit resume --run-id <run-id> --db build/ledger.sqlite
copybook-kit reprocess --run-id <run-id> --rules fixtures/rules/negative_rules.yaml
copybook-kit accrue-balances --manifest fixtures/revision_manifest.example.json \
  --db build/ledger.sqlite --start 2026-02-28 --end 2026-03-05 \
  --route historical
copybook-kit profile --run-id <run-id> --db build/ledger.sqlite
copybook-kit serve --db build/ledger.sqlite --port 5000
copybook-kit demo
```

`validate` 输出字段树（offset/length/pic/usage/scale/occurs/redefines）、记录总长、
warnings 与 errors；存在任何 CB 错误时退出码为 2。运行退出码：`0` 完成，
`3` 文件级失败（截断、约束不满足），`2` CLI/契约/IO 错误。

## 离线多日计息与修订权威

`accrue-balances` 不重新摄入文件，只读取已完成运行的台账与 `accepted.jsonl` /
`rejects.jsonl`，原运行及其产物始终只读。版本权威只来自显式的本地修订清单
（示例见 `fixtures/revision_manifest.example.json`）：每条给出 `run_id`、该文件的
`business_date`（须与记录 `BOOK-DATE` 一致）、预期 `ACCOUNT-ID` 清单、被替代
`supersedes_run_id`、`authorized_at` 与 `close_at`。同账户同日期的两个独立 run
若没有可核对的替代链即视为冲突，不按文件名、运行创建时间或清单顺序猜版本。

- **原账路线（`--route historical`，默认）**：以关账时刻之前已授权的版本冻结当时
  每账户每天的状态；后来的替代文件只进入差异披露，不改动旧结论。
- **重述路线（`--route restatement --as-of <ISO-8601>`）**：按报告指定的后续授权
  截止时刻沿修订链选择最新有效版本，重算受影响日期起的利息，并同时输出旧/新结果
  与可追溯差额；两条路线各自带独立来源标签，重述不覆盖原账。

期间为半开日区间 `[start, end)`；某日快照自当日生效至下一快照前一日，每日金额为
`AMOUNT × RATE / 365`，跨日用 `Decimal` 全精度相加，仅在报告最终按 half-even
舍入到分，绝不逐日舍入，也不使用 float。拒绝记录、`max-records` 前缀、文件级失败、
输入/规则/`accepted.jsonl` 摘要不匹配都会让受影响账户/日期进入未知区间；拒绝记录
无法可靠识别 `ACCOUNT-ID` 时该文件全部预期账户在该日期未知，缺行不会被解释为零
余额。修订链分叉/成环或开始日无已授权状态时输出分段未知且不产生整期确定金额，
无冲突的账户仍可独立结算。

## 目录

```
copybook_kit/
  copybook/   固定格式词法、子句解析、布局计算
  decode/     DISPLAY / 带符号 / COMP-3 解码与稳定诊断
  rules/      规则 schema、字段检查、记录/文件引擎
  pipeline/   读取、处理、幂等运行、断点、画像、报告、离线计息（accrual.py）
  store/      SQLite 台账 schema 与查询
  report/     Markdown / 自包含 HTML
  web/        127.0.0.1 只读控制台
fixtures/     三个 copybook、四份规则、合成数据、确定性生成器、修订清单示例
docs/         子集、规则契约、输出与台账说明
tests/        解析、布局、解码、规则、管道、台账、恢复、报告、Web、CLI、黄金样例
```

## 复现性

运行键由输入、copybook、规则、字符集、RECFM、记录上限与父运行的 SHA-256 指纹决定；
重复提交同一组合直接返回既有结果并标记 `idempotent=true`，不会重复入账。
JSON 使用排序键与紧凑分隔符，Decimal 按声明 scale 输出字符串；时间戳一律由
`--now` 注入（测试固定为 `2026-01-01T00:00:00Z`），因此同一输入的报告与台账可逐字节比较。

## 稳定错误码

`CB001`–`CB009` copybook，`RD001`–`RD004` 读取，`DE001`–`DE003` 解码，
`R001`–`R010` 字段/记录规则，`FL001`–`FL005` 文件规则，`RC001`–`RC004` 规则契约，
`LD001`–`LD003` 台账，`IO001` 文件系统，`CLI001` 参数。完整清单见
[docs/CONTRACT.md](docs/CONTRACT.md)，子集与显式拒绝清单见
[docs/COPYBOOK_SUBSET.md](docs/COPYBOOK_SUBSET.md)，产物与台账见
[docs/OUTPUTS.md](docs/OUTPUTS.md)。

## 非目标

不是通用 ETL/调度平台，不访问主机/FTP/MQ/外部数据库/云，不做脱敏、迁移或超集兼容；
不支持的 copybook 语法会显式报错，而不是静默猜测。

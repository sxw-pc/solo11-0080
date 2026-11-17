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
pytest -q          # 101 个测试，含黄金样例、逐字节复现与复核抽样检查
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
copybook-kit profile --run-id <run-id> --db build/ledger.sqlite
copybook-kit plan-reject-audit --request audit-request.json --db build/ledger.sqlite
copybook-kit serve --db build/ledger.sqlite --port 5000
copybook-kit demo
```

`validate` 输出字段树（offset/length/pic/usage/scale/occurs/redefines）、记录总长、
warnings 与 errors；存在任何 CB 错误时退出码为 2。运行退出码：`0` 完成，
`3` 文件级失败（截断、约束不满足），`2` CLI/契约/IO 错误。

## 离线拒绝复核抽样（plan-reject-audit）

人工复核名额有限，而一轮摄入可能产生大量拒绝。`plan-reject-audit` 从**已完成** run 的
拒绝台账生成只读复核样本清单：输入每个错误码需要的独立记录数 `quotas`、总审阅上限
`budget`，以及可选的“不可同时披露”记录组 `exclusion_groups`（按 `record_no` 给出，
同组至多入选一条）。一条拒绝携带多个码时可同时满足多个配额，但同一码下一条记录只计一次。

请求 JSON 示例：

```json
{
  "run_id": "run-...",
  "quotas": {"R002_ENUM": 2, "R005_PATTERN": 1},
  "budget": 3,
  "exclusion_groups": [[7, 12]],
  "contract": "exact",
  "node_budget": 2000000
}
```

两种都正确但保证不同的合同：

- `exact`（精确审计）：仅在声明的小规模上限内（候选与预算各 ≤ 24）做穷举分支限界，
  返回**证明为最小**的记录集；不可行时给出可核对的缺口证书（候选不足、预算下界或
  穷尽搜索无可行选择）。
- `bounded`（有界规划）：在固定节点预算 `node_budget` 内寻找**任意**可行集合，找到即可用
  但**不声称最小**；预算耗尽只报 `undecided` 并列出已证实的部分覆盖，绝不把“未判定”
  冒充“不可行”。

计划只引用台账中真实的 `reject_no`、`record_no`、`byte_offset`、`raw_sha256` 与
`raw_file`；生成前会重新哈希 `rejects/*.bin` 原字节并与台账摘要核对（不一致报 AU001），
绝不从 `partial` 解码值重造证据。FL 文件级问题没有拒绝行，不会被伪装成可抽样错误码。
输出含每个配额由哪些原记录覆盖、未满足配额的证明/未知状态、所用合同与计算预算、
输入运行及规则指纹；记录顺序按稳定的 `(record_no, raw_sha256)` 规则，不受 SQLite
返回顺序影响，且不改动任何已有 run 与原始拒绝字节。

## 目录

```
copybook_kit/
  copybook/   固定格式词法、子句解析、布局计算
  decode/     DISPLAY / 带符号 / COMP-3 解码与稳定诊断
  rules/      规则 schema、字段检查、记录/文件引擎
  pipeline/   读取、处理、幂等运行、断点、画像、报告、拒绝复核抽样
  store/      SQLite 台账 schema 与查询
  report/     Markdown / 自包含 HTML
  web/        127.0.0.1 只读控制台
fixtures/     三个 copybook、四份规则、合成数据与确定性生成器
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
`LD001`–`LD003` 台账，`IO001` 文件系统，`CLI001` 参数，`AU001` 复核证据原字节校验失败，
`AU002` 复核计划请求或合同规模非法。完整清单见
[docs/CONTRACT.md](docs/CONTRACT.md)，子集与显式拒绝清单见
[docs/COPYBOOK_SUBSET.md](docs/COPYBOOK_SUBSET.md)，产物与台账见
[docs/OUTPUTS.md](docs/OUTPUTS.md)。

## 非目标

不是通用 ETL/调度平台，不访问主机/FTP/MQ/外部数据库/云，不做脱敏、迁移或超集兼容；
不支持的 copybook 语法会显式报错，而不是静默猜测。

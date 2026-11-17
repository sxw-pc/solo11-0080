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
pytest -q          # 84 个测试，含黄金样例与逐字节复现检查
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
copybook-kit serve --db build/ledger.sqlite --port 5000
copybook-kit demo
```

`validate` 输出字段树（offset/length/pic/usage/scale/occurs/redefines）、记录总长、
warnings 与 errors；存在任何 CB 错误时退出码为 2。运行退出码：`0` 完成，
`3` 文件级失败（截断、约束不满足），`2` CLI/契约/IO 错误。

## 离线 copybook 替换认证（certify-copybook）

替换定长文件的 copybook 前，先离线回答：旧规则下已接收的每条原始记录，指定业务字段在
新布局下是否仍解码为同一值，且记录边界、偏移、字符集、Decimal scale、OCCURS 下标与
REDEFINES 共用存储没有被静默换义。

```bash
copybook-kit certify-copybook \
  --old-copybook old.cpy --new-copybook new.cpy \
  --rules rules.yaml --input archive.dat \
  --field CUSTOMER-ID --field BALANCE \
  --required-level file-reuse --output certificate.json
```

凭证是带摘要的 JSON，包含两份 copybook、规则与输入的 SHA-256、两级判定、逐字段证据与
可复核反例，并用覆盖全部声明的 `digest` 封缄。两个等级互不替对方背书：

| 等级 | 证明方式 | 授权路线 |
| --- | --- | --- |
| `file-reuse` 本文件可复用 | 用两版布局逐条解释本次字节并比较指定路径业务值；旧版拒绝记录单列隔离，不算兼容通过 | 仅允许这一份归档文件按新版复用 |
| `universal` 通用可替换 | 对旧布局允许的全部字段字节做静态等价证明，不枚举输入、不把样本当证明；不支持的 PIC/字符集/REDEFINES 情形判为 `unknown` 而非通过 | 允许未来同契约文件无重采集地切换解析 |

同一份旧 `X(4)` 字段恰好全是数字、新版改成 `9(4)` 的文件，可以 `file-reuse` 通过而
`universal` 拒绝（静态引擎给出字母字节反例）。本文件级的新解码失败或值变化会记录记录号、
字段路径、旧/新字节范围、字段十六进制与输入摘要。

带凭证启用新版子运行时由 run 入口门禁驱动，未达所请求等级（`CF002`）或凭证与输入/任一
copybook 字节不符（`CF001`/`CF003`）时阻止新子运行；达到等级时子运行台账留下凭证摘要事件，
并在运行目录保存 `certificate.json` 与 `old_copybook.cpy`，原始输入与旧运行保持可回读。
普通 run/reprocess 不提供凭证时行为与历史完全一致，不倒改台账。

```bash
copybook-kit run --input archive.dat --copybook new.cpy --rules rules.yaml \
  --certificate certificate.json --required-level file-reuse --old-copybook old.cpy
```

## 目录

```
copybook_kit/
  copybook/   固定格式词法、子句解析、布局计算
  decode/     DISPLAY / 带符号 / COMP-3 解码与稳定诊断
  certify/    离线两级 copybook 替换认证、凭证封缄与门禁校验
  rules/      规则 schema、字段检查、记录/文件引擎
  pipeline/   读取、处理、幂等运行、断点、画像、报告
  store/      SQLite 台账 schema 与查询
  report/     Markdown / 自包含 HTML
  web/        127.0.0.1 只读控制台
fixtures/     三个 copybook、四份规则、合成数据与确定性生成器
docs/         子集、规则契约、输出与台账说明
tests/        解析、布局、解码、规则、认证、管道、台账、恢复、报告、Web、CLI、黄金样例
```

## 复现性

运行键由输入、copybook、规则、字符集、RECFM、记录上限与父运行的 SHA-256 指纹决定；
重复提交同一组合直接返回既有结果并标记 `idempotent=true`，不会重复入账。
JSON 使用排序键与紧凑分隔符，Decimal 按声明 scale 输出字符串；时间戳一律由
`--now` 注入（测试固定为 `2026-01-01T00:00:00Z`），因此同一输入的报告与台账可逐字节比较。

## 稳定错误码

`CB001`–`CB009` copybook，`RD001`–`RD004` 读取，`DE001`–`DE003` 解码，
`R001`–`R010` 字段/记录规则，`FL001`–`FL005` 文件规则，`RC001`–`RC004` 规则契约，
`LD001`–`LD003` 台账，`IO001` 文件系统，`CLI001` 参数，
`CF001`–`CF003` 替换凭证（材料失配、等级不足、凭证失效）。完整清单见
[docs/CONTRACT.md](docs/CONTRACT.md)，子集与显式拒绝清单见
[docs/COPYBOOK_SUBSET.md](docs/COPYBOOK_SUBSET.md)，产物与台账见
[docs/OUTPUTS.md](docs/OUTPUTS.md)。

## 非目标

不是通用 ETL/调度平台，不访问主机/FTP/MQ/外部数据库/云，不做脱敏、迁移或超集兼容；
不支持的 copybook 语法会显式报错，而不是静默猜测。

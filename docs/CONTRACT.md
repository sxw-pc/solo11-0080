# 规则契约

规则文件是 UTF-8 YAML，最大 512 KiB。顶层只允许
`version, name, record_type, trailer, limits, fields, record_rules`；重复键、未知键、
浮点数字面量都会被拒绝（`RC001` / `RC003`），避免隐式精度和静默拼写错误。
`fields` 中的键必须是布局里存在的 elementary 字段名，否则 `RC004`。

## fields

```yaml
fields:
  CUSTOMER-ID:
    required: true              # 空值 -> R001_REQUIRED
    enum: ["A", "I"]            # -> R002_ENUM
    range: {min: "1", max: "9"} # 数值或日期区间 -> R003_RANGE
    date_format: "%Y%m%d"       # -> R004_DATE
    pattern: "^CUST[0-9]{6}$"   # 整串匹配 -> R005_PATTERN
    check_digit: {weights: [2,3,4,5,6], modulus: 11}  # -> R006_CHECK_DIGIT
    scale: 2                    # 实际 Decimal 小数位超过 2 -> R007_SCALE
    blank: null                 # null | zero | error（空值策略，默认 null）
```

检查顺序固定为：required → 日期格式 → enum → range → pattern → check digit → scale；
空值仅执行 required/blank 语义，避免重复报错。日期区间边界既可用同一 `date_format`，
也可用 ISO `YYYY-MM-DD`。正则最大 200 字符，禁止反向引用、环视与嵌套量词（`RC002`）。
校验和权重为 1–9 的整数，模数 2–97，值长度必须等于权重数 + 1。

## record_rules

```yaml
record_rules:
  - name: customer-id-unique
    kind: unique_key
    fields: [CUSTOMER-ID]       # 复合键按字段顺序拼接；重复/缺失 -> R008_UNIQUE
  - name: renewal-not-before-open
    kind: cross_field
    left: OPEN-DATE
    op: "<="                    # <= | < | == | !=
    right: RENEW-DATE           # 失败 -> R009_CROSS_FIELD
```

唯一键状态只在整条记录没有其他命中时提交；一条记录同时触发其他规则时，不会污染后续
文件的唯一性判断。比较两侧能解析为 `Decimal` 时按数值比较，否则按字符串比较。

## record_type / trailer / limits

```yaml
record_type: {field: REC-TYPE, data: D, header: H, trailer: T}
trailer: {count_field: OPEN-DATE, amount_field: BALANCE}
limits: {min_records: 1, max_records: 100}
```

- 未声明的记录类型 -> `R010_RECORD_TYPE`；header/trailer 不参与业务字段校验。
- 首条不是 header、中间出现 header/trailer、末条不是 trailer -> `FL005`。
- trailer 缺失或数量不符 -> `FL001`；金额合计不符 -> `FL002`。
- 数据记录数低于 `min_records` -> `FL003`，高于 `max_records` -> `FL004`。

## 码表

| 类别 | 码 |
| --- | --- |
| 字段规则 | `R001_REQUIRED` `R002_ENUM` `R003_RANGE` `R004_DATE` `R005_PATTERN` `R006_CHECK_DIGIT` `R007_SCALE` |
| 记录规则 | `R008_UNIQUE` `R009_CROSS_FIELD` `R010_RECORD_TYPE` |
| 文件规则 | `FL001` 数量/缺失、`FL002` 金额、`FL003` 下限、`FL004` 上限、`FL005` 位置 |
| 规则契约 | `RC001` schema、`RC002` 正则、`RC003` 值类型/范围、`RC004` 未知字段 |
| 读取/解码 | `RD001` 字符集、`RD002` 截断、`RD003` 空文件、`RD004` 记录上限；`DE001` DISPLAY、`DE002` COMP-3、`DE003` 空值策略 |
| 台账/IO/CLI | `LD001` 未完成同键运行、`LD002` 运行不存在、`LD003` 键冲突；`IO001`；`CLI001` |
| 替换凭证 | `CF001` 凭证与输入/规则/copybook 字节失配、`CF002` 未达请求等级或门禁参数缺失、`CF003` 凭证 JSON 损坏或摘要不符 |

## copybook 替换凭证（certify-copybook）

凭证回答一个业务问题：旧规则下已接收的原始记录，指定业务字段路径在新布局下是否仍解码为
同一值，且记录边界、偏移、字符集、Decimal scale、OCCURS 下标、REDEFINES 共用存储未被静默
换义。凭证 JSON 由覆盖全部声明的 `digest` 封缄，含两份 copybook、规则与输入的 SHA-256、
两级判定、逐字段字节证据与可复核反例。

- `file-reuse`（本文件可复用）：用两版布局逐条解释本次字节并比较指定路径业务值。旧版拒绝
  记录进入 `legacy_rejects` 单列隔离，绝不伪装成兼容通过；新解码失败或值变化给出记录号、
  字段路径、旧/新字节范围、字段十六进制与输入摘要。只授权这一份归档文件。
- `universal`（通用可替换）：对旧布局允许的全部字段字节做静态等价证明，不枚举输入、不把
  样本当证明。同存储同 PIC 同字符集判 `pass`；类别/USAGE/scale/偏移变化给出构造性反例判
  `reject`；REDEFINES 共用存储等无法静态证明的情形判 `unknown`，绝不默认通过。授权未来同
  契约文件无重采集切换解析。

两个等级互不替对方背书。`run` 带 `--certificate/--required-level/--old-copybook` 时由入口
门禁校验：未达等级抛 `CF002` 阻止新子运行，材料或摘要失配抛 `CF001`/`CF003`；通过时子运行
台账记录 `certificate_attached` 事件并在运行目录保存 `certificate.json` 与 `old_copybook.cpy`。
不带凭证的普通 run/reprocess 保持历史行为，run_key 不含凭证字段。

# 供应商历史数据导入工具

把 ZIP 压缩包里的 CSV / JSONL 供应商文件转换为统一的入库明细 (SQLite)。
不同供应商的字段名、日期格式、金额单位由 JSON 规则配置管理，每次导入
逐条记录实际使用的规则版本。

纯 Python 3.11 标准库实现，无第三方依赖。

## 功能一览

| 需求 | 实现 |
|---|---|
| 统一入库明细 | `inbound_records` 表：供应商、单据号、行号、修订、物料、数量、单价、金额、币种、日期 |
| 规则配置化 + 版本记录 | `rules/*.json`，内容哈希即版本标识，存入 `rule_versions`，每条入库/隔离记录都引用 |
| 原件保留 + 溯源 | 压缩包存 `store/archives/<sha256>.zip`，文件存 `store/files/<sha256>`；每条记录带来源文件 + 原始物理行号 |
| 十进制金额 | 全程 `Decimal`，库中以字符串存储，支持单位换算 (分→元、万元→元) 与欧陆数字格式 |
| 隔离区 | 空值 / 格式错误 / 缺关键字段 / 版本冲突 → `quarantine`，含具体原因；不填默认值，不拖垮整份文件 |
| 幂等导入 | 压缩包、文件均按 SHA-256 去重；同名不同内容识别为新文件；记录级同内容跳过 |
| 修订版本 | 同 (供应商, 单据号, 行号) 高修订版成为当前记录，旧版保留可查；同版本内容冲突阻止替换并进隔离区 |
| 重放 | 修正规则后 `replay` 重放隔离记录；原失败记录保留，重放结果写入 `quarantine_replays` |
| ZIP 安全 | 拒绝路径穿越 (`..`、绝对路径、盘符、反斜杠、符号链接) 与超上限内容 (文件数/单文件/总大小/压缩比)，违规整包拒绝并清理半成品 |

## 快速开始

```bash
cd supplier_import_tool

# 生成样例数据 (samples/out/)
python3 samples/make_samples.py

# 批量导入 (ZIP / CSV / JSONL / 目录均可, 可多个)
python3 -m supplier_importer --db data/importer.db import \
    samples/out/mixed_suppliers.zip samples/out/bad_rows.zip \
    --rules rules --report data/report.html

# 修订流程
python3 -m supplier_importer --db data/importer.db import samples/out/rev1.zip --rules rules
python3 -m supplier_importer --db data/importer.db import samples/out/rev2.zip --rules rules
python3 -m supplier_importer --db data/importer.db import samples/out/conflict_rev1.zip --rules rules

# 查看
python3 -m supplier_importer --db data/importer.db records [--all] [--supplier ACME]
python3 -m supplier_importer --db data/importer.db quarantine [--status all]
python3 -m supplier_importer --db data/importer.db runs

# 修正规则后重放隔离区
python3 -m supplier_importer --db data/importer.db replay --rules rules \
    [--supplier ACME] [--ids 3,4] --report data/replay.html

# 补出历史批次报告
python3 -m supplier_importer --db data/importer.db report --run 1 --out data/r1.html
```

解压上限可在导入时调整：`--max-files / --max-total-mb / --max-file-mb / --max-ratio`。

## 规则配置 (rules/*.json)

```jsonc
{
  "supplier": "BOMCORP",
  "version": "1.1.0",
  "priority": 10,                       // 自动识别时的匹配优先级
  "match": {                            // 自动识别: 文件名模式 + 必需表头
    "filename": ["*bomcorp*"],
    "required_headers": ["Belegnr", "Position"]
  },
  "csv":  { "encoding": "utf-8-sig", "delimiter": ";" },
  "number": { "thousands_separator": ".", "decimal_point": "," },
  "fields": {
    "doc_no":  { "source": "Belegnr",  "required": true },
    "line_no": { "source": "Position", "required": true, "type": "int" },
    "amount":  { "source": "Gesamtpreis", "required": true, "type": "decimal",
                 "multiplier": "0.01" },          // 单位换算: 分 -> 元
    "doc_date": { "source": "Belegdatum", "required": true, "type": "date",
                  "formats": ["%d.%m.%Y"] }
  }
}
```

- 目标字段固定为 `doc_no, line_no, revision, item_code, description,
  quantity, unit_price, amount, currency, doc_date`；`doc_no`/`line_no` 必须映射。
- `revision` 不映射时按规则定义为 1（供应商无修订概念）；映射但为空则进隔离区。
- 未映射的原始列全部进 `extra_json` 保留，不丢数据。
- 配置内容哈希即规则版本：改配置即产生新版本，历史记录仍指向旧版本。

## 数据目录结构

```
data/
  importer.db            # SQLite
  store/
    archives/<sha256>.zip   # 原始压缩包留档
    files/<sha256>.<ext>    # 进入导入流程的文件留档
```

## 样例数据 (samples/out/)

| 文件 | 说明 |
|---|---|
| `mixed_suppliers.zip` | 三供应商混装：普通 CSV、带 BOM 的分号 CSV (欧陆数字、金额单位为分)、JSONL (金额为万元) |
| `quoted.zip` | 带引号内嵌换行与 `""` 转义的字段 |
| `bad_rows.zip` | 缺单据号 / 空数量 / 坏日期 / 非数字单价，夹两条好数据 |
| `rev1.zip` → `rev2.zip` | 修订升级：高版本成为当前记录 |
| `conflict_rev1.zip` | 同版本内容冲突：阻止替换并进隔离区 |
| `iso_dates.zip` | ISO 日期，配合旧规则演示 "隔离 → 修规则 → 重放" |
| `evil_paths.zip` | 路径穿越条目，整包拒绝 |
| `evil_bomb.zip` | 30 MB 高压缩比内容，超上限拒绝 |

## 测试

```bash
python3 -m unittest discover -s tests -v    # 17 个用例
```

覆盖：三供应商混导、BOM、引号换行与物理行号、坏行隔离且文件存活、
压缩包/文件/记录三级幂等、同名不同内容新文件、修订升级与冲突阻止、
重放成功与仍失败、路径穿越/炸弹拒绝与清理、十进制精确性、报告生成。

"""生成演示与测试用的样例数据, 输出到 samples/out/.

包含:
- 三个供应商的不同格式 (普通 CSV / 带 BOM 的分号 CSV / JSONL)
- 带引号内嵌换行与引号转义的字段
- 混合坏行 (缺关键字段、空值、格式错误) 的文件
- 修订文件 (rev1 -> rev2 升级, 以及同版本内容冲突)
- 恶意压缩包 (路径穿越 / 超上限), 用于验证拒绝逻辑
"""

from __future__ import annotations

import io
import json
import shutil
import zipfile
from pathlib import Path

OUT = Path(__file__).parent / "out"

# ---------------------------------------------------------------- ACME 普通 CSV
ACME_CSV = """Order No,Line,Rev,SKU,Description,Qty,Unit Price,Amount,Currency,Order Date
A-1001,1,1,SKU-RED,Red widget,10,1.50,15.00,USD,03/15/2024
A-1001,2,1,SKU-BLU,Blue widget,5,2.00,10.00,USD,03/15/2024
A-1002,1,1,SKU-GRN,Green gadget,3,9.99,29.97,USD,03/16/2024
"""

# 带引号内嵌换行与双引号转义的描述字段
QUOTED_CSV = (
    "Order No,Line,Rev,SKU,Description,Qty,Unit Price,Amount,Currency,Order Date\n"
    'A-2001,1,1,SKU-MLT,"Multi-line description:\nsecond line\nthird line",'
    "2,4.25,8.50,USD,04/01/2024\n"
    'A-2001,2,1,SKU-QTE,"He said ""hello"" loudly",1,7.00,7.00,USD,04/01/2024\n'
)

# 修订版: A-1001 第 1 行数量 10 -> 12 (Rev 2), 新增一行
ACME_REV2_CSV = """Order No,Line,Rev,SKU,Description,Qty,Unit Price,Amount,Currency,Order Date
A-1001,1,2,SKU-RED,Red widget,12,1.50,18.00,USD,03/15/2024
A-1001,3,1,SKU-YLW,Yellow add-on,1,3.00,3.00,USD,03/15/2024
"""

# ISO 日期 (YYYY-MM-DD): 用于演示 "规则不支持 -> 隔离 -> 修规则 -> 重放"
ACME_ISO_CSV = """Order No,Line,Rev,SKU,Description,Qty,Unit Price,Amount,Currency,Order Date
A-4001,1,1,SKU-ISO,ISO dated row,2,3.00,6.00,USD,2024-05-01
"""

# 同版本冲突: A-1001 第 2 行仍是 Rev 1 但数量被改成 6
ACME_CONFLICT_CSV = """Order No,Line,Rev,SKU,Description,Qty,Unit Price,Amount,Currency,Order Date
A-1001,2,1,SKU-BLU,Blue widget,6,2.00,12.00,USD,03/15/2024
"""

# 混合坏行: 缺单据号 / 空数量 / 坏日期 / 非数字单价, 夹两条好数据
BAD_ROWS_CSV = """Order No,Line,Rev,SKU,Description,Qty,Unit Price,Amount,Currency,Order Date
A-3001,1,1,SKU-OK1,Good row one,2,1.00,2.00,USD,05/01/2024
,2,1,SKU-NOD,Missing doc no,1,1.00,1.00,USD,05/01/2024
A-3001,3,1,SKU-NOQ,Empty qty,,1.00,1.00,USD,05/01/2024
A-3001,4,1,SKU-BDT,Bad date,1,1.00,1.00,USD,2024-13-45
A-3001,5,1,SKU-BPR,Bad price,1,abc,1.00,USD,05/01/2024
A-3002,1,1,SKU-OK2,Good row two,4,2.50,10.00,USD,05/02/2024
"""

# ------------------------------------------------------- BOMCORP 带 BOM 的 CSV
# utf-8 BOM + 分号分隔 + 德语表头 + 欧陆数字 (1.234,56) + 金额单位: 分
BOMCORP_CSV = (
    "Belegnr;Position;Version;Artikelnr;Bezeichnung;Menge;Einzelpreis;"
    "Gesamtpreis;Waehrung;Belegdatum\n"
    "B-5001;1;1;ART-001;Schraube M6;100;150;15000;EUR;15.03.2024\n"
    "B-5001;2;1;ART-002;Mutter M6;100;50;5000;EUR;15.03.2024\n"
    "B-5002;1;1;ART-003;Dichtung, ø50mm;2;1.234,56;2.469,12;EUR;16.03.2024\n"
)

# ------------------------------------------------------------------ JSONL 文件
JSONL_LINES = [
    {"orderId": "J-9001", "lineNo": 1, "rev": 1, "sku": "SKU-J1",
     "title": "JSON widget", "qty": "7", "priceYuan": "3.50",
     "totalWan": "0.00245", "ccy": "CNY", "ordered": "2024/03/20",
     "remark": "供应商自定义字段"},
    {"orderId": "J-9001", "lineNo": 2, "rev": 1, "sku": "SKU-J2",
     "title": "JSON gadget", "qty": "1", "priceYuan": "99.00",
     "totalWan": "0.0099", "ccy": "CNY", "ordered": "2024/03/20"},
    {"orderId": "", "lineNo": 3, "rev": 1, "sku": "SKU-J3",
     "title": "missing order id", "qty": "1", "priceYuan": "1.00",
     "totalWan": "0.0001", "ccy": "CNY", "ordered": "2024/03/21"},
]
JSONL_TEXT = "\n".join(json.dumps(o, ensure_ascii=False) for o in JSONL_LINES) + "\n"
JSONL_TEXT += "{this is not valid json}\n"


def write(path: Path, text: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding=encoding)


def make_zip(path: Path, members: dict[str, str | bytes],
             bom_names: set[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in members.items():
            if isinstance(content, str):
                enc = "utf-8-sig" if bom_names and name in bom_names else "utf-8"
                content = content.encode(enc)
            zf.writestr(name, content)


def main() -> None:
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    # 散装文件
    write(OUT / "loose" / "acme_orders_2024.csv", ACME_CSV)
    write(OUT / "loose" / "acme_quoted_fields.csv", QUOTED_CSV)
    write(OUT / "loose" / "jsonline_shipments.jsonl", JSONL_TEXT)

    # 正常压缩包: 三个供应商混装 (BOMCORP 文件带 BOM)
    make_zip(OUT / "mixed_suppliers.zip", {
        "acme/acme_orders_2024.csv": ACME_CSV,
        "bomcorp/bomcorp_invoice.csv": BOMCORP_CSV,
        "jsonline/jsonline_shipments.jsonl": JSONL_TEXT,
        "readme.txt": "非数据文件, 应被跳过",
    }, bom_names={"bomcorp/bomcorp_invoice.csv"})

    # 坏行文件包
    make_zip(OUT / "bad_rows.zip", {"acme_bad_rows.csv": BAD_ROWS_CSV})

    # 修订流程包: 先 rev1, 再 rev2, 再同版本冲突
    make_zip(OUT / "rev1.zip", {"acme_rev1.csv": ACME_CSV})
    make_zip(OUT / "rev2.zip", {"acme_rev2.csv": ACME_REV2_CSV})
    make_zip(OUT / "conflict_rev1.zip", {"acme_conflict.csv": ACME_CONFLICT_CSV})

    # 带引号换行字段的包
    make_zip(OUT / "quoted.zip", {"acme_quoted.csv": QUOTED_CSV})

    # ISO 日期文件: 配合旧版规则演示隔离与重放
    make_zip(OUT / "iso_dates.zip", {"acme_iso_dates.csv": ACME_ISO_CSV})

    # ---- 恶意压缩包: 路径穿越 ----
    evil = OUT / "evil_paths.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../escape.txt", "should never be written")
        zf.writestr("../../etc/pwned.txt", "should never be written")
        zf.writestr("ok/acme_orders_2024.csv", ACME_CSV)

    # ---- 恶意压缩包: 超大解压内容 (高压缩比的零字节流) ----
    bomb = OUT / "evil_bomb.zip"
    payload = b"0" * (30 * 1024 * 1024)   # 30 MB, 超过默认单文件 20 MB 上限
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("big.csv", payload)

    print(f"样例已生成到 {OUT}")
    for p in sorted(OUT.rglob("*")):
        if p.is_file():
            print(f"  {p.relative_to(OUT)}  ({p.stat().st_size} bytes)")


if __name__ == "__main__":
    main()

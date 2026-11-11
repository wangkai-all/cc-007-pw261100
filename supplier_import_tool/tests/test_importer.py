"""端到端与单元测试. 运行: python3 -m unittest discover -s tests -v"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

TOOL_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOL_ROOT))

from supplier_importer.archive import ArchiveSafetyError, safe_extract_zip  # noqa: E402
from supplier_importer.db import connect  # noqa: E402
from supplier_importer.importer import Importer  # noqa: E402
from supplier_importer.report import generate_report  # noqa: E402

RULES = TOOL_ROOT / "rules"

ACME_CSV = """Order No,Line,Rev,SKU,Description,Qty,Unit Price,Amount,Currency,Order Date
A-1001,1,1,SKU-RED,Red widget,10,1.50,15.00,USD,03/15/2024
A-1001,2,1,SKU-BLU,Blue widget,5,2.00,10.00,USD,03/15/2024
A-1002,1,1,SKU-GRN,Green gadget,3,9.99,29.97,USD,03/16/2024
"""

QUOTED_CSV = (
    "Order No,Line,Rev,SKU,Description,Qty,Unit Price,Amount,Currency,Order Date\n"
    'A-2001,1,1,SKU-MLT,"Multi-line description:\nsecond line\nthird line",'
    "2,4.25,8.50,USD,04/01/2024\n"
    'A-2001,2,1,SKU-QTE,"He said ""hello"" loudly",1,7.00,7.00,USD,04/01/2024\n'
)

BAD_ROWS_CSV = """Order No,Line,Rev,SKU,Description,Qty,Unit Price,Amount,Currency,Order Date
A-3001,1,1,SKU-OK1,Good row one,2,1.00,2.00,USD,05/01/2024
,2,1,SKU-NOD,Missing doc no,1,1.00,1.00,USD,05/01/2024
A-3001,3,1,SKU-NOQ,Empty qty,,1.00,1.00,USD,05/01/2024
A-3001,4,1,SKU-BDT,Bad date,1,1.00,1.00,USD,2024-13-45
A-3001,5,1,SKU-BPR,Bad price,1,abc,1.00,USD,05/01/2024
A-3002,1,1,SKU-OK2,Good row two,4,2.50,10.00,USD,05/02/2024
"""

BOMCORP_CSV = (
    "Belegnr;Position;Version;Artikelnr;Bezeichnung;Menge;Einzelpreis;"
    "Gesamtpreis;Waehrung;Belegdatum\n"
    "B-5001;1;1;ART-001;Schraube M6;100;150;15000;EUR;15.03.2024\n"
    "B-5002;1;1;ART-003;Dichtung;2;1.234,56;2.469,12;EUR;16.03.2024\n"
)

JSONL_TEXT = (
    '{"orderId": "J-9001", "lineNo": 1, "rev": 1, "sku": "SKU-J1",'
    ' "title": "JSON widget", "qty": "7", "priceYuan": "3.50",'
    ' "totalWan": "0.00245", "ccy": "CNY", "ordered": "2024/03/20",'
    ' "remark": "自定义字段"}\n'
    '{"orderId": "", "lineNo": 2, "rev": 1, "sku": "SKU-J2",'
    ' "qty": "1", "priceYuan": "9.00", "totalWan": "0.0009",'
    ' "ordered": "2024/03/20"}\n'
    "{not valid json}\n"
)

REV2_CSV = """Order No,Line,Rev,SKU,Description,Qty,Unit Price,Amount,Currency,Order Date
A-1001,1,2,SKU-RED,Red widget,12,1.50,18.00,USD,03/15/2024
"""

CONFLICT_CSV = """Order No,Line,Rev,SKU,Description,Qty,Unit Price,Amount,Currency,Order Date
A-1001,2,1,SKU-BLU,Blue widget CHANGED,6,2.00,12.00,USD,03/15/2024
"""

ISO_DATE_CSV = """Order No,Line,Rev,SKU,Description,Qty,Unit Price,Amount,Currency,Order Date
A-4001,1,1,SKU-ISO,ISO date row,1,1.00,1.00,USD,2024-05-01
"""


def make_zip(path: Path, members: dict[str, bytes | str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, content in members.items():
            if isinstance(content, str):
                content = content.encode("utf-8")
            zf.writestr(name, content)
    return path


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="imp_test_"))
        self.db = self.tmp / "data" / "importer.db"
        self.imp = Importer(self.db)

    def tearDown(self):
        self.imp.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def query(self, sql, params=()):
        return self.imp.conn.execute(sql, params).fetchall()

    def one(self, sql, params=()):
        return self.imp.conn.execute(sql, params).fetchone()


class TestBasicImport(Base):
    def test_mixed_zip_three_suppliers(self):
        zp = make_zip(self.tmp / "mixed.zip", {
            "acme/acme_orders.csv": ACME_CSV,
            "bomcorp/bomcorp_invoice.csv": BOMCORP_CSV.encode("utf-8-sig"),
            "jsonline/shipments.jsonl": JSONL_TEXT,
            "readme.txt": "not data",
        })
        run_id, stats = self.imp.import_paths([zp], RULES)
        self.assertEqual(stats["archives_imported"], 1)
        self.assertEqual(stats["files_processed"], 3)
        self.assertEqual(stats["files_unsupported"], 1)   # readme.txt
        # ACME 3 + BOMCORP 2 + JSONL 1 条好数据
        self.assertEqual(stats["records_imported_current"], 6)
        # JSONL: 空 orderId + 非法 JSON 各一条
        self.assertEqual(stats["records_quarantined"], 2)

        # BOM 文件正确识别, 金额单位 分->元, 欧陆数字格式
        row = self.one("SELECT unit_price, amount FROM inbound_records"
                       " WHERE supplier='BOMCORP' AND doc_no='B-5001'")
        self.assertEqual(row["unit_price"], "1.5")
        self.assertEqual(row["amount"], "150")
        row2 = self.one("SELECT unit_price, amount FROM inbound_records"
                        " WHERE doc_no='B-5002'")
        self.assertEqual(row2["unit_price"], "12.3456")   # 1.234,56 分
        self.assertEqual(row2["amount"], "24.6912")

        # JSONL 万元->元, 十进制精确
        j = self.one("SELECT amount, extra_json FROM inbound_records"
                     " WHERE supplier='JSONLINE'")
        self.assertEqual(j["amount"], "24.5")             # 0.00245 万, 无浮点误差
        self.assertIn("自定义字段", j["extra_json"])       # 未映射字段保留

        # 隔离原因具体
        reasons = self.query("SELECT reasons FROM quarantine ORDER BY id")
        codes = [json.loads(r["reasons"])[0]["code"] for r in reasons]
        self.assertIn("missing_field", codes)
        self.assertIn("invalid_json", codes)

    def test_quoted_newline_fields_and_line_numbers(self):
        zp = make_zip(self.tmp / "quoted.zip", {"acme_quoted.csv": QUOTED_CSV})
        _, stats = self.imp.import_paths([zp], RULES)
        self.assertEqual(stats["records_imported_current"], 2)
        r1 = self.one("SELECT description, source_line_no FROM inbound_records"
                      " WHERE doc_no='A-2001' AND line_no=1")
        self.assertIn("second line", r1["description"])   # 内嵌换行保留
        self.assertEqual(r1["source_line_no"], 2)         # 记录起始物理行
        r2 = self.one("SELECT description, source_line_no FROM inbound_records"
                      " WHERE doc_no='A-2001' AND line_no=2")
        self.assertEqual(r2["source_line_no"], 5)         # 前一条占了 2-4 行
        self.assertIn('"hello"', r2["description"])       # 引号转义还原

    def test_bad_rows_quarantined_file_survives(self):
        zp = make_zip(self.tmp / "bad.zip", {"acme_bad.csv": BAD_ROWS_CSV})
        _, stats = self.imp.import_paths([zp], RULES)
        self.assertEqual(stats["records_imported_current"], 2)   # 两条好数据入库
        self.assertEqual(stats["records_quarantined"], 4)        # 四条进隔离区
        status = self.one("SELECT status FROM source_files")["status"]
        self.assertEqual(status, "imported")                     # 文件未被整体丢弃

        rows = self.query("SELECT reasons FROM quarantine ORDER BY id")
        codes = [json.loads(r["reasons"])[0]["code"] for r in rows]
        self.assertEqual(codes, ["missing_field", "missing_field",
                                 "bad_date", "bad_decimal"])
        # 空数量没有变成 0 或 NULL 蒙混入库
        gone = self.query("SELECT * FROM inbound_records WHERE doc_no='A-3001'")
        self.assertEqual(len(gone), 1)                           # 只有第 1 行入库
        self.assertEqual(gone[0]["quantity"], "2")

    def test_traceability_columns(self):
        zp = make_zip(self.tmp / "t.zip", {"acme/a.csv": ACME_CSV})
        self.imp.import_paths([zp], RULES)
        r = self.one(
            "SELECT r.source_line_no, sf.original_name, sf.inner_path,"
            "       a.original_name AS zip_name, rv.version"
            " FROM inbound_records r"
            " JOIN source_files sf ON sf.id=r.source_file_id"
            " JOIN archives a ON a.id=sf.archive_id"
            " JOIN rule_versions rv ON rv.id=r.rule_version_id"
            " WHERE r.doc_no='A-1001' AND r.line_no=2")
        self.assertEqual(r["source_line_no"], 3)
        self.assertEqual(r["inner_path"], "acme/a.csv")
        self.assertEqual(r["zip_name"], "t.zip")
        self.assertEqual(r["version"], "1.0.0")


class TestIdempotency(Base):
    def test_reimport_same_zip_no_duplicates(self):
        zp = make_zip(self.tmp / "a.zip", {"acme_a.csv": ACME_CSV})
        self.imp.import_paths([zp], RULES)
        n1 = self.one("SELECT COUNT(*) c FROM inbound_records")["c"]
        _, stats = self.imp.import_paths([zp], RULES)
        n2 = self.one("SELECT COUNT(*) c FROM inbound_records")["c"]
        self.assertEqual(n1, n2)
        self.assertEqual(stats["archives_skipped_duplicate"], 1)
        self.assertEqual(stats["records_imported_current"], 0)

    def test_same_file_in_different_zips_deduped(self):
        z1 = make_zip(self.tmp / "z1.zip", {"x/acme_a.csv": ACME_CSV})
        z2 = make_zip(self.tmp / "z2.zip", {"y/acme_a.csv": ACME_CSV})
        self.imp.import_paths([z1], RULES)
        _, stats = self.imp.import_paths([z2], RULES)
        self.assertEqual(stats["files_skipped_duplicate"], 1)
        self.assertEqual(self.one("SELECT COUNT(*) c FROM source_files")["c"], 1)
        self.assertEqual(self.one("SELECT COUNT(*) c FROM inbound_records")["c"], 3)

    def test_same_name_different_content_is_new_file(self):
        z1 = make_zip(self.tmp / "z1.zip", {"data/acme_same.csv": ACME_CSV})
        changed = ACME_CSV.replace("A-1002", "A-1003")
        z2 = make_zip(self.tmp / "z2.zip", {"data/acme_same.csv": changed})
        self.imp.import_paths([z1], RULES)
        _, stats = self.imp.import_paths([z2], RULES)
        self.assertEqual(stats["files_processed"], 1)
        files = self.query("SELECT original_name, sha256 FROM source_files")
        self.assertEqual(len(files), 2)                       # 识别为新文件
        self.assertEqual({f["original_name"] for f in files}, {"acme_same.csv"})
        self.assertNotEqual(files[0]["sha256"], files[1]["sha256"])
        self.assertIsNotNone(self.one(
            "SELECT 1 FROM inbound_records WHERE doc_no='A-1003'"))


class TestRevisions(Base):
    def test_higher_revision_becomes_current(self):
        z1 = make_zip(self.tmp / "r1.zip", {"acme_r1.csv": ACME_CSV})
        z2 = make_zip(self.tmp / "r2.zip", {"acme_r2.csv": REV2_CSV})
        self.imp.import_paths([z1], RULES)
        _, stats = self.imp.import_paths([z2], RULES)
        self.assertEqual(stats["records_imported_current"], 1)

        rows = self.query(
            "SELECT revision, is_current, quantity FROM inbound_records"
            " WHERE doc_no='A-1001' AND line_no=1 ORDER BY revision")
        self.assertEqual(len(rows), 2)                        # 旧版保留可查
        self.assertEqual((rows[0]["revision"], rows[0]["is_current"]), (1, 0))
        self.assertEqual((rows[1]["revision"], rows[1]["is_current"]), (2, 1))
        self.assertEqual(rows[1]["quantity"], "12")

    def test_same_revision_conflict_blocked(self):
        z1 = make_zip(self.tmp / "r1.zip", {"acme_r1.csv": ACME_CSV})
        zc = make_zip(self.tmp / "rc.zip", {"acme_rc.csv": CONFLICT_CSV})
        self.imp.import_paths([z1], RULES)
        _, stats = self.imp.import_paths([zc], RULES)
        self.assertEqual(stats["records_conflict"], 1)
        self.assertEqual(stats["records_quarantined"], 1)
        # 原记录未被替换
        row = self.one("SELECT quantity, description FROM inbound_records"
                       " WHERE doc_no='A-1001' AND line_no=2 AND revision=1")
        self.assertEqual(row["quantity"], "5")
        self.assertEqual(row["description"], "Blue widget")
        reason = json.loads(self.one("SELECT reasons FROM quarantine")["reasons"])
        self.assertEqual(reason[0]["code"], "revision_conflict")

    def test_same_revision_same_content_idempotent(self):
        z1 = make_zip(self.tmp / "r1.zip", {"acme_r1.csv": ACME_CSV})
        # 第二份文件内容不同 (多一行新数据) => 新文件; 其中 3 行与库中完全
        # 一致 => 记录级幂等跳过, 只有新行入库
        extended = ACME_CSV + (
            "A-1004,1,1,SKU-NEW,Brand new row,1,5.00,5.00,USD,03/17/2024\n")
        z2 = make_zip(self.tmp / "r2.zip", {"acme_r1_again.csv": extended})
        self.imp.import_paths([z1], RULES)
        _, stats = self.imp.import_paths([z2], RULES)
        self.assertEqual(stats["records_duplicate_skipped"], 3)
        self.assertEqual(stats["records_imported_current"], 1)
        self.assertEqual(stats["records_conflict"], 0)


class TestReplay(Base):
    def _rules_without_iso(self) -> Path:
        d = self.tmp / "rules_v1"
        d.mkdir()
        shutil.copy(RULES / "bomcorp.json", d)
        shutil.copy(RULES / "jsonline.json", d)
        acme = json.loads((RULES / "acme.json").read_text(encoding="utf-8"))
        acme["fields"]["doc_date"]["formats"] = ["%m/%d/%Y"]   # 不支持 ISO
        (d / "acme.json").write_text(json.dumps(acme), encoding="utf-8")
        return d

    def test_replay_after_rule_fix(self):
        rules_v1 = self._rules_without_iso()
        zp = make_zip(self.tmp / "iso.zip", {"acme_iso.csv": ISO_DATE_CSV})
        _, stats = self.imp.import_paths([zp], rules_v1)
        self.assertEqual(stats["records_quarantined"], 1)      # ISO 日期被拒
        qid = self.one("SELECT id FROM quarantine")["id"]

        # 修正规则: 增加 ISO 日期格式, 版本号升级
        acme = json.loads((rules_v1 / "acme.json").read_text(encoding="utf-8"))
        acme["version"] = "1.0.1"
        acme["fields"]["doc_date"]["formats"].append("%Y-%m-%d")
        (rules_v1 / "acme.json").write_text(json.dumps(acme), encoding="utf-8")

        _, rstats = self.imp.replay(rules_v1)
        self.assertEqual(rstats["replays_success"], 1)

        rec = self.one("SELECT doc_date FROM inbound_records WHERE doc_no='A-4001'")
        self.assertEqual(rec["doc_date"], "2024-05-01")
        q = self.one("SELECT status, reasons FROM quarantine WHERE id=?", (qid,))
        self.assertEqual(q["status"], "resolved")
        self.assertIn("bad_date", q["reasons"])                # 原失败记录保留
        rp = self.one("SELECT result, record_id FROM quarantine_replays"
                      " WHERE quarantine_id=?", (qid,))
        self.assertEqual(rp["result"], "success")              # 重放结果留痕
        self.assertIsNotNone(rp["record_id"])

    def test_replay_keeps_failure_when_rule_still_broken(self):
        rules_v1 = self._rules_without_iso()
        zp = make_zip(self.tmp / "iso.zip", {"acme_iso.csv": ISO_DATE_CSV})
        self.imp.import_paths([zp], rules_v1)
        _, rstats = self.imp.replay(rules_v1)                  # 规则没修, 仍失败
        self.assertEqual(rstats["replays_failed"], 1)
        self.assertEqual(self.one("SELECT status FROM quarantine")["status"],
                         "pending")
        self.assertEqual(self.one("SELECT COUNT(*) c FROM quarantine_replays")["c"],
                         1)


class TestArchiveSafety(Base):
    def test_zip_slip_rejected(self):
        zp = self.tmp / "evil.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("../escape.txt", "x")
            zf.writestr("ok/acme.csv", ACME_CSV)
        _, stats = self.imp.import_paths([zp], RULES)
        self.assertEqual(stats["archives_rejected"], 1)
        self.assertEqual(stats["archives_imported"], 0)
        self.assertEqual(self.one("SELECT COUNT(*) c FROM archives")["c"], 0)
        self.assertEqual(self.one("SELECT COUNT(*) c FROM inbound_records")["c"], 0)
        self.assertFalse((self.tmp / "escape.txt").exists())
        self.assertFalse((self.tmp / "data" / "escape.txt").exists())

    def test_absolute_and_backslash_paths_rejected(self):
        for name in ("/abs/path.csv", "C:\\\\win.csv", "a/../../b.csv"):
            zp = self.tmp / "evil2.zip"
            with zipfile.ZipFile(zp, "w") as zf:
                zf.writestr(name, "x")
            _, stats = self.imp.import_paths([zp], RULES)
            self.assertEqual(stats["archives_rejected"], 1, name)

    def test_oversize_and_ratio_rejected(self):
        payload = b"0" * (21 * 1024 * 1024)   # 超过默认 20 MB 单文件上限
        zp = make_zip(self.tmp / "bomb.zip", {"big.csv": payload})
        _, stats = self.imp.import_paths([zp], RULES)
        self.assertEqual(stats["archives_rejected"], 1)
        self.assertIn("上限", stats["rejected_archives"][0]["reason"])

    def test_safe_extract_cleans_up_on_reject(self):
        zp = self.tmp / "evil3.zip"
        with zipfile.ZipFile(zp, "w") as zf:
            zf.writestr("good.csv", "a,b\n1,2\n")
            zf.writestr("../evil.csv", "x")
        dest = self.tmp / "extract_dest"
        with self.assertRaises(ArchiveSafetyError):
            safe_extract_zip(zp, dest)
        self.assertFalse(dest.exists())       # 半成品已清理


class TestReport(Base):
    def test_html_report(self):
        zp = make_zip(self.tmp / "mixed.zip", {
            "acme_a.csv": ACME_CSV, "acme_bad.csv": BAD_ROWS_CSV})
        run_id, _ = self.imp.import_paths([zp], RULES)
        out = generate_report(self.imp.conn, run_id, self.tmp / "report.html")
        html_text = out.read_text(encoding="utf-8")
        self.assertIn("批次 #", html_text)
        self.assertIn("A-1001", html_text)          # 入库明细
        self.assertIn("missing_field", html_text)   # 隔离原因
        self.assertIn("acme_a.csv", html_text)      # 溯源
        self.assertIn("1.0.0", html_text)           # 规则版本


if __name__ == "__main__":
    unittest.main()

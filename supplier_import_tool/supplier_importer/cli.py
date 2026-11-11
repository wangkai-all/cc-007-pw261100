"""命令行: import / replay / report / runs / quarantine / records."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .archive import ExtractLimits
from .db import connect, init_db
from .importer import Importer
from .report import generate_report


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="supplier_importer",
        description="供应商历史数据导入工具 (CSV/JSONL/ZIP -> 统一入库明细)")
    p.add_argument("--db", default="importer_data/importer.db",
                   help="SQLite 数据库路径 (留档目录在其旁的 store/)")
    sub = p.add_subparsers(dest="command", required=True)

    imp = sub.add_parser("import", help="批量导入 ZIP / CSV / JSONL / 目录")
    imp.add_argument("paths", nargs="+", help="压缩包、文件或目录")
    imp.add_argument("--rules", required=True, help="规则配置目录")
    imp.add_argument("--supplier", help="强制指定供应商 (跳过自动识别)")
    imp.add_argument("--max-files", type=int, default=1000, help="单包最大文件数")
    imp.add_argument("--max-total-mb", type=float, default=100,
                     help="单包最大总解压大小 (MB)")
    imp.add_argument("--max-file-mb", type=float, default=20,
                     help="单文件最大解压大小 (MB)")
    imp.add_argument("--max-ratio", type=float, default=100,
                     help="最大压缩比 (防 zip 炸弹)")
    imp.add_argument("--report", help="导入完成后生成 HTML 报告到该路径")

    rep = sub.add_parser("replay", help="用当前规则重放隔离区待处理记录")
    rep.add_argument("--rules", required=True, help="规则配置目录")
    rep.add_argument("--supplier", help="只重放该供应商")
    rep.add_argument("--ids", help="只重放指定隔离记录 id, 逗号分隔")
    rep.add_argument("--report", help="重放完成后生成 HTML 报告到该路径")

    rpt = sub.add_parser("report", help="生成 HTML 报告")
    rpt.add_argument("--run", type=int, help="批次 id, 默认最近一次")
    rpt.add_argument("--out", required=True, help="输出 HTML 路径")

    sub.add_parser("runs", help="列出导入批次")

    quar = sub.add_parser("quarantine", help="列出隔离区记录")
    quar.add_argument("--status", default="pending",
                      choices=["pending", "resolved", "all"])

    rec = sub.add_parser("records", help="列出入库明细")
    rec.add_argument("--supplier", help="按供应商过滤")
    rec.add_argument("--all", action="store_true",
                     help="包含历史版本 (默认只看当前版本)")
    return p


def cmd_import(args) -> int:
    limits = ExtractLimits(
        max_files=args.max_files,
        max_total_uncompressed=int(args.max_total_mb * 1024 * 1024),
        max_file_size=int(args.max_file_mb * 1024 * 1024),
        max_compression_ratio=args.max_ratio,
    )
    imp = Importer(args.db)
    try:
        run_id, stats = imp.import_paths(args.paths, args.rules,
                                         supplier=args.supplier, limits=limits)
    finally:
        imp.close()
    _print_stats(run_id, stats)
    if args.report:
        _write_report(args.db, run_id, args.report)
    return 0


def cmd_replay(args) -> int:
    ids = [int(x) for x in args.ids.split(",")] if args.ids else None
    imp = Importer(args.db)
    try:
        run_id, stats = imp.replay(args.rules, supplier=args.supplier,
                                   quarantine_ids=ids)
    finally:
        imp.close()
    _print_stats(run_id, stats)
    if args.report:
        _write_report(args.db, run_id, args.report)
    return 0


def cmd_report(args) -> int:
    conn = connect(args.db)
    init_db(conn)
    try:
        out = generate_report(conn, args.run, args.out)
    finally:
        conn.close()
    print(f"报告已生成: {out}")
    return 0


def cmd_runs(args) -> int:
    conn = connect(args.db)
    init_db(conn)
    rows = conn.execute(
        "SELECT id, command, status, started_at, finished_at FROM import_runs"
        " ORDER BY id DESC").fetchall()
    conn.close()
    if not rows:
        print("还没有任何批次")
        return 0
    for r in rows:
        print(f"#{r['id']:<4} {r['command']:<8} {r['status']:<8} "
              f"{r['started_at']} -> {r['finished_at'] or '—'}")
    return 0


def cmd_quarantine(args) -> int:
    conn = connect(args.db)
    init_db(conn)
    sql = ("SELECT q.id, q.status, q.source_line_no, q.reasons,"
           " sf.original_name AS file_name, sf.supplier"
           " FROM quarantine q JOIN source_files sf ON sf.id = q.source_file_id")
    if args.status != "all":
        sql += " WHERE q.status = ?"
        rows = conn.execute(sql, (args.status,)).fetchall()
    else:
        rows = conn.execute(sql).fetchall()
    conn.close()
    if not rows:
        print("隔离区为空")
        return 0
    for r in rows:
        reasons = "；".join(
            f"{x['code']}({x['field']})" for x in json.loads(r["reasons"]))
        print(f"#{r['id']:<4} [{r['status']}] {r['file_name']} "
              f"第{r['source_line_no']}行 供应商={r['supplier']} 原因: {reasons}")
    return 0


def cmd_records(args) -> int:
    conn = connect(args.db)
    init_db(conn)
    sql = ("SELECT r.*, sf.original_name AS file_name FROM inbound_records r"
           " JOIN source_files sf ON sf.id = r.source_file_id")
    cond, params = [], []
    if not args.all:
        cond.append("r.is_current = 1")
    if args.supplier:
        cond.append("r.supplier = ?")
        params.append(args.supplier)
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    sql += " ORDER BY r.supplier, r.doc_no, r.line_no, r.revision"
    rows = conn.execute(sql, params).fetchall()
    conn.close()
    if not rows:
        print("没有入库记录")
        return 0
    for r in rows:
        cur = "当前" if r["is_current"] else "历史"
        print(f"#{r['id']:<4} {r['supplier']:<10} 单据={r['doc_no']} "
              f"行={r['line_no']} 修订={r['revision']} [{cur}] "
              f"数量={r['quantity']} 金额={r['amount']} {r['currency'] or ''} "
              f"日期={r['doc_date']} 来源={r['file_name']}:第{r['source_line_no']}行")
    return 0


def _print_stats(run_id: int, stats: dict) -> None:
    print(f"批次 #{run_id} 完成:")
    for k, v in stats.items():
        if k == "rejected_archives":
            for item in v:
                print(f"  拒绝压缩包 {item['name']}: {item['reason']}")
        elif v:
            print(f"  {k}: {v}")


def _write_report(db_path: str, run_id: int, out: str) -> None:
    conn = connect(db_path)
    init_db(conn)
    try:
        path = generate_report(conn, run_id, out)
    finally:
        conn.close()
    print(f"报告已生成: {path}")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handler = {
        "import": cmd_import, "replay": cmd_replay, "report": cmd_report,
        "runs": cmd_runs, "quarantine": cmd_quarantine, "records": cmd_records,
    }[args.command]
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())

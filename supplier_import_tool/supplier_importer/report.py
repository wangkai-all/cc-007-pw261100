"""HTML 导入结果报告: 自包含单文件, 全部数据转义."""

from __future__ import annotations

import html
import json
import sqlite3
from pathlib import Path

CSS = """
body{font-family:-apple-system,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
margin:0;background:#f5f6f8;color:#1f2430}
header{background:#26314a;color:#fff;padding:20px 32px}
header h1{margin:0;font-size:20px}
header .meta{opacity:.75;font-size:13px;margin-top:6px}
main{padding:24px 32px;max-width:1280px;margin:0 auto}
.cards{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:24px}
.card{background:#fff;border:1px solid #e3e6ec;border-radius:8px;padding:14px 18px;
min-width:130px}
.card .num{font-size:24px;font-weight:600}
.card .lbl{font-size:12px;color:#6b7280;margin-top:2px}
.card.warn .num{color:#b45309}.card.bad .num{color:#b91c1c}.card.ok .num{color:#15803d}
section{background:#fff;border:1px solid #e3e6ec;border-radius:8px;margin-bottom:20px;
padding:16px 20px}
h2{font-size:15px;margin:0 0 12px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border-bottom:1px solid #eceef2;padding:7px 9px;text-align:left;
vertical-align:top}
th{background:#f8f9fb;font-weight:600;color:#4b5563;white-space:nowrap}
tr:last-child td{border-bottom:none}
code{background:#f1f3f7;border-radius:4px;padding:1px 5px;font-size:12px}
.pill{display:inline-block;border-radius:10px;padding:1px 9px;font-size:11px;
font-weight:600}
.pill.ok{background:#dcfce7;color:#166534}.pill.warn{background:#fef3c7;color:#92400e}
.pill.bad{background:#fee2e2;color:#991b1b}.pill.info{background:#e0e7ff;color:#3730a3}
.reasons{margin:0;padding-left:16px}
.reasons li{margin:2px 0}
.raw{font-family:ui-monospace,Menlo,Consolas,monospace;font-size:11px;color:#6b7280;
max-width:420px;word-break:break-all;white-space:pre-wrap}
.empty{color:#9ca3af;font-size:13px}
"""


def esc(v) -> str:
    return html.escape("" if v is None else str(v))


def pill(text: str, kind: str) -> str:
    return f'<span class="pill {kind}">{esc(text)}</span>'


STATUS_PILL = {
    "imported": ("已导入", "ok"), "failed": ("失败", "bad"),
    "no_rule": ("无匹配规则", "warn"), "done": ("完成", "ok"),
    "running": ("进行中", "info"), "pending": ("待处理", "warn"),
    "resolved": ("已解决", "ok"), "processed": ("已处理", "ok"),
    "duplicate_skipped": ("重复跳过", "info"),
    "success": ("成功", "ok"), "conflict": ("冲突", "bad"),
}


def status_pill(s: str) -> str:
    label, kind = STATUS_PILL.get(s, (s, "info"))
    return pill(label, kind)


def reasons_html(reasons_json: str | None) -> str:
    if not reasons_json:
        return '<span class="empty">—</span>'
    try:
        reasons = json.loads(reasons_json)
    except json.JSONDecodeError:
        return esc(reasons_json)
    items = "".join(
        f"<li><code>{esc(r.get('code'))}</code> "
        f"<b>{esc(r.get('field'))}</b>: {esc(r.get('message'))}</li>"
        for r in reasons
    )
    return f'<ul class="reasons">{items}</ul>'


def table(headers: list[str], rows: list[list[str]], empty_msg: str) -> str:
    if not rows:
        return f'<p class="empty">{esc(empty_msg)}</p>'
    head = "".join(f"<th>{esc(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return f"<table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"


def card(label: str, value, kind: str = "") -> str:
    return (f'<div class="card {kind}"><div class="num">{esc(value)}</div>'
            f'<div class="lbl">{esc(label)}</div></div>')


def generate_report(conn: sqlite3.Connection, run_id: int | None,
                    out_path: str | Path) -> Path:
    if run_id is None:
        row = conn.execute(
            "SELECT id FROM import_runs ORDER BY id DESC LIMIT 1").fetchone()
        if row is None:
            raise SystemExit("还没有任何导入批次")
        run_id = row["id"]
    run = conn.execute("SELECT * FROM import_runs WHERE id=?", (run_id,)).fetchone()
    if run is None:
        raise SystemExit(f"批次 #{run_id} 不存在")
    stats = json.loads(run["stats_json"] or "{}")

    files = conn.execute(
        "SELECT sf.*, irf.disposition, a.original_name AS archive_name"
        " FROM import_run_files irf"
        " JOIN source_files sf ON sf.id = irf.source_file_id"
        " LEFT JOIN archives a ON a.id = sf.archive_id"
        " WHERE irf.run_id=? ORDER BY sf.id", (run_id,)).fetchall()

    records = conn.execute(
        "SELECT r.*, sf.original_name AS file_name, a.original_name AS archive_name,"
        "       rv.supplier AS rv_supplier, rv.version AS rv_version"
        " FROM inbound_records r"
        " JOIN source_files sf ON sf.id = r.source_file_id"
        " LEFT JOIN archives a ON a.id = sf.archive_id"
        " JOIN rule_versions rv ON rv.id = r.rule_version_id"
        " WHERE r.import_run_id=? ORDER BY r.id", (run_id,)).fetchall()

    quarantined = conn.execute(
        "SELECT q.*, sf.original_name AS file_name, rv.version AS rv_version"
        " FROM quarantine q"
        " JOIN source_files sf ON sf.id = q.source_file_id"
        " JOIN rule_versions rv ON rv.id = q.rule_version_id"
        " WHERE q.import_run_id=? ORDER BY q.id", (run_id,)).fetchall()

    replays = conn.execute(
        "SELECT qr.*, q.source_line_no, sf.original_name AS file_name,"
        "       rv.version AS rv_version"
        " FROM quarantine_replays qr"
        " JOIN quarantine q ON q.id = qr.quarantine_id"
        " JOIN source_files sf ON sf.id = q.source_file_id"
        " LEFT JOIN rule_versions rv ON rv.id = qr.rule_version_id"
        " WHERE qr.import_run_id=? ORDER BY qr.id", (run_id,)).fetchall()

    rule_versions = conn.execute(
        "SELECT DISTINCT rv.* FROM rule_versions rv WHERE rv.id IN ("
        "  SELECT rule_version_id FROM inbound_records WHERE import_run_id=?"
        "  UNION SELECT rule_version_id FROM quarantine WHERE import_run_id=?"
        "  UNION SELECT rule_version_id FROM quarantine_replays WHERE import_run_id=?)"
        " ORDER BY rv.supplier", (run_id, run_id, run_id)).fetchall()

    # ---- 汇总卡片 ----
    cards = "".join([
        card("处理文件", stats.get("files_processed", 0)),
        card("重复跳过", stats.get("files_skipped_duplicate", 0)),
        card("入库(当前版)", stats.get("records_imported_current", 0), "ok"),
        card("入库(历史版)", stats.get("records_imported_history", 0)),
        card("重复记录跳过", stats.get("records_duplicate_skipped", 0)),
        card("隔离记录", stats.get("records_quarantined", 0),
             "warn" if stats.get("records_quarantined") else ""),
        card("版本冲突", stats.get("records_conflict", 0),
             "bad" if stats.get("records_conflict") else ""),
        card("拒绝的压缩包", stats.get("archives_rejected", 0),
             "bad" if stats.get("archives_rejected") else ""),
    ])
    if run["command"] == "replay":
        cards = "".join([
            card("重放总数", stats.get("replays_total", 0)),
            card("成功", stats.get("replays_success", 0), "ok"),
            card("仍失败", stats.get("replays_failed", 0),
                 "warn" if stats.get("replays_failed") else ""),
            card("冲突", stats.get("replays_conflict", 0),
                 "bad" if stats.get("replays_conflict") else ""),
        ])

    # ---- 被拒绝的压缩包 ----
    rejected_html = ""
    if stats.get("rejected_archives"):
        rows = [[esc(r["name"]), esc(r["reason"])] for r in stats["rejected_archives"]]
        rejected_html = ("<section><h2>被拒绝的压缩包 (安全策略)</h2>"
                         + table(["压缩包", "拒绝原因"], rows, "") + "</section>")

    # ---- 文件清单 ----
    file_rows = []
    for f in files:
        file_rows.append([
            esc(f["original_name"]),
            esc(f["archive_name"] or "—"),
            esc(f["supplier"] or "—"),
            esc(f["format"]),
            status_pill(f["status"]),
            status_pill(f["disposition"]),
            esc(f["note"] or ""),
        ])
    files_html = table(
        ["文件", "来源压缩包", "供应商", "格式", "状态", "处置", "备注"],
        file_rows, "本批次没有文件")

    # ---- 入库明细 (含溯源) ----
    rec_rows = []
    for r in records:
        lineage = (f'{esc(r["file_name"])}'
                   + (f' (来自 {esc(r["archive_name"])})' if r["archive_name"] else "")
                   + f' 第 {r["source_line_no"]} 行')
        rec_rows.append([
            str(r["id"]), esc(r["supplier"]), esc(r["doc_no"]),
            str(r["line_no"]), str(r["revision"]),
            pill("当前", "ok") if r["is_current"] else pill("历史", "info"),
            esc(r["item_code"]), esc(r["quantity"]), esc(r["unit_price"]),
            esc(r["amount"]), esc(r["currency"] or ""), esc(r["doc_date"] or ""),
            f'<code>{esc(r["rv_supplier"])} v{esc(r["rv_version"])}</code>',
            lineage,
        ])
    records_html = table(
        ["#", "供应商", "单据号", "行号", "修订", "状态", "物料", "数量",
         "单价", "金额", "币种", "日期", "规则版本", "来源溯源"],
        rec_rows, "本批次没有入库记录")

    # ---- 隔离区 ----
    q_rows = []
    for q in quarantined:
        q_rows.append([
            str(q["id"]), esc(q["file_name"]), str(q["source_line_no"] or "—"),
            reasons_html(q["reasons"]), status_pill(q["status"]),
            f'<code>v{esc(q["rv_version"])}</code>',
            f'<div class="raw">{esc(q["raw_content"][:300])}</div>',
        ])
    quarantine_html = table(
        ["#", "文件", "行号", "隔离原因", "状态", "规则版本", "原始内容"],
        q_rows, "本批次没有隔离记录")

    # ---- 重放结果 ----
    replay_html = ""
    if replays:
        rp_rows = []
        for rp in replays:
            rp_rows.append([
                str(rp["quarantine_id"]), esc(rp["file_name"]),
                str(rp["source_line_no"] or "—"),
                status_pill(rp["result"]),
                f'#{rp["record_id"]}' if rp["record_id"] else "—",
                f'<code>v{esc(rp["rv_version"] or "?")}</code>',
                reasons_html(rp["reasons"]),
            ])
        replay_html = ("<section><h2>重放结果</h2>" + table(
            ["隔离记录#", "文件", "行号", "结果", "入库记录", "规则版本", "原因"],
            rp_rows, "") + "</section>")

    # ---- 规则版本 ----
    rv_rows = [[esc(rv["supplier"]), esc(rv["version"]),
                f'<code>{esc(rv["config_hash"][:12])}</code>', esc(rv["created_at"])]
               for rv in rule_versions]
    rv_html = table(["供应商", "版本", "配置哈希", "首次使用"],
                    rv_rows, "本批次没有使用规则")

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>导入批次 #{run_id} 报告</title><style>{CSS}</style></head>
<body>
<header><h1>供应商数据导入报告 — 批次 #{run_id}</h1>
<div class="meta">命令: {esc(run["command"])} · 状态: {esc(run["status"])} ·
开始: {esc(run["started_at"])} · 结束: {esc(run["finished_at"] or "—")}</div>
</header>
<main>
<div class="cards">{cards}</div>
{rejected_html}
<section><h2>文件处理清单</h2>{files_html}</section>
<section><h2>入库明细 (可追溯到来源文件与原始行号)</h2>{records_html}</section>
<section><h2>隔离区</h2>{quarantine_html}</section>
{replay_html}
<section><h2>本批次使用的规则版本</h2>{rv_html}</section>
</main></body></html>"""

    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(doc, encoding="utf-8")
    return out

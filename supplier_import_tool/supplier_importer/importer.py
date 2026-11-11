"""导入编排: 去重、安全解压、转换、修订版本、隔离区、重放."""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from pathlib import Path

from . import db as dbmod
from .archive import ArchiveSafetyError, ExtractLimits, safe_extract_zip
from .config import RuleConfig, load_rules, match_rule
from .parsers import ParseError, detect_format, parse_file
from .transform import content_hash, transform_row

SUPPORTED_SUFFIXES = (".csv", ".jsonl", ".ndjson")


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def new_stats() -> dict:
    return {
        "archives_imported": 0,
        "archives_skipped_duplicate": 0,
        "archives_rejected": 0,
        "rejected_archives": [],
        "files_processed": 0,
        "files_retried_after_failure": 0,
        "files_skipped_duplicate": 0,
        "files_failed": 0,
        "files_no_rule": 0,
        "files_unsupported": 0,
        "records_imported_current": 0,
        "records_imported_history": 0,
        "records_duplicate_skipped": 0,
        "records_conflict": 0,
        "records_quarantined": 0,
        "replays_total": 0,
        "replays_success": 0,
        "replays_failed": 0,
        "replays_conflict": 0,
    }


class Importer:
    def __init__(self, db_path: str | Path, store_dir: str | Path | None = None):
        self.db_path = Path(db_path)
        self.store_dir = Path(store_dir) if store_dir else self.db_path.parent / "store"
        self.archives_dir = self.store_dir / "archives"
        self.files_dir = self.store_dir / "files"
        for d in (self.archives_dir, self.files_dir):
            d.mkdir(parents=True, exist_ok=True)
        self.conn = dbmod.connect(self.db_path)
        dbmod.init_db(self.conn)

    def close(self) -> None:
        self.conn.close()

    # ------------------------------------------------------------------ runs
    def _open_run(self, command: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO import_runs (command, status, started_at) VALUES (?,?,?)",
            (command, "running", dbmod.utcnow()),
        )
        self.conn.commit()
        return cur.lastrowid

    def _close_run(self, run_id: int, stats: dict, status: str = "done") -> None:
        self.conn.execute(
            "UPDATE import_runs SET status=?, finished_at=?, stats_json=? WHERE id=?",
            (status, dbmod.utcnow(), json.dumps(stats, ensure_ascii=False), run_id),
        )
        self.conn.commit()

    # ------------------------------------------------------- rule versions
    def _ensure_rule_version(self, rule: RuleConfig) -> int:
        self.conn.execute(
            "INSERT OR IGNORE INTO rule_versions"
            " (supplier, version, config_hash, config_json, created_at)"
            " VALUES (?,?,?,?,?)",
            (rule.supplier, rule.version, rule.content_hash,
             json.dumps(rule.raw, ensure_ascii=False), dbmod.utcnow()),
        )
        row = self.conn.execute(
            "SELECT id FROM rule_versions WHERE supplier=? AND config_hash=?",
            (rule.supplier, rule.content_hash),
        ).fetchone()
        return row["id"]

    # ---------------------------------------------------------------- import
    def import_paths(
        self,
        paths: list[str | Path],
        rules_dir: str | Path,
        supplier: str | None = None,
        limits: ExtractLimits | None = None,
    ) -> tuple[int, dict]:
        rules = load_rules(rules_dir)
        run_id = self._open_run("import")
        stats = new_stats()
        try:
            for p in paths:
                self._dispatch_path(Path(p), rules, supplier, limits, run_id, stats)
        except Exception:
            self._close_run(run_id, stats, status="failed")
            raise
        self._close_run(run_id, stats)
        return run_id, stats

    def _dispatch_path(self, path, rules, supplier, limits, run_id, stats):
        if path.is_dir():
            for child in sorted(path.iterdir()):
                self._dispatch_path(child, rules, supplier, limits, run_id, stats)
        elif path.name.lower().endswith(".zip"):
            self._process_zip(path, rules, supplier, limits, run_id, stats)
        elif detect_format(path.name):
            self._process_loose_file(path, rules, supplier, run_id, stats)
        else:
            stats["files_unsupported"] += 1

    # ------------------------------------------------------------- archives
    def _process_zip(self, zip_path, rules, supplier, limits, run_id, stats):
        digest = sha256_file(zip_path)
        if self.conn.execute(
            "SELECT 1 FROM archives WHERE sha256=?", (digest,)
        ).fetchone():
            stats["archives_skipped_duplicate"] += 1
            return

        staging = Path(tempfile.mkdtemp(prefix=f"extract_{run_id}_",
                                        dir=self.store_dir))
        try:
            extracted = safe_extract_zip(zip_path, staging / "out", limits)
        except ArchiveSafetyError as e:
            stats["archives_rejected"] += 1
            stats["rejected_archives"].append(
                {"name": zip_path.name, "reason": str(e)})
            shutil.rmtree(staging, ignore_errors=True)
            return

        stored_zip = self.archives_dir / f"{digest}.zip"
        shutil.copy2(zip_path, stored_zip)
        cur = self.conn.execute(
            "INSERT INTO archives"
            " (sha256, original_name, stored_path, size_bytes, import_run_id, imported_at)"
            " VALUES (?,?,?,?,?,?)",
            (digest, zip_path.name, str(stored_zip), zip_path.stat().st_size,
             run_id, dbmod.utcnow()),
        )
        archive_id = cur.lastrowid
        self.conn.commit()
        stats["archives_imported"] += 1

        for member in sorted(extracted.members, key=lambda m: m.inner_path):
            if detect_format(member.inner_path) is None:
                stats["files_unsupported"] += 1
                continue
            self._process_file(
                member.extracted_path, member.inner_path, archive_id,
                rules, supplier, run_id, stats,
            )
        shutil.rmtree(staging, ignore_errors=True)

    def _process_loose_file(self, path, rules, supplier, run_id, stats):
        self._process_file(path, path.name, None, rules, supplier, run_id, stats)

    # ---------------------------------------------------------------- files
    def _process_file(self, path, display_name, archive_id, rules,
                      supplier_override, run_id, stats):
        fmt = detect_format(display_name)
        digest = sha256_file(path)

        existing = self.conn.execute(
            "SELECT id, status FROM source_files WHERE sha256=?", (digest,)
        ).fetchone()
        if existing and existing["status"] == "imported":
            # 同一文件重复导入: 直接跳过, 不产生重复入库
            self.conn.execute(
                "INSERT OR IGNORE INTO import_run_files (run_id, source_file_id, disposition)"
                " VALUES (?,?,?)", (run_id, existing["id"], "duplicate_skipped"))
            self.conn.commit()
            stats["files_skipped_duplicate"] += 1
            return
        retry = existing is not None   # 之前失败/未识别, 本次重试

        # 先用默认参数解析拿到表头, 用于规则识别
        try:
            parsed = parse_file(path, fmt)
        except (ParseError, UnicodeDecodeError) as e:
            self._record_file(digest, display_name, archive_id, path, fmt, None,
                              "failed", f"解析失败: {e}", run_id, stats, retry)
            return

        rule = match_rule(rules, display_name, parsed.headers, supplier_override)
        if rule is None:
            self._record_file(digest, display_name, archive_id, path, fmt, None,
                              "no_rule", "没有匹配的转换规则", run_id, stats, retry)
            return

        # 规则指定了不同的编码/分隔符时按规则重新解析
        if fmt == "csv" and (rule.csv_encoding != "utf-8-sig" or rule.csv_delimiter):
            try:
                parsed = parse_file(path, fmt, encoding=rule.csv_encoding,
                                    delimiter=rule.csv_delimiter)
            except (ParseError, UnicodeDecodeError) as e:
                self._record_file(digest, display_name, archive_id, path, fmt,
                                  rule.supplier, "failed",
                                  f"按规则解析失败: {e}", run_id, stats, retry)
                return

        rule_version_id = self._ensure_rule_version(rule)
        file_id = self._record_file(digest, display_name, archive_id, path, fmt,
                                    rule.supplier, "imported", None,
                                    run_id, stats, retry)
        stats["files_processed"] += 1
        if retry:
            stats["files_retried_after_failure"] += 1

        stored = self.files_dir / f"{digest}{Path(display_name).suffix.lower()}"
        if not stored.exists():
            shutil.copy2(path, stored)

        for row in parsed.rows:
            self._process_row(row, rule, rule_version_id, file_id, run_id, stats)
        self.conn.commit()

    def _record_file(self, digest, display_name, archive_id, path, fmt,
                     supplier, status, note, run_id, stats, retry):
        if status == "failed":
            stats["files_failed"] += 1
        elif status == "no_rule":
            stats["files_no_rule"] += 1
        stored = self.files_dir / f"{digest}{Path(display_name).suffix.lower()}"
        if not stored.exists():
            shutil.copy2(path, stored)
        now = dbmod.utcnow()
        if retry:
            self.conn.execute(
                "UPDATE source_files SET status=?, note=?, supplier=COALESCE(?, supplier),"
                " import_run_id=?, imported_at=? WHERE id=?",
                (status, note, supplier, run_id, now, _id_of(self.conn, digest)))
            file_id = _id_of(self.conn, digest)
        else:
            cur = self.conn.execute(
                "INSERT INTO source_files"
                " (sha256, archive_id, inner_path, original_name, stored_path,"
                "  format, supplier, status, note, import_run_id, imported_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (digest, archive_id,
                 display_name if archive_id is not None else None,
                 Path(display_name).name, str(stored), fmt, supplier, status,
                 note, run_id, now),
            )
            file_id = cur.lastrowid
        self.conn.execute(
            "INSERT OR IGNORE INTO import_run_files (run_id, source_file_id, disposition)"
            " VALUES (?,?,?)", (run_id, file_id, "processed"))
        self.conn.commit()
        return file_id

    # ----------------------------------------------------------------- rows
    def _process_row(self, row, rule, rule_version_id, file_id, run_id, stats):
        result = transform_row(row.values, rule)
        if row.column_count_error:
            from .transform import FieldError
            result.errors.insert(0, FieldError(
                field="*", code="column_count",
                message="列数与表头不一致或 JSON 行无效"))
        if not result.ok:
            self._quarantine(file_id, row.line_no, row.raw_text, result.errors,
                             rule_version_id, run_id)
            stats["records_quarantined"] += 1
            return

        rec = result.record
        rec["supplier"] = rule.supplier
        rec["extra_json"] = json.dumps(result.extra, ensure_ascii=False)
        rec["content_hash"] = content_hash(
            {k: v for k, v in rec.items() if k != "extra_json"})
        outcome, record_id, existing_id = self._upsert_record(
            rec, file_id, row.line_no, rule_version_id, run_id)
        if outcome == "duplicate":
            stats["records_duplicate_skipped"] += 1
        elif outcome == "conflict":
            from .transform import FieldError
            self._quarantine(
                file_id, row.line_no, row.raw_text,
                [FieldError(
                    field="revision", code="revision_conflict",
                    message=f"同版本修订内容冲突: 与已入库记录 #{existing_id} "
                            f"(supplier={rec['supplier']}, doc={rec['doc_no']}, "
                            f"line={rec['line_no']}, rev={rec['revision']}) 内容不同, 已阻止替换")],
                rule_version_id, run_id)
            stats["records_conflict"] += 1
            stats["records_quarantined"] += 1
        elif outcome == "current":
            stats["records_imported_current"] += 1
        else:
            stats["records_imported_history"] += 1

    def _upsert_record(self, rec, file_id, line_no, rule_version_id, run_id):
        key = (rec["supplier"], rec["doc_no"], rec["line_no"], rec["revision"])
        row = self.conn.execute(
            "SELECT id, content_hash FROM inbound_records"
            " WHERE supplier=? AND doc_no=? AND line_no=? AND revision=?",
            key).fetchone()
        if row:
            if row["content_hash"] == rec["content_hash"]:
                return "duplicate", row["id"], None
            return "conflict", row["id"], row["id"]

        maxrev = self.conn.execute(
            "SELECT MAX(revision) AS m FROM inbound_records"
            " WHERE supplier=? AND doc_no=? AND line_no=?",
            key[:3]).fetchone()["m"]
        is_current = 1 if maxrev is None or rec["revision"] >= maxrev else 0
        if is_current:
            self.conn.execute(
                "UPDATE inbound_records SET is_current=0"
                " WHERE supplier=? AND doc_no=? AND line_no=?", key[:3])
        cur = self.conn.execute(
            "INSERT INTO inbound_records"
            " (supplier, doc_no, line_no, revision, item_code, description,"
            "  quantity, unit_price, amount, currency, doc_date, extra_json,"
            "  content_hash, is_current, source_file_id, source_line_no,"
            "  rule_version_id, import_run_id, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rec["supplier"], rec["doc_no"], rec["line_no"], rec["revision"],
             rec["item_code"], rec["description"], rec["quantity"],
             rec["unit_price"], rec["amount"], rec["currency"], rec["doc_date"],
             rec["extra_json"], rec["content_hash"], is_current,
             file_id, line_no, rule_version_id, run_id, dbmod.utcnow()),
        )
        return ("current" if is_current else "history"), cur.lastrowid, None

    def _quarantine(self, file_id, line_no, raw_text, errors, rule_version_id, run_id):
        self.conn.execute(
            "INSERT INTO quarantine"
            " (source_file_id, source_line_no, raw_content, reasons, status,"
            "  rule_version_id, import_run_id, created_at)"
            " VALUES (?,?,?,?,'pending',?,?,?)",
            (file_id, line_no, raw_text,
             json.dumps([e.to_dict() for e in errors], ensure_ascii=False),
             rule_version_id, run_id, dbmod.utcnow()),
        )

    # --------------------------------------------------------------- replay
    def replay(self, rules_dir, supplier=None, quarantine_ids=None,
               limits=None) -> tuple[int, dict]:
        """用当前规则重放隔离区中待处理的记录.

        原失败记录保留; 每次重放写入 quarantine_replays 留痕.
        """
        rules = load_rules(rules_dir)
        run_id = self._open_run("replay")
        stats = new_stats()
        try:
            sql = ("SELECT q.*, sf.supplier AS file_supplier, sf.stored_path,"
                   "       sf.format, sf.original_name"
                   " FROM quarantine q JOIN source_files sf ON sf.id = q.source_file_id"
                   " WHERE q.status = 'pending'")
            params: list = []
            if supplier:
                sql += " AND sf.supplier = ?"
                params.append(supplier)
            if quarantine_ids:
                sql += f" AND q.id IN ({','.join('?' * len(quarantine_ids))})"
                params.extend(quarantine_ids)
            sql += " ORDER BY q.id"
            pending = self.conn.execute(sql, params).fetchall()
            stats["replays_total"] = len(pending)

            for q in pending:
                self._replay_one(q, rules, run_id, stats)
            self.conn.commit()
        except Exception:
            self._close_run(run_id, stats, status="failed")
            raise
        self._close_run(run_id, stats)
        return run_id, stats

    def _replay_one(self, q, rules, run_id, stats):
        supplier = q["file_supplier"]
        rule = next((r for r in rules if r.supplier == supplier), None)
        if rule is None:
            self._record_replay(q, None, run_id, "failed", None,
                                [{"field": "*", "code": "no_rule",
                                  "message": f"规则目录中找不到供应商 {supplier} 的规则"}],
                                stats)
            return
        rule_version_id = self._ensure_rule_version(rule)

        stored = Path(q["stored_path"])
        if not stored.exists():
            self._record_replay(q, rule_version_id, run_id, "failed", None,
                                [{"field": "*", "code": "source_missing",
                                  "message": f"留档文件不存在: {stored}"}], stats)
            return

        try:
            parsed = parse_file(stored, q["format"],
                                encoding=rule.csv_encoding,
                                delimiter=rule.csv_delimiter)
        except (ParseError, UnicodeDecodeError) as e:
            self._record_replay(q, rule_version_id, run_id, "failed", None,
                                [{"field": "*", "code": "parse_error",
                                  "message": str(e)}], stats)
            return

        row = next((r for r in parsed.rows if r.line_no == q["source_line_no"]), None)
        if row is None:
            self._record_replay(q, rule_version_id, run_id, "failed", None,
                                [{"field": "*", "code": "line_missing",
                                  "message": "留档文件中找不到原始行"}], stats)
            return

        result = transform_row(row.values, rule)
        if not result.ok:
            self._record_replay(q, rule_version_id, run_id, "failed", None,
                                [e.to_dict() for e in result.errors], stats)
            return

        rec = result.record
        rec["supplier"] = rule.supplier
        rec["extra_json"] = json.dumps(result.extra, ensure_ascii=False)
        rec["content_hash"] = content_hash(
            {k: v for k, v in rec.items() if k != "extra_json"})
        outcome, record_id, existing_id = self._upsert_record(
            rec, q["source_file_id"], row.line_no, rule_version_id, run_id)

        if outcome == "conflict":
            self._record_replay(q, rule_version_id, run_id, "conflict", None,
                                [{"field": "revision", "code": "revision_conflict",
                                  "message": f"仍与已入库记录 #{existing_id} 冲突"}],
                                stats)
            return
        # success (duplicate 也视为成功: 数据已在库中, 隔离原因已消除)
        self._record_replay(q, rule_version_id, run_id, "success", record_id,
                            None, stats)
        self.conn.execute("UPDATE quarantine SET status='resolved' WHERE id=?",
                          (q["id"],))

    def _record_replay(self, q, rule_version_id, run_id, result, record_id,
                       reasons, stats):
        stats[f"replays_{result}"] += 1
        self.conn.execute(
            "INSERT INTO quarantine_replays"
            " (quarantine_id, rule_version_id, import_run_id, result, record_id,"
            "  reasons, replayed_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (q["id"], rule_version_id, run_id, result, record_id,
             json.dumps(reasons, ensure_ascii=False) if reasons else None,
             dbmod.utcnow()),
        )


def _id_of(conn, digest) -> int:
    return conn.execute(
        "SELECT id FROM source_files WHERE sha256=?", (digest,)).fetchone()["id"]

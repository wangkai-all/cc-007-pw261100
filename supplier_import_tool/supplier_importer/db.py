"""SQLite 存储层: 表结构、连接与通用辅助."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- 规则版本: 同一供应商的每次配置内容变化都会产生一条新记录
CREATE TABLE IF NOT EXISTS rule_versions (
    id          INTEGER PRIMARY KEY,
    supplier    TEXT NOT NULL,
    version     TEXT NOT NULL,          -- 配置文件里声明的版本号
    config_hash TEXT NOT NULL,          -- 配置规范化后的内容哈希
    config_json TEXT NOT NULL,          -- 配置原文
    created_at  TEXT NOT NULL,
    UNIQUE (supplier, config_hash)
);

-- 导入批次
CREATE TABLE IF NOT EXISTS import_runs (
    id          INTEGER PRIMARY KEY,
    command     TEXT NOT NULL,          -- import / replay
    status      TEXT NOT NULL,          -- running / done / failed
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    stats_json  TEXT
);

-- 原始压缩包, 按内容哈希去重并留档
CREATE TABLE IF NOT EXISTS archives (
    id            INTEGER PRIMARY KEY,
    sha256        TEXT NOT NULL UNIQUE,
    original_name TEXT NOT NULL,
    stored_path   TEXT NOT NULL,
    size_bytes    INTEGER NOT NULL,
    import_run_id INTEGER NOT NULL REFERENCES import_runs(id),
    imported_at   TEXT NOT NULL
);

-- 解压/散装进入导入流程的单个源文件, 按内容哈希去重
CREATE TABLE IF NOT EXISTS source_files (
    id            INTEGER PRIMARY KEY,
    sha256        TEXT NOT NULL UNIQUE,   -- 同名不同内容 => 哈希不同 => 新文件
    archive_id    INTEGER REFERENCES archives(id),
    inner_path    TEXT,                   -- 压缩包内路径; 散装文件为 NULL
    original_name TEXT NOT NULL,
    stored_path   TEXT NOT NULL,          -- 留档位置, 可追溯
    format        TEXT NOT NULL,          -- csv / jsonl
    supplier      TEXT,                   -- 识别出的供应商; 未识别为 NULL
    status        TEXT NOT NULL,          -- imported / failed / no_rule
    note          TEXT,
    import_run_id INTEGER NOT NULL REFERENCES import_runs(id),
    imported_at   TEXT NOT NULL
);

-- 批次与文件的关联 (含被跳过的重复文件, 便于报告)
CREATE TABLE IF NOT EXISTS import_run_files (
    run_id         INTEGER NOT NULL REFERENCES import_runs(id),
    source_file_id INTEGER NOT NULL REFERENCES source_files(id),
    disposition    TEXT NOT NULL,         -- processed / duplicate_skipped
    PRIMARY KEY (run_id, source_file_id)
);

-- 统一入库明细. 金额/数量以十进制字符串存储, 不经过浮点.
CREATE TABLE IF NOT EXISTS inbound_records (
    id              INTEGER PRIMARY KEY,
    supplier        TEXT NOT NULL,
    doc_no          TEXT NOT NULL,
    line_no         INTEGER NOT NULL,
    revision        INTEGER NOT NULL,
    item_code       TEXT,
    description     TEXT,
    quantity        TEXT,                 -- decimal 字符串
    unit_price      TEXT,                 -- decimal 字符串
    amount          TEXT,                 -- decimal 字符串
    currency        TEXT,
    doc_date        TEXT,                 -- ISO YYYY-MM-DD
    extra_json      TEXT,                 -- 未映射的原始字段, 不丢失
    content_hash    TEXT NOT NULL,        -- 规范化内容哈希, 用于冲突检测
    is_current      INTEGER NOT NULL,     -- 同一 (supplier,doc_no,line_no) 只有最高版本为 1
    source_file_id  INTEGER NOT NULL REFERENCES source_files(id),
    source_line_no  INTEGER NOT NULL,     -- 原始文件中的起始物理行号 (表头为第 1 行)
    rule_version_id INTEGER NOT NULL REFERENCES rule_versions(id),
    import_run_id   INTEGER NOT NULL REFERENCES import_runs(id),
    created_at      TEXT NOT NULL,
    UNIQUE (supplier, doc_no, line_no, revision)
);

-- 隔离区: 空值 / 格式错误 / 缺关键字段 / 版本冲突的记录, 带具体原因
CREATE TABLE IF NOT EXISTS quarantine (
    id              INTEGER PRIMARY KEY,
    source_file_id  INTEGER NOT NULL REFERENCES source_files(id),
    source_line_no  INTEGER,              -- 文件级错误为 NULL
    raw_content     TEXT NOT NULL,        -- 原始行内容, 原样保留
    reasons         TEXT NOT NULL,        -- JSON: [{field, code, message}]
    status          TEXT NOT NULL DEFAULT 'pending',  -- pending / resolved
    rule_version_id INTEGER NOT NULL REFERENCES rule_versions(id),
    import_run_id   INTEGER NOT NULL REFERENCES import_runs(id),
    created_at      TEXT NOT NULL
);

-- 重放历史: 原失败记录永不删除, 每次重放的结果单独留痕
CREATE TABLE IF NOT EXISTS quarantine_replays (
    id              INTEGER PRIMARY KEY,
    quarantine_id   INTEGER NOT NULL REFERENCES quarantine(id),
    rule_version_id INTEGER NOT NULL REFERENCES rule_versions(id),
    import_run_id   INTEGER NOT NULL REFERENCES import_runs(id),
    result          TEXT NOT NULL,        -- success / failed / conflict
    record_id       INTEGER REFERENCES inbound_records(id),
    reasons         TEXT,                 -- 失败/冲突时的原因 JSON
    replayed_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_records_key     ON inbound_records (supplier, doc_no, line_no);
CREATE INDEX IF NOT EXISTS idx_records_file    ON inbound_records (source_file_id);
CREATE INDEX IF NOT EXISTS idx_quarantine_file ON quarantine (source_file_id);
CREATE INDEX IF NOT EXISTS idx_quarantine_stat ON quarantine (status);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(db_path: str | Path) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()

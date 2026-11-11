"""规则配置: 加载、校验、版本化与供应商匹配.

配置为 JSON 文件, 每个供应商一个. 内容哈希即规则版本标识,
每次导入的每条记录都会记录实际使用的规则版本.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

# 统一入库明细的规范字段
CANONICAL_FIELDS = (
    "doc_no", "line_no", "revision", "item_code", "description",
    "quantity", "unit_price", "amount", "currency", "doc_date",
)
KEY_FIELDS = ("doc_no", "line_no")          # 业务主键 (revision 缺省为 1)
FIELD_TYPES = ("text", "int", "decimal", "date")


class ConfigError(Exception):
    """配置文件本身不合法."""


@dataclass
class FieldRule:
    name: str
    sources: list[str]                      # 原始列名候选, 按优先级
    required: bool = False
    type: str = "text"                      # text / int / decimal / date
    formats: list[str] = field(default_factory=list)   # date 类型的 strptime 格式
    multiplier: str = "1"                   # decimal 单位换算, 如 "0.01" 表示分->元


@dataclass
class RuleConfig:
    supplier: str
    version: str
    priority: int
    match_filename: list[str]
    match_headers: list[str]
    fields: list[FieldRule]
    csv_encoding: str
    csv_delimiter: str | None               # None 表示自动嗅探
    thousands_separator: str | None
    decimal_point: str
    raw: dict
    content_hash: str

    def field_rule(self, name: str) -> FieldRule | None:
        for f in self.fields:
            if f.name == name:
                return f
        return None


def _canonical_json(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def load_rule(path: str | Path) -> RuleConfig:
    path = Path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise ConfigError(f"规则文件无法解析: {path}: {e}") from e

    supplier = raw.get("supplier")
    version = raw.get("version")
    if not supplier or not version:
        raise ConfigError(f"规则缺少 supplier/version: {path}")

    fields: list[FieldRule] = []
    seen: set[str] = set()
    for name, spec in (raw.get("fields") or {}).items():
        if name not in CANONICAL_FIELDS:
            raise ConfigError(f"{path}: 未知目标字段 {name!r}, 允许: {CANONICAL_FIELDS}")
        sources = spec.get("source") or spec.get("sources")
        if isinstance(sources, str):
            sources = [sources]
        if not sources:
            raise ConfigError(f"{path}: 字段 {name} 缺少 source 列名")
        ftype = spec.get("type", "text")
        if ftype not in FIELD_TYPES:
            raise ConfigError(f"{path}: 字段 {name} 类型非法: {ftype}")
        formats = spec.get("formats") or []
        if ftype == "date" and not formats:
            raise ConfigError(f"{path}: 日期字段 {name} 必须给出 formats")
        fields.append(FieldRule(
            name=name,
            sources=list(sources),
            required=bool(spec.get("required", False)),
            type=ftype,
            formats=list(formats),
            multiplier=str(spec.get("multiplier", "1")),
        ))
        seen.add(name)

    for key in KEY_FIELDS:
        if key not in seen:
            raise ConfigError(f"{path}: 必须映射关键字段 {key}")

    csv_cfg = raw.get("csv") or {}
    num_cfg = raw.get("number") or {}
    content_hash = hashlib.sha256(_canonical_json(raw).encode("utf-8")).hexdigest()

    return RuleConfig(
        supplier=supplier,
        version=str(version),
        priority=int(raw.get("priority", 0)),
        match_filename=list((raw.get("match") or {}).get("filename") or []),
        match_headers=list((raw.get("match") or {}).get("required_headers") or []),
        fields=fields,
        csv_encoding=csv_cfg.get("encoding", "utf-8-sig"),
        csv_delimiter=csv_cfg.get("delimiter"),
        thousands_separator=num_cfg.get("thousands_separator"),
        decimal_point=num_cfg.get("decimal_point", "."),
        raw=raw,
        content_hash=content_hash,
    )


def load_rules(rules_dir: str | Path) -> list[RuleConfig]:
    rules = [load_rule(p) for p in sorted(Path(rules_dir).glob("*.json"))]
    if not rules:
        raise ConfigError(f"规则目录为空: {rules_dir}")
    # 优先级高的先匹配, 同级按供应商名排序保证确定性
    rules.sort(key=lambda r: (-r.priority, r.supplier))
    return rules


def match_rule(
    rules: list[RuleConfig],
    filename: str,
    headers: list[str],
    supplier_override: str | None = None,
) -> RuleConfig | None:
    """按供应商名 (显式指定) 或 文件名模式 + 必需表头 自动识别规则."""
    if supplier_override:
        for r in rules:
            if r.supplier == supplier_override:
                return r
        return None
    header_set = {h.strip() for h in headers}
    lower = filename.lower()
    for r in rules:
        if r.match_filename and not any(
            fnmatch.fnmatch(lower, pat.lower()) for pat in r.match_filename
        ):
            continue
        if not all(h in header_set for h in r.match_headers):
            continue
        return r
    return None

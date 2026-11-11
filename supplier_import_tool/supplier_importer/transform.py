"""记录转换: 字段映射、日期/十进制解析、校验.

原则: 任何空值、格式错误、缺关键字段都显式报错进隔离区,
绝不填默认值蒙混; 金额全程十进制, 不经过浮点.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation

from .config import CANONICAL_FIELDS, RuleConfig


@dataclass
class FieldError:
    field: str
    code: str        # missing_field / empty_value / bad_format / bad_date / bad_decimal / bad_int / column_count / invalid_json
    message: str

    def to_dict(self) -> dict:
        return {"field": self.field, "code": self.code, "message": self.message}


@dataclass
class TransformResult:
    record: dict | None = None        # 规范字段字典, 十进制以字符串表示
    extra: dict = field(default_factory=dict)   # 未映射的原始列
    errors: list[FieldError] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and self.record is not None


def content_hash(record: dict) -> str:
    """规范化内容的稳定哈希, 用于同版本冲突检测."""
    canon = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _normalize_number_literal(s: str, rule: RuleConfig) -> str:
    """按供应商的数字习惯 (千分位、小数点) 规范化成标准十进制字面量."""
    s = s.strip()
    if rule.thousands_separator:
        s = s.replace(rule.thousands_separator, "")
    if rule.decimal_point != ".":
        s = s.replace(rule.decimal_point, ".")
    return s


def _to_decimal(s: str, rule: RuleConfig) -> Decimal:
    lit = _normalize_number_literal(s, rule)
    try:
        d = Decimal(lit)
    except InvalidOperation:
        raise ValueError(f"不是合法的十进制数: {s!r}")
    if not d.is_finite():
        raise ValueError(f"数值非法 (NaN/Inf): {s!r}")
    return d


def decimal_to_str(d: Decimal) -> str:
    """十进制转存储字符串, 避免科学计数法."""
    return format(d.normalize(), "f")


def _convert_date(value: str, formats: list[str]) -> str:
    for fmt in formats:
        try:
            return datetime.strptime(value.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    raise ValueError(f"无法匹配任何日期格式 {formats}")


def transform_row(values: dict, rule: RuleConfig) -> TransformResult:
    """把一行原始数据按规则转换为规范记录. 收集全部错误而不是遇错即停."""
    result = TransformResult()
    record: dict = {}
    used_sources: set[str] = set()

    if "__invalid_json__" in values:
        result.errors.append(FieldError(
            field="*", code="invalid_json",
            message=f"JSON 行解析失败: {values['__invalid_json__']}",
        ))
        return result

    for fr in rule.fields:
        raw_value = None
        found = False
        for src in fr.sources:
            if src in values and values[src] is not None:
                candidate = values[src]
                if isinstance(candidate, str):
                    candidate = candidate.strip()
                if candidate != "":
                    raw_value = candidate
                    found = True
                    used_sources.add(src)
                    break
                used_sources.add(src)   # 列存在但为空

        if not found:
            if fr.required:
                result.errors.append(FieldError(
                    field=fr.name, code="missing_field",
                    message=f"缺少必填字段 (源列 {fr.sources} 不存在或为空)",
                ))
            record[fr.name] = None
            continue

        text = raw_value if isinstance(raw_value, str) else str(raw_value)
        if fr.type == "text":
            record[fr.name] = text
        elif fr.type == "int":
            try:
                lit = _normalize_number_literal(text, rule)
                n = int(lit)
                if str(n) != lit and not (lit.startswith("+") and str(n) == lit[1:]):
                    raise ValueError
                record[fr.name] = n
            except (ValueError, TypeError):
                result.errors.append(FieldError(
                    field=fr.name, code="bad_int",
                    message=f"整数字段格式错误: {text!r}",
                ))
                record[fr.name] = None
        elif fr.type == "decimal":
            try:
                d = _to_decimal(text, rule) * Decimal(fr.multiplier)
                record[fr.name] = decimal_to_str(d)
            except (ValueError, InvalidOperation) as e:
                result.errors.append(FieldError(
                    field=fr.name, code="bad_decimal",
                    message=f"金额/数字格式错误: {text!r} ({e})",
                ))
                record[fr.name] = None
        elif fr.type == "date":
            try:
                record[fr.name] = _convert_date(text, fr.formats)
            except ValueError as e:
                result.errors.append(FieldError(
                    field=fr.name, code="bad_date",
                    message=f"日期格式错误: {text!r}, {e}",
                ))
                record[fr.name] = None

    # 必填字段即使类型转换失败也要保证最后 key 字段检查能报出
    for key in ("doc_no", "line_no", "revision"):
        if key in record and record[key] is None:
            fr = rule.field_rule(key)
            if fr is not None and fr.required and not any(
                e.field == key for e in result.errors
            ):
                result.errors.append(FieldError(
                    field=key, code="empty_value",
                    message=f"关键字段 {key} 为空",
                ))

    # revision 缺省为 1 是规则层面的定义 (供应商无修订列或该列非必填且为空),
    # 不是蒙混默认值; 必填修订列为空时前面已经报错进隔离区.
    rev_rule = rule.field_rule("revision")
    if record.get("revision") is None and (rev_rule is None or not rev_rule.required):
        record["revision"] = 1

    # 未映射的原始列全部保留
    mapped = set(used_sources)
    for fr in rule.fields:
        mapped.update(fr.sources)
    result.extra = {
        k: v for k, v in values.items()
        if k not in mapped and not k.startswith("__")
    }

    result.record = {k: record.get(k) for k in CANONICAL_FIELDS}
    return result

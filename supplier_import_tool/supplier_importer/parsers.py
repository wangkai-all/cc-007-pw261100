"""CSV / JSONL 解析, 产出 (物理起始行号, 行字典, 原始文本).

CSV 使用 utf-8-sig 兼容带 BOM 的文件, csv 模块原生支持带引号的
内嵌换行字段; 每条记录记录其在原始文件中的起始物理行号, 保证可追溯.
"""

from __future__ import annotations

import csv
import io
import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ParsedRow:
    line_no: int          # 该记录在原始文件中的起始物理行号 (表头算第 1 行)
    values: dict          # 列名 -> 原始字符串 (CSV) 或 JSON 值 (JSONL)
    raw_text: str         # 原始内容, 供隔离区留档
    column_count_error: bool = False   # CSV 列数与表头不符


@dataclass
class ParsedFile:
    headers: list[str]
    rows: list[ParsedRow]


class ParseError(Exception):
    """文件级解析失败 (无法得到表头/结构)."""


def read_text(path: str | Path, encoding: str) -> str:
    return Path(path).read_text(encoding=encoding)


def sniff_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        return ","


def parse_csv(
    path: str | Path,
    encoding: str = "utf-8-sig",
    delimiter: str | None = None,
) -> ParsedFile:
    text = read_text(path, encoding)
    if not text.strip():
        raise ParseError("文件为空")
    delim = delimiter or sniff_delimiter(text[:8192])

    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delim)
    rows: list[ParsedRow] = []
    headers: list[str] | None = None

    while True:
        start_line = reader.line_num + 1     # 下一条记录的起始物理行
        try:
            record = next(reader)
        except StopIteration:
            break
        except csv.Error as e:
            raise ParseError(f"CSV 解析失败 (第 {start_line} 行附近): {e}") from e
        if headers is None:
            headers = [h.strip() for h in record]
            if not any(headers):
                raise ParseError("表头为空")
            continue
        if not any(cell.strip() for cell in record):
            continue                          # 跳过纯空行
        count_error = len(record) != len(headers)
        values = {}
        for i, h in enumerate(headers):
            values[h] = record[i] if i < len(record) else None
        if len(record) > len(headers):
            values["__extra_columns__"] = record[len(headers):]
        rows.append(ParsedRow(
            line_no=start_line,
            values=values,
            raw_text=",".join(record)[:2000],
            column_count_error=count_error,
        ))

    if headers is None:
        raise ParseError("文件没有表头")
    return ParsedFile(headers=headers, rows=rows)


def parse_jsonl(path: str | Path, encoding: str = "utf-8-sig") -> ParsedFile:
    text = read_text(path, encoding)
    if not text.strip():
        raise ParseError("文件为空")
    rows: list[ParsedRow] = []
    headers: list[str] = []
    seen_keys: set[str] = set()

    for idx, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as e:
            # 单条坏 JSON 不拖垮整份文件: 作为行级错误行产出
            rows.append(ParsedRow(
                line_no=idx,
                values={"__invalid_json__": str(e)},
                raw_text=line[:2000],
            ))
            continue
        if not isinstance(obj, dict):
            rows.append(ParsedRow(
                line_no=idx,
                values={"__invalid_json__": "JSON 行不是对象"},
                raw_text=line[:2000],
            ))
            continue
        for k in obj:
            if k not in seen_keys:
                seen_keys.add(k)
                headers.append(k)
        rows.append(ParsedRow(line_no=idx, values=obj, raw_text=line[:2000]))

    if not headers and not rows:
        raise ParseError("文件没有有效内容")
    return ParsedFile(headers=headers, rows=rows)


def parse_file(
    path: str | Path,
    fmt: str,
    encoding: str = "utf-8-sig",
    delimiter: str | None = None,
) -> ParsedFile:
    if fmt == "csv":
        return parse_csv(path, encoding=encoding, delimiter=delimiter)
    if fmt == "jsonl":
        return parse_jsonl(path, encoding=encoding)
    raise ParseError(f"不支持的格式: {fmt}")


def detect_format(filename: str) -> str | None:
    lower = filename.lower()
    if lower.endswith(".csv"):
        return "csv"
    if lower.endswith(".jsonl") or lower.endswith(".ndjson"):
        return "jsonl"
    return None

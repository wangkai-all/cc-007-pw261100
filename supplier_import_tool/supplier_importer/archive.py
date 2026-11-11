"""ZIP 安全解压: 拒绝越界路径与超出配置上限的内容.

防线:
1. 路径校验 —— 绝对路径、盘符、".."、反斜杠跳转、符号链接一律拒绝;
   解压目标解析后的真实路径必须仍在目标目录内.
2. 配额校验 —— 文件数、单文件大小、总解压大小、压缩比上限,
   超限即整个压缩包拒绝处理, 已产生的临时文件全部清理.
"""

from __future__ import annotations

import re
import shutil
import stat
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath


class ArchiveSafetyError(Exception):
    """压缩包违反安全策略, 整个拒绝."""


@dataclass(frozen=True)
class ExtractLimits:
    max_files: int = 1000
    max_total_uncompressed: int = 100 * 1024 * 1024   # 100 MB
    max_file_size: int = 20 * 1024 * 1024             # 20 MB
    max_compression_ratio: float = 100.0              # 防 zip 炸弹


@dataclass
class ExtractedMember:
    inner_path: str          # 包内路径 (POSIX 风格)
    extracted_path: Path     # 落盘位置
    size: int


@dataclass
class ExtractResult:
    members: list[ExtractedMember] = field(default_factory=list)
    skipped_dirs: int = 0


_DRIVE_RE = re.compile(r"^[a-zA-Z]:")


def validate_member_name(name: str) -> str:
    """校验并返回规范化后的包内相对路径; 不合法则抛 ArchiveSafetyError."""
    if not name or name.endswith("/"):
        raise ArchiveSafetyError(f"目录条目不应参与文件校验: {name!r}")
    if "\\" in name:
        raise ArchiveSafetyError(f"拒绝含反斜杠的路径 (可能是 Windows 路径穿越): {name!r}")
    if name.startswith("/") or _DRIVE_RE.match(name):
        raise ArchiveSafetyError(f"拒绝绝对路径: {name!r}")
    parts = PurePosixPath(name).parts
    if any(p in ("..", "", ".") for p in parts):
        raise ArchiveSafetyError(f"拒绝含越界/异常分量的路径: {name!r}")
    return str(PurePosixPath(*parts))


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return stat.S_ISLNK(mode)


def safe_extract_zip(
    zip_path: str | Path,
    dest_dir: str | Path,
    limits: ExtractLimits | None = None,
) -> ExtractResult:
    """把 zip_path 安全解压到 dest_dir, 返回成员清单. 任何违规都整体拒绝."""
    limits = limits or ExtractLimits()
    zip_path = Path(zip_path)
    dest_dir = Path(dest_dir)
    dest_root = dest_dir.resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    result = ExtractResult()

    try:
        with zipfile.ZipFile(zip_path) as zf:
            infos = zf.infolist()
            files = [i for i in infos if not i.is_dir()]
            if len(files) > limits.max_files:
                raise ArchiveSafetyError(
                    f"文件数 {len(files)} 超过上限 {limits.max_files}"
                )

            total = 0
            for info in files:
                # --- 路径与类型校验 ---
                safe_name = validate_member_name(info.filename)
                if _is_symlink(info):
                    raise ArchiveSafetyError(f"拒绝符号链接条目: {info.filename!r}")

                # --- 配额校验 (先按声明值快速失败) ---
                if info.file_size > limits.max_file_size:
                    raise ArchiveSafetyError(
                        f"{info.filename}: 解压后大小 {info.file_size} 超过单文件上限 "
                        f"{limits.max_file_size}"
                    )
                total += info.file_size
                if total > limits.max_total_uncompressed:
                    raise ArchiveSafetyError(
                        f"总解压大小超过上限 {limits.max_total_uncompressed}"
                    )
                if info.compress_size > 0:
                    ratio = info.file_size / info.compress_size
                    if ratio > limits.max_compression_ratio:
                        raise ArchiveSafetyError(
                            f"{info.filename}: 压缩比 {ratio:.0f} 超过上限 "
                            f"{limits.max_compression_ratio} (疑似 zip 炸弹)"
                        )

                # --- 落盘, 边写边复核真实字节数 ---
                target = (dest_dir / safe_name).resolve()
                if not (target == dest_root or dest_root in target.parents):
                    raise ArchiveSafetyError(
                        f"目标路径越出解压目录: {info.filename!r}"
                    )
                target.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with zf.open(info) as src, open(target, "wb") as dst:
                    while True:
                        chunk = src.read(65536)
                        if not chunk:
                            break
                        written += len(chunk)
                        if written > limits.max_file_size:
                            raise ArchiveSafetyError(
                                f"{info.filename}: 实际解压大小超过单文件上限"
                            )
                        dst.write(chunk)
                result.members.append(
                    ExtractedMember(inner_path=safe_name, extracted_path=target, size=written)
                )
            result.skipped_dirs = len(infos) - len(files)
    except ArchiveSafetyError:
        shutil.rmtree(dest_dir, ignore_errors=True)   # 整体拒绝, 不留半成品
        raise
    except zipfile.BadZipFile as e:
        shutil.rmtree(dest_dir, ignore_errors=True)
        raise ArchiveSafetyError(f"不是合法的 ZIP 文件: {e}") from e

    return result

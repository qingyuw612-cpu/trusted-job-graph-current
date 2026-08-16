"""岗位归一化项目的 CSV/JSONL 文件输入输出工具。"""

from __future__ import annotations

import csv
import json
import os
from pathlib import Path
from typing import Any, Iterable, Iterator


def detect_csv_encoding(path: Path) -> str:
    """检测 UTF-8 BOM、UTF-8 和 GB18030 三种常见中文 CSV 编码。"""

    sample = path.read_bytes()[:65536]
    if sample.startswith(b"\xef\xbb\xbf"):
        return "utf-8-sig"
    for encoding in ("utf-8", "gb18030"):
        try:
            sample.decode(encoding)
            return encoding
        except UnicodeDecodeError:
            continue
    raise UnicodeError(f"无法识别 CSV 编码：{path}")


def iter_csv_rows(path: Path) -> Iterator[dict[str, str]]:
    """逐行读取 CSV，兼容包含换行符的 JD 字段。"""

    encoding = detect_csv_encoding(path)
    with path.open("r", encoding=encoding, newline="") as stream:
        reader = csv.DictReader(stream)
        if not reader.fieldnames:
            raise ValueError(f"CSV 没有表头：{path}")
        yield from reader


def write_csv_atomic(path: Path, rows: Iterable[dict[str, Any]], fieldnames: list[str]) -> None:
    """先写临时文件再原子替换，避免中断时留下不完整结果。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(temporary, path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def write_jsonl_atomic(path: Path, rows: Iterable[dict[str, Any]]) -> None:
    """以 UTF-8 JSONL 原子写出审核队列或新岗位候选。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        os.replace(temporary, path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    """原子写出运行清单和汇总指标。"""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise

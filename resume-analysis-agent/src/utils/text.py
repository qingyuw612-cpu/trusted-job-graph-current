"""文本转换工具 — markitdown 封装（PDF/DOCX → Markdown）。"""

import os
import tempfile
from pathlib import Path

RAW_SUFFIXES = {".pdf", ".docx", ".doc"}


def is_raw_file(path: str) -> bool:
    """判断是否为需要转换的原始简历文件。"""
    return Path(path).suffix.lower() in RAW_SUFFIXES


def convert_to_markdown(file_path: str) -> str:
    """PDF/DOCX → Markdown 文本。

    Args:
        file_path: 本地简历文件路径。

    Returns:
        Markdown 文本。

    Raises:
        FileNotFoundError: 文件不存在。
        ValueError: markitdown 转换结果为空。
    """
    path = Path(file_path)
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在: {path}")
    if not is_raw_file(str(path)):
        # 非原始格式（如 .md/.txt）直接读文本
        return path.read_text(encoding="utf-8", errors="ignore").strip()

    from markitdown import MarkItDown

    md = MarkItDown()
    result = md.convert(str(path))
    text = (result.text_content or "").strip()
    if not text:
        raise ValueError(f"markitdown 转换结果为空: {path}")
    return text


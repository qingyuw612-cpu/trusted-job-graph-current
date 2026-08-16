"""批量给“按搜索名称拆分”目录中的 CSV 新增岗位归一化列。

默认只处理 CSV，并将结果写入新的平级目录，不覆盖原始数据。脚本采用
逐行流式读写，适合处理包含大段、多行 JD 的招聘数据。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from trusted_graph_agent.job_title_normalizer import JobTitleNormalizer


PROJECT_ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = PROJECT_ROOT.parent
DEFAULT_INPUT_DIR = WORKSPACE_ROOT / "2026数据51job" / "按搜索名称拆分"
DEFAULT_OUTPUT_DIR = WORKSPACE_ROOT / "2026数据51job" / "按搜索名称拆分_岗位归一化"


@dataclass(slots=True)
class FileResult:
    """记录单个 CSV 的处理结果，便于最后汇总和排查。"""

    source: Path
    destination: Path
    rows: int = 0
    unresolved_rows: int = 0
    status: str = "processed"
    message: str = ""


def detect_csv_encoding(path: Path) -> str:
    """检测常见中文 CSV 编码；输出文件统一使用 Excel 兼容的 UTF-8 BOM。"""

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


def discover_csv_files(input_dir: Path, recursive: bool) -> list[Path]:
    """按文件名稳定排序发现 CSV；默认忽略拆分清单等非 CSV 文件。"""

    iterator: Iterable[Path]
    iterator = input_dir.rglob("*.csv") if recursive else input_dir.glob("*.csv")
    return sorted((path for path in iterator if path.is_file()), key=lambda item: str(item).casefold())


def _build_output_columns(
    source_columns: list[str],
    target_column: str,
    tags_column: str | None,
) -> list[str]:
    """在原字段末尾追加结果列，并防止重复列名。"""

    columns = list(source_columns)
    if target_column not in columns:
        columns.append(target_column)
    if tags_column and tags_column not in columns:
        columns.append(tags_column)
    return columns


def normalize_csv_file(
    source: Path,
    destination: Path,
    normalizer: JobTitleNormalizer,
    source_column: str = "原始职位名称",
    target_column: str = "归一化岗位名称",
    tags_column: str | None = None,
    overwrite: bool = False,
) -> FileResult:
    """流式处理一个 CSV，在不改变原字段内容的前提下追加归一化结果。"""

    result = FileResult(source=source, destination=destination)
    if destination.exists() and not overwrite:
        result.status = "skipped"
        result.message = "目标文件已存在；如需重做请增加 --overwrite"
        return result

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.tmp")
    encoding = detect_csv_encoding(source)

    try:
        with source.open("r", encoding=encoding, newline="") as input_stream:
            reader = csv.DictReader(input_stream)
            source_columns = list(reader.fieldnames or [])
            if not source_columns:
                raise ValueError("CSV 没有表头")
            if source_column not in source_columns:
                available = "、".join(source_columns)
                raise KeyError(f"找不到列“{source_column}”；现有列：{available}")

            output_columns = _build_output_columns(source_columns, target_column, tags_column)
            with temporary.open("w", encoding="utf-8-sig", newline="") as output_stream:
                writer = csv.DictWriter(
                    output_stream,
                    fieldnames=output_columns,
                    dialect=reader.dialect,
                    extrasaction="raise",
                )
                writer.writeheader()
                for row in reader:
                    title = str(row.get(source_column) or "").strip()
                    normalized = normalizer.normalize(title)
                    row[target_column] = normalized.normalized_name
                    if tags_column:
                        row[tags_column] = json.dumps(
                            list(normalized.tags),
                            ensure_ascii=False,
                            separators=(",", ":"),
                        )
                    writer.writerow(row)
                    result.rows += 1
                    if title and not normalized.normalized_name:
                        result.unresolved_rows += 1

        # 临时文件完整写完后再替换，避免异常中断破坏目标文件。
        os.replace(temporary, destination)
        return result
    except Exception:
        if temporary.exists():
            temporary.unlink()
        raise


def normalize_directory(
    input_dir: Path,
    output_dir: Path,
    normalizer: JobTitleNormalizer,
    source_column: str,
    target_column: str,
    tags_column: str | None,
    recursive: bool,
    overwrite: bool,
) -> list[FileResult]:
    """处理目录中的全部 CSV，并在输出目录中保留相对路径和文件名。"""

    if not input_dir.is_dir():
        raise NotADirectoryError(f"输入目录不存在：{input_dir}")
    if input_dir.resolve() == output_dir.resolve():
        raise ValueError("输入目录和输出目录不能相同，防止覆盖原始数据")

    files = discover_csv_files(input_dir, recursive=recursive)
    if not files:
        raise FileNotFoundError(f"输入目录中没有找到 CSV：{input_dir}")

    results: list[FileResult] = []
    for index, source in enumerate(files, start=1):
        destination = output_dir / source.relative_to(input_dir)
        try:
            result = normalize_csv_file(
                source=source,
                destination=destination,
                normalizer=normalizer,
                source_column=source_column,
                target_column=target_column,
                tags_column=tags_column,
                overwrite=overwrite,
            )
            results.append(result)
            print(
                f"[{index}/{len(files)}] {result.status}: {source.name} "
                f"(行数={result.rows}, 未解析={result.unresolved_rows})",
                flush=True,
            )
        except Exception as exc:
            results.append(
                FileResult(
                    source=source,
                    destination=destination,
                    status="failed",
                    message=str(exc),
                )
            )
            print(f"[{index}/{len(files)}] failed: {source.name}: {exc}", file=sys.stderr, flush=True)
    return results


def build_argument_parser() -> argparse.ArgumentParser:
    """定义命令行参数，并提供符合当前目录结构的默认值。"""

    parser = argparse.ArgumentParser(
        description="给按搜索名称拆分的 CSV 新增岗位归一化列（默认不覆盖源文件）"
    )
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR, help="输入 CSV 目录")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR, help="新文件输出目录")
    parser.add_argument("--source-column", default="原始职位名称", help="待归一化的原始岗位列名")
    parser.add_argument("--target-column", default="归一化岗位名称", help="新增的标准岗位列名")
    parser.add_argument(
        "--tags-column",
        default=None,
        help="可选：同时新增方向标签列，例如 --tags-column 方向标签；默认不新增",
    )
    parser.add_argument("--recursive", action="store_true", help="递归处理输入目录下的子目录")
    parser.add_argument("--overwrite", action="store_true", help="覆盖输出目录中已经存在的同名结果文件")
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行批处理并以退出码表示是否存在失败文件。"""

    args = build_argument_parser().parse_args(argv)
    normalizer = JobTitleNormalizer()
    try:
        results = normalize_directory(
            input_dir=args.input_dir.resolve(),
            output_dir=args.output_dir.resolve(),
            normalizer=normalizer,
            source_column=args.source_column,
            target_column=args.target_column,
            tags_column=args.tags_column,
            recursive=args.recursive,
            overwrite=args.overwrite,
        )
    except Exception as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 2

    processed = sum(item.status == "processed" for item in results)
    skipped = sum(item.status == "skipped" for item in results)
    failed = sum(item.status == "failed" for item in results)
    rows = sum(item.rows for item in results)
    unresolved = sum(item.unresolved_rows for item in results)
    print(
        f"完成：成功文件={processed}，跳过文件={skipped}，失败文件={failed}，"
        f"总行数={rows}，未解析岗位={unresolved}，输出目录={args.output_dir.resolve()}",
        flush=True,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

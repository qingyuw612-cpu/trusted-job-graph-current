"""CLI 入口 — rank / enhance / analyze 三个子命令。

用法示例：
    python src/main.py rank -r D:/简历/张三.pdf --topk 5
    python src/main.py rank -r results/test2.json --topk 10
    python src/main.py enhance -r rank_result.json --resume D:/简历/张三.pdf --topk 20
    python src/main.py analyze -r role.json --resume D:/简历/张三.pdf
"""

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from src.store import create_store
from src.tools.rank import rank_resume
from src.tools.enhance import enhance_matches
from src.tools.analyze import analyze_gap
from src.tools.modify import suggest_resume_edit
from src.tools.resume_extract import extract_resume_batch, load_resume_items
from src.utils.llm import LLM_PROVIDERS
from src.utils.text import convert_to_markdown


def _load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8-sig") as f:
        return json.load(f)


def _read_resume(resume_src: str) -> str:
    """简历输入：原始文件（PDF/DOCX/MD/TXT）→ Markdown 文本。"""
    path = Path(resume_src)
    if path.is_file():
        return convert_to_markdown(resume_src)
    raise FileNotFoundError(f"简历文件不存在: {resume_src}")


def cmd_rank(args) -> int:
    resume_text = _read_resume(args.resume)
    store = create_store(args.store or os.environ.get("STORE_BACKEND") or "memory")
    result = rank_resume(resume_text, topk=args.topk, store=store)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_enhance(args) -> int:
    rank_result = _load_json(args.rank_result)
    resume_text = _read_resume(args.resume)
    result = enhance_matches(rank_result, resume_text, topk=args.topk)
    if args.analyze and result.get("results"):
        # 用复核后的第 1 名做差距分析，保证两阶段数据一致
        gap = analyze_gap(result["results"][0], resume_text)
        result = {"enhanced": result, "gap_analysis": gap}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_analyze(args) -> int:
    role = _load_json(args.role)
    resume_text = _read_resume(args.resume)
    result = analyze_gap(role, resume_text)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_modify(args) -> int:
    role = _load_json(args.role)
    if "results" in role and isinstance(role["results"], list) and role["results"]:
        role = role["results"][0]
    resume_text = _read_resume(args.resume)
    result = suggest_resume_edit(role, resume_text)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


def cmd_extract_resume(args) -> int:
    if args.provider:
        os.environ["LLM_PROVIDER"] = args.provider
    items = load_resume_items(args.input)
    if not items:
        raise ValueError(f"输入中没有可解析的简历: {args.input}")

    def _progress(done: int, total: int) -> None:
        print(f"进度: {done}/{total}", flush=True)

    result = extract_resume_batch(
        items,
        position=args.position,
        max_workers=args.workers,
        progress_cb=_progress if args.workers > 1 else None,
    )
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"提取完成: {result['ok']}/{result['total']} 份 -> {out}")
    print("维度覆盖率:", json.dumps(result["coverage"], ensure_ascii=False))
    if result["failed"]:
        print("失败明细:", json.dumps(result["errors"], ensure_ascii=False, indent=2))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="resume-agent",
        description="简历人岗匹配分析 CLI（rank / enhance / analyze）",
    )
    parser.add_argument(
        "--store",
        choices=["memory", "neo4j"],
        default=None,
        help="数据源后端（默认读 STORE_BACKEND 环境变量，缺省 memory）",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_rank = subparsers.add_parser("rank", help="简历 vs 全部 Role 关键词命中粗排")
    p_rank.add_argument("-r", "--resume", required=True, help="简历文件（PDF/DOCX/MD/TXT）")
    p_rank.add_argument("--topk", type=int, default=10, help="返回前 N 名（默认 10）")

    p_enhance = subparsers.add_parser("enhance", help="LLM 复核 rank_resume 结果")
    p_enhance.add_argument("-r", "--rank-result", required=True, help="rank 结果 JSON 文件")
    p_enhance.add_argument("--resume", required=True, help="简历文件（PDF/DOCX/MD/TXT）")
    p_enhance.add_argument("--topk", type=int, default=20, help="复核前 N 名（默认 20）")
    p_enhance.add_argument(
        "--analyze",
        action="store_true",
        help="复核后自动对第 1 名做差距分析（enhance → analyze 一步完成）",
    )

    p_analyze = subparsers.add_parser("analyze", help="单 Role 差距分析 + 学习路径")
    p_analyze.add_argument("-r", "--role", required=True, help="单个 role JSON 文件")
    p_analyze.add_argument("--resume", required=True, help="简历文件（PDF/DOCX/MD/TXT）")

    p_modify = subparsers.add_parser("modify", help="针对目标岗位生成简历修改建议")
    p_modify.add_argument("-r", "--role", required=True, help="单个 role JSON 文件（或 rank 结果文件）")
    p_modify.add_argument("--resume", required=True, help="简历文件（PDF/DOCX/MD/TXT）")

    p_extract = subparsers.add_parser(
        "extract-resume",
        help="简历 → 7 维画像（LLM 批量提取，标准化输出）",
    )
    p_extract.add_argument(
        "-i", "--input", required=True,
        help="简历文件夹（PDF/DOCX/MD/TXT）或 JSON 数组文件（faircv 格式）",
    )
    p_extract.add_argument(
        "-o", "--output", default="resume_profiles.json",
        help="输出 JSON 路径（默认 resume_profiles.json）",
    )
    p_extract.add_argument(
        "--position", default=None,
        help="目标岗位（未提供时取简历求职意向）",
    )
    p_extract.add_argument(
        "--provider", default=None, choices=sorted(LLM_PROVIDERS),
        help="临时切换 LLM 供应商（默认读 .env 的 LLM_PROVIDER）",
    )
    p_extract.add_argument(
        "--workers", type=int, default=4,
        help="并发线程数（默认 4；注意讯飞 API 有并发路数限制，被流控时调小）",
    )

    args = parser.parse_args()
    if args.store is not None:
        os.environ["STORE_BACKEND"] = args.store

    if args.command == "rank":
        sys.exit(cmd_rank(args))
    elif args.command == "enhance":
        sys.exit(cmd_enhance(args))
    elif args.command == "analyze":
        sys.exit(cmd_analyze(args))
    elif args.command == "modify":
        sys.exit(cmd_modify(args))
    elif args.command == "extract-resume":
        sys.exit(cmd_extract_resume(args))


if __name__ == "__main__":
    main()


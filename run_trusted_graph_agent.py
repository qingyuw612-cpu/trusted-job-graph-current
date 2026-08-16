"""可信岗位能力图谱构建 Agent 命令行入口。

示例构建：
    python run_trusted_graph_agent.py build

启动 API 与全景页：
    python run_trusted_graph_agent.py serve
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from trusted_graph_agent import AgentConfig, TrustedGraphAgent
from trusted_graph_agent.api_server import run_server


BASE_DIR = Path(__file__).resolve().parent
WORKSPACE_DIR = BASE_DIR.parent
DEFAULT_INPUT = WORKSPACE_DIR / "提取后原始数据"
DEFAULT_OUTPUT = BASE_DIR / "output" / "all_it_roles_knowledge_graph_v5_neo4j"
STATIC_PAGE = BASE_DIR / "trusted_graph_agent" / "static" / "panorama.html"
SAMPLE_PATTERNS = [
    "互联网产品经理_output_folder/产品经理.csv",
    "人工智能_output_folder/算法工程师.csv",
    "后端开发_output_folder/python开发工程师.csv",
]


def add_build_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT, help="CSV 根目录")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT, help="产物目录")
    parser.add_argument("--all-files", action="store_true", help="处理输入目录下全部 CSV")
    parser.add_argument("--include", action="append", default=[], help="按相对路径、文件名或片段筛选，可重复")
    parser.add_argument("--max-rows-per-file", type=int, default=180, help="每个岗位 CSV 最多保留多少条 JD；0 表示不限制")
    parser.add_argument("--scan-rows-per-file", type=int, default=0, help="抽样时每个 CSV 最多检查多少行；0 表示与保留数量相同")
    parser.add_argument("--group-by-file-role", action="store_true", help="将 CSV 文件名作为标准岗位，并优先抽取标题相关的 JD")
    parser.add_argument("--it-only", action="store_true", help="只处理信息技术岗位分类表中定义的 CSV")
    parser.add_argument("--half-life-months", type=float, default=12.0, help="时间衰减半衰期（月）")
    parser.add_argument("--template-similarity", type=float, default=0.97, help="整条提取结果高度相似降权阈值")
    parser.add_argument("--required-support", type=float, default=0.60, help="必备技能最低支持度")
    parser.add_argument("--min-required-companies", type=float, default=3.0, help="必备技能最低有效公司数")
    parser.add_argument("--llm-endpoint", default="", help="可选 LLM 技能抽取 Webhook URL")


def build_agent(args: argparse.Namespace):
    patterns = [] if args.all_files else (args.include or SAMPLE_PATTERNS)
    config = AgentConfig(
        input_dir=args.input_dir.resolve(),
        output_dir=args.output_dir.resolve(),
        max_rows_per_file=max(0, args.max_rows_per_file),
        scan_rows_per_file=max(0, args.scan_rows_per_file),
        group_by_file_role=args.group_by_file_role,
        it_only=args.it_only,
        half_life_months=args.half_life_months,
        template_similarity=args.template_similarity,
        required_support_threshold=args.required_support,
        min_required_companies=args.min_required_companies,
        include_patterns=patterns,
        llm_endpoint=args.llm_endpoint,
    )
    agent = TrustedGraphAgent(config)
    bundle = agent.run()
    page_path = config.output_dir / "panorama.html"
    shutil.copyfile(STATIC_PAGE, page_path)
    print("\n构建完成")
    print(json.dumps(bundle.run["summary"], ensure_ascii=False, indent=2))
    print(f"\nSQLite 数据库：{config.output_dir / 'knowledge_graph.db'}")
    print(f"Neo4j 导入目录：{config.output_dir / 'neo4j'}")
    print(f"可视化页面：{page_path}")
    return config, bundle


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CSV → 可信技能证据 → 岗位画像 → Neo4j/SQLite → API/全景页")
    subparsers = parser.add_subparsers(dest="command")
    build_parser = subparsers.add_parser("build", help="构建图谱产物")
    add_build_arguments(build_parser)
    serve_parser = subparsers.add_parser("serve", help="启动本地 API 与全景页")
    serve_parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8010)
    serve_parser.add_argument("--backend", choices=("auto", "sqlite", "neo4j"), default="auto")
    serve_parser.add_argument("--neo4j-config", type=Path, default=None)
    demo_parser = subparsers.add_parser("demo", help="构建后立即启动服务")
    add_build_arguments(demo_parser)
    demo_parser.add_argument("--host", default="127.0.0.1")
    demo_parser.add_argument("--port", type=int, default=8010)
    return parser


def main() -> None:
    if len(sys.argv) == 1:
        sys.argv.append("build")
    args = create_parser().parse_args()
    if args.command == "build":
        build_agent(args)
    elif args.command == "serve":
        output_dir = args.output_dir.resolve()
        run_server(
            output_dir / "knowledge_graph.db",
            output_dir / "panorama.html",
            args.host,
            args.port,
            backend=args.backend,
            neo4j_config=args.neo4j_config.resolve() if args.neo4j_config else None,
        )
    elif args.command == "demo":
        config, _ = build_agent(args)
        run_server(config.output_dir / "knowledge_graph.db", config.output_dir / "panorama.html", args.host, args.port)
    else:
        create_parser().print_help()


if __name__ == "__main__":
    main()

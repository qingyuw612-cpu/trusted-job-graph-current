"""Launch the local role-evolution workbench."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .workbench import (
    MAAS_API_KEY_ENV,
    _load_maas_api_key,
    check_workbench,
    run_workbench,
)


FEATURE_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = FEATURE_DIR.parent
DEFAULT_NEO4J_CONFIG = PROJECT_ROOT / "config" / "neo4j_connection.json"
DEFAULT_DATA_ROOT = PROJECT_ROOT / "output" / "role_evolution_workbench_v2"
WORKBENCH_PAGE = FEATURE_DIR / "static" / "index.html"


def _port(value: str) -> int:
    port = int(value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口必须在 1 到 65535 之间")
    return port


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "启动本地岗位演化工作台：只读分析 Neo4j 正式图谱，"
            "将候选和人工决定写入独立审核子图"
        )
    )
    parser.add_argument(
        "--neo4j-config",
        type=Path,
        default=DEFAULT_NEO4J_CONFIG,
        help=f"Neo4j 连接配置（默认：{DEFAULT_NEO4J_CONFIG}）",
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=DEFAULT_DATA_ROOT,
        help=f"任务状态和分析结果目录（默认：{DEFAULT_DATA_ROOT}）",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="监听地址；本地使用请保持 127.0.0.1",
    )
    parser.add_argument("--port", type=_port, default=8070, help="监听端口（默认：8070）")
    parser.add_argument(
        "--check",
        action="store_true",
        help="只读检查 Neo4j、工作台页面和 Qwen APIKey 状态后退出",
    )
    return parser


def _check(neo4j_config_path: Path, data_root: Path) -> dict:
    result = check_workbench(neo4j_config_path, data_root)
    page_exists = WORKBENCH_PAGE.is_file()
    api_key_configured = bool(_load_maas_api_key())

    result["page"] = {
        "path": str(WORKBENCH_PAGE),
        "exists": page_exists,
    }
    result["qwen"] = {
        "api_key_environment": MAAS_API_KEY_ENV,
        "configured": api_key_configured,
    }
    # Missing Qwen credentials only disables optional semantic review. Neo4j
    # connectivity and the page are required for the workbench itself.
    result["ready"] = bool(result.get("ready", False) and page_exists)
    return result


def main() -> int:
    args = create_parser().parse_args()
    neo4j_config_path = args.neo4j_config.expanduser().resolve()
    data_root = args.data_root.expanduser().resolve()

    if args.check:
        result = _check(neo4j_config_path, data_root)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ready"] else 1

    run_workbench(
        neo4j_config_path=neo4j_config_path,
        data_root=data_root,
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

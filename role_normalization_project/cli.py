"""岗位概念归一化批处理命令行入口。

本程序只生成 CSV/JSONL 审核产物，不连接或修改 Neo4j。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any


PROJECT_DIR = Path(__file__).resolve().parent
REPOSITORY_DIR = PROJECT_DIR.parent
WORKSPACE_DIR = REPOSITORY_DIR.parent
if str(REPOSITORY_DIR) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_DIR))

from role_normalizer.config import load_config  # noqa: E402
from role_normalizer.discovery import NewRoleDiscovery  # noqa: E402
from role_normalizer.embedding import HashingTextEmbedder, SentenceTransformerEmbedder  # noqa: E402
from role_normalizer.io import iter_csv_rows, write_csv_atomic, write_json_atomic, write_jsonl_atomic  # noqa: E402
from role_normalizer.models import JobTitleRecord, ResolutionType  # noqa: E402
from role_normalizer.pair_similarity import RecordPairSimilarity, default_blocking_keys  # noqa: E402
from role_normalizer.preprocessing import TitlePreprocessor  # noqa: E402
from role_normalizer.resolver import ExistingRoleResolver  # noqa: E402
from role_normalizer.taxonomy_adapter import load_role_registry, registry_summary  # noqa: E402


DEFAULT_INPUT = WORKSPACE_DIR / "2026数据51job" / "按搜索名称拆分_岗位归一化"
DEFAULT_OUTPUT = PROJECT_DIR / "output" / "role_normalization_run"
DEFAULT_REGISTRY = REPOSITORY_DIR / "trusted_graph_agent" / "it_role_taxonomy.json"
DEFAULT_LOCAL_MODEL = (
    REPOSITORY_DIR
    / "models"
    / "hf_cache"
    / "hub"
    / "models--BAAI--bge-small-zh-v1.5"
    / "snapshots"
    / "7999e1d3359715c523056ef9478215996d62a620"
)
RESPONSIBILITY_START = re.compile(r"(?:岗位职责|工作职责|职位描述|工作内容)[:：]?", re.IGNORECASE)
RESPONSIBILITY_END = re.compile(r"(?:任职要求|任职资格|职位要求|岗位要求)[:：]?", re.IGNORECASE)


def parse_list(value: str) -> list[str]:
    """解析 JSON 数组或常见分隔符拼接的技能、标签字段。"""

    text = str(value or "").strip()
    if not text:
        return []
    if text.startswith(("[", "{")):
        try:
            payload = json.loads(text)
            if isinstance(payload, list):
                return [str(item).strip() for item in payload if str(item).strip()]
            if isinstance(payload, dict):
                flattened: list[str] = []
                for group_value in payload.values():
                    if isinstance(group_value, list):
                        flattened.extend(
                            str(item).strip() for item in group_value if str(item).strip()
                        )
                    else:
                        flattened.extend(parse_list(str(group_value or "")))
                return list(dict.fromkeys(flattened))
        except json.JSONDecodeError:
            pass
    return [item.strip() for item in re.split(r"[，,、;；|\n]+", text) if item.strip()]


def extract_responsibilities(jd_text: str, limit: int = 2000) -> str:
    """从 JD 中截取职责段；无法识别标题时保守使用开头文本。"""

    text = re.sub(r"\s+", " ", str(jd_text or "")).strip()
    if not text:
        return ""
    start = RESPONSIBILITY_START.search(text)
    begin = start.end() if start else 0
    end_match = RESPONSIBILITY_END.search(text, begin)
    end = end_match.start() if end_match else min(len(text), begin + limit)
    return text[begin:end].strip()[:limit]


def template_id(jd_text: str) -> str:
    """生成粗粒度 JD 模板标识，供新岗位独立模板门禁使用。"""

    normalized = re.sub(r"\d+", "#", re.sub(r"\s+", "", str(jd_text or "")).casefold())
    return "template:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def discover_input_files(path: Path, recursive: bool) -> list[Path]:
    """接受单个 CSV 或目录，并按稳定顺序返回文件列表。"""

    if path.is_file():
        if path.suffix.casefold() != ".csv":
            raise ValueError(f"输入文件不是 CSV：{path}")
        return [path]
    if not path.is_dir():
        raise FileNotFoundError(f"输入路径不存在：{path}")
    iterator = path.rglob("*.csv") if recursive else path.glob("*.csv")
    files = sorted((item for item in iterator if item.is_file()), key=lambda item: str(item).casefold())
    if not files:
        raise FileNotFoundError(f"没有找到 CSV：{path}")
    return files


def create_embedder(args: argparse.Namespace, model_name: str):
    """根据命令行选择测试哈希向量器或生产语义模型。"""

    if args.embedder == "hashing":
        return HashingTextEmbedder(dimension=args.hash_dimension)
    selected_model = args.model or (str(DEFAULT_LOCAL_MODEL) if DEFAULT_LOCAL_MODEL.is_dir() else model_name)
    return SentenceTransformerEmbedder(
        selected_model,
        device=args.device or None,
        batch_size=args.embedding_batch_size,
    )


def _required_value(row: dict[str, str], column: str, source: Path, row_number: int) -> str:
    """读取必填列，并给出可定位到文件和行号的错误。"""

    if column not in row:
        raise KeyError(f"{source.name} 缺少列“{column}”")
    value = str(row.get(column) or "").strip()
    if not value:
        raise ValueError(f"{source.name} 第 {row_number} 行的“{column}”为空")
    return value


def run(args: argparse.Namespace) -> dict[str, Any]:
    """执行文件级归一化并生成审核产物；不写图数据库。"""

    # Keep a user-supplied relative path relative on Windows.  Some bundled
    # Python runtimes decode a non-ASCII current directory with the legacy
    # code page while resolving it, which produces an unusable path.
    output_dir = args.output_dir
    if output_dir.exists() and any(output_dir.iterdir()) and not args.overwrite:
        raise FileExistsError(f"输出目录非空：{output_dir}；如需重做请增加 --overwrite")

    config = load_config(args.config)
    registry = load_role_registry(args.registry)
    embedder = create_embedder(args, config.embedding_model)
    resolver = ExistingRoleResolver(registry, embedder, config.to_dict())
    preprocessor = TitlePreprocessor()

    output_rows: list[dict[str, Any]] = []
    resolutions = []
    files = discover_input_files(args.input, args.recursive)
    for source in files:
        for row_number, row in enumerate(iter_csv_rows(source), start=2):
            original = _required_value(row, args.title_column, source, row_number)
            normalized_from_file = str(row.get(args.normalized_column) or "").strip()
            normalized, direction_tags = preprocessor.normalize(original)
            if normalized_from_file:
                normalized = normalized_from_file
            jd_text = str(row.get(args.jd_column) or "")
            skills = parse_list(str(row.get(args.skills_column) or "")) if args.skills_column else []
            source_id = str(row.get(args.id_column) or f"{source.name}:{row_number}").strip()
            search_name = str(
                row.get(args.search_column)
                or row.get("搜索关键词")
                or source.stem
            ).strip()
            company = str(row.get(args.company_column) or "").strip()
            record = JobTitleRecord(
                original_name=original,
                normalized_name=normalized,
                tags=list(direction_tags),
                responsibilities=extract_responsibilities(jd_text),
                skills=skills,
                source_id=source_id,
                search_name=search_name,
                metadata={
                    "company_id": company,
                    "company_name": company,
                    "template_id": template_id(jd_text),
                    "source_file": str(source),
                    "source_row": row_number,
                },
            )
            resolution = resolver.resolve(record)
            resolutions.append(resolution)
            scores = resolution.scores.to_dict()
            output_rows.append(
                {
                    **row,
                    "规则归一化名称": normalized,
                    "方向标签": json.dumps(list(direction_tags), ensure_ascii=False),
                    "受控岗位ID": resolution.role_id or "",
                    "受控岗位名称": resolution.canonical_name or "",
                    "岗位归一化状态": resolution.resolution_type.value,
                    "岗位综合相似度": round(scores["combined_similarity"], 6),
                    "最近候选岗位": json.dumps(
                        resolution.metadata.get("nearest_roles", []), ensure_ascii=False
                    ),
                    "归一化原因": resolution.reason,
                    "岗位解析器版本": resolution.resolver_version,
                    "来源文件": source.name,
                }
            )

    unresolved = [
        item
        for item in resolutions
        if item.resolution_type in {ResolutionType.REVIEW, ResolutionType.UNMAPPED}
    ]
    weights = config.weights.to_dict()
    pair_similarity = RecordPairSimilarity(embedder, weights)
    pair_similarity.prepare([item.record for item in unresolved])
    discovery_cfg = config.raw.get("new_role_discovery", {})
    discovery = NewRoleDiscovery(
        pair_similarity,
        min_jds=int(discovery_cfg.get("min_jds", config.thresholds.new_role_min_jds)),
        min_companies=int(discovery_cfg.get("min_companies", config.thresholds.new_role_min_companies)),
        min_templates=int(discovery_cfg.get("min_templates", config.thresholds.new_role_min_templates)),
        min_skills=int(discovery_cfg.get("min_skills", config.thresholds.new_role_min_skills)),
        cluster_similarity=float(discovery_cfg.get("cluster_similarity", config.thresholds.review)),
        max_cluster_size=int(discovery_cfg.get("max_cluster_size", 200)),
        max_pair_comparisons=int(discovery_cfg.get("max_pair_comparisons", 5_000_000)),
        blocking_key=default_blocking_keys,
    )
    candidates = discovery.discover(unresolved)

    result_columns = list(output_rows[0]) if output_rows else []
    write_csv_atomic(output_dir / "role_resolutions.csv", output_rows, result_columns)
    review_rows = [item.to_dict() for item in resolutions if item.resolution_type == ResolutionType.REVIEW]
    review_rows.extend(item.to_dict() for item in candidates if item.passed_gate)
    write_jsonl_atomic(output_dir / "review_queue.jsonl", review_rows)
    write_jsonl_atomic(
        output_dir / "new_role_candidates.jsonl",
        (item.to_dict() for item in candidates if item.passed_gate),
    )
    write_jsonl_atomic(
        output_dir / "unmapped_clusters.jsonl",
        (item.to_dict() for item in candidates if not item.passed_gate),
    )

    counts = Counter(item.resolution_type.value for item in resolutions)
    manifest = {
        "mode": "FILE_ONLY_NO_GRAPH_WRITE",
        "config_version": config.version,
        "resolver_version": resolver.resolver_version,
        "input_files": [str(item) for item in files],
        "records": len(resolutions),
        "resolution_counts": dict(sorted(counts.items())),
        "registry": registry_summary(registry),
        "new_role_candidates": sum(item.passed_gate for item in candidates),
        "unmapped_clusters": sum(not item.passed_gate for item in candidates),
        "output_dir": str(output_dir),
    }
    write_json_atomic(output_dir / "manifest.json", manifest)
    return manifest


def build_parser() -> argparse.ArgumentParser:
    """定义批处理参数；默认值适配当前工作区但允许完全覆盖。"""

    parser = argparse.ArgumentParser(description="岗位概念归一化：精确匹配、向量召回和新岗位候选发现")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="输入 CSV 或目录")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT, help="结果输出目录")
    parser.add_argument("--config", type=Path, default=None, help="匹配权重与阈值 JSON")
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY, help="受控岗位注册表 JSON")
    parser.add_argument("--embedder", choices=("sentence-transformer", "hashing"), default="sentence-transformer")
    parser.add_argument("--model", default="", help="语义模型名称或本地路径")
    parser.add_argument("--device", default="", help="例如 cpu 或 cuda")
    parser.add_argument("--embedding-batch-size", type=int, default=64)
    parser.add_argument("--hash-dimension", type=int, default=384)
    parser.add_argument("--id-column", default="职位ID")
    parser.add_argument("--title-column", default="原始职位名称")
    parser.add_argument("--normalized-column", default="归一化岗位名称")
    parser.add_argument("--jd-column", default="JD全文")
    parser.add_argument("--skills-column", default="", help="可选的已归一化技能列")
    parser.add_argument("--company-column", default="公司全称")
    parser.add_argument("--search-column", default="岗位关键词")
    parser.add_argument("--recursive", action="store_true")
    parser.add_argument("--overwrite", action="store_true", help="允许覆盖既有结果文件")
    return parser


def main(argv: list[str] | None = None) -> int:
    """执行命令并以非零退出码表示失败。"""

    args = build_parser().parse_args(argv)
    try:
        manifest = run(args)
    except Exception as exc:
        print(f"ROLE_NORMALIZATION_FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

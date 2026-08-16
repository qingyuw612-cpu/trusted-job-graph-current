"""为 jobs_2026_it_含能力提取结果.csv 追加最终“岗位名称”列。

脚本默认输出新文件，不覆盖源 CSV；内部执行规则清洗、现有岗位匹配、
向量审核和新岗位候选聚类，但最终只向 CSV 增加一列。
"""

from __future__ import annotations

import argparse
import csv
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

from cli import (  # noqa: E402
    DEFAULT_LOCAL_MODEL,
    extract_responsibilities,
    parse_list,
    template_id,
)
from role_normalizer.config import load_config  # noqa: E402
from role_normalizer.discovery import NewRoleDiscovery  # noqa: E402
from role_normalizer.embedding import HashingTextEmbedder, SentenceTransformerEmbedder  # noqa: E402
from role_normalizer.io import detect_csv_encoding, iter_csv_rows, write_csv_atomic  # noqa: E402
from role_normalizer.models import JobTitleRecord, ResolutionType, RoleResolution  # noqa: E402
from role_normalizer.pair_similarity import RecordPairSimilarity  # noqa: E402
from role_normalizer.preprocessing import TitlePreprocessor  # noqa: E402
from role_normalizer.resolver import ExistingRoleResolver  # noqa: E402
from role_normalizer.taxonomy_adapter import load_role_registry  # noqa: E402
from role_normalizer.taxonomy_adapter import enrich_registry_with_role_skills  # noqa: E402


DEFAULT_INPUT = WORKSPACE_DIR / "2026数据51job" / "jobs_2026_it_含能力提取结果.csv"
DEFAULT_OUTPUT = WORKSPACE_DIR / "2026数据51job" / "jobs_2026_it_含能力提取结果_岗位归一化.csv"
DEFAULT_REGISTRY = REPOSITORY_DIR / "trusted_graph_agent" / "it_role_taxonomy.json"
DEFAULT_ROLE_SKILLS = REPOSITORY_DIR / "output" / "processed_normalization_full" / "skill_reports" / "role_top_skills.csv"
QUALIFICATION_NOISE = re.compile(r"(?:学历|专业$|相关专业|工作经验|项目经验|开发经验|管理经验)$")


def parse_ability_profile(value: str, categories: tuple[str, ...]) -> list[str]:
    """从能力 JSON 中提取岗位区分度较高的类别，并过滤资格条件。"""

    text = str(value or "").strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return [item for item in parse_list(text) if not QUALIFICATION_NOISE.search(item)]
    if not isinstance(payload, dict):
        return [item for item in parse_list(text) if not QUALIFICATION_NOISE.search(item)]
    output: list[str] = []
    for category in categories:
        output.extend(parse_list(str(payload.get(category) or "")))
    return list(dict.fromkeys(item for item in output if not QUALIFICATION_NOISE.search(item)))


def read_header(path: Path) -> list[str]:
    """读取并验证 CSV 表头，不加载数据行。"""

    with path.open("r", encoding=detect_csv_encoding(path), newline="") as stream:
        reader = csv.reader(stream)
        header = next(reader, [])
    if not header:
        raise ValueError(f"CSV 没有表头：{path}")
    return header


def build_embedder(args: argparse.Namespace, configured_model: str):
    """创建生产 BGE 或仅供联调的哈希向量器。"""

    if args.embedder == "hashing":
        return HashingTextEmbedder(args.hash_dimension)
    model = args.model or (str(DEFAULT_LOCAL_MODEL) if DEFAULT_LOCAL_MODEL.is_dir() else configured_model)
    return SentenceTransformerEmbedder(
        model,
        device=args.device or None,
        batch_size=args.embedding_batch_size,
    )


def build_records(args: argparse.Namespace) -> list[JobTitleRecord]:
    """第一遍读取：构建轻量岗位特征，不保留完整原始行。"""

    preprocessor = TitlePreprocessor()
    records: list[JobTitleRecord] = []
    seen_ids: set[str] = set()
    for row_number, row in enumerate(iter_csv_rows(args.input), start=2):
        source_id = str(row.get(args.id_column) or "").strip()
        original = str(row.get(args.title_column) or "").strip()
        if not source_id:
            raise ValueError(f"第 {row_number} 行“{args.id_column}”为空")
        if source_id in seen_ids:
            raise ValueError(f"职位ID重复：{source_id}")
        if not original:
            raise ValueError(f"第 {row_number} 行“{args.title_column}”为空")
        seen_ids.add(source_id)

        normalized, tags = preprocessor.normalize(original)
        jd_text = str(row.get(args.jd_column) or "")
        company = str(row.get(args.company_column) or "").strip()
        skills = parse_ability_profile(
            str(row.get(args.ability_column) or ""),
            tuple(item.strip() for item in args.ability_categories.split(",") if item.strip()),
        )
        records.append(
            JobTitleRecord(
                original_name=original,
                normalized_name=normalized,
                tags=list(tags),
                responsibilities=extract_responsibilities(jd_text),
                skills=skills,
                source_id=source_id,
                search_name=str(
                    row.get(args.search_column)
                    or row.get("搜索关键词")
                    or ""
                ).strip(),
                metadata={
                    "company_id": company,
                    "company_name": company,
                    "template_id": template_id(jd_text),
                    "source_row": row_number,
                },
            )
        )
    return records


def assign_final_titles(
    records: list[JobTitleRecord],
    resolutions: list[RoleResolution],
    candidates,
) -> dict[str, str]:
    """按现有岗位、新岗位候选、规则名称的优先级生成最终岗位名称。"""

    final_names: dict[str, str] = {}
    record_by_id = {record.source_id: record for record in records}
    for resolution in resolutions:
        record = resolution.record
        final_names[record.source_id] = (
            str(resolution.canonical_name or "").strip()
            or record.normalized_name.strip()
            or record.original_name.strip()
        )
    for candidate in candidates:
        if not candidate.passed_gate:
            continue
        for source_id in candidate.record_ids:
            if source_id in record_by_id:
                final_names[source_id] = candidate.representative_name
    return final_names


def run(args: argparse.Namespace) -> dict[str, Any]:
    """执行岗位归一化并生成仅多一列的新 CSV。"""

    def phase(message: str) -> None:
        """立即输出批处理阶段，长任务运行时不再表现为无响应。"""

        print(message, flush=True)

    args.input = args.input.resolve()
    args.output = args.output.resolve()
    if not args.input.is_file():
        raise FileNotFoundError(f"输入 CSV 不存在：{args.input}")
    if args.input == args.output:
        raise ValueError("输出文件不能与输入文件相同")
    if args.output.exists() and not args.overwrite:
        raise FileExistsError(f"输出文件已存在：{args.output}；如需重做请增加 --overwrite")

    phase("[1/6] 检查输入 CSV 和字段...")
    header = read_header(args.input)
    required = {
        args.id_column,
        args.title_column,
        args.jd_column,
        args.company_column,
        args.ability_column,
    }
    missing = sorted(required - set(header))
    if missing:
        raise KeyError(f"输入 CSV 缺少列：{'、'.join(missing)}")
    if args.output_column in header and not args.replace_column:
        raise ValueError(
            f"输入 CSV 已有“{args.output_column}”列；如需更新请增加 --replace-column"
        )

    phase("[2/6] 加载配置、岗位库和向量模型...")
    config = load_config(args.config)
    registry = load_role_registry(args.registry)
    registry = enrich_registry_with_role_skills(
        registry,
        args.role_skills,
        categories=tuple(
            item.strip() for item in args.role_skill_categories.split(",") if item.strip()
        ),
        max_skills_per_role=args.max_role_skills,
    )
    embedder = build_embedder(args, config.embedding_model)
    resolver_config = config.to_dict()
    resolver_config["record_batch_size"] = args.record_batch_size
    resolver = ExistingRoleResolver(registry, embedder, resolver_config)

    phase("[3/6] 读取并清洗岗位记录...")
    records = build_records(args)
    phase(f"      已读取 {len(records):,} 条；开始匹配现有岗位。")

    def resolution_progress(done: int, total: int) -> None:
        """逐批报告现有岗位匹配进度。"""

        print(f"\r      现有岗位匹配：{done:,}/{total:,}", end="", flush=True)

    resolutions = resolver.resolve_many(records, progress_callback=resolution_progress)
    print(flush=True)
    unresolved = [
        item
        for item in resolutions
        if item.resolution_type in {ResolutionType.REVIEW, ResolutionType.UNMAPPED}
    ]
    pair_similarity = RecordPairSimilarity(embedder, config.weights.to_dict())
    for item in unresolved:
        cached = resolver.cached_record_vectors(item.record)
        if cached is not None:
            pair_similarity.seed_record_vectors(item.record, cached)
    phase(f"[4/6] 准备新岗位发现：{len(unresolved):,} 条待确认记录...")
    pair_similarity.prepare([item.record for item in unresolved], args.record_batch_size)
    discovery_cfg = config.raw.get("new_role_discovery", {})

    def top_k_candidates(candidate_records):
        """生成并报告有限的 Top-K 候选对，便于观察聚类工作量。"""

        empty_names = sum(
            not str(record.normalized_name or "").strip() for record in candidate_records
        )
        pairs = pair_similarity.candidate_pairs(
            candidate_records,
            top_k=args.discovery_top_k,
            chunk_size=args.ann_chunk_size,
        )
        detail = f"；跳过 {empty_names:,} 条空名称记录" if empty_names else ""
        phase(f"      已召回 {len(pairs):,} 个候选对{detail}。")
        return pairs

    discovery = NewRoleDiscovery(
        pair_similarity,
        min_jds=int(discovery_cfg.get("min_jds", config.thresholds.new_role_min_jds)),
        min_companies=int(discovery_cfg.get("min_companies", config.thresholds.new_role_min_companies)),
        min_templates=int(discovery_cfg.get("min_templates", config.thresholds.new_role_min_templates)),
        min_skills=int(discovery_cfg.get("min_skills", config.thresholds.new_role_min_skills)),
        cluster_similarity=float(discovery_cfg.get("cluster_similarity", config.thresholds.review)),
        max_cluster_size=int(discovery_cfg.get("max_cluster_size", 200)),
        max_pair_comparisons=int(discovery_cfg.get("max_pair_comparisons", 5_000_000)),
        candidate_pair_provider=top_k_candidates,
    )
    phase(
        f"[5/6] Top-{args.discovery_top_k} 候选召回并聚类（不再进行组内全量两两比较）..."
    )
    candidates = discovery.discover(unresolved)
    final_names = assign_final_titles(records, resolutions, candidates)

    output_header = list(header)
    if args.output_column not in output_header:
        output_header.append(args.output_column)

    def output_rows():
        """第二遍流式复制所有原字段，并只设置最终岗位名称列。"""

        for row in iter_csv_rows(args.input):
            source_id = str(row.get(args.id_column) or "").strip()
            row[args.output_column] = final_names[source_id]
            yield row

    phase("[6/6] 写入带“岗位名称”新列的 CSV...")
    write_csv_atomic(args.output, output_rows(), output_header)
    counts = Counter(item.resolution_type.value for item in resolutions)
    return {
        "input": str(args.input),
        "output": str(args.output),
        "rows": len(records),
        "output_column": args.output_column,
        "resolution_counts": dict(sorted(counts.items())),
        "new_role_candidates": sum(item.passed_gate for item in candidates),
        "mode": "CSV_ONLY_NO_GRAPH_WRITE",
    }


def build_parser() -> argparse.ArgumentParser:
    """定义专用脚本参数，默认值与目标 CSV 表头完全一致。"""

    parser = argparse.ArgumentParser(description="为含能力提取结果的 2026 CSV 新增岗位名称列")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--registry", type=Path, default=DEFAULT_REGISTRY)
    parser.add_argument("--role-skills", type=Path, default=DEFAULT_ROLE_SKILLS)
    parser.add_argument("--embedder", choices=("sentence-transformer", "hashing"), default="sentence-transformer")
    parser.add_argument("--model", default="")
    parser.add_argument("--device", default="")
    parser.add_argument("--embedding-batch-size", type=int, default=64)
    parser.add_argument("--record-batch-size", type=int, default=256)
    parser.add_argument("--discovery-top-k", type=int, default=12)
    parser.add_argument("--ann-chunk-size", type=int, default=256)
    parser.add_argument("--hash-dimension", type=int, default=384)
    parser.add_argument("--id-column", default="职位ID")
    parser.add_argument("--title-column", default="原始职位名称")
    parser.add_argument("--jd-column", default="JD全文")
    parser.add_argument("--company-column", default="公司全称")
    parser.add_argument("--search-column", default="岗位关键词")
    parser.add_argument("--ability-column", default="能力提取结果")
    parser.add_argument("--ability-categories", default="技术,知识")
    parser.add_argument("--role-skill-categories", default="技术,知识")
    parser.add_argument("--max-role-skills", type=int, default=20)
    parser.add_argument("--output-column", default="岗位名称")
    parser.add_argument("--replace-column", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """运行专用批处理并输出汇总；异常时不留下半成品 CSV。"""

    args = build_parser().parse_args(argv)
    try:
        result = run(args)
    except Exception as exc:
        print(f"JOB_TITLE_NORMALIZATION_FAILED: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

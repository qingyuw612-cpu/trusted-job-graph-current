"""2026岗位概念归纳、审核队列和全量映射表生成入口。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from concept_standardization import ConceptStandardizationEngine, EngineConfig


PROJECT_DIR = Path(__file__).resolve().parent
REPOSITORY_DIR = PROJECT_DIR.parent
WORKSPACE_DIR = REPOSITORY_DIR.parent


def main() -> int:
    parser = argparse.ArgumentParser(description="生成干净岗位主数据、审核队列和招聘记录映射草案")
    parser.add_argument("--input", type=Path, default=WORKSPACE_DIR / "2026数据51job" / "jobs_2026_it_含能力提取结果_岗位归一化.csv")
    parser.add_argument("--registry", type=Path, default=REPOSITORY_DIR / "trusted_graph_agent" / "it_role_taxonomy.json")
    parser.add_argument("--role-skills", type=Path, default=REPOSITORY_DIR / "output" / "processed_normalization_full" / "skill_reports" / "role_top_skills.csv")
    parser.add_argument("--output-dir", type=Path, default=WORKSPACE_DIR / "2026数据51job" / "岗位概念标准化结果")
    parser.add_argument("--config", type=Path, default=PROJECT_DIR / "config" / "concept_standardization.json")
    args = parser.parse_args()
    raw = json.loads(args.config.read_text(encoding="utf-8"))
    weights, thresholds, retrieval = raw["weights"], raw["thresholds"], raw["retrieval"]
    config = EngineConfig(
        title_weight=float(weights["title"]), skill_weight=float(weights["skill"]),
        review_merge_score=float(thresholds["review_merge_score"]),
        review_subrole_score=float(thresholds["review_subrole_score"]),
        min_new_role_jds=int(thresholds["min_new_role_jds"]),
        min_new_role_companies=int(thresholds["min_new_role_companies"]),
        min_new_role_skills=int(thresholds["min_new_role_skills"]),
        top_k=int(retrieval["top_k_existing_roles"]), sample_jds=int(retrieval["sample_jds_per_candidate"]),
        version=str(raw["version"]),
    )
    engine = ConceptStandardizationEngine(args.registry, args.role_skills, config)
    print(json.dumps(engine.run(args.input, args.output_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

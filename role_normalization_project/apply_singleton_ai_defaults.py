"""对第二轮后剩余单例进行保守AI默认分类；单例不创建新岗位。"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from apply_second_round_ai_defaults import ALIASES, MASTER, MAPPING, RESULTS, decide, normalize_lookup_key, read_csv, write_csv
from concept_standardization.engine import parse_skills

WORKSPACE = Path(__file__).resolve().parents[2]
INPUT = WORKSPACE / "2026数据51job" / "jobs_2026_it_含能力提取结果_岗位归一化.csv"
OUTPUT = RESULTS / "second_round_clustering" / "singleton_ai_decisions.csv"


def main() -> int:
    master_rows = read_csv(MASTER); role_by_name = {row["canonical_name"]: row for row in master_rows}
    alias_index = {normalize_lookup_key(row["source_name"]): row for row in read_csv(ALIASES)}
    mapping_rows = read_csv(MAPPING); pending = {row["职位ID"] for row in mapping_rows if row.get("匹配类型") == "PENDING"}
    source_rows = {row["职位ID"]: row for row in read_csv(INPUT) if row.get("职位ID") in pending}
    decisions, effective = [], {}
    for job_id in sorted(pending):
        row = source_rows.get(job_id, {})
        candidate = str(row.get("岗位名称") or "").strip()
        original = str(row.get("原始职位名称") or "").strip()
        # 单条记录以招聘方原始职位名为最强证据；旧聚类产生的候选名称只作补充。
        item = {"representative_name": original or candidate, "top_names": f"{original}；{candidate}",
                "top_skills": "；".join(parse_skills(str(row.get("能力提取结果") or ""))),
                "search_keywords": str(row.get("搜索关键词") or row.get("岗位关键词") or ""),
                "record_count": 1, "company_count": 1}
        result = decide(item, role_by_name, alias_index)
        # 单例无权创建新岗位；若该概念已由前两轮建立，则映射已有岗位，否则保留观察。
        if result["decision"] == "NEW_ROLE_CANDIDATE":
            existing = role_by_name.get(str(result["canonical_name"]))
            if existing:
                result.update({"decision": "SUBROLE_OF", "role_id": existing["role_id"], "confidence": 0.82,
                               "reason": "单条记录命中前两轮已经建立的AI默认岗位概念。"})
            else:
                result.update({"decision": "INSUFFICIENT_INFO", "role_id": "", "confidence": 0.55,
                               "reason": "单条记录不能独立创建新岗位，保留观察等待后续数据。"})
        output = {"职位ID": job_id, "岗位名称": candidate, "原始职位名称": original,
                  "搜索关键词": item["search_keywords"], **result, "status": "AI_APPROVED_SINGLETON"
                  if result["decision"] != "INSUFFICIENT_INFO" else "OBSERVE"}
        decisions.append(output)
        if result["decision"] != "INSUFFICIENT_INFO": effective[job_id] = output

    by_id = {row["职位ID"]: row for row in mapping_rows}
    for job_id, decision in effective.items():
        row = by_id[job_id]
        row.update({"role_id": decision["role_id"], "标准岗位名称": decision["canonical_name"],
                    "匹配类型": decision["decision"], "置信度": f"{float(decision['confidence']):.4f}",
                    "定义版本": "1", "审核状态": "AI_APPROVED_SINGLETON"})
    fields = ["职位ID", "原始职位名称", "归一化候选名称", "role_id", "标准岗位名称", "匹配类型", "置信度",
              "上级岗位ID", "方向标签", "定义版本", "审核状态"]
    write_csv(MAPPING, mapping_rows, fields)
    decision_fields = ["职位ID", "岗位名称", "原始职位名称", "搜索关键词", "decision", "role_id", "canonical_name",
                       "confidence", "reason", "status"]
    write_csv(OUTPUT, decisions, decision_fields)
    counts: dict[str, int] = {}
    for item in decisions: counts[item["decision"]] = counts.get(item["decision"], 0) + 1
    result = {"input_pending_records": len(pending), "decisions": counts, "resolved_records": len(effective),
              "remaining_pending_records": len(pending) - len(effective), "neo4j_written": False}
    (RESULTS / "second_round_clustering" / "singleton_resolution_manifest.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

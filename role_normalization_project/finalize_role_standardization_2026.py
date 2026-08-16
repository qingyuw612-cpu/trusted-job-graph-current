"""汇总岗位概念标准化结果，不重跑向量模型，也不写入 Neo4j。"""
from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from pathlib import Path


PROJECT = Path(__file__).resolve().parent
WORKSPACE = PROJECT.parents[1]
DATA_DIR = WORKSPACE / "2026数据51job"
RESULTS = DATA_DIR / "岗位概念标准化结果"
SOURCE = DATA_DIR / "jobs_2026_it_含能力提取结果_岗位归一化.csv"
MAPPING = RESULTS / "job_role_mapping_draft.csv"
MASTER = RESULTS / "role_master_draft.csv"

FULL_OUTPUT = RESULTS / "jobs_2026_it_岗位概念标准化全量结果.csv"
CLEAN_OUTPUT = RESULTS / "jobs_2026_it_按标准岗位分类_已归类.csv"
IT_ONLY_OUTPUT = RESULTS / "jobs_2026_it_按标准岗位分类_仅IT岗位.csv"
OBSERVE_OUTPUT = RESULTS / "jobs_2026_it_待观察记录.csv"
SUMMARY_OUTPUT = RESULTS / "标准岗位分类汇总.csv"
MANIFEST_OUTPUT = RESULTS / "final_manifest.json"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write_csv(path: Path, rows: list[dict[str, object]], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    source_rows = read_csv(SOURCE)
    mapping_rows = read_csv(MAPPING)
    master_rows = read_csv(MASTER)
    mapping_by_id = {row.get("职位ID", ""): row for row in mapping_rows}
    role_by_id = {row.get("role_id", ""): row for row in master_rows}

    if len(source_rows) != len(mapping_rows):
        raise ValueError(f"原始记录数 {len(source_rows)} 与映射记录数 {len(mapping_rows)} 不一致")

    output_rows: list[dict[str, object]] = []
    clean_rows: list[dict[str, object]] = []
    it_rows: list[dict[str, object]] = []
    observe_rows: list[dict[str, object]] = []
    decision_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    grouped: dict[tuple[str, str], dict[str, object]] = defaultdict(
        lambda: {"招聘记录数": 0, "企业": set(), "原始名称": Counter(), "搜索词": Counter()}
    )

    standard_fields = [
        "role_id", "标准岗位名称", "匹配类型", "置信度", "上级岗位ID",
        "方向标签", "岗位定义版本", "处理状态", "是否进入正式岗位统计",
    ]
    for source in source_rows:
        job_id = source.get("职位ID", "")
        mapping = mapping_by_id.get(job_id)
        if mapping is None:
            raise ValueError(f"职位ID {job_id} 缺少映射结果")
        decision = mapping.get("匹配类型") or "PENDING"
        resolved = decision != "PENDING" and bool(mapping.get("role_id") or decision == "NON_IT")
        role = role_by_id.get(mapping.get("role_id", ""), {})
        status = "已归类" if resolved else "待观察"
        standard = {
            "role_id": mapping.get("role_id", ""),
            "标准岗位名称": "非IT岗位" if decision == "NON_IT" else mapping.get("标准岗位名称", ""),
            "匹配类型": "INSUFFICIENT_INFO" if decision == "PENDING" else decision,
            "置信度": mapping.get("置信度", ""),
            "上级岗位ID": mapping.get("上级岗位ID") or role.get("parent_role_id", ""),
            "方向标签": mapping.get("方向标签", ""),
            "岗位定义版本": mapping.get("定义版本") or role.get("definition_version", ""),
            "处理状态": status,
            "是否进入正式岗位统计": "是" if resolved and decision != "NON_IT" else "否",
        }
        combined = dict(source)
        # 原表已有“岗位名称”，保留它作为算法阶段的候选名称，标准结论使用新字段。
        combined.update(standard)
        output_rows.append(combined)
        decision_counts[standard["匹配类型"]] += 1
        status_counts[status] += 1
        if resolved:
            clean_rows.append(combined)
        else:
            observe_rows.append(combined)
        if standard["是否进入正式岗位统计"] == "是":
            it_rows.append(combined)
            key = (str(standard["role_id"]), str(standard["标准岗位名称"]))
            group = grouped[key]
            group["招聘记录数"] += 1
            company = source.get("公司全称", "").strip()
            if company:
                group["企业"].add(company)
            original = source.get("原始职位名称", "").strip()
            if original:
                group["原始名称"][original] += 1
            keyword = (source.get("搜索关键词") or source.get("岗位关键词") or "").strip()
            if keyword:
                group["搜索词"][keyword] += 1

    source_fields = list(source_rows[0].keys()) if source_rows else []
    output_fields = source_fields + [field for field in standard_fields if field not in source_fields]
    write_csv(FULL_OUTPUT, output_rows, output_fields)
    clean_rows.sort(key=lambda row: (str(row.get("标准岗位名称", "")), str(row.get("职位ID", ""))))
    write_csv(CLEAN_OUTPUT, clean_rows, output_fields)
    it_rows.sort(key=lambda row: (str(row.get("标准岗位名称", "")), str(row.get("职位ID", ""))))
    write_csv(IT_ONLY_OUTPUT, it_rows, output_fields)
    write_csv(OBSERVE_OUTPUT, observe_rows, output_fields)

    summary_rows = []
    for (role_id, name), values in grouped.items():
        top_names = "；".join(name for name, _ in values["原始名称"].most_common(8))
        top_keywords = "；".join(name for name, _ in values["搜索词"].most_common(5))
        summary_rows.append({
            "role_id": role_id,
            "标准岗位名称": name,
            "招聘记录数": values["招聘记录数"],
            "企业数": len(values["企业"]),
            "常见原始名称": top_names,
            "主要搜索词": top_keywords,
        })
    summary_rows.sort(key=lambda row: (-int(row["招聘记录数"]), str(row["标准岗位名称"])))
    write_csv(SUMMARY_OUTPUT, summary_rows, [
        "role_id", "标准岗位名称", "招聘记录数", "企业数", "常见原始名称", "主要搜索词"
    ])

    manifest = {
        "format_version": "2.0.0",
        "input_rows": len(source_rows),
        "resolved_rows": status_counts["已归类"],
        "observation_rows": status_counts["待观察"],
        "it_role_rows": len(it_rows),
        "non_it_rows": decision_counts["NON_IT"],
        "coverage": round(status_counts["已归类"] / len(source_rows), 6) if source_rows else 0,
        "role_master_count": len(master_rows),
        "roles_with_records": len(summary_rows),
        "decision_counts": dict(decision_counts),
        "ai_decisions_default_effective": True,
        "manual_override_supported": True,
        "neo4j_written": False,
        "outputs": {
            "full": str(FULL_OUTPUT),
            "classified": str(CLEAN_OUTPUT),
            "it_only": str(IT_ONLY_OUTPUT),
            "observation": str(OBSERVE_OUTPUT),
            "summary": str(SUMMARY_OUTPUT),
        },
    }
    MANIFEST_OUTPUT.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

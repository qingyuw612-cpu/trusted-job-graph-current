from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from .models import GraphBundle


def _unique_check(name: str, rows: list[dict], key: str) -> dict:
    values = [row.get(key, "") for row in rows]
    missing = sum(not value for value in values)
    duplicates = len(values) - len(set(values))
    return {
        "name": name,
        "status": "PASS" if missing == 0 and duplicates == 0 else "FAIL",
        "rows": len(rows),
        "missing_ids": missing,
        "duplicate_ids": duplicates,
    }


def validate_and_write(bundle: GraphBundle, output_dir: Path) -> dict:
    checks = [
        _unique_check("岗位族主键唯一", bundle.role_families, "family_id"),
        _unique_check("岗位别名主键唯一", bundle.role_aliases, "alias_id"),
        _unique_check("岗位层级关系主键唯一", bundle.role_relations, "relation_id"),
        _unique_check("岗位主键唯一", bundle.roles, "role_id"),
        _unique_check("岗位画像主键唯一", bundle.role_profiles, "profile_id"),
        _unique_check("技能主键唯一", bundle.skills, "skill_id"),
        _unique_check("时间窗口主键唯一", bundle.time_windows, "window_id"),
        _unique_check("JD 主键唯一", bundle.jds, "jd_id"),
        _unique_check("岗位技能关系主键唯一", bundle.role_skill_edges, "edge_id"),
        _unique_check("岗位技能快照主键唯一", bundle.role_skill_snapshots, "snapshot_id"),
        _unique_check("JD 技能证据主键唯一", bundle.jd_skill_edges, "edge_id"),
    ]
    profile_ids = {row["profile_id"] for row in bundle.role_profiles}
    skill_ids = {row["skill_id"] for row in bundle.skills}
    jd_ids = {row["jd_id"] for row in bundle.jds}
    invalid_role_edges = sum(
        row.get("profile_id") not in profile_ids or row.get("skill_id") not in skill_ids
        for row in bundle.role_skill_edges
    )
    checks.append(
        {
            "name": "岗位技能关系引用完整",
            "status": "PASS" if invalid_role_edges == 0 else "FAIL",
            "invalid_references": invalid_role_edges,
        }
    )
    invalid_jd_edges = sum(
        row.get("jd_id") not in jd_ids
        or (not str(row.get("skill_id", "")).startswith("skill_unknown:") and row.get("skill_id") not in skill_ids)
        for row in bundle.jd_skill_edges
    )
    checks.append(
        {
            "name": "证据关系引用完整",
            "status": "PASS" if invalid_jd_edges == 0 else "FAIL",
            "invalid_references": invalid_jd_edges,
        }
    )
    verified_without_quote = sum(
        row.get("evidence_status") == "VERIFIED" and not row.get("evidence_quote")
        for row in bundle.jd_skill_edges
    )
    checks.append(
        {
            "name": "已验证技能均保留原文证据",
            "status": "PASS" if verified_without_quote == 0 else "FAIL",
            "missing_quotes": verified_without_quote,
        }
    )
    neo4j_dir = output_dir / "neo4j"
    expected_files = {
        "role_families.csv", "role_aliases.csv", "role_relations.csv", "role_skill_snapshots.csv",
        "roles.csv", "role_profiles.csv", "skills.csv", "industries.csv", "levels.csv", "companies.csv",
        "time_windows.csv", "jds.csv", "role_skill_edges.csv", "jd_skill_edges.csv", "skill_related_edges.csv",
        "evolution_edges.csv", "constraints.cypher", "import.cypher",
    }
    missing_files = sorted(name for name in expected_files if not (neo4j_dir / name).exists())
    checks.append(
        {
            "name": "Neo4j 分阶段导入文件齐全",
            "status": "PASS" if not missing_files else "FAIL",
            "missing_files": missing_files,
        }
    )

    statuses = Counter(row.get("evidence_status", "UNKNOWN") for row in bundle.jd_skill_edges)
    verified = statuses.get("VERIFIED", 0)
    rejected = sum(amount for status, amount in statuses.items() if status.startswith("REJECTED"))
    total = sum(statuses.values())
    failed = [check for check in checks if check["status"] == "FAIL"]
    report = {
        "status": "PASS" if not failed else "FAIL",
        "checks_passed": len(checks) - len(failed),
        "checks_total": len(checks),
        "checks": checks,
        "quality_metrics": {
            "verified_evidence_rate": round(verified / total, 6) if total else 0.0,
            "hallucination_or_unknown_blocked": rejected,
            "review_task_count": len(bundle.review_tasks),
            "duplicate_rate": round(
                sum(bool(row.get("duplicate_of")) for row in bundle.jds) / len(bundle.jds), 6
            ) if bundle.jds else 0.0,
            "template_downweighted_rate": round(
                sum(bool(row.get("template_cluster_id")) for row in bundle.jds) / len(bundle.jds), 6
            ) if bundle.jds else 0.0,
            "required_edge_count": sum(row.get("tier") == "required" for row in bundle.role_skill_edges),
            "common_edge_count": sum(row.get("tier") == "common" for row in bundle.role_skill_edges),
            "emerging_edge_count": sum(row.get("tier") == "emerging" for row in bundle.role_skill_edges),
            "bonus_edge_count": sum(row.get("tier") == "bonus" for row in bundle.role_skill_edges),
        },
    }
    (output_dir / "validation_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown = [
        "# 图谱构建自动验收报告",
        "",
        f"- 总体状态：**{report['status']}**",
        f"- 通过检查：**{report['checks_passed']} / {report['checks_total']}**",
        f"- 已验证证据率：**{report['quality_metrics']['verified_evidence_rate']:.1%}**",
        f"- 拦截幻觉/未知技能候选：**{rejected}**",
        f"- 待人工审核：**{len(bundle.review_tasks)}**",
        "",
        "## 检查项",
        "",
        *[f"- [{'x' if item['status'] == 'PASS' else ' '}] {item['name']}：{item['status']}" for item in checks],
        "",
        "## 关系分层",
        "",
        f"- 必备：{report['quality_metrics']['required_edge_count']}",
        f"- 常见：{report['quality_metrics']['common_edge_count']}",
        f"- 新兴：{report['quality_metrics']['emerging_edge_count']}",
        f"- 加分：{report['quality_metrics']['bonus_edge_count']}",
    ]
    (output_dir / "validation_report.md").write_text("\n".join(markdown), encoding="utf-8")
    return report

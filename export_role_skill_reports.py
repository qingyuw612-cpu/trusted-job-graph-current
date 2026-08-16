from __future__ import annotations

import argparse
import csv
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

from trusted_graph_agent.text_utils import stable_id


BASE_DIR = Path(__file__).resolve().parent


def safe_filename(value: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", value).strip().rstrip(".") or "未命名岗位"


def write_csv(path: Path, rows: list[dict], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description="将标准化结果导出为逐岗位报告与Neo4j数据包")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=BASE_DIR / "output" / "all_it_roles_sample",
    )
    args = parser.parse_args()
    output_dir = args.output_dir.resolve()
    report_dir = output_dir / "skill_reports"
    source = report_dir / "role_top_skills.csv"
    with source.open("r", encoding="utf-8-sig", newline="") as file:
        rows = list(csv.DictReader(file))

    by_role: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        by_role[row["role"]].append(row)

    display_fields = [
        "岗位",
        "能力类别",
        "标准技能点",
        "综合得分",
        "覆盖公司数",
        "覆盖JD数",
        "原文验证JD数",
        "概念状态",
    ]
    summary_rows = []
    markdown = [
        "# 新一代信息技术岗位标准技能报告",
        "",
        f"- 岗位数量：**{len(by_role)}**",
        f"- 入选核心技能点：**{len(rows)}**",
        "- 数据范围：每个信息技术CSV抽取80条代表性JD。",
        "- 说明：这里只展示通过公司数、JD数、证据和时间权重筛选后的核心技能。",
        "",
    ]
    for role in sorted(by_role):
        role_rows = sorted(
            by_role[role],
            key=lambda row: (row["category"], int(row["mmr_rank"])),
        )
        exported = []
        markdown.extend([f"## {role}", "", "| 类别 | 标准技能点 | 得分 | 公司数 | JD数 |", "|---|---|---:|---:|---:|"])
        for row in role_rows:
            item = {
                "岗位": role,
                "能力类别": row["category"],
                "标准技能点": row["canonical_name"],
                "综合得分": row["final_score"],
                "覆盖公司数": row["company_count"],
                "覆盖JD数": row["jd_count"],
                "原文验证JD数": row["verified_jd_count"],
                "概念状态": row["concept_status"],
            }
            exported.append(item)
            summary_rows.append(item)
            markdown.append(
                f"| {row['category']} | {row['canonical_name']} | {float(row['final_score']):.3f} | "
                f"{row['company_count']} | {row['jd_count']} |"
            )
        markdown.append("")
        write_csv(report_dir / "by_role" / f"{safe_filename(role)}.csv", exported, display_fields)

    write_csv(report_dir / "全岗位核心技能汇总.csv", summary_rows, display_fields)
    (report_dir / "全岗位核心技能报告.md").write_text("\n".join(markdown), encoding="utf-8")

    neo4j_dir = output_dir / "neo4j_normalized"
    role_rows = [
        {"role_id": stable_id("role", row["role"]), "role_name": row["role"]}
        for row in ({"role": role} for role in sorted(by_role))
    ]
    skill_by_id = {}
    edge_rows = []
    for row in rows:
        skill_by_id.setdefault(
            row["concept_id"],
            {
                "concept_id": row["concept_id"],
                "canonical_name": row["canonical_name"],
                "category": row["category"],
                "concept_status": row["concept_status"],
            },
        )
        edge_rows.append(
            {
                "role_id": stable_id("role", row["role"]),
                "concept_id": row["concept_id"],
                "final_score": row["final_score"],
                "company_count": row["company_count"],
                "jd_count": row["jd_count"],
                "verified_jd_count": row["verified_jd_count"],
                "rank": row["mmr_rank"],
            }
        )
    write_csv(neo4j_dir / "normalized_roles.csv", role_rows, ["role_id", "role_name"])
    write_csv(
        neo4j_dir / "normalized_skills.csv",
        list(skill_by_id.values()),
        ["concept_id", "canonical_name", "category", "concept_status"],
    )
    write_csv(
        neo4j_dir / "normalized_role_skills.csv",
        edge_rows,
        [
            "role_id", "concept_id", "final_score", "company_count", "jd_count",
            "verified_jd_count", "rank",
        ],
    )
    snapshot_rows = []
    evidence_rows = []
    database = output_dir / "knowledge_graph.db"
    if database.exists():
        with sqlite3.connect(database) as connection:
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'normalized_role_skill_snapshots'"
            ).fetchone():
                snapshot_rows = [
                    {
                        "role_id": row[0],
                        "concept_id": row[1],
                        "time_window": row[2],
                        "window_start": row[3],
                        "final_score": row[4],
                        "company_count": row[5],
                        "jd_count": row[6],
                        "verified_jd_count": row[7],
                        "rank": row[8],
                        "trend": row[9],
                        "delta": row[10],
                    }
                    for row in connection.execute(
                        "SELECT * FROM normalized_role_skill_snapshots ORDER BY role_id, time_window, rank"
                    )
                ]
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'normalized_evidence_map'"
            ).fetchone():
                evidence_rows = [
                    {
                        "jd_id": row[0],
                        "concept_id": row[1],
                        "original_skill_id": row[2],
                        "raw_term": row[3],
                        "requirement_type": row[4],
                        "evidence_quote": row[5],
                        "evidence_status": row[6],
                        "confidence": row[7],
                        "source": row[8],
                    }
                    for row in connection.execute(
                        """
                        SELECT m.jd_id, m.concept_id, m.original_skill_id,
                               e.raw_term, e.requirement_type, e.evidence_quote,
                               e.evidence_status, e.confidence, e.source
                        FROM normalized_evidence_map m
                        JOIN jd_skill_edges e
                          ON e.jd_id = m.jd_id AND e.skill_id = m.original_skill_id
                        ORDER BY m.jd_id, m.concept_id, m.original_skill_id
                        """
                    )
                ]
    write_csv(
        neo4j_dir / "normalized_role_skill_snapshots.csv",
        snapshot_rows,
        [
            "role_id", "concept_id", "time_window", "window_start", "final_score",
            "company_count", "jd_count", "verified_jd_count", "rank", "trend", "delta",
        ],
    )
    write_csv(
        neo4j_dir / "normalized_skill_evidence.csv",
        evidence_rows,
        [
            "jd_id", "concept_id", "original_skill_id", "raw_term", "requirement_type",
            "evidence_quote", "evidence_status", "confidence", "source",
        ],
    )
    (neo4j_dir / "import_normalized.cypher").write_text(
        """CREATE CONSTRAINT normalized_skill_id IF NOT EXISTS
FOR (s:NormalizedSkill) REQUIRE s.concept_id IS UNIQUE;

MATCH (s:NormalizedSkill) DETACH DELETE s;

LOAD CSV WITH HEADERS FROM 'file:///normalized_roles.csv' AS row
MERGE (r:Role {role_id: row.role_id})
SET r.name = row.role_name, r.role_name = row.role_name;

LOAD CSV WITH HEADERS FROM 'file:///normalized_skills.csv' AS row
MERGE (s:NormalizedSkill {concept_id: row.concept_id})
SET s.canonical_name = row.canonical_name,
    s.category = row.category,
    s.concept_status = row.concept_status;

LOAD CSV WITH HEADERS FROM 'file:///normalized_role_skills.csv' AS row
MATCH (r:Role {role_id: row.role_id})
MATCH (s:NormalizedSkill {concept_id: row.concept_id})
MERGE (r)-[e:HAS_CORE_SKILL]->(s)
SET e.final_score = toFloat(row.final_score),
    e.company_count = toInteger(row.company_count),
    e.jd_count = toInteger(row.jd_count),
    e.verified_jd_count = toInteger(row.verified_jd_count),
    e.rank = toInteger(row.rank);

LOAD CSV WITH HEADERS FROM 'file:///normalized_role_skill_snapshots.csv' AS row
MATCH (r:Role {role_id: row.role_id})
MATCH (s:NormalizedSkill {concept_id: row.concept_id})
MERGE (r)-[e:HAS_SKILL_SNAPSHOT {time_window: row.time_window}]->(s)
SET e.window_start = row.window_start,
    e.final_score = toFloat(row.final_score),
    e.company_count = toInteger(row.company_count),
    e.jd_count = toInteger(row.jd_count),
    e.verified_jd_count = toInteger(row.verified_jd_count),
    e.rank = toInteger(row.rank),
    e.trend = row.trend,
    e.delta = toFloat(row.delta);

LOAD CSV WITH HEADERS FROM 'file:///normalized_skill_evidence.csv' AS row
MATCH (j:JD {jd_id: row.jd_id})
MATCH (s:NormalizedSkill {concept_id: row.concept_id})
MERGE (j)-[e:MENTIONS_NORMALIZED_SKILL {original_skill_id: row.original_skill_id}]->(s)
SET e.raw_term = row.raw_term,
    e.requirement_type = row.requirement_type,
    e.evidence_quote = row.evidence_quote,
    e.evidence_status = row.evidence_status,
    e.confidence = toFloat(row.confidence),
    e.source = row.source;
""",
        encoding="utf-8",
    )
    print(f"岗位报告：{report_dir / '全岗位核心技能报告.md'}")
    print(f"岗位CSV目录：{report_dir / 'by_role'}")
    print(f"Neo4j归一化数据：{neo4j_dir}")


if __name__ == "__main__":
    main()

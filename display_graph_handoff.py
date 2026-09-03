"""Export, import, and verify the small Neo4j graph used by the panorama UI.

The handoff deliberately excludes raw JD text, companies, processing reviews,
ability candidates, and all ingestion history. The generated JSON file is a
private team artifact and is ignored by Git.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "neo4j_connection.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "output" / "team_handoff"
PAGE_PATH = PROJECT_ROOT / "trusted_graph_agent" / "static" / "panorama.html"
FORMAT_VERSION = 1
PAGE_SIZE = 10_000


def stable_display_id(prefix: str, value: str) -> str:
    return f"{prefix}:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"


def quarter_from_date(value: str) -> str:
    parts = str(value or "").replace("-", "/").split("/")
    if len(parts) < 2:
        return ""
    try:
        year, month = int(parts[0]), int(parts[1])
    except ValueError:
        return ""
    if not 1 <= month <= 12:
        return ""
    return f"{year}Q{(month - 1) // 3 + 1}"


def quarter_start(value: str) -> str:
    quarter = quarter_from_date(value)
    if not quarter:
        return ""
    year, q = quarter.split("Q")
    return f"{year}-{(int(q) - 1) * 3 + 1:02d}-01"


def published_quarter_aggregates(
    client: Any,
    run_id: str,
    existing_windows: set[str],
    role_ids: set[str],
) -> tuple[list[dict], list[dict], list[dict], list[dict], dict[str, dict]]:
    """Build display-only quarterly aggregates from already processed JDs.

    This intentionally returns counts and normalized skill references only.  It
    never exports JD text, companies, evidence quotes, or processing nodes.
    Existing handoff quarters remain the authoritative historical snapshots;
    only quarters not already represented in RoleProfile are materialized here.
    """
    raw_rows = query_pages(
        client,
        """
        MATCH (raw:RawJDVersion)-[:HAS_PROCESSING_RESULT]->(processed:ProcessedJD {status:'COMPLETED'})
        WHERE raw.standard_role_id IS NOT NULL
        WITH raw.standard_role_id AS role_id, raw.publish_time_raw AS posted_at,
             raw.company_id AS company_id, raw.version_id AS version_id
        RETURN role_id, posted_at, company_id, version_id
        """,
    )
    totals: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(
        lambda: {"jds": set(), "companies": set()}
    )
    raw_quarters: set[str] = set()
    for row in raw_rows:
        role_id = str(row.get("role_id") or "")
        quarter = quarter_from_date(str(row.get("posted_at") or ""))
        if role_id not in role_ids or not quarter or quarter in existing_windows:
            continue
        raw_quarters.add(quarter)
        totals[(role_id, quarter)]["jds"].add(str(row.get("version_id") or ""))
        company = str(row.get("company_id") or "")
        if company:
            totals[(role_id, quarter)]["companies"].add(company)

    if not raw_quarters:
        return [], [], [], [], {}

    skill_rows = query_pages(
        client,
        """
        MATCH (raw:RawJDVersion)-[:HAS_PROCESSING_RESULT]->
              (processed:ProcessedJD {status:'COMPLETED'})
              -[mention:HAS_ABILITY]->(:AbilityCandidate)
              -[:NORMALIZES_TO {run_id:$run_id}]->(skill:NormalizedSkill)
        WHERE raw.standard_role_id IS NOT NULL
        WITH raw.standard_role_id AS role_id, raw.publish_time_raw AS posted_at,
             raw.version_id AS version_id, raw.company_id AS company_id, skill, count(mention) AS mentions
        RETURN role_id, posted_at, version_id, company_id, skill.concept_id AS concept_id,
               skill.canonical_name AS canonical_name, skill.category AS category,
               sum(mentions) AS evidence_count
        """,
        {"run_id": run_id},
    )
    grouped: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)
    for row in skill_rows:
        role_id = str(row.get("role_id") or "")
        quarter = quarter_from_date(str(row.get("posted_at") or ""))
        concept_id = str(row.get("concept_id") or "")
        if role_id not in role_ids or quarter not in raw_quarters or not concept_id:
            continue
        key = (role_id, quarter)
        item = grouped[key].setdefault(
            concept_id,
            {
                "concept_id": concept_id,
                "canonical_name": row.get("canonical_name") or "",
                "category": row.get("category") or "",
                "jds": set(),
                "companies": set(),
                "evidence_count": 0,
            },
        )
        item["jds"].add(str(row.get("version_id") or ""))
        company = str(row.get("company_id") or "")
        if company:
            item["companies"].add(company)
        item["evidence_count"] += int(row.get("evidence_count") or 0)

    profiles: list[dict] = []
    profile_edges: list[dict] = []
    windows: list[dict] = []
    window_edges: list[dict] = []
    snapshots: list[dict] = []
    quarter_stats: dict[str, dict] = {}
    previous_support: dict[tuple[str, str], float] = {}
    ordered_quarters = sorted(raw_quarters)
    for quarter in ordered_quarters:
        windows.append(
            {
                "properties": {
                    "window_id": stable_display_id("window", quarter),
                    "name": quarter,
                    "window_start": quarter_start(quarter),
                    "display_aggregate": True,
                    "source_run_id": run_id,
                }
            }
        )
        roles_in_quarter = 0
        jds_in_quarter = 0
        skills_in_quarter = 0
        for (role_id, item_quarter), role_skills in sorted(grouped.items()):
            if item_quarter != quarter:
                continue
            totals_item = totals[(role_id, quarter)]
            jd_count = len(totals_item["jds"])
            if not jd_count:
                continue
            company_count = len(totals_item["companies"])
            profile_id = stable_display_id("profile", f"{role_id}|{quarter}|display")
            profiles.append(
                {
                    "properties": {
                        "profile_id": profile_id,
                        "role_id": role_id,
                        "time_window": quarter,
                        "window_start": quarter_start(quarter),
                        "industry_id": "all",
                        "industry_name": "全行业",
                        "level_id": "all",
                        "level_name": "全级别",
                        "jd_count": jd_count,
                        "company_count": company_count,
                        "display_aggregate": True,
                        "source_run_id": run_id,
                    }
                }
            )
            profile_edges.append(
                {
                    "source_id": role_id,
                    "target_id": profile_id,
                    "properties": {"display_aggregate": True, "source_run_id": run_id},
                }
            )
            window_edges.append(
                {
                    "source_id": profile_id,
                    "target_id": stable_display_id("window", quarter),
                    "properties": {"display_aggregate": True, "source_run_id": run_id},
                }
            )
            ranked = sorted(
                role_skills.values(),
                key=lambda item: (-len(item["jds"]) / jd_count, -item["evidence_count"], item["canonical_name"]),
            )
            roles_in_quarter += 1
            jds_in_quarter += jd_count
            skills_in_quarter += len(ranked)
            for rank, item in enumerate(ranked, 1):
                support = len(item["jds"]) / jd_count
                previous = previous_support.get((role_id, item["concept_id"]), 0.0)
                delta = support - previous
                trend = "rising" if delta >= 0.05 else "falling" if delta <= -0.05 else "STABLE"
                snapshots.append(
                    {
                        "source_id": role_id,
                        "target_id": item["concept_id"],
                        "properties": {
                            "time_window": quarter,
                            "window_start": quarter_start(quarter),
                            "final_score": round(support, 8),
                            "jd_count": len(item["jds"]),
                            "company_count": len(item["companies"]),
                            "verified_jd_count": item["evidence_count"],
                            "rank": rank,
                            "trend": trend,
                            "delta": round(delta, 8),
                            "generated_display": True,
                            "source_run_id": run_id,
                        },
                    }
                )
                previous_support[(role_id, item["concept_id"])] = support
        quarter_stats[quarter] = {
            "jds": jds_in_quarter,
            "roles": roles_in_quarter,
            "skills": skills_in_quarter,
            "snapshots": sum(1 for row in snapshots if row["properties"]["time_window"] == quarter),
        }
    # The display package has one relationship stream for each relation type.
    return profiles, windows, profile_edges, window_edges, {
        "snapshots": snapshots,
        "quarter_stats": quarter_stats,
    }


def repository(config_path: Path):
    from trusted_graph_agent.neo4j_repository import Neo4jGraphRepository

    return Neo4jGraphRepository(config_path).client


def query_pages(client: Any, statement: str, parameters: dict | None = None) -> list[dict]:
    rows: list[dict] = []
    skip = 0
    while True:
        page = client.query(
            statement + " SKIP $skip LIMIT $page_size",
            {**(parameters or {}), "skip": skip, "page_size": PAGE_SIZE},
        )
        rows.extend(page)
        if len(page) < PAGE_SIZE:
            return rows
        skip += len(page)


def active_run_id(client: Any) -> str:
    rows = client.query(
        """
        MATCH (:NormalizationPointer {name:'core'})-[:ACTIVE]->(run:NormalizationRun)
        RETURN run.run_id AS run_id
        """
    )
    run_id = str(rows[0].get("run_id") or "") if rows else ""
    if not run_id:
        raise ValueError("Neo4j 没有活动归一化版本，无法导出展示层。")
    return run_id


def export_payload(client: Any) -> dict:
    run_id = active_run_id(client)
    nodes = {
        "normalization_runs": query_pages(
            client,
            """
            MATCH (n:NormalizationRun {run_id:$run_id})
            RETURN {
                run_id:n.run_id,
                status:n.status,
                created_at:n.created_at,
                activated_at:n.activated_at,
                expected_roles:n.expected_roles,
                expected_core_edges:n.expected_core_edges,
                expected_concepts:n.expected_concepts
            } AS properties
            """,
            {"run_id": run_id},
        ),
        "roles": query_pages(
            client,
            """
            MATCH (n:Role)-[:HAS_CORE_SKILL {run_id:$run_id}]->(:NormalizedSkill)
            RETURN DISTINCT properties(n) AS properties
            """,
            {"run_id": run_id},
        ),
        "role_families": query_pages(
            client,
            """
            MATCH (n:RoleFamily)-[:HAS_ROLE]->(role:Role)
            WHERE EXISTS { MATCH (role)-[:HAS_CORE_SKILL {run_id:$run_id}]->() }
            RETURN DISTINCT properties(n) AS properties
            """,
            {"run_id": run_id},
        ),
        "normalized_skills": query_pages(
            client,
            """
            MATCH (:AbilityCandidate)-[:NORMALIZES_TO {run_id:$run_id}]->(n:NormalizedSkill)
            RETURN DISTINCT properties(n) AS properties
            """,
            {"run_id": run_id},
        ),
        "role_profiles": query_pages(
            client,
            """
            MATCH (role:Role)-[:HAS_PROFILE]->(n:RoleProfile)
            WHERE EXISTS { MATCH (role)-[:HAS_CORE_SKILL {run_id:$run_id}]->() }
            RETURN DISTINCT properties(n) AS properties
            """,
            {"run_id": run_id},
        ),
        "industries": query_pages(
            client,
            """
            MATCH (profile:RoleProfile)-[:IN_INDUSTRY]->(n:Industry)
            WHERE EXISTS { MATCH (:Role)-[:HAS_PROFILE]->(profile) }
            RETURN DISTINCT properties(n) AS properties
            """,
        ),
        "levels": query_pages(
            client,
            "MATCH (:RoleProfile)-[:AT_LEVEL]->(n:Level) RETURN DISTINCT properties(n) AS properties",
        ),
        "time_windows": query_pages(
            client,
            "MATCH (:RoleProfile)-[:IN_WINDOW]->(n:TimeWindow) RETURN DISTINCT properties(n) AS properties",
        ),
        "role_aliases": query_pages(
            client,
            """
            MATCH (n:RoleAlias)-[:ALIAS_OF]->(role:Role)
            WHERE EXISTS { MATCH (role)-[:HAS_CORE_SKILL {run_id:$run_id}]->() }
            RETURN DISTINCT properties(n) AS properties
            """,
            {"run_id": run_id},
        ),
    }
    relationships = {
        "has_role": query_pages(
            client,
            """
            MATCH (family:RoleFamily)-[rel:HAS_ROLE]->(role:Role)
            WHERE EXISTS { MATCH (role)-[:HAS_CORE_SKILL {run_id:$run_id}]->() }
            RETURN family.family_id AS source_id, role.role_id AS target_id,
                   properties(rel) AS properties
            """,
            {"run_id": run_id},
        ),
        "alias_of": query_pages(
            client,
            """
            MATCH (alias:RoleAlias)-[rel:ALIAS_OF]->(role:Role)
            WHERE EXISTS { MATCH (role)-[:HAS_CORE_SKILL {run_id:$run_id}]->() }
            RETURN alias.alias_id AS source_id, role.role_id AS target_id,
                   properties(rel) AS properties
            """,
            {"run_id": run_id},
        ),
        "has_profile": query_pages(
            client,
            """
            MATCH (role:Role)-[rel:HAS_PROFILE]->(profile:RoleProfile)
            WHERE EXISTS { MATCH (role)-[:HAS_CORE_SKILL {run_id:$run_id}]->() }
            RETURN role.role_id AS source_id, profile.profile_id AS target_id,
                   properties(rel) AS properties
            """,
            {"run_id": run_id},
        ),
        "in_industry": query_pages(
            client,
            """
            MATCH (profile:RoleProfile)-[rel:IN_INDUSTRY]->(industry:Industry)
            WHERE EXISTS {
                MATCH (role:Role)-[:HAS_PROFILE]->(profile)
                WHERE EXISTS { MATCH (role)-[:HAS_CORE_SKILL {run_id:$run_id}]->() }
            }
            RETURN profile.profile_id AS source_id, industry.industry_id AS target_id,
                   properties(rel) AS properties
            """,
            {"run_id": run_id},
        ),
        "at_level": query_pages(
            client,
            """
            MATCH (profile:RoleProfile)-[rel:AT_LEVEL]->(level:Level)
            WHERE EXISTS {
                MATCH (role:Role)-[:HAS_PROFILE]->(profile)
                WHERE EXISTS { MATCH (role)-[:HAS_CORE_SKILL {run_id:$run_id}]->() }
            }
            RETURN profile.profile_id AS source_id, level.level_id AS target_id,
                   properties(rel) AS properties
            """,
            {"run_id": run_id},
        ),
        "in_window": query_pages(
            client,
            """
            MATCH (profile:RoleProfile)-[rel:IN_WINDOW]->(window:TimeWindow)
            WHERE EXISTS {
                MATCH (role:Role)-[:HAS_PROFILE]->(profile)
                WHERE EXISTS { MATCH (role)-[:HAS_CORE_SKILL {run_id:$run_id}]->() }
            }
            RETURN profile.profile_id AS source_id, window.window_id AS target_id,
                   properties(rel) AS properties
            """,
            {"run_id": run_id},
        ),
        "has_core_skill": query_pages(
            client,
            """
            MATCH (role:Role)-[rel:HAS_CORE_SKILL {run_id:$run_id}]->(skill:NormalizedSkill)
            RETURN role.role_id AS source_id, skill.concept_id AS target_id,
                   properties(rel) AS properties
            """,
            {"run_id": run_id},
        ),
        "has_skill_snapshot": query_pages(
            client,
            """
            MATCH (role:Role)-[rel:HAS_SKILL_SNAPSHOT]->(skill:NormalizedSkill)
            WHERE EXISTS { MATCH (role)-[:HAS_CORE_SKILL {run_id:$run_id}]->() }
              AND EXISTS { MATCH (:Role)-[:HAS_CORE_SKILL {run_id:$run_id}]->(skill) }
            RETURN role.role_id AS source_id, skill.concept_id AS target_id,
                   properties(rel) AS properties
            """,
            {"run_id": run_id},
        ),
    }
    generated_profiles, generated_windows, generated_profile_edges, generated_window_edges, generated_meta = (
        published_quarter_aggregates(
            client,
            run_id,
            {
                str(row.get("properties", {}).get("name") or row.get("properties", {}).get("time_window") or "")
                for row in nodes["time_windows"]
            },
            {str(row.get("properties", {}).get("role_id") or "") for row in nodes["roles"]},
        )
    )
    nodes["role_profiles"].extend(generated_profiles)
    nodes["time_windows"].extend(generated_windows)
    relationships["has_profile"].extend(generated_profile_edges)
    relationships["in_window"].extend(generated_window_edges)
    relationships["has_skill_snapshot"].extend(generated_meta["snapshots"])

    data_windows = sorted(
        {
            str(row.get("properties", {}).get("name") or row.get("properties", {}).get("time_window") or "")
            for row in nodes["time_windows"]
            if row.get("properties", {}).get("name") or row.get("properties", {}).get("time_window")
        }
    )
    counts = {
        **{name: len(rows) for name, rows in nodes.items()},
        **{name: len(rows) for name, rows in relationships.items()},
    }
    return {
        "format": "trusted-job-graph-display",
        "format_version": FORMAT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "active_run_id": run_id,
        "source_activity": {"normalization_run_id": run_id},
        "release_id": stable_display_id(
            "display",
            run_id + "|" + ",".join(data_windows)
            + "|" + str(len(nodes["normalized_skills"]))
            + "|" + str(len(relationships["has_skill_snapshot"])),
        ),
        "data_window": {
            "quarters": data_windows,
            "min": data_windows[0] if data_windows else "",
            "max": data_windows[-1] if data_windows else "",
        },
        "quarter_stats": generated_meta["quarter_stats"],
        "privacy": {
            "contains_raw_jd": False,
            "contains_company": False,
            "contains_processing_intermediates": False,
            "contains_evidence_quotes": False,
        },
        "counts": counts,
        "nodes": nodes,
        "relationships": relationships,
    }


def write_package(payload: dict, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    data_path = output_dir / "display_graph.json"
    data_path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    digest = hashlib.sha256(data_path.read_bytes()).hexdigest()
    manifest = {
        "format": payload["format"],
        "format_version": payload["format_version"],
        "exported_at": payload["exported_at"],
        "generated_at": payload["exported_at"],
        "active_run_id": payload["active_run_id"],
        "source_activity": payload.get("source_activity", {}),
        "release_id": payload.get("release_id", ""),
        "data_window": payload.get("data_window", {}),
        "quarter_stats": payload.get("quarter_stats", {}),
        "sha256": digest,
        "counts": payload["counts"],
        "privacy": payload["privacy"],
    }
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    instructions = output_dir / "TEAM_HANDOFF.txt"
    instructions.write_text(
        "可信岗位图谱前端展示数据包\n\n"
        "1. 克隆 GitHub 仓库：https://github.com/qingyuw612-cpu/trusted-job-graph\n"
        "2. 将本压缩包解压到仓库任意位置。\n"
        "3. 新建一个空 Neo4j 数据库，建议命名 trusted-job-graph-demo。\n"
        "4. 复制 config/neo4j_connection.example.json 为 config/neo4j_connection.json，"
        "填写该空数据库的连接信息。\n"
        "5. 在仓库根目录执行：\n"
        "   python display_graph_handoff.py import --package <display_graph.json路径> "
        "--neo4j-config config/neo4j_connection.json\n"
        "6. 启动前端：\n"
        "   python display_graph_handoff.py serve --neo4j-config config/neo4j_connection.json\n"
        "7. 浏览器打开 http://127.0.0.1:8010/\n\n"
        "注意：必须导入空数据库；该包不含原始JD、公司、处理中间数据和证据原文。\n",
        encoding="utf-8",
    )
    archive = output_dir / "trusted-job-graph-display.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for path in (data_path, manifest_path, instructions):
            bundle.write(path, path.name)
    return archive


def chunks(rows: list[dict], size: int = 1000) -> Iterable[list[dict]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def merge_rows(client: Any, statement: str, rows: list[dict]) -> None:
    for batch in chunks(rows):
        client.query(statement, {"rows": batch}, access_mode="Write")


NODE_IMPORTS = {
    "normalization_runs": "UNWIND $rows AS row MERGE (n:NormalizationRun {run_id:row.properties.run_id}) SET n += row.properties RETURN count(n) AS n",
    "roles": "UNWIND $rows AS row MERGE (n:Role {role_id:row.properties.role_id}) SET n += row.properties RETURN count(n) AS n",
    "role_families": "UNWIND $rows AS row MERGE (n:RoleFamily {family_id:row.properties.family_id}) SET n += row.properties RETURN count(n) AS n",
    "normalized_skills": "UNWIND $rows AS row MERGE (n:NormalizedSkill {concept_id:row.properties.concept_id}) SET n += row.properties RETURN count(n) AS n",
    "role_profiles": "UNWIND $rows AS row MERGE (n:RoleProfile {profile_id:row.properties.profile_id}) SET n += row.properties RETURN count(n) AS n",
    "industries": "UNWIND $rows AS row MERGE (n:Industry {industry_id:row.properties.industry_id}) SET n += row.properties RETURN count(n) AS n",
    "levels": "UNWIND $rows AS row MERGE (n:Level {level_id:row.properties.level_id}) SET n += row.properties RETURN count(n) AS n",
    "time_windows": "UNWIND $rows AS row MERGE (n:TimeWindow {window_id:row.properties.window_id}) SET n += row.properties RETURN count(n) AS n",
    "role_aliases": "UNWIND $rows AS row MERGE (n:RoleAlias {alias_id:row.properties.alias_id}) SET n += row.properties RETURN count(n) AS n",
}


REL_IMPORTS = {
    "has_role": "UNWIND $rows AS row MATCH (a:RoleFamily {family_id:row.source_id}), (b:Role {role_id:row.target_id}) MERGE (a)-[r:HAS_ROLE]->(b) SET r += row.properties RETURN count(r) AS n",
    "alias_of": "UNWIND $rows AS row MATCH (a:RoleAlias {alias_id:row.source_id}), (b:Role {role_id:row.target_id}) MERGE (a)-[r:ALIAS_OF]->(b) SET r += row.properties RETURN count(r) AS n",
    "has_profile": "UNWIND $rows AS row MATCH (a:Role {role_id:row.source_id}), (b:RoleProfile {profile_id:row.target_id}) MERGE (a)-[r:HAS_PROFILE]->(b) SET r += row.properties RETURN count(r) AS n",
    "in_industry": "UNWIND $rows AS row MATCH (a:RoleProfile {profile_id:row.source_id}), (b:Industry {industry_id:row.target_id}) MERGE (a)-[r:IN_INDUSTRY]->(b) SET r += row.properties RETURN count(r) AS n",
    "at_level": "UNWIND $rows AS row MATCH (a:RoleProfile {profile_id:row.source_id}), (b:Level {level_id:row.target_id}) MERGE (a)-[r:AT_LEVEL]->(b) SET r += row.properties RETURN count(r) AS n",
    "in_window": "UNWIND $rows AS row MATCH (a:RoleProfile {profile_id:row.source_id}), (b:TimeWindow {window_id:row.target_id}) MERGE (a)-[r:IN_WINDOW]->(b) SET r += row.properties RETURN count(r) AS n",
    "has_core_skill": "UNWIND $rows AS row MATCH (a:Role {role_id:row.source_id}), (b:NormalizedSkill {concept_id:row.target_id}) MERGE (a)-[r:HAS_CORE_SKILL {run_id:row.properties.run_id}]->(b) SET r += row.properties RETURN count(r) AS n",
    "has_skill_snapshot": "UNWIND $rows AS row MATCH (a:Role {role_id:row.source_id}), (b:NormalizedSkill {concept_id:row.target_id}) MERGE (a)-[r:HAS_SKILL_SNAPSHOT {time_window:row.properties.time_window}]->(b) SET r += row.properties RETURN count(r) AS n",
}


def ensure_empty(client: Any) -> None:
    rows = client.query("MATCH (n) RETURN count(n) AS n")
    count = int(rows[0].get("n") or 0) if rows else 0
    if count:
        raise ValueError(f"目标数据库不是空库（已有 {count} 个节点）；为防止覆盖或混入数据，已停止导入。")


def validate_payload(payload: dict, manifest: dict | None = None) -> None:
    if payload.get("format") != "trusted-job-graph-display" or payload.get("format_version") != FORMAT_VERSION:
        raise ValueError("不是受支持的展示图谱数据包。")
    privacy = payload.get("privacy") or {}
    if privacy.get("contains_raw_jd") or privacy.get("contains_evidence_quotes") or privacy.get("contains_company"):
        raise ValueError("发布包隐私门禁失败：不能包含原文、证据引用或公司信息。")
    if manifest is not None:
        expected = str(manifest.get("sha256") or "")
        if expected:
            actual = hashlib.sha256(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            if actual != expected:
                raise ValueError("发布包 manifest SHA-256 校验失败。")
        if manifest.get("active_run_id") and manifest["active_run_id"] != payload.get("active_run_id"):
            raise ValueError("manifest 与数据包的活动运行版本不一致。")
    # Legacy handoff zips predate explicit window metadata.  Reconstruct it
    # from their sanitized TimeWindow nodes so they remain valid rollback
    # targets; all newly generated packages always carry the field.
    if "data_window" not in payload:
        quarters = sorted(
            {
                str(row.get("properties", {}).get("name") or row.get("properties", {}).get("time_window") or "")
                for row in (payload.get("nodes", {}).get("time_windows", []) if isinstance(payload.get("nodes"), dict) else [])
                if row.get("properties", {}).get("name") or row.get("properties", {}).get("time_window")
            }
        )
        payload["data_window"] = {
            "quarters": quarters,
            "min": quarters[0] if quarters else "",
            "max": quarters[-1] if quarters else "",
        }
    for key in ("nodes", "relationships", "counts", "active_run_id", "data_window"):
        if key not in payload:
            raise ValueError(f"发布包缺少字段：{key}")


def load_package(package_path: Path) -> tuple[dict, dict]:
    package_path = package_path.expanduser().resolve()
    if package_path.suffix.lower() == ".zip":
        with zipfile.ZipFile(package_path) as bundle:
            names = set(bundle.namelist())
            if "display_graph.json" not in names or "manifest.json" not in names:
                raise ValueError("压缩包缺少 display_graph.json 或 manifest.json。")
            payload = json.loads(bundle.read("display_graph.json").decode("utf-8"))
            manifest = json.loads(bundle.read("manifest.json").decode("utf-8"))
            digest = hashlib.sha256(bundle.read("display_graph.json")).hexdigest()
            if digest != str(manifest.get("sha256") or ""):
                raise ValueError("压缩包内 display_graph.json 的 SHA-256 校验失败。")
    else:
        payload = json.loads(package_path.read_text(encoding="utf-8"))
        manifest_path = package_path.with_name("manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}
    validate_payload(payload, manifest or None)
    return payload, manifest


def import_payload(client: Any, payload: dict, *, allow_existing: bool = False, atomic: bool = False) -> None:
    if payload.get("format") != "trusted-job-graph-display" or payload.get("format_version") != FORMAT_VERSION:
        raise ValueError("不是受支持的展示图谱数据包。")
    validate_payload(payload)
    if atomic:
        import_payload_atomic(client, payload)
        return
    if not allow_existing:
        ensure_empty(client)
    for name, statement in NODE_IMPORTS.items():
        merge_rows(client, statement, payload["nodes"].get(name, []))
    for name, statement in REL_IMPORTS.items():
        merge_rows(client, statement, payload["relationships"].get(name, []))
    run_id = str(payload["active_run_id"])
    client.query(
        """
        MERGE (pointer:NormalizationPointer {name:'core'})
        WITH pointer
        MATCH (run:NormalizationRun {run_id:$run_id})
        MERGE (pointer)-[:ACTIVE]->(run)
        RETURN run.run_id AS run_id
        """,
        {"run_id": run_id},
        access_mode="Write",
    )


def import_payload_atomic(client: Any, payload: dict) -> None:
    """Import and activate a release in one Neo4j transaction.

    The display graph is intentionally small.  A single auto-commit Cypher
    request makes staging, replacement of generated quarter aggregates, and
    pointer activation all-or-nothing while leaving raw/review nodes untouched.
    The previous zip is retained by the deployment wrapper for rollback.
    """
    validate_payload(payload)
    release_id = str(payload.get("release_id") or stable_display_id("display", payload["active_run_id"]))
    release_properties = {
        "release_id": release_id,
        "status": "STAGING",
        "active_run_id": str(payload["active_run_id"]),
        "generated_at": str(payload.get("exported_at") or ""),
        "data_window_min": str((payload.get("data_window") or {}).get("min") or ""),
        "data_window_max": str((payload.get("data_window") or {}).get("max") or ""),
        "data_windows": list((payload.get("data_window") or {}).get("quarters") or []),
        "counts_json": json.dumps(payload.get("counts") or {}, ensure_ascii=False, separators=(",", ":")),
        "quarter_stats_json": json.dumps(payload.get("quarter_stats") or {}, ensure_ascii=False, separators=(",", ":")),
    }
    rows = {
        "normalization_runs": payload["nodes"].get("normalization_runs", []),
        "roles": payload["nodes"].get("roles", []),
        "role_families": payload["nodes"].get("role_families", []),
        "normalized_skills": payload["nodes"].get("normalized_skills", []),
        "role_profiles": payload["nodes"].get("role_profiles", []),
        "industries": payload["nodes"].get("industries", []),
        "levels": payload["nodes"].get("levels", []),
        "time_windows": payload["nodes"].get("time_windows", []),
        "role_aliases": payload["nodes"].get("role_aliases", []),
        **{name: payload["relationships"].get(name, []) for name in REL_IMPORTS},
        "generated_windows": [
            str(row.get("properties", {}).get("name") or "")
            for row in payload["nodes"].get("time_windows", [])
            if row.get("properties", {}).get("display_aggregate")
        ],
    }
    query = """
    CALL { WITH $normalization_runs AS rows UNWIND rows AS row MERGE (n:NormalizationRun {run_id:row.properties.run_id}) SET n += row.properties RETURN count(*) AS n1 }
    CALL { WITH $roles AS rows UNWIND rows AS row MERGE (n:Role {role_id:row.properties.role_id}) SET n += row.properties RETURN count(*) AS n2 }
    CALL { WITH $role_families AS rows UNWIND rows AS row MERGE (n:RoleFamily {family_id:row.properties.family_id}) SET n += row.properties RETURN count(*) AS n3 }
    CALL { WITH $normalized_skills AS rows UNWIND rows AS row MERGE (n:NormalizedSkill {concept_id:row.properties.concept_id}) SET n += row.properties RETURN count(*) AS n4 }
    CALL { WITH $role_profiles AS rows UNWIND rows AS row MERGE (n:RoleProfile {profile_id:row.properties.profile_id}) SET n += row.properties RETURN count(*) AS n5 }
    CALL { WITH $industries AS rows UNWIND rows AS row MERGE (n:Industry {industry_id:row.properties.industry_id}) SET n += row.properties RETURN count(*) AS n6 }
    CALL { WITH $levels AS rows UNWIND rows AS row MERGE (n:Level {level_id:row.properties.level_id}) SET n += row.properties RETURN count(*) AS n7 }
    CALL { WITH $time_windows AS rows UNWIND rows AS row MERGE (n:TimeWindow {window_id:row.properties.window_id}) SET n += row.properties RETURN count(*) AS n8 }
    CALL { WITH $role_aliases AS rows UNWIND rows AS row MERGE (n:RoleAlias {alias_id:row.properties.alias_id}) SET n += row.properties RETURN count(*) AS n9 }
    CALL { WITH $generated_windows AS windows MATCH ()-[r:HAS_SKILL_SNAPSHOT]->() WHERE coalesce(r.generated_display,false)=true AND r.time_window IN windows DELETE r RETURN count(*) AS n10 }
    CALL { WITH $has_role AS rows UNWIND rows AS row MATCH (a:RoleFamily {family_id:row.source_id}), (b:Role {role_id:row.target_id}) MERGE (a)-[r:HAS_ROLE]->(b) SET r += row.properties RETURN count(*) AS n11 }
    CALL { WITH $alias_of AS rows UNWIND rows AS row MATCH (a:RoleAlias {alias_id:row.source_id}), (b:Role {role_id:row.target_id}) MERGE (a)-[r:ALIAS_OF]->(b) SET r += row.properties RETURN count(*) AS n12 }
    CALL { WITH $has_profile AS rows UNWIND rows AS row MATCH (a:Role {role_id:row.source_id}), (b:RoleProfile {profile_id:row.target_id}) MERGE (a)-[r:HAS_PROFILE]->(b) SET r += row.properties RETURN count(*) AS n13 }
    CALL { WITH $in_industry AS rows UNWIND rows AS row MATCH (a:RoleProfile {profile_id:row.source_id}), (b:Industry {industry_id:row.target_id}) MERGE (a)-[r:IN_INDUSTRY]->(b) SET r += row.properties RETURN count(*) AS n14 }
    CALL { WITH $at_level AS rows UNWIND rows AS row MATCH (a:RoleProfile {profile_id:row.source_id}), (b:Level {level_id:row.target_id}) MERGE (a)-[r:AT_LEVEL]->(b) SET r += row.properties RETURN count(*) AS n15 }
    CALL { WITH $in_window AS rows UNWIND rows AS row MATCH (a:RoleProfile {profile_id:row.source_id}), (b:TimeWindow {window_id:row.target_id}) MERGE (a)-[r:IN_WINDOW]->(b) SET r += row.properties RETURN count(*) AS n16 }
    CALL { WITH $has_core_skill AS rows UNWIND rows AS row MATCH (a:Role {role_id:row.source_id}), (b:NormalizedSkill {concept_id:row.target_id}) MERGE (a)-[r:HAS_CORE_SKILL {run_id:row.properties.run_id}]->(b) SET r += row.properties RETURN count(*) AS n17 }
    CALL { WITH $has_skill_snapshot AS rows UNWIND rows AS row MATCH (a:Role {role_id:row.source_id}), (b:NormalizedSkill {concept_id:row.target_id}) MERGE (a)-[r:HAS_SKILL_SNAPSHOT {time_window:row.properties.time_window}]->(b) SET r += row.properties RETURN count(*) AS n18 }
    MERGE (release:DisplayGraphRelease {release_id:$release_id}) SET release += $release_properties
    MERGE (pointer:DisplayGraphPointer {name:'active'})
    WITH release, pointer
    OPTIONAL MATCH (pointer)-[old:ACTIVE]->(previous:DisplayGraphRelease)
    DELETE old
    WITH release, pointer, collect(previous) AS previous_runs
    MERGE (pointer)-[:ACTIVE]->(release)
    SET release.status='ACTIVE', release.activated_at=$activated_at
    FOREACH (item IN previous_runs | SET item.status = CASE WHEN item.release_id = release.release_id THEN 'ACTIVE' ELSE 'ARCHIVED' END)
    RETURN release.release_id AS release_id, release.status AS status
    """
    client.query(
        query,
        {**rows, "release_id": release_id, "release_properties": release_properties,
         "activated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
        access_mode="Write",
    )


def import_payload_batched(client: Any, payload: dict, batch_size: int = 250) -> None:
    """Stage a package with bounded transactions, then activate its pointer.

    The caller must keep the graph API unavailable while staging.  Every
    batch is small enough for the production Neo4j memory cap; the final
    pointer transaction is tiny.  A deployment failure is recovered by the
    caller from the retained previous package before the API is reopened.
    """
    validate_payload(payload)
    batch_size = max(25, min(int(batch_size), 500))
    release_id = str(payload.get("release_id") or stable_display_id("display", payload["active_run_id"]))
    # Generated quarter snapshots are fully replaced by each release.  This
    # also makes rollback to a legacy package exact without deleting any
    # hand-authored historical snapshot relations.
    while True:
        removed = client.query(
            "MATCH ()-[r:HAS_SKILL_SNAPSHOT]->() "
            "WHERE coalesce(r.generated_display,false)=true "
            "WITH r LIMIT $limit DELETE r RETURN count(*) AS removed",
            {"limit": batch_size},
            access_mode="Write",
        )
        if not removed or int(removed[0].get("removed") or 0) == 0:
            break
    for label in ("RoleProfile", "TimeWindow"):
        while True:
            removed = client.query(
                f"MATCH (n:{label}) WHERE coalesce(n.display_aggregate,false)=true "
                "WITH n LIMIT $limit DETACH DELETE n RETURN count(*) AS removed",
                {"limit": batch_size},
                access_mode="Write",
            )
            if not removed or int(removed[0].get("removed") or 0) == 0:
                break
    for name, statement in NODE_IMPORTS.items():
        rows = payload["nodes"].get(name, [])
        for batch in chunks(rows, batch_size):
            client.query(statement, {"rows": batch}, access_mode="Write")
    for name, statement in REL_IMPORTS.items():
        rows = payload["relationships"].get(name, [])
        for batch in chunks(rows, batch_size):
            client.query(statement, {"rows": batch}, access_mode="Write")
    release_properties = {
        "release_id": release_id,
        "status": "STAGING",
        "active_run_id": str(payload["active_run_id"]),
        "generated_at": str(payload.get("exported_at") or ""),
        "data_window_min": str((payload.get("data_window") or {}).get("min") or ""),
        "data_window_max": str((payload.get("data_window") or {}).get("max") or ""),
        "data_windows": list((payload.get("data_window") or {}).get("quarters") or []),
        "counts_json": json.dumps(payload.get("counts") or {}, ensure_ascii=False, separators=(",", ":")),
        "quarter_stats_json": json.dumps(payload.get("quarter_stats") or {}, ensure_ascii=False, separators=(",", ":")),
    }
    client.query(
        """
        MERGE (release:DisplayGraphRelease {release_id:$release_id}) SET release += $release_properties
        MERGE (pointer:DisplayGraphPointer {name:'active'})
        WITH release, pointer
        OPTIONAL MATCH (pointer)-[old:ACTIVE]->(previous:DisplayGraphRelease)
        DELETE old
        WITH release, pointer, collect(previous) AS previous_runs
        MERGE (pointer)-[:ACTIVE]->(release)
        SET release.status='ACTIVE', release.activated_at=$activated_at
        FOREACH (item IN previous_runs | SET item.status = CASE WHEN item.release_id = release.release_id THEN 'ACTIVE' ELSE 'ARCHIVED' END)
        RETURN release.release_id AS release_id, release.status AS status
        """,
        {
            "release_id": release_id,
            "release_properties": release_properties,
            "activated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
        access_mode="Write",
    )


def verify(client: Any) -> dict:
    rows = client.query(
        """
        CALL { MATCH (n) RETURN count(n) AS nodes }
        CALL { MATCH ()-[r]->() RETURN count(r) AS relationships }
        CALL { MATCH (n:Role) RETURN count(n) AS roles }
        CALL { MATCH (n:NormalizedSkill) RETURN count(n) AS skills }
        CALL { MATCH (n:RoleProfile) RETURN count(n) AS profiles }
        CALL { MATCH ()-[r:HAS_CORE_SKILL]->() RETURN count(r) AS core_skills }
        CALL { MATCH ()-[r:HAS_SKILL_SNAPSHOT]->() RETURN count(r) AS snapshots }
        CALL { MATCH (n) WHERE n:RawJDVersion OR n:ProcessedJD OR n:AbilityCandidate
                               OR n:ProcessingReview OR n:Company OR n:JD
               RETURN count(n) AS forbidden_nodes }
        CALL { OPTIONAL MATCH (:DisplayGraphPointer {name:'active'})-[:ACTIVE]->(release:DisplayGraphRelease)
               RETURN release.release_id AS active_display_release,
                      release.data_window_min AS display_data_window_min,
                      release.data_window_max AS display_data_window_max,
                      release.data_windows AS display_data_windows }
        RETURN nodes, relationships, roles, skills, profiles, core_skills, snapshots, forbidden_nodes,
               active_display_release, display_data_window_min, display_data_window_max, display_data_windows
        """
    )
    result = rows[0] if rows else {}
    result["ready"] = bool(
        int(result.get("roles") or 0)
        and int(result.get("skills") or 0)
        and int(result.get("core_skills") or 0)
        and not int(result.get("forbidden_nodes") or 0)
    )
    return result


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="导出、导入和验证脱敏的 Neo4j 前端展示层")
    commands = root.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export", help="从正式 Neo4j 生成可发送的数据包")
    export.add_argument("--neo4j-config", type=Path, default=DEFAULT_CONFIG)
    export.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    load = commands.add_parser("import", help="将展示包导入一个空 Neo4j 数据库")
    load.add_argument("--package", type=Path, required=True, help="display_graph.json 或发布 zip")
    load.add_argument("--neo4j-config", type=Path, default=DEFAULT_CONFIG)
    load.add_argument("--allow-existing", action="store_true", help="允许导入到已有业务/审核数据的 Neo4j")
    load.add_argument("--atomic", action="store_true", help="用单事务导入并切换展示版本")
    check = commands.add_parser("verify", help="检查目标数据库仅包含可展示数据")
    check.add_argument("--neo4j-config", type=Path, default=DEFAULT_CONFIG)
    serve = commands.add_parser("serve", help="使用展示数据库启动岗位能力全景页")
    serve.add_argument("--neo4j-config", type=Path, default=DEFAULT_CONFIG)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8010)
    return root


def main() -> int:
    args = parser().parse_args()
    config_path = args.neo4j_config.expanduser().resolve()
    if args.command == "export":
        payload = export_payload(repository(config_path))
        archive = write_package(payload, args.output_dir.expanduser().resolve())
        print(json.dumps({"archive": str(archive), "counts": payload["counts"], "privacy": payload["privacy"]}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "import":
        payload, _manifest = load_package(args.package)
        client = repository(config_path)
        import_payload(client, payload, allow_existing=args.allow_existing, atomic=args.atomic)
        result = verify(client)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ready"] else 1
    if args.command == "verify":
        result = verify(repository(config_path))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["ready"] else 1
    if args.command == "serve":
        from trusted_graph_agent.api_server import run_server

        run_server(Path("unused.db"), PAGE_PATH, args.host, args.port, backend="neo4j", neo4j_config=config_path)
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "neo4j_connection.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "output" / "team_handoff"
PAGE_PATH = PROJECT_ROOT / "trusted_graph_agent" / "static" / "panorama.html"
FORMAT_VERSION = 1
PAGE_SIZE = 10_000


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
            MATCH (:Role)-[:HAS_CORE_SKILL {run_id:$run_id}]->(n:NormalizedSkill)
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
    counts = {
        **{name: len(rows) for name, rows in nodes.items()},
        **{name: len(rows) for name, rows in relationships.items()},
    }
    return {
        "format": "trusted-job-graph-display",
        "format_version": FORMAT_VERSION,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "active_run_id": run_id,
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
        "active_run_id": payload["active_run_id"],
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


def import_payload(client: Any, payload: dict) -> None:
    if payload.get("format") != "trusted-job-graph-display" or payload.get("format_version") != FORMAT_VERSION:
        raise ValueError("不是受支持的展示图谱数据包。")
    ensure_empty(client)
    for name, statement in NODE_IMPORTS.items():
        merge_rows(client, statement, payload["nodes"].get(name, []))
    for name, statement in REL_IMPORTS.items():
        merge_rows(client, statement, payload["relationships"].get(name, []))
    run_id = str(payload["active_run_id"])
    client.query(
        """
        MERGE (pointer:NormalizationPointer {name:'core'})
        MATCH (run:NormalizationRun {run_id:$run_id})
        MERGE (pointer)-[:ACTIVE]->(run)
        RETURN run.run_id AS run_id
        """,
        {"run_id": run_id},
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
        RETURN nodes, relationships, roles, skills, profiles, core_skills, snapshots, forbidden_nodes
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
    load.add_argument("--package", type=Path, required=True, help="解压后的 display_graph.json")
    load.add_argument("--neo4j-config", type=Path, default=DEFAULT_CONFIG)
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
        payload = json.loads(args.package.expanduser().resolve().read_text(encoding="utf-8"))
        client = repository(config_path)
        import_payload(client, payload)
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

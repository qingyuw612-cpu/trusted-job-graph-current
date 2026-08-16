"""为既有岗位归一化流程准备猎聘未映射批次。

本脚本只做字段适配和历史批准注册表转换，不实现新的岗位判断算法，
也不修改 Neo4j。后续匹配、BGE 召回和新岗位聚类仍由 cli.py 完成。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


AGENT_ROOT = Path(".")
PROJECT_DIR = AGENT_ROOT / "role_normalization_project"
WORKSPACE_ROOT = Path("..")
if str(AGENT_ROOT) not in sys.path:
    sys.path.insert(0, str(AGENT_ROOT))

from trusted_graph_agent.neo4j_repository import Neo4jGraphRepository  # noqa: E402


DEFAULT_SOURCE_ID = "rawsource:6916d9be33e842c4460bf43a9cefe4f44a27d420"
DEFAULT_RESULTS = WORKSPACE_ROOT / "2026数据51job" / "岗位概念标准化结果"
DEFAULT_OUTPUT = AGENT_ROOT / "output" / "liepin_role_normalization"


FETCH_UNMAPPED = """
MATCH (:RawJob)-[:CURRENT_VERSION]->(raw:RawJDVersion)
      -[:FROM_SOURCE]->(:RawSourceFile {source_file_id:$source_id})
WHERE raw.domain_label = 'IT'
  AND coalesce(raw.domain_role, '') = ''
RETURN raw.version_id AS version_id,
       raw.source_job_id AS source_job_id,
       raw.title AS title,
       raw.description AS description,
       raw.company_id AS company_id,
       raw.company_name AS company_name,
       raw.source_platform AS source_platform,
       raw.industry AS industry,
       raw.ability_analysis_raw AS ability_analysis_raw
ORDER BY raw.version_id
"""


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def approved(value: str) -> bool:
    return value in {
        "CONTROLLED",
        "SYSTEM_APPROVED",
        "HUMAN_APPROVED",
        "HUMAN_APPROVED_NEW",
        "AI_APPROVED",
        "AI_APPROVED_NEW",
        "AI_APPROVED_NEW_ROUND2",
    }


def build_registry(
    results_dir: Path,
    taxonomy_path: Path,
    role_skills_path: Path,
) -> dict[str, Any]:
    master_rows = read_csv(results_dir / "role_master_draft.csv")
    alias_rows = read_csv(results_dir / "role_alias_draft.csv")
    taxonomy = json.loads(taxonomy_path.read_text(encoding="utf-8"))
    taxonomy_by_name = {
        str(row.get("role_name") or ""): row for row in taxonomy.get("roles", [])
    }
    aliases: dict[str, list[str]] = defaultdict(list)
    role_skills: dict[str, list[tuple[float, str]]] = defaultdict(list)
    if role_skills_path.is_file():
        for row in read_csv(role_skills_path):
            if str(row.get("category") or "") not in {"技术", "知识"}:
                continue
            role_name = str(row.get("role") or "").strip()
            skill_name = str(row.get("canonical_name") or "").strip()
            if not role_name or not skill_name:
                continue
            try:
                score = float(row.get("final_score") or 0.0)
            except ValueError:
                score = 0.0
            role_skills[role_name].append((score, skill_name))
    for row in alias_rows:
        if not approved(str(row.get("status") or "")):
            continue
        role_id = str(row.get("role_id") or "").strip()
        source_name = str(row.get("source_name") or "").strip()
        if role_id and source_name:
            aliases[role_id].append(source_name)

    roles = []
    for row in master_rows:
        status = str(row.get("status") or "").strip()
        if not approved(status):
            continue
        role_id = str(row.get("role_id") or "").strip()
        name = str(row.get("canonical_name") or "").strip()
        if not role_id or not name:
            continue
        taxonomy_role = taxonomy_by_name.get(name, {})
        values = [name, *taxonomy_role.get("aliases", []), *aliases.get(role_id, [])]
        unique_aliases = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
        roles.append(
            {
                "role_id": role_id,
                "canonical_name": name,
                "aliases": [value for value in unique_aliases if value != name],
                "family": str(row.get("family") or taxonomy_role.get("family_id") or ""),
                "description": str(taxonomy_role.get("description") or ""),
                "is_it_role": True,
                "tags": list(taxonomy_role.get("tags") or []),
                "metadata": {
                    "approval_status": status,
                    "definition_version": str(row.get("definition_version") or ""),
                    "source": "historical_approved_role_catalog",
                    "skills": list(
                        dict.fromkeys(
                            skill
                            for _score, skill in sorted(
                                role_skills.get(name, []),
                                key=lambda item: (-item[0], item[1]),
                            )
                        )
                    )[:20],
                },
            }
        )
    return {
        "version": "historical-approved-2026-08-02",
        "roles": sorted(roles, key=lambda item: item["canonical_name"]),
    }


def ability_profile(raw: str) -> str:
    try:
        payload = json.loads(str(raw or ""))
    except json.JSONDecodeError:
        return ""
    if not isinstance(payload, dict):
        return ""
    values: list[str] = []
    for key in ("Skill", "Skills", "Technology", "Technical", "技术", "Knowledge", "知识"):
        group = payload.get(key, [])
        if isinstance(group, list):
            values.extend(str(item).strip() for item in group if str(item).strip())
        elif str(group or "").strip():
            values.append(str(group).strip())
    return json.dumps(list(dict.fromkeys(values)), ensure_ascii=False)


def write_batch(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = [
        "version_id",
        "source_job_id",
        "title",
        "description",
        "company_id",
        "company_name",
        "source_platform",
        "industry",
        "ability_analysis_raw",
        "normalized_skills",
    ]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({**row, "normalized_skills": ability_profile(row.get("ability_analysis_raw", ""))})


def main() -> int:
    parser = argparse.ArgumentParser(description="准备猎聘未映射岗位的既有归一化流程输入")
    parser.add_argument("--neo4j-config", type=Path, default=AGENT_ROOT / "config" / "neo4j_connection.json")
    parser.add_argument("--source-id", default=DEFAULT_SOURCE_ID)
    parser.add_argument("--historical-results", type=Path, default=DEFAULT_RESULTS)
    parser.add_argument("--taxonomy", type=Path, default=AGENT_ROOT / "trusted_graph_agent" / "it_role_taxonomy.json")
    parser.add_argument(
        "--role-skills",
        type=Path,
        default=AGENT_ROOT / "output" / "processed_normalization_incremental" / "skill_reports" / "role_top_skills.csv",
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()

    output_dir = args.output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    repository = Neo4jGraphRepository(args.neo4j_config)
    rows = repository.client.query(FETCH_UNMAPPED, {"source_id": args.source_id})
    registry = build_registry(
        args.historical_results,
        args.taxonomy,
        args.role_skills,
    )
    batch_path = output_dir / "liepin_unmapped_it.csv"
    registry_path = output_dir / "historical_approved_registry.json"
    manifest_path = output_dir / "prepare_manifest.json"
    write_batch(batch_path, rows)
    registry_path.write_text(json.dumps(registry, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest = {
        "mode": "ADAPTER_ONLY_NO_GRAPH_WRITE",
        "source_id": args.source_id,
        "records": len(rows),
        "approved_roles": len(registry["roles"]),
        "approved_aliases": sum(len(row["aliases"]) for row in registry["roles"]),
        "batch": str(batch_path),
        "registry": str(registry_path),
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(".")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from trusted_graph_agent.neo4j_repository import Neo4jGraphRepository  # noqa: E402
from trusted_graph_agent.text_utils import normalize_text, stable_id  # noqa: E402


@dataclass(slots=True)
class PublishSnapshot:
    run_id: str
    created_at: str
    source_database: Path
    normalization_dir: Path
    roles: list[dict[str, Any]]
    concepts: list[dict[str, Any]]
    core_edges: list[dict[str, Any]]
    mappings: list[dict[str, Any]]

    @property
    def expected(self) -> dict[str, int]:
        return {
            "roles": len(self.roles),
            "concepts": len(self.concepts),
            "core_edges": len(self.core_edges),
            "mapping_names": len(self.mappings),
        }


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as file:
        return list(csv.DictReader(file))


def chunks(rows: list[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def taxonomy_lookup() -> tuple[dict[str, dict[str, str]], dict[str, dict[str, str]]]:
    path = PROJECT_ROOT / "trusted_graph_agent" / "it_role_taxonomy.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    families = {item["family_id"]: item for item in payload["families"]}
    lookup: dict[str, dict[str, str]] = {}
    for role in payload["roles"]:
        values = [role["role_name"], *role.get("aliases", [])]
        values.extend(Path(source).stem for source in role.get("sources", []))
        family = families[role["family_id"]]
        metadata = {
            "family_id": family["family_id"],
            "family_name": family["family_name"],
            "domain_id": payload["domain"]["domain_id"],
            "domain_name": payload["domain"]["domain_name"],
        }
        for value in values:
            lookup.setdefault(normalize_text(value), metadata)
    fallback = {
        "family_id": "extended_roles",
        "family_name": "扩展岗位（待细分）",
        "domain_id": "multi_industry",
        "domain_name": "多行业岗位",
    }
    return lookup, {"fallback": fallback}


def snapshot_digest(paths: list[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        with path.open("rb") as file:
            while block := file.read(1024 * 1024):
                digest.update(block)
    return digest.hexdigest()


def load_snapshot(database: Path, normalization_dir: Path) -> PublishSnapshot:
    top_path = normalization_dir / "role_top_skills.csv"
    concept_path = normalization_dir / "normalized_concepts.csv"
    mapping_path = normalization_dir / "skill_normalization_mapping.csv"
    report_path = normalization_dir / "normalization_report.json"
    required = [top_path, concept_path, mapping_path, report_path]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError(f"缺少归一化结果文件：{missing}")
    if not database.exists():
        raise FileNotFoundError(f"SQLite 数据库不存在：{database}")

    top_rows = read_csv(top_path)
    concept_rows = read_csv(concept_path)
    mapping_rows = read_csv(mapping_path)
    digest = snapshot_digest(required)
    run_id = f"normalization:{digest[:20]}"
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    concepts: list[dict[str, Any]] = []
    concept_ids: set[str] = set()
    for row in concept_rows:
        concept_id = row["concept_id"]
        if not concept_id or concept_id in concept_ids:
            raise ValueError(f"标准概念 ID 缺失或重复：{concept_id}")
        concept_ids.add(concept_id)
        concepts.append(
            {
                "concept_id": concept_id,
                "canonical_name": row["canonical_name"],
                "category": row["category"],
                "concept_status": row["status"],
                "source_phrase_count": int(row["source_phrase_count"]),
                "jd_count": int(row["jd_count"]),
                "company_count": int(row["company_count"]),
                "verified_rate": float(row["verified_rate"]),
                "snapshot_run_id": run_id,
            }
        )

    role_totals: dict[str, tuple[int, int]] = {}
    connection = sqlite3.connect(database)
    try:
        for role, jd_count, company_count in connection.execute(
            """
            SELECT canonical_role, COUNT(DISTINCT jd_id), COUNT(DISTINCT company_id)
            FROM jds
            WHERE duplicate_of = ''
            GROUP BY canonical_role
            """
        ):
            role_totals[str(role)] = (int(jd_count), int(company_count))
    finally:
        connection.close()

    lookup, fallbacks = taxonomy_lookup()
    role_names = sorted({row["role"] for row in top_rows})
    roles: list[dict[str, Any]] = []
    for role_name in role_names:
        if role_name not in role_totals:
            raise ValueError(f"核心技能岗位在 SQLite 中不存在：{role_name}")
        family = lookup.get(normalize_text(role_name), fallbacks["fallback"])
        jd_count, company_count = role_totals[role_name]
        roles.append(
            {
                "role_id": stable_id("role", role_name),
                "name": role_name,
                "role_name": role_name,
                "document_count": jd_count,
                "company_count": company_count,
                "source_role_names": [role_name],
                "normalization_run_id": run_id,
                **family,
            }
        )

    core_edges: list[dict[str, Any]] = []
    seen_edges: set[tuple[str, str]] = set()
    grouped: dict[str, list[dict[str, str]]] = {}
    for row in top_rows:
        grouped.setdefault(row["role"], []).append(row)
    for role_name, rows in grouped.items():
        role_id = stable_id("role", role_name)
        for rank, row in enumerate(
            sorted(rows, key=lambda item: (-float(item["final_score"]), item["canonical_name"])),
            1,
        ):
            concept_id = row["concept_id"]
            key = (role_id, concept_id)
            if concept_id not in concept_ids:
                raise ValueError(f"核心技能引用了未知概念：{concept_id}")
            if key in seen_edges:
                raise ValueError(f"岗位核心技能重复：{role_name} / {concept_id}")
            if int(row["verified_jd_count"]) < int(row["minimum_verified_jd_count"]):
                raise ValueError(f"岗位核心技能低于验证证据门槛：{role_name} / {row['canonical_name']}")
            seen_edges.add(key)
            core_edges.append(
                {
                    "role_id": role_id,
                    "concept_id": concept_id,
                    "run_id": run_id,
                    "final_score": float(row["final_score"]),
                    "company_count": int(row["company_count"]),
                    "jd_count": int(row["jd_count"]),
                    "verified_jd_count": int(row["verified_jd_count"]),
                    "rank": rank,
                    "category_rank": int(row["mmr_rank"]),
                    "published_at": created_at,
                }
            )

    mapping_by_name: dict[str, str] = {}
    for row in mapping_rows:
        normalized_name = normalize_text(row["source_name"])
        concept_id = row["concept_id"]
        if not normalized_name or concept_id not in concept_ids:
            continue
        previous = mapping_by_name.setdefault(normalized_name, concept_id)
        if previous != concept_id:
            raise ValueError(
                f"同一规范化短语映射到多个概念：{row['source_name']} -> {previous}, {concept_id}"
            )
    mappings = [
        {
            "normalized_name": name,
            "concept_id": concept_id,
            "run_id": run_id,
            "published_at": created_at,
        }
        for name, concept_id in sorted(mapping_by_name.items())
    ]
    return PublishSnapshot(
        run_id=run_id,
        created_at=created_at,
        source_database=database,
        normalization_dir=normalization_dir,
        roles=roles,
        concepts=concepts,
        core_edges=core_edges,
        mappings=mappings,
    )


class NormalizationPublisher:
    def __init__(self, repository: Neo4jGraphRepository, batch_size: int = 500):
        self.client = repository.client
        self.batch_size = max(50, min(batch_size, 2000))

    def query(
        self,
        statement: str,
        parameters: dict[str, Any] | None = None,
        *,
        write: bool = False,
        attempts: int = 8,
    ) -> list[dict]:
        for attempt in range(1, attempts + 1):
            try:
                return self.client.query(
                    statement,
                    parameters,
                    access_mode="Write" if write else "Read",
                )
            except (ValueError, TimeoutError, OSError) as error:
                if attempt == attempts:
                    raise
                delay = min(2**attempt, 30)
                print(
                    f"neo4j_retry={attempt}/{attempts - 1} waiting={delay}s "
                    f"error={str(error)[:180]}",
                    flush=True,
                )
                time.sleep(delay)
        raise RuntimeError("Neo4j 重试状态异常")

    def inspect(self, snapshot: PublishSnapshot) -> dict[str, Any]:
        current = self.query(
            """
            OPTIONAL MATCH (:NormalizationPointer {name:'core'})-[:ACTIVE]->(run:NormalizationRun)
            RETURN run.run_id AS active_run_id, run.status AS active_status
            """
        )
        counts = self.query(
            """
            CALL { MATCH (r:Role) RETURN count(r) AS roles }
            CALL { MATCH (s:NormalizedSkill) RETURN count(s) AS concepts }
            CALL { MATCH (a:AbilityCandidate) RETURN count(a) AS abilities }
            RETURN roles, concepts, abilities
            """
        )
        role_ids = [row["role_id"] for row in snapshot.roles]
        overlap = self.query(
            "UNWIND $ids AS id OPTIONAL MATCH (r:Role {role_id:id}) "
            "RETURN count(r) AS existing_roles",
            {"ids": role_ids},
        )
        return {
            "run_id": snapshot.run_id,
            "expected": snapshot.expected,
            "active": current[0] if current else {},
            "graph_counts": counts[0] if counts else {},
            "existing_role_overlap": int(overlap[0]["existing_roles"]) if overlap else 0,
            "mode": "DRY_RUN",
        }

    def _ensure_schema(self) -> None:
        statements = [
            "CREATE CONSTRAINT normalization_run_id IF NOT EXISTS "
            "FOR (n:NormalizationRun) REQUIRE n.run_id IS UNIQUE",
            "CREATE CONSTRAINT normalization_pointer_name IF NOT EXISTS "
            "FOR (n:NormalizationPointer) REQUIRE n.name IS UNIQUE",
            "CREATE CONSTRAINT normalized_skill_id IF NOT EXISTS "
            "FOR (n:NormalizedSkill) REQUIRE n.concept_id IS UNIQUE",
            "CREATE INDEX ability_candidate_normalized_name IF NOT EXISTS "
            "FOR (n:AbilityCandidate) ON (n.normalized_name)",
        ]
        for statement in statements:
            self.query(statement, write=True)
        for _ in range(120):
            rows = self.query(
                "SHOW INDEXES YIELD name, state "
                "WHERE name = 'ability_candidate_normalized_name' RETURN state"
            )
            if rows and rows[0].get("state") == "ONLINE":
                return
            time.sleep(5)
        raise TimeoutError("AbilityCandidate.normalized_name 索引在 10 分钟内未上线")

    def publish(self, snapshot: PublishSnapshot) -> dict[str, Any]:
        self._ensure_schema()
        self.query(
            """
            MERGE (run:NormalizationRun {run_id:$run_id})
            SET run.status='STAGING', run.created_at=$created_at,
                run.source_database=$source_database,
                run.normalization_dir=$normalization_dir,
                run.expected_roles=$roles, run.expected_concepts=$concepts,
                run.expected_core_edges=$core_edges,
                run.expected_mapping_names=$mapping_names
            """,
            {
                "run_id": snapshot.run_id,
                "created_at": snapshot.created_at,
                "source_database": str(snapshot.source_database),
                "normalization_dir": str(snapshot.normalization_dir),
                **snapshot.expected,
            },
            write=True,
        )

        for batch_number, batch in enumerate(chunks(snapshot.roles, self.batch_size), 1):
            self.query(
                """
                UNWIND $rows AS row
                MERGE (domain:Domain {domain_id:row.domain_id})
                SET domain.name=row.domain_name
                MERGE (family:RoleFamily {family_id:row.family_id})
                SET family.name=row.family_name,
                    family.domain_id=row.domain_id,
                    family.domain_name=row.domain_name
                MERGE (domain)-[:HAS_FAMILY]->(family)
                MERGE (role:Role {role_id:row.role_id})
                SET role.name=row.name, role.role_name=row.role_name,
                    role.document_count=row.document_count,
                    role.company_count=row.company_count,
                    role.source_role_names=row.source_role_names,
                    role.normalization_run_id=row.normalization_run_id,
                    role.family_id=row.family_id,
                    role.family_name=row.family_name,
                    role.domain_id=row.domain_id,
                    role.domain_name=row.domain_name
                MERGE (family)-[:HAS_ROLE]->(role)
                """,
                {"rows": batch},
                write=True,
            )
            print(f"publish_roles={min(batch_number * self.batch_size, len(snapshot.roles))}", flush=True)

        for batch_number, batch in enumerate(chunks(snapshot.concepts, self.batch_size), 1):
            self.query(
                """
                UNWIND $rows AS row
                MERGE (skill:NormalizedSkill {concept_id:row.concept_id})
                SET skill.canonical_name=row.canonical_name,
                    skill.category=row.category,
                    skill.concept_status=row.concept_status,
                    skill.source_phrase_count=row.source_phrase_count,
                    skill.jd_count=row.jd_count,
                    skill.company_count=row.company_count,
                    skill.verified_rate=row.verified_rate,
                    skill.snapshot_run_id=row.snapshot_run_id
                """,
                {"rows": batch},
                write=True,
            )
            print(
                f"publish_concepts={min(batch_number * self.batch_size, len(snapshot.concepts))}",
                flush=True,
            )

        for batch_number, batch in enumerate(chunks(snapshot.core_edges, self.batch_size), 1):
            self.query(
                """
                UNWIND $rows AS row
                MATCH (role:Role {role_id:row.role_id})
                MATCH (skill:NormalizedSkill {concept_id:row.concept_id})
                MERGE (role)-[edge:HAS_CORE_SKILL {run_id:row.run_id}]->(skill)
                SET edge.final_score=row.final_score,
                    edge.company_count=row.company_count,
                    edge.jd_count=row.jd_count,
                    edge.verified_jd_count=row.verified_jd_count,
                    edge.rank=row.rank,
                    edge.category_rank=row.category_rank,
                    edge.published_at=row.published_at
                """,
                {"rows": batch},
                write=True,
            )
            print(
                f"publish_core_edges={min(batch_number * self.batch_size, len(snapshot.core_edges))}",
                flush=True,
            )

        mapped_edges = 0
        for batch_number, batch in enumerate(chunks(snapshot.mappings, self.batch_size), 1):
            result = self.query(
                """
                UNWIND $rows AS row
                MATCH (ability:AbilityCandidate {normalized_name:row.normalized_name})
                MATCH (skill:NormalizedSkill {concept_id:row.concept_id})
                MERGE (ability)-[edge:NORMALIZES_TO {run_id:row.run_id}]->(skill)
                SET edge.published_at=row.published_at
                RETURN count(edge) AS mapped
                """,
                {"rows": batch},
                write=True,
            )
            mapped_edges += int(result[0]["mapped"]) if result else 0
            print(
                f"publish_mapping_names={min(batch_number * self.batch_size, len(snapshot.mappings))}",
                flush=True,
            )

        staged = self.verify(snapshot.run_id)
        if (
            staged["roles"] != len(snapshot.roles)
            or staged["core_edges"] != len(snapshot.core_edges)
            or staged["concepts"] < len({row["concept_id"] for row in snapshot.core_edges})
            or staged["mapping_edges"] == 0
        ):
            raise ValueError(f"发布前图计数校验失败：{staged}")

        self.query(
            """
            MATCH (run:NormalizationRun {run_id:$run_id})
            MERGE (pointer:NormalizationPointer {name:'core'})
            OPTIONAL MATCH (pointer)-[old:ACTIVE]->(previous:NormalizationRun)
            DELETE old
            WITH pointer, run, collect(previous) AS previous_runs
            MERGE (pointer)-[:ACTIVE]->(run)
            SET run.status='ACTIVE', run.activated_at=$activated_at,
                run.mapped_edges=$mapped_edges
            FOREACH (previous IN previous_runs |
                SET previous.status =
                    CASE WHEN previous.run_id = run.run_id THEN 'ACTIVE' ELSE 'ARCHIVED' END
            )
            """,
            {
                "run_id": snapshot.run_id,
                "activated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "mapped_edges": mapped_edges,
            },
            write=True,
        )
        return {
            "run_id": snapshot.run_id,
            "status": "ACTIVE",
            "expected": snapshot.expected,
            "graph": self.verify(snapshot.run_id),
        }

    def verify(self, run_id: str) -> dict[str, int]:
        rows = self.query(
            """
            MATCH (run:NormalizationRun {run_id:$run_id})
            CALL (run) {
                MATCH (role:Role)-[edge:HAS_CORE_SKILL {run_id:run.run_id}]->(:NormalizedSkill)
                RETURN count(DISTINCT role) AS roles, count(edge) AS core_edges
            }
            CALL (run) {
                MATCH (:Role)-[:HAS_CORE_SKILL {run_id:run.run_id}]->(skill:NormalizedSkill)
                RETURN count(DISTINCT skill) AS concepts
            }
            CALL (run) {
                MATCH (:AbilityCandidate)-[edge:NORMALIZES_TO {run_id:run.run_id}]->(:NormalizedSkill)
                RETURN count(edge) AS mapping_edges
            }
            RETURN roles, core_edges, concepts, mapping_edges
            """,
            {"run_id": run_id},
        )
        return {key: int(value or 0) for key, value in (rows[0] if rows else {}).items()}

    def rollback(self, run_id: str) -> dict[str, Any]:
        counts = self.verify(run_id)
        if not counts["core_edges"]:
            raise ValueError(f"目标归一化版本不存在或没有核心技能边：{run_id}")
        self.query(
            """
            MATCH (run:NormalizationRun {run_id:$run_id})
            MERGE (pointer:NormalizationPointer {name:'core'})
            OPTIONAL MATCH (pointer)-[old:ACTIVE]->(previous:NormalizationRun)
            DELETE old
            WITH pointer, run, collect(previous) AS previous_runs
            MERGE (pointer)-[:ACTIVE]->(run)
            SET run.status='ACTIVE', run.activated_at=$activated_at
            FOREACH (previous IN previous_runs |
                SET previous.status =
                    CASE WHEN previous.run_id = run.run_id THEN 'ACTIVE' ELSE 'ARCHIVED' END
            )
            """,
            {
                "run_id": run_id,
                "activated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            write=True,
        )
        return {"run_id": run_id, "status": "ACTIVE", "graph": counts}

    def deactivate(self) -> dict[str, Any]:
        rows = self.query(
            """
            MATCH (pointer:NormalizationPointer {name:'core'})
                  -[active:ACTIVE]->(run:NormalizationRun)
            DELETE active
            SET run.status='ARCHIVED', run.deactivated_at=$deactivated_at
            RETURN run.run_id AS archived_run_id
            """,
            {
                "deactivated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            write=True,
        )
        return {
            "status": "LEGACY_FALLBACK",
            "archived_run_id": rows[0].get("archived_run_id") if rows else "",
        }

    def status(self) -> dict[str, Any]:
        rows = self.query(
            """
            OPTIONAL MATCH (:NormalizationPointer {name:'core'})-[:ACTIVE]->(run:NormalizationRun)
            RETURN run.run_id AS run_id, run.status AS status,
                   run.activated_at AS activated_at, run.created_at AS created_at
            """
        )
        active = rows[0] if rows else {}
        run_id = active.get("run_id") or ""
        return {"active": active, "graph": self.verify(run_id) if run_id else {}}


def main() -> None:
    parser = argparse.ArgumentParser(description="版本化发布全量岗位技能归一化快照")
    parser.add_argument(
        "--database",
        type=Path,
        default=PROJECT_ROOT / "output" / "processed_normalization_full" / "knowledge_graph.db",
    )
    parser.add_argument(
        "--normalization-dir",
        type=Path,
        default=PROJECT_ROOT / "output" / "processed_normalization_full" / "skill_reports",
    )
    parser.add_argument(
        "--neo4j-config",
        type=Path,
        default=PROJECT_ROOT / "config" / "neo4j_connection.json",
    )
    parser.add_argument("--batch-size", type=int, default=500)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--publish", action="store_true")
    mode.add_argument("--status", action="store_true")
    mode.add_argument("--rollback", default="")
    mode.add_argument("--deactivate", action="store_true")
    args = parser.parse_args()

    repository = Neo4jGraphRepository(args.neo4j_config)
    publisher = NormalizationPublisher(repository, args.batch_size)
    if args.status:
        result = publisher.status()
    elif args.deactivate:
        result = publisher.deactivate()
    elif args.rollback:
        result = publisher.rollback(args.rollback)
    else:
        snapshot = load_snapshot(args.database, args.normalization_dir)
        result = publisher.publish(snapshot) if args.publish else publisher.inspect(snapshot)
        manifest = args.normalization_dir / "publish_manifest.json"
        manifest.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

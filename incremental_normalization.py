from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from trusted_graph_agent.neo4j_repository import Neo4jGraphRepository  # noqa: E402
from trusted_graph_agent.normalization_experiment import (  # noqa: E402
    HashingTextEmbedder,
    NormalizationConfig,
    SentenceTransformerEmbedder,
    lexical_similarity,
    merge_allowed,
)
from trusted_graph_agent.text_utils import normalize_text, stable_id  # noqa: E402


ALGORITHM_VERSION = "incremental-normalization-v1"
CONFIG_PATH = PROJECT_ROOT / "trusted_graph_agent" / "normalization_config_v5.json"
MODEL_PATH = (
    PROJECT_ROOT / "models" / "hf_cache" / "hub"
    / "models--BAAI--bge-small-zh-v1.5" / "snapshots"
    / "7999e1d3359715c523056ef9478215996d62a620"
)
ACCEPTED_EVIDENCE = {"VERIFIED", "LOW_CONFIDENCE", "ANALYSIS_ONLY"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def chunks(rows: list[dict[str, Any]], size: int = 500):
    for index in range(0, len(rows), size):
        yield rows[index : index + size]


def build_embedder(config: NormalizationConfig):
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    if MODEL_PATH.exists():
        try:
            return (
                SentenceTransformerEmbedder(str(MODEL_PATH), config.embedding_batch_size, "cpu"),
                "sentence_transformer",
            )
        except RuntimeError as error:
            print(f"warning=semantic_runtime_unavailable fallback=hashing detail={error}", flush=True)
    return HashingTextEmbedder(), "deterministic_hashing"


class IncrementalNormalizationPublisher:
    def __init__(self, repository: Neo4jGraphRepository, config: NormalizationConfig):
        self.client = repository.client
        self.config = config

    def query(self, statement: str, parameters: dict[str, Any] | None = None, *, write: bool = False):
        return self.client.query(statement, parameters, access_mode="Write" if write else "Read")

    def active_run(self) -> str:
        rows = self.query(
            "MATCH (:NormalizationPointer {name:'core'})-[:ACTIVE]->(run:NormalizationRun) "
            "WHERE run.status='ACTIVE' RETURN run.run_id AS run_id LIMIT 1"
        )
        return str(rows[0].get("run_id") or "") if rows else ""

    def affected_roles(self, ingest_run_ids: list[str]) -> list[dict[str, str]]:
        rows = self.query(
            """
            MATCH (:RawJob)-[:CURRENT_VERSION]->(raw:RawJDVersion)
                  -[:HAS_PROCESSING_RESULT]->(:ProcessedJD {status:'COMPLETED'})
            WHERE raw.last_ingest_run_id IN $ingest_run_ids
              AND raw.domain_label='IT' AND coalesce(raw.domain_role,'')<>''
            RETURN DISTINCT raw.domain_role AS role_name
            ORDER BY role_name
            """,
            {"ingest_run_ids": ingest_run_ids},
        )
        return [
            {"role_name": str(row["role_name"]), "role_id": stable_id("role", str(row["role_name"]))}
            for row in rows
        ]

    def stage(self, active: str, run_id: str, ingest_run_ids: list[str], roles: list[dict[str, str]]) -> None:
        now = utc_now()
        self.query(
            """
            MERGE (run:NormalizationRun {run_id:$run_id})
            SET run.status='STAGING',run.created_at=$now,run.source_run_id=$active,
                run.algorithm_version=$algorithm,run.mode='INCREMENTAL',
                run.ingest_run_ids=$ingest_run_ids,run.affected_roles=$affected_roles
            """,
            {
                "run_id": run_id,
                "now": now,
                "active": active,
                "algorithm": ALGORITHM_VERSION,
                "ingest_run_ids": ingest_run_ids,
                "affected_roles": [row["role_id"] for row in roles],
            },
            write=True,
        )
        self.query(
            """
            MATCH (ability:AbilityCandidate)-[:NORMALIZES_TO {run_id:$active}]->(skill:NormalizedSkill)
            MERGE (ability)-[edge:NORMALIZES_TO {run_id:$run_id}]->(skill)
            SET edge.published_at=$now,edge.source_run_id=$active
            """,
            {"active": active, "run_id": run_id, "now": now},
            write=True,
        )
        self.query(
            """
            MATCH (role:Role)-[old:HAS_CORE_SKILL {run_id:$active}]->(skill:NormalizedSkill)
            MERGE (role)-[edge:HAS_CORE_SKILL {run_id:$run_id}]->(skill)
            SET edge=properties(old),edge.run_id=$run_id,edge.source_run_id=$active,edge.published_at=$now
            """,
            {"active": active, "run_id": run_id, "now": now},
            write=True,
        )

    def map_new_abilities(self, run_id: str, ingest_run_ids: list[str]) -> dict[str, int | str]:
        abilities = self.query(
            """
            MATCH (raw:RawJDVersion)-[:HAS_PROCESSING_RESULT]->(processed:ProcessedJD {status:'COMPLETED'})
                  -[:HAS_ABILITY]->(ability:AbilityCandidate)
            WHERE raw.last_ingest_run_id IN $ingest_run_ids
              AND NOT (ability)-[:NORMALIZES_TO {run_id:$run_id}]->(:NormalizedSkill)
            RETURN DISTINCT ability.ability_id AS ability_id,ability.name AS name,
                   ability.normalized_name AS normalized_name,ability.category AS category,
                   ability.tech_stack AS tech_stack
            ORDER BY ability_id
            """,
            {"ingest_run_ids": ingest_run_ids, "run_id": run_id},
        )
        if not abilities:
            return {"abilities": 0, "new_concepts": 0, "embedding_backend": "not_needed"}
        concepts = self.query(
            "MATCH (skill:NormalizedSkill) RETURN skill.concept_id AS concept_id,"
            "skill.canonical_name AS canonical_name,skill.category AS category,"
            "skill.tech_stack AS tech_stack"
        )
        alias_rows = self.query(
            """
            MATCH (ability:AbilityCandidate)-[:NORMALIZES_TO {run_id:$run_id}]->(skill:NormalizedSkill)
            RETURN ability.normalized_name AS normalized_name,skill.concept_id AS concept_id
            """,
            {"run_id": run_id},
        )
        aliases = {
            normalize_text(str(row.get("normalized_name") or "")): str(row.get("concept_id") or "")
            for row in alias_rows
            if row.get("normalized_name") and row.get("concept_id")
        }
        concept_by_id = {str(row["concept_id"]): dict(row) for row in concepts}
        canonical = {
            (str(row.get("category") or ""), normalize_text(str(row.get("canonical_name") or ""))): str(row["concept_id"])
            for row in concepts
        }
        embedder, backend = build_embedder(self.config)
        concept_vectors = embedder.encode([str(row.get("canonical_name") or "") for row in concepts]) if concepts else None
        ability_vectors = embedder.encode([str(row.get("name") or "") for row in abilities])
        mappings: list[dict[str, Any]] = []
        new_concepts: list[dict[str, Any]] = []
        for index, ability in enumerate(abilities):
            name = str(ability.get("name") or ability.get("normalized_name") or "").strip()
            normalized = normalize_text(str(ability.get("normalized_name") or name))
            category = str(ability.get("category") or "其他能力")
            concept_id = aliases.get(normalized) or canonical.get((category, normalized), "")
            if not concept_id and concepts:
                similarities = concept_vectors @ ability_vectors[index]
                for candidate_index in similarities.argsort()[::-1][: self.config.nearest_neighbor_top_k]:
                    concept = concepts[int(candidate_index)]
                    if str(concept.get("category") or "") != category:
                        continue
                    candidate_name = str(concept.get("canonical_name") or "")
                    lexical = lexical_similarity(name, candidate_name)
                    combined = 0.9 * float(similarities[int(candidate_index)]) + 0.1 * lexical
                    if combined >= self.config.auto_merge_score and merge_allowed(
                        name, candidate_name, lexical, self.config.minimum_lexical_guard
                    ):
                        concept_id = str(concept["concept_id"])
                        break
            if not concept_id:
                concept_id = stable_id("normalized_skill", category, name)
                if concept_id not in concept_by_id:
                    concept = {
                        "concept_id": concept_id,
                        "canonical_name": name,
                        "category": category,
                        "tech_stack": (
                            ""
                            if str(ability.get("tech_stack") or "") == "待审核"
                            else str(ability.get("tech_stack") or "")
                        ),
                        "concept_status": "CANDIDATE",
                    }
                    concept_by_id[concept_id] = concept
                    new_concepts.append(concept)
            mappings.append({"ability_id": str(ability["ability_id"]), "concept_id": concept_id})
        for batch in chunks(new_concepts):
            self.query(
                """
                UNWIND $rows AS row MERGE (skill:NormalizedSkill {concept_id:row.concept_id})
                SET skill.canonical_name=row.canonical_name,skill.category=row.category,
                    skill.tech_stack=CASE WHEN coalesce(row.tech_stack, '') <> ''
                                         THEN row.tech_stack
                                         ELSE coalesce(skill.tech_stack, '') END,
                    skill.concept_status=row.concept_status,skill.snapshot_run_id=$run_id
                """,
                {"rows": batch, "run_id": run_id},
                write=True,
            )
        for batch in chunks(mappings):
            self.query(
                """
                UNWIND $rows AS row MATCH (ability:AbilityCandidate {ability_id:row.ability_id})
                MATCH (skill:NormalizedSkill {concept_id:row.concept_id})
                MERGE (ability)-[edge:NORMALIZES_TO {run_id:$run_id}]->(skill)
                SET edge.published_at=$now
                """,
                {"rows": batch, "run_id": run_id, "now": utc_now()},
                write=True,
            )
        self.query(
            """
            MATCH (skill:NormalizedSkill)
            WHERE coalesce(skill.tech_stack, '') = ''
            MATCH (ability:AbilityCandidate)-[:NORMALIZES_TO {run_id:$run_id}]->(skill)
            WHERE coalesce(ability.tech_stack, '') <> ''
              AND ability.tech_stack <> '待审核'
            WITH skill, ability.tech_stack AS tech_stack, count(*) AS mentions
            ORDER BY skill.concept_id, mentions DESC, tech_stack
            WITH skill, collect(tech_stack)[0] AS tech_stack
            SET skill.tech_stack = tech_stack
            """,
            {"run_id": run_id},
            write=True,
        )
        return {"abilities": len(mappings), "new_concepts": len(new_concepts), "embedding_backend": backend}

    def recompute_roles(self, run_id: str, roles: list[dict[str, str]]) -> int:
        if not roles:
            return 0
        role_count_rows = self.query(
            """
            MATCH (:RawJob)-[:CURRENT_VERSION]->(raw:RawJDVersion)
                  -[:HAS_PROCESSING_RESULT]->(:ProcessedJD {status:'COMPLETED'})
            WHERE raw.domain_label='IT' AND coalesce(raw.domain_role,'')<>''
            RETURN count(DISTINCT raw.domain_role) AS role_count
            """
        )
        role_count = max(1, int(role_count_rows[0].get("role_count") or 1))
        output: list[dict[str, Any]] = []
        concept_names: list[str] = []
        candidate_rows: list[dict[str, Any]] = []
        for role in roles:
            totals = self.query(
                """
                MATCH (role:Role {role_id:$role_id})
                MATCH (:RawJob)-[:CURRENT_VERSION]->(raw:RawJDVersion)
                      -[:HAS_PROCESSING_RESULT]->(:ProcessedJD {status:'COMPLETED'})
                WHERE raw.domain_label='IT' AND raw.domain_role=$role_name
                RETURN count(DISTINCT raw.version_id) AS jd_total,
                       count(DISTINCT raw.company_id) AS company_total,
                       toInteger(coalesce(role.document_count,0)) AS baseline_jd_total
                """,
                {"role_id": role["role_id"], "role_name": role["role_name"]},
            )[0]
            jd_total = int(totals.get("jd_total") or 0)
            company_total = int(totals.get("company_total") or 0)
            baseline_jd_total = int(totals.get("baseline_jd_total") or 0)
            # The production graph can intentionally retain only a lightweight
            # evidence delta while Role counts describe the full published
            # corpus.  Replacing its baseline core edges from that partial
            # sample would silently erase valid skills.  New ability mappings
            # are still published immediately; the weekly full calibration
            # performs the authoritative role-level weight recomputation.
            if baseline_jd_total > jd_total:
                continue
            rows = self.query(
                """
                MATCH (:RawJob)-[:CURRENT_VERSION]->(raw:RawJDVersion)
                      -[:HAS_PROCESSING_RESULT]->(processed:ProcessedJD {status:'COMPLETED'})
                      -[mention:HAS_ABILITY]->(ability:AbilityCandidate)
                      -[:NORMALIZES_TO {run_id:$run_id}]->(skill:NormalizedSkill)
                WHERE raw.domain_label='IT' AND raw.domain_role=$role_name
                  AND mention.evidence_status IN ['VERIFIED','LOW_CONFIDENCE','ANALYSIS_ONLY']
                RETURN skill.concept_id AS concept_id,skill.canonical_name AS canonical_name,
                       skill.category AS category,count(DISTINCT raw.version_id) AS jd_count,
                       count(DISTINCT raw.company_id) AS company_count,
                       count(DISTINCT CASE WHEN mention.evidence_status='VERIFIED' THEN raw.version_id END) AS verified_jd_count,
                       avg(CASE WHEN mention.requirement_type='required' THEN 1.0 ELSE 0.4 END) AS requirement_score,
                       avg(toFloat(mention.confidence)) AS evidence_score
                """,
                {"run_id": run_id, "role_name": role["role_name"]},
            )
            for row in rows:
                candidate = {**row, **role, "jd_total": jd_total, "company_total": company_total}
                candidate_rows.append(candidate)
                concept_names.append(str(row.get("canonical_name") or ""))
        if not candidate_rows:
            return 0
        concept_ids = list({str(row["concept_id"]) for row in candidate_rows})
        distinct_rows = self.query(
            """
            UNWIND $concept_ids AS concept_id
            MATCH (:RawJob)-[:CURRENT_VERSION]->(raw:RawJDVersion)
                  -[:HAS_PROCESSING_RESULT]->(:ProcessedJD {status:'COMPLETED'})
                  -[:HAS_ABILITY]->(:AbilityCandidate)-[:NORMALIZES_TO {run_id:$run_id}]
                  ->(skill:NormalizedSkill {concept_id:concept_id})
            WHERE raw.domain_label='IT'
            RETURN concept_id,count(DISTINCT raw.domain_role) AS roles_with_skill
            """,
            {"concept_ids": concept_ids, "run_id": run_id},
        )
        roles_by_skill = {str(row["concept_id"]): int(row.get("roles_with_skill") or 1) for row in distinct_rows}
        scored_by_role_category: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for row in candidate_rows:
            jd_total = max(1, int(row["jd_total"]))
            company_total = max(1, int(row["company_total"]))
            if jd_total < self.config.small_role_cutoff:
                min_jds = max(3, math.ceil(jd_total * self.config.small_core_ratio))
                min_companies = max(2, math.ceil(company_total * self.config.small_core_ratio))
            else:
                min_jds = max(self.config.core_min_jds, math.ceil(jd_total * self.config.core_jd_ratio))
                min_companies = max(self.config.core_min_companies, math.ceil(company_total * self.config.core_company_ratio))
            verified_min = min(self.config.minimum_verified_jd_count, min_jds)
            if (
                int(row.get("jd_count") or 0) < min_jds
                or int(row.get("company_count") or 0) < min_companies
                or int(row.get("verified_jd_count") or 0) < verified_min
            ):
                continue
            roles_with_skill = max(1, roles_by_skill.get(str(row["concept_id"]), 1))
            distinctiveness = 0.0 if role_count <= 1 else math.log(role_count / roles_with_skill) / math.log(role_count)
            score = (
                self.config.company_coverage_weight * int(row["company_count"]) / company_total
                + self.config.jd_coverage_weight * int(row["jd_count"]) / jd_total
                + self.config.role_distinctiveness_weight * distinctiveness
                + self.config.requirement_weight * float(row.get("requirement_score") or 0.0)
                + self.config.evidence_weight * float(row.get("evidence_score") or 0.0)
                + self.config.time_weight
            )
            row = {**row, "final_score": round(score, 6)}
            scored_by_role_category[(str(row["role_id"]), str(row.get("category") or "其他能力"))].append(row)
        embedder, _ = build_embedder(self.config)
        for (role_id, category), candidates in scored_by_role_category.items():
            candidates.sort(key=lambda item: (-float(item["final_score"]), str(item["canonical_name"])))
            vectors = embedder.encode([str(item["canonical_name"]) for item in candidates])
            chosen: list[int] = []
            quota = int(self.config.top_k.get(category, 2))
            while len(chosen) < quota:
                eligible = [index for index in range(len(candidates)) if index not in chosen]
                if not eligible:
                    break
                def mmr(index: int) -> float:
                    redundancy = max((float(vectors[index] @ vectors[old]) for old in chosen), default=0.0)
                    return self.config.mmr_relevance_weight * float(candidates[index]["final_score"]) - (1 - self.config.mmr_relevance_weight) * redundancy
                chosen.append(max(eligible, key=mmr))
            for category_rank, index in enumerate(chosen, 1):
                row = candidates[index]
                output.append(
                    {
                        "role_id": role_id,
                        "concept_id": str(row["concept_id"]),
                        "final_score": float(row["final_score"]),
                        "company_count": int(row["company_count"]),
                        "jd_count": int(row["jd_count"]),
                        "verified_jd_count": int(row["verified_jd_count"]),
                        "category_rank": category_rank,
                    }
                )
        role_ids = sorted({row["role_id"] for row in output})
        self.query(
            "MATCH (role:Role)-[edge:HAS_CORE_SKILL {run_id:$run_id}]->() "
            "WHERE role.role_id IN $role_ids DELETE edge",
            {"run_id": run_id, "role_ids": role_ids},
            write=True,
        )
        by_role: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in output:
            by_role[row["role_id"]].append(row)
        ranked: list[dict[str, Any]] = []
        for role_id, rows in by_role.items():
            for rank, row in enumerate(sorted(rows, key=lambda item: -float(item["final_score"])), 1):
                ranked.append({**row, "rank": rank})
        for batch in chunks(ranked):
            self.query(
                """
                UNWIND $rows AS row MATCH (role:Role {role_id:row.role_id})
                MATCH (skill:NormalizedSkill {concept_id:row.concept_id})
                MERGE (role)-[edge:HAS_CORE_SKILL {run_id:$run_id}]->(skill)
                SET edge.final_score=row.final_score,edge.company_count=row.company_count,
                    edge.jd_count=row.jd_count,edge.verified_jd_count=row.verified_jd_count,
                    edge.rank=row.rank,edge.category_rank=row.category_rank,edge.published_at=$now
                """,
                {"rows": batch, "run_id": run_id, "now": utc_now()},
                write=True,
            )
        return len(ranked)

    def verify_and_activate(self, run_id: str, active: str) -> dict[str, Any]:
        rows = self.query(
            """
            CALL { MATCH (:Role)-[edge:HAS_CORE_SKILL {run_id:$run_id}]->(:NormalizedSkill) RETURN count(edge) AS core_edges }
            CALL { MATCH (:AbilityCandidate)-[edge:NORMALIZES_TO {run_id:$run_id}]->(:NormalizedSkill) RETURN count(edge) AS mapping_edges }
            RETURN core_edges,mapping_edges
            """,
            {"run_id": run_id},
        )
        counts = rows[0] if rows else {}
        if int(counts.get("core_edges") or 0) < 1 or int(counts.get("mapping_edges") or 0) < 1:
            raise ValueError(f"增量归一化发布校验失败，活动版本保持不变：{counts}")
        self.query(
            """
            MATCH (run:NormalizationRun {run_id:$run_id})
            MERGE (pointer:NormalizationPointer {name:'core'})
            WITH pointer,run
            OPTIONAL MATCH (pointer)-[old:ACTIVE]->(previous:NormalizationRun)
            DELETE old
            WITH pointer,run,collect(previous) AS previous_runs
            MERGE (pointer)-[:ACTIVE]->(run)
            SET run.status='ACTIVE',run.activated_at=$now,
                run.actual_core_edges=$core_edges,run.mapped_edges=$mapping_edges
            FOREACH (previous IN previous_runs |
                SET previous.status=CASE WHEN previous.run_id=$run_id THEN 'ACTIVE' ELSE 'ARCHIVED' END)
            """,
            {"run_id": run_id, "now": utc_now(), **counts},
            write=True,
        )
        return {"status": "ACTIVE", "run_id": run_id, "previous_run_id": active, **counts}

    def run(self, ingest_run_ids: list[str], *, publish: bool) -> dict[str, Any]:
        ingest_run_ids = sorted(dict.fromkeys(ingest_run_ids))
        previous = self.query(
            """
            MATCH (run:NormalizationRun {algorithm_version:$algorithm,status:'ACTIVE'})
            WHERE run.ingest_run_ids=$ingest_run_ids
            RETURN run.run_id AS run_id LIMIT 1
            """,
            {"algorithm": ALGORITHM_VERSION, "ingest_run_ids": ingest_run_ids},
        )
        if previous:
            return {"status": "ALREADY_ACTIVE", "run_id": str(previous[0]["run_id"])}
        active = self.active_run()
        if not active:
            return {"status": "FULL_REBUILD_REQUIRED", "reason": "没有活动归一化基线"}
        roles = self.affected_roles(ingest_run_ids)
        if not roles:
            return {"status": "NO_CHANGES", "ingest_run_ids": ingest_run_ids}
        digest = hashlib.sha256(
            "|".join([active, *sorted(ingest_run_ids), ALGORITHM_VERSION]).encode("utf-8")
        ).hexdigest()[:20]
        run_id = f"normalization:inc:{digest}"
        if run_id == active:
            return {"status": "ALREADY_ACTIVE", "run_id": run_id}
        if not publish:
            return {
                "status": "READY",
                "run_id": run_id,
                "previous_run_id": active,
                "affected_roles": roles,
                "publish": False,
            }
        self.stage(active, run_id, ingest_run_ids, roles)
        mapping = self.map_new_abilities(run_id, ingest_run_ids)
        updated_edges = self.recompute_roles(run_id, roles)
        result = self.verify_and_activate(run_id, active)
        return {**result, "affected_roles": roles, "updated_core_edges": updated_edges, "mapping": mapping}


def main() -> None:
    parser = argparse.ArgumentParser(description="对新增JD执行能力归一化和受影响岗位局部重算")
    parser.add_argument("--neo4j-config", type=Path, default=PROJECT_ROOT / "config" / "neo4j_connection.json")
    parser.add_argument("--ingest-run-id", action="append", default=[])
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--config", type=Path, default=CONFIG_PATH)
    args = parser.parse_args()
    ingest_run_ids = list(dict.fromkeys(value.strip() for value in args.ingest_run_id if value.strip()))
    if not ingest_run_ids:
        parser.error("至少需要一个 --ingest-run-id")
    publisher = IncrementalNormalizationPublisher(
        Neo4jGraphRepository(args.neo4j_config.resolve()),
        NormalizationConfig.load(args.config.resolve()),
    )
    print(json.dumps(publisher.run(ingest_run_ids, publish=args.publish), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

"""Versioned human-review workspace stored beside the production Neo4j graph.

Discovery never mutates :Role or its production relationships.  This module
only owns labels prefixed by the role-evolution workflow and links immutable
snapshots back to their supporting raw JD versions.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from trusted_graph_agent.neo4j_repository import Neo4jGraphRepository


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _chunks(values: list[dict[str, Any]], size: int = 100) -> Iterable[list[dict[str, Any]]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


class EvolutionReviewRepository:
    """Persist candidate snapshots and immutable expert decisions in Neo4j."""

    def __init__(self, config_path: Path, *, client: Any | None = None) -> None:
        self.client = (
            client
            if client is not None
            else Neo4jGraphRepository(Path(config_path)).client
        )
        self._schema_ready = False

    def ensure_schema(self) -> None:
        if self._schema_ready:
            return
        statements = (
            "CREATE CONSTRAINT evolution_run_id IF NOT EXISTS "
            "FOR (n:EvolutionRun) REQUIRE n.run_id IS UNIQUE",
            "CREATE CONSTRAINT role_concept_candidate_id IF NOT EXISTS "
            "FOR (n:RoleConceptCandidate) REQUIRE n.candidate_id IS UNIQUE",
            "CREATE CONSTRAINT candidate_snapshot_id IF NOT EXISTS "
            "FOR (n:CandidateSnapshot) REQUIRE n.snapshot_id IS UNIQUE",
            "CREATE CONSTRAINT role_definition_version_id IF NOT EXISTS "
            "FOR (n:RoleDefinitionVersion) REQUIRE n.definition_version_id IS UNIQUE",
            "CREATE CONSTRAINT review_decision_id IF NOT EXISTS "
            "FOR (n:ReviewDecision) REQUIRE n.review_id IS UNIQUE",
            "CREATE CONSTRAINT evolution_source_family_id IF NOT EXISTS "
            "FOR (n:SourceFamily) REQUIRE n.source_family_id IS UNIQUE",
        )
        for statement in statements:
            self.client.query(statement, access_mode="Write")
        self._schema_ready = True

    def health(self) -> dict[str, Any]:
        rows = self.client.query(
            """
            CALL {
                MATCH (n:EvolutionRun)
                RETURN count(n) AS runs
            }
            CALL {
                MATCH (n:RoleConceptCandidate)
                RETURN count(n) AS candidates
            }
            CALL {
                MATCH (n:CandidateSnapshot)
                RETURN count(n) AS snapshots
            }
            CALL {
                MATCH (n:RoleDefinitionVersion)
                RETURN count(n) AS definition_versions
            }
            CALL {
                MATCH (n:ReviewDecision)
                RETURN count(n) AS review_decisions
            }
            RETURN runs, candidates, snapshots,
                   definition_versions, review_decisions
            """,
            access_mode="Read",
        )
        counts = dict(rows[0]) if rows else {}
        return {
            "backend": "neo4j",
            "mode": "versioned_review_subgraph",
            "available": True,
            "counts": {
                key: int(counts.get(key) or 0)
                for key in (
                    "runs",
                    "candidates",
                    "snapshots",
                    "definition_versions",
                    "review_decisions",
                )
            },
        }

    def persist_run(
        self,
        *,
        task_id: str,
        run_id: str,
        manifest: dict[str, Any],
        candidates: list[dict[str, Any]],
    ) -> None:
        self.ensure_schema()
        source = manifest.get("source") or {}
        self.client.query(
            """
            MERGE (run:EvolutionRun {run_id:$run_id})
            ON CREATE SET run.created_at = $created_at
            SET run.task_id = $task_id,
                run.state = 'REVIEW_READY',
                run.started_at = $started_at,
                run.completed_at = $completed_at,
                run.source_fingerprint = $source_fingerprint,
                run.normalization_run_id = $normalization_run_id,
                run.parameters_json = $parameters_json,
                run.summary_json = $summary_json
            """,
            {
                "run_id": run_id,
                "task_id": task_id,
                "created_at": _now(),
                "started_at": str(manifest.get("started_at") or ""),
                "completed_at": str(manifest.get("completed_at") or ""),
                "source_fingerprint": str(source.get("fingerprint") or ""),
                "normalization_run_id": str(
                    source.get("active_normalization_run_id") or ""
                ),
                "parameters_json": json.dumps(
                    manifest.get("config") or {},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "summary_json": json.dumps(
                    manifest.get("summary") or {},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            },
            access_mode="Write",
        )

        prepared: list[dict[str, Any]] = []
        for candidate in candidates:
            required = candidate.get("required_skill_draft") or []
            bonus = candidate.get("bonus_skill_draft") or []
            responsibilities = candidate.get("responsibility_evidence") or []
            industries = candidate.get("typical_industries") or []
            sources = candidate.get("source_distribution") or []
            nearest = (candidate.get("nearest_existing_roles") or [{}])[0]
            evidence_jd_ids = sorted(
                {
                    str(item.get("jd_id") or "")
                    for item in responsibilities
                    if str(item.get("jd_id") or "")
                }
                | {
                    str(evidence.get("jd_id") or "")
                    for skill in candidate.get("candidate_skills_detail") or []
                    for evidence in skill.get("evidence") or []
                    if str(evidence.get("jd_id") or "")
                }
            )
            candidate_id = str(candidate["candidate_id"])
            snapshot_id = f"{run_id}:{candidate_id}"
            prepared.append(
                {
                    "candidate_id": candidate_id,
                    "snapshot_id": snapshot_id,
                    "definition_version_id": f"{snapshot_id}:definition:0",
                    "candidate_title": str(
                        candidate.get("candidate_title") or ""
                    ),
                    "concept_key": str(candidate.get("concept_key") or ""),
                    "rule_state": str(candidate.get("rule_state") or ""),
                    "lifecycle_state": {
                        "REVIEW": "REVIEW_READY",
                        "WATCH": "WATCHING",
                        "AUTO_REJECT": "AUTO_REJECTED",
                    }.get(
                        str(candidate.get("rule_state") or ""),
                        "DISCOVERED",
                    ),
                    "confirmation_state": str(
                        candidate.get("confirmation_state") or ""
                    ),
                    "emergence_score": float(
                        candidate.get("emergence_score") or 0.0
                    ),
                    "continuous_windows": int(
                        candidate.get("continuous_windows") or 0
                    ),
                    "growth_windows": int(
                        candidate.get("growth_windows") or 0
                    ),
                    "independent_source_count": int(
                        candidate.get("independent_source_count") or 0
                    ),
                    "current_jd_count": int(
                        candidate.get("current_jd_count") or 0
                    ),
                    "current_company_count": int(
                        candidate.get("current_company_count") or 0
                    ),
                    "current_template_count": int(
                        candidate.get("current_template_count") or 0
                    ),
                    "monthly_trend_json": json.dumps(
                        candidate.get("monthly_trend") or [],
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    "rule_reasons": [
                        str(value)
                        for value in candidate.get("rule_reasons") or []
                    ],
                    "raw_title_variants": [
                        str(item.get("title") or "")
                        for item in candidate.get("raw_title_variants") or []
                        if str(item.get("title") or "")
                    ],
                    "name": str(candidate.get("candidate_title") or ""),
                    "responsibilities": [
                        str(item.get("text") or "")
                        for item in responsibilities
                        if str(item.get("text") or "")
                    ],
                    "required_skill_ids": [
                        str(item.get("skill_id") or "")
                        for item in required
                        if str(item.get("skill_id") or "")
                    ],
                    "required_skills": [
                        str(item.get("skill") or "")
                        for item in required
                        if str(item.get("skill") or "")
                    ],
                    "bonus_skill_ids": [
                        str(item.get("skill_id") or "")
                        for item in bonus
                        if str(item.get("skill_id") or "")
                    ],
                    "bonus_skills": [
                        str(item.get("skill") or "")
                        for item in bonus
                        if str(item.get("skill") or "")
                    ],
                    "industries": [
                        str(item.get("industry") or "")
                        for item in industries
                        if str(item.get("industry") or "")
                    ],
                    "sources": [
                        {
                            "source_family_id": str(
                                item.get("source_family") or "UNKNOWN"
                            ),
                            "company_count": int(
                                item.get("company_count") or 0
                            ),
                            "jd_count": int(item.get("jd_count") or 0),
                        }
                        for item in sources
                    ],
                    "evidence_jd_ids": evidence_jd_ids,
                    "nearest_role": str(nearest.get("role") or ""),
                    "nearest_similarity": float(
                        nearest.get("composite_similarity")
                        or nearest.get("weighted_skill_jaccard")
                        or 0.0
                    ),
                }
            )

        for chunk in _chunks(prepared):
            self.client.query(
                """
                MATCH (run:EvolutionRun {run_id:$run_id})
                UNWIND $rows AS row
                MERGE (candidate:RoleConceptCandidate {
                    candidate_id:row.candidate_id
                })
                ON CREATE SET candidate.first_seen_at = $now,
                              candidate.current_version = 0,
                              candidate.current_state = 'DISCOVERED'
                SET candidate.latest_title = row.candidate_title,
                    candidate.concept_key = row.concept_key,
                    candidate.latest_run_id = $run_id,
                    candidate.latest_snapshot_id = row.snapshot_id,
                    candidate.current_state =
                        CASE
                        WHEN coalesce(candidate.current_version, 0) = 0
                        THEN row.lifecycle_state
                        ELSE candidate.current_state
                        END,
                    candidate.updated_at = $now
                MERGE (snapshot:CandidateSnapshot {
                    snapshot_id:row.snapshot_id
                })
                SET snapshot.run_id = $run_id,
                    snapshot.candidate_title = row.candidate_title,
                    snapshot.rule_state = row.rule_state,
                    snapshot.confirmation_state = row.confirmation_state,
                    snapshot.emergence_score = row.emergence_score,
                    snapshot.continuous_windows = row.continuous_windows,
                    snapshot.growth_windows = row.growth_windows,
                    snapshot.independent_source_count = row.independent_source_count,
                    snapshot.current_jd_count = row.current_jd_count,
                    snapshot.current_company_count = row.current_company_count,
                    snapshot.current_template_count = row.current_template_count,
                    snapshot.monthly_trend_json = row.monthly_trend_json,
                    snapshot.rule_reasons = row.rule_reasons,
                    snapshot.raw_title_variants = row.raw_title_variants,
                    snapshot.nearest_role = row.nearest_role,
                    snapshot.nearest_similarity = row.nearest_similarity,
                    snapshot.created_at = $now
                MERGE (run)-[:PRODUCED]->(snapshot)
                MERGE (candidate)-[:HAS_SNAPSHOT]->(snapshot)
                MERGE (definition:RoleDefinitionVersion {
                    definition_version_id:row.definition_version_id
                })
                ON CREATE SET definition.version = 0,
                              definition.status = 'ALGORITHM_DRAFT',
                              definition.created_at = $now,
                              definition.created_by = 'ROLE_EVOLUTION_ENGINE'
                SET definition.name = row.name,
                    definition.core_responsibilities = row.responsibilities,
                    definition.required_skills = row.required_skills,
                    definition.required_skill_ids = row.required_skill_ids,
                    definition.bonus_skills = row.bonus_skills,
                    definition.bonus_skill_ids = row.bonus_skill_ids,
                    definition.industry_scenarios = row.industries,
                    definition.source_snapshot_id = row.snapshot_id
                MERGE (candidate)-[:HAS_DEFINITION_VERSION]->(definition)
                MERGE (snapshot)-[:HAS_DEFINITION_DRAFT]->(definition)
                """,
                {"run_id": run_id, "rows": chunk, "now": _now()},
                access_mode="Write",
            )
            self.client.query(
                """
                UNWIND $rows AS row
                MATCH (snapshot:CandidateSnapshot {
                    snapshot_id:row.snapshot_id
                })
                UNWIND row.sources AS source
                MERGE (family:SourceFamily {
                    source_family_id:source.source_family_id
                })
                ON CREATE SET family.name = source.source_family_id
                MERGE (snapshot)-[edge:OBSERVED_IN]->(family)
                SET edge.company_count = source.company_count,
                    edge.jd_count = source.jd_count
                """,
                {"rows": chunk},
                access_mode="Write",
            )
            self.client.query(
                """
                UNWIND $rows AS row
                MATCH (snapshot:CandidateSnapshot {
                    snapshot_id:row.snapshot_id
                })
                UNWIND row.evidence_jd_ids AS jd_id
                MATCH (raw:RawJDVersion {version_id:jd_id})
                MERGE (snapshot)-[:SUPPORTED_BY]->(raw)
                """,
                {"rows": chunk},
                access_mode="Write",
            )
            self.client.query(
                """
                UNWIND $rows AS row
                WITH row WHERE row.nearest_role <> ''
                MATCH (snapshot:CandidateSnapshot {
                    snapshot_id:row.snapshot_id
                })
                MATCH (role:Role {name:row.nearest_role})
                MERGE (snapshot)-[edge:NEAREST_TO]->(role)
                SET edge.similarity = row.nearest_similarity
                """,
                {"rows": chunk},
                access_mode="Write",
            )

    def load_reviews(self, candidate_ids: list[str]) -> dict[str, dict[str, Any]]:
        if not candidate_ids:
            return {}
        self.ensure_schema()
        rows = self.client.query(
            """
            UNWIND $candidate_ids AS candidate_id
            OPTIONAL MATCH (candidate:RoleConceptCandidate {
                candidate_id:candidate_id
            })
            OPTIONAL MATCH (candidate)-[:HAS_DEFINITION_VERSION]->(definition)
            WITH candidate_id, candidate, definition
            ORDER BY definition.version DESC
            WITH candidate_id, candidate, head(collect(definition)) AS latest
            OPTIONAL MATCH (candidate)-[:HAS_REVIEW]->(review:ReviewDecision)
            WITH candidate_id, candidate, latest, review
            ORDER BY review.created_at DESC
            RETURN candidate_id,
                   coalesce(candidate.current_version, 0) AS candidate_version,
                   coalesce(candidate.current_state, 'DISCOVERED') AS current_state,
                   CASE WHEN latest IS NULL THEN {} ELSE properties(latest) END
                       AS definition,
                   collect(CASE WHEN review IS NULL THEN null
                                ELSE properties(review) END)[0..20]
                       AS review_history
            """,
            {"candidate_ids": candidate_ids},
            access_mode="Read",
        )
        return {
            str(row["candidate_id"]): {
                "candidate_version": int(row.get("candidate_version") or 0),
                "current_state": str(row.get("current_state") or "DISCOVERED"),
                "definition": dict(row.get("definition") or {}),
                "review_history": [
                    dict(item)
                    for item in row.get("review_history") or []
                    if item
                ],
            }
            for row in rows
        }

    def save_review(
        self,
        *,
        candidate_id: str,
        run_id: str,
        expected_version: int,
        decision: str,
        state: str,
        reviewer: str,
        comment: str,
        definition: dict[str, Any],
    ) -> dict[str, Any] | None:
        self.ensure_schema()
        next_version = expected_version + 1
        review_id = f"review:{uuid.uuid4().hex}"
        definition_version_id = (
            f"{candidate_id}:definition:{next_version}:{uuid.uuid4().hex[:8]}"
        )
        rows = self.client.query(
            """
            MATCH (candidate:RoleConceptCandidate {
                candidate_id:$candidate_id
            })
            WHERE coalesce(candidate.current_version, 0) = $expected_version
            CREATE (review:ReviewDecision {
                review_id:$review_id,
                run_id:$run_id,
                candidate_version:$next_version,
                decision:$decision,
                reviewer:$reviewer,
                comment:$comment,
                created_at:$now
            })
            CREATE (definition:RoleDefinitionVersion {
                definition_version_id:$definition_version_id,
                version:$next_version,
                status:'HUMAN_REVIEWED',
                name:$name,
                parent_role_id:$parent_role_id,
                core_responsibilities:$responsibilities,
                required_skills:$required_skills,
                bonus_skills:$bonus_skills,
                industry_scenarios:$industries,
                expert_supplied_fields:$expert_supplied_fields,
                created_by:$reviewer,
                created_at:$now,
                source_run_id:$run_id
            })
            MERGE (candidate)-[:HAS_REVIEW]->(review)
            MERGE (candidate)-[:HAS_DEFINITION_VERSION]->(definition)
            MERGE (review)-[:DECIDED_DEFINITION]->(definition)
            SET candidate.current_version = $next_version,
                candidate.current_state = $state,
                candidate.updated_at = $now,
                candidate.latest_definition_version_id =
                    $definition_version_id
            RETURN candidate.current_version AS candidate_version,
                   candidate.current_state AS current_state,
                   properties(definition) AS definition,
                   properties(review) AS review
            """,
            {
                "candidate_id": candidate_id,
                "run_id": run_id,
                "expected_version": expected_version,
                "next_version": next_version,
                "review_id": review_id,
                "definition_version_id": definition_version_id,
                "decision": decision,
                "state": state,
                "reviewer": reviewer,
                "comment": comment,
                "name": str(definition.get("name") or ""),
                "parent_role_id": str(
                    definition.get("parent_role_id") or ""
                ),
                "responsibilities": list(
                    definition.get("core_responsibilities") or []
                ),
                "required_skills": list(
                    definition.get("required_skills") or []
                ),
                "bonus_skills": list(
                    definition.get("bonus_skills") or []
                ),
                "industries": list(
                    definition.get("industry_scenarios") or []
                ),
                "expert_supplied_fields": list(
                    definition.get("expert_supplied_fields") or []
                ),
                "reviewer": reviewer,
                "now": _now(),
            },
            access_mode="Write",
        )
        return dict(rows[0]) if rows else None


__all__ = ["EvolutionReviewRepository"]

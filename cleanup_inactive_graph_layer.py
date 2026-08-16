from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trusted_graph_agent.neo4j_repository import Neo4jGraphRepository


PROJECT_ROOT = Path(__file__).resolve().parent


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class InactiveGraphCleanup:
    def __init__(self, config_path: Path, batch_size: int) -> None:
        self.client = Neo4jGraphRepository(config_path).client
        self.batch_size = max(100, min(batch_size, 5000))

    def query(
        self,
        statement: str,
        parameters: dict[str, Any] | None = None,
        *,
        write: bool = False,
    ) -> list[dict[str, Any]]:
        return self.client.query(
            statement,
            parameters or {},
            access_mode="Write" if write else "Read",
        )

    def active_run_id(self) -> str:
        rows = self.query(
            """
            MATCH (:NormalizationPointer {name:'core'})-[:ACTIVE]->(run:NormalizationRun)
            WHERE run.status = 'ACTIVE'
            RETURN run.run_id AS run_id
            """
        )
        run_id = str((rows[0] if rows else {}).get("run_id") or "")
        if not run_id:
            raise RuntimeError("No active normalization run; refusing cleanup.")
        return run_id

    def snapshot(self) -> dict[str, int | str]:
        run_id = self.active_run_id()
        rows = self.query(
            """
            CALL { MATCH (n) RETURN count(n) AS nodes }
            CALL { MATCH ()-[r]->() RETURN count(r) AS relationships }
            CALL { MATCH (r:Role) RETURN count(r) AS roles }
            CALL {
              MATCH (r:Role)-[:HAS_CORE_SKILL {run_id:$run_id}]->()
              RETURN count(DISTINCT r) AS active_roles
            }
            CALL { MATCH (n:JD) RETURN count(n) AS jds }
            CALL { MATCH (n:RoleProfile) RETURN count(n) AS role_profiles }
            CALL { MATCH (n:SkillSnapshot) RETURN count(n) AS skill_snapshots }
            CALL { MATCH (n:RoleFamily) RETURN count(n) AS role_families }
            RETURN nodes, relationships, roles, active_roles, jds,
                   role_profiles, skill_snapshots, role_families
            """,
            {"run_id": run_id},
        )
        return {"active_run_id": run_id, **(rows[0] if rows else {})}

    def preflight(self) -> dict[str, int | str]:
        run_id = self.active_run_id()
        rows = self.query(
            """
            CALL {
              MATCH (role:Role)
              WHERE NOT EXISTS {
                MATCH (role)-[:HAS_CORE_SKILL {run_id:$run_id}]->()
              }
              RETURN count(role) AS stale_roles
            }
            CALL {
              MATCH (jd:JD)-[:INSTANCE_OF]->(role:Role)
              WHERE NOT EXISTS {
                MATCH (role)-[:HAS_CORE_SKILL {run_id:$run_id}]->()
              }
                AND NOT EXISTS {
                  MATCH (jd)-[:INSTANCE_OF]->(active:Role)
                  WHERE EXISTS {
                    MATCH (active)-[:HAS_CORE_SKILL {run_id:$run_id}]->()
                  }
                }
              RETURN count(DISTINCT jd) AS stale_only_jds
            }
            CALL {
              MATCH (family:RoleFamily)-[edge:HAS_ROLE]->(role:Role)
              WHERE EXISTS {
                MATCH (role)-[:HAS_CORE_SKILL {run_id:$run_id}]->()
              }
                AND family.family_id <> coalesce(role.family_id, '')
              RETURN count(edge) AS mismatched_active_family_edges
            }
            CALL {
              MATCH (role:Role)-[:HAS_PROFILE]->(node:RoleProfile)
              WHERE NOT EXISTS {
                MATCH (role)-[:HAS_CORE_SKILL {run_id:$run_id}]->()
              }
              RETURN count(DISTINCT node) AS stale_profiles
            }
            CALL {
              MATCH (role:Role)-[:HAS_SKILL_SNAPSHOT]->(node:SkillSnapshot)
              WHERE NOT EXISTS {
                MATCH (role)-[:HAS_CORE_SKILL {run_id:$run_id}]->()
              }
              RETURN count(DISTINCT node) AS stale_skill_snapshots
            }
            CALL {
              MATCH (node:RoleAlias)-[:ALIAS_OF]->(role:Role)
              WHERE NOT EXISTS {
                MATCH (role)-[:HAS_CORE_SKILL {run_id:$run_id}]->()
              }
              RETURN count(DISTINCT node) AS stale_aliases
            }
            CALL {
              MATCH (node:CandidateSnapshot)-[:NEAREST_TO]->(role:Role)
              WHERE NOT EXISTS {
                MATCH (role)-[:HAS_CORE_SKILL {run_id:$run_id}]->()
              }
              RETURN count(DISTINCT node) AS stale_candidate_snapshots
            }
            RETURN stale_roles, stale_only_jds, mismatched_active_family_edges,
                   stale_profiles, stale_skill_snapshots, stale_aliases,
                   stale_candidate_snapshots
            """,
            {"run_id": run_id},
        )
        return {"active_run_id": run_id, **(rows[0] if rows else {})}

    def delete_batches(self, statement: str, metric: str, run_id: str) -> int:
        total = 0
        while True:
            rows = self.query(
                statement,
                {"run_id": run_id, "batch_size": self.batch_size},
                write=True,
            )
            deleted = int((rows[0] if rows else {}).get("count") or 0)
            if not deleted:
                break
            total += deleted
            print(f"{metric}={total}", flush=True)
        return total

    def execute(self) -> dict[str, Any]:
        run_id = self.active_run_id()
        before = self.snapshot()
        preflight = self.preflight()
        metrics: dict[str, int] = {}

        metrics["deleted_stale_jds"] = self.delete_batches(
            """
            MATCH (jd:JD)-[:INSTANCE_OF]->(role:Role)
            WHERE NOT EXISTS {
              MATCH (role)-[:HAS_CORE_SKILL {run_id:$run_id}]->()
            }
              AND NOT EXISTS {
                MATCH (jd)-[:INSTANCE_OF]->(active:Role)
                WHERE EXISTS {
                  MATCH (active)-[:HAS_CORE_SKILL {run_id:$run_id}]->()
                }
              }
            WITH DISTINCT jd LIMIT $batch_size
            DETACH DELETE jd
            RETURN count(*) AS count
            """,
            "deleted_stale_jds",
            run_id,
        )

        child_queries = {
            "deleted_stale_profiles": """
                MATCH (role:Role)-[:HAS_PROFILE]->(node:RoleProfile)
                WHERE NOT EXISTS {
                  MATCH (role)-[:HAS_CORE_SKILL {run_id:$run_id}]->()
                }
                WITH DISTINCT node LIMIT $batch_size
                DETACH DELETE node RETURN count(*) AS count
            """,
            "deleted_stale_skill_snapshots": """
                MATCH (role:Role)-[:HAS_SKILL_SNAPSHOT]->(node:SkillSnapshot)
                WHERE NOT EXISTS {
                  MATCH (role)-[:HAS_CORE_SKILL {run_id:$run_id}]->()
                }
                WITH DISTINCT node LIMIT $batch_size
                DETACH DELETE node RETURN count(*) AS count
            """,
            "deleted_stale_aliases": """
                MATCH (node:RoleAlias)-[:ALIAS_OF]->(role:Role)
                WHERE NOT EXISTS {
                  MATCH (role)-[:HAS_CORE_SKILL {run_id:$run_id}]->()
                }
                WITH DISTINCT node LIMIT $batch_size
                DETACH DELETE node RETURN count(*) AS count
            """,
            "deleted_stale_candidate_snapshots": """
                MATCH (node:CandidateSnapshot)-[:NEAREST_TO]->(role:Role)
                WHERE NOT EXISTS {
                  MATCH (role)-[:HAS_CORE_SKILL {run_id:$run_id}]->()
                }
                WITH DISTINCT node LIMIT $batch_size
                DETACH DELETE node RETURN count(*) AS count
            """,
        }
        for metric, statement in child_queries.items():
            metrics[metric] = self.delete_batches(statement, metric, run_id)

        metrics["deleted_stale_roles"] = self.delete_batches(
            """
            MATCH (role:Role)
            WHERE NOT EXISTS {
              MATCH (role)-[:HAS_CORE_SKILL {run_id:$run_id}]->()
            }
            WITH role LIMIT $batch_size
            DETACH DELETE role
            RETURN count(*) AS count
            """,
            "deleted_stale_roles",
            run_id,
        )

        rows = self.query(
            """
            MATCH (family:RoleFamily)-[edge:HAS_ROLE]->(role:Role)
            WHERE EXISTS {
              MATCH (role)-[:HAS_CORE_SKILL {run_id:$run_id}]->()
            }
              AND family.family_id <> coalesce(role.family_id, '')
            DELETE edge
            RETURN count(edge) AS count
            """,
            {"run_id": run_id},
            write=True,
        )
        metrics["deleted_mismatched_family_edges"] = int(
            (rows[0] if rows else {}).get("count") or 0
        )

        orphan_queries = {
            "deleted_orphan_role_families": """
                MATCH (node:RoleFamily)
                WHERE NOT (node)-[:HAS_ROLE]->(:Role)
                DETACH DELETE node RETURN count(*) AS count
            """,
            "deleted_orphan_domains": """
                MATCH (node:Domain)
                WHERE NOT (node)-[:HAS_FAMILY]->(:RoleFamily)
                DETACH DELETE node RETURN count(*) AS count
            """,
            "deleted_isolated_companies": """
                MATCH (node:Company) WHERE NOT (node)--()
                DELETE node RETURN count(*) AS count
            """,
            "deleted_isolated_skills": """
                MATCH (node:Skill) WHERE NOT (node)--()
                DELETE node RETURN count(*) AS count
            """,
        }
        for metric, statement in orphan_queries.items():
            rows = self.query(statement, write=True)
            metrics[metric] = int((rows[0] if rows else {}).get("count") or 0)

        return {
            "started_at": utc_now(),
            "finished_at": utc_now(),
            "policy": {
                "keep_only_roles_with_active_core_edges": True,
                "delete_jds_linked_only_to_inactive_roles": True,
                "delete_inactive_role_artifacts": True,
                "delete_mismatched_family_edges": True,
                "delete_safe_orphans": True,
            },
            "preflight": preflight,
            "metrics": metrics,
            "before": before,
            "after": self.snapshot(),
        }


def main() -> None:
    parser = argparse.ArgumentParser(description="Delete inactive role-layer graph artifacts.")
    parser.add_argument(
        "--neo4j-config",
        type=Path,
        default=PROJECT_ROOT / "config" / "neo4j_connection.json",
    )
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--report",
        type=Path,
        default=PROJECT_ROOT / "output" / "cleanup" / "inactive_graph_cleanup.json",
    )
    args = parser.parse_args()
    cleanup = InactiveGraphCleanup(args.neo4j_config.resolve(), args.batch_size)
    if not args.execute:
        payload = {"snapshot": cleanup.snapshot(), "preflight": cleanup.preflight()}
    else:
        payload = cleanup.execute()
        report_path = args.report.resolve()
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        payload["report"] = str(report_path)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

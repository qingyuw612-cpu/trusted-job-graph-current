from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from trusted_graph_agent.neo4j_repository import Neo4jGraphRepository


PROJECT_ROOT = Path(__file__).resolve().parent


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RetentionCleanup:
    def __init__(self, config_path: Path, batch_size: int) -> None:
        self.client = Neo4jGraphRepository(config_path).client
        self.batch_size = max(100, min(batch_size, 5000))
        self.metrics: dict[str, int] = {}

    def query(
        self,
        statement: str,
        parameters: dict[str, Any] | None = None,
        write: bool = False,
    ) -> list[dict]:
        return self.client.query(
            statement,
            parameters or {},
            access_mode="Write" if write else "Read",
        )

    def count(self, statement: str) -> int:
        rows = self.query(statement)
        return int((rows[0] if rows else {}).get("count") or 0)

    def snapshot(self) -> dict[str, Any]:
        counts = self.query(
            """
            CALL { MATCH (n) RETURN count(n) AS nodes }
            CALL { MATCH ()-[r]->() RETURN count(r) AS relationships }
            CALL { MATCH (n:RawJob) RETURN count(n) AS raw_jobs }
            CALL { MATCH (n:RawJDVersion) RETURN count(n) AS raw_versions }
            CALL { MATCH (n:ProcessedJD) RETURN count(n) AS processed_jds }
            CALL { MATCH (n:AbilityCandidate) RETURN count(n) AS ability_candidates }
            CALL { MATCH ()-[r:HAS_ABILITY]->() RETURN count(r) AS ability_mentions }
            RETURN nodes, relationships, raw_jobs, raw_versions,
                   processed_jds, ability_candidates, ability_mentions
            """
        )
        domains = self.query(
            """
            MATCH (:RawJob)-[:CURRENT_VERSION]->(raw:RawJDVersion)
            RETURN coalesce(raw.domain_label, 'UNCLASSIFIED') AS state,
                   count(raw) AS count
            ORDER BY count DESC
            """
        )
        active = self.query(
            """
            OPTIONAL MATCH (:NormalizationPointer {name:'core'})-[:ACTIVE]->
                           (run:NormalizationRun)
            RETURN run.run_id AS run_id, run.status AS status
            """
        )
        return {
            **(counts[0] if counts else {}),
            "domains": domains,
            "active_normalization": active[0] if active else {},
        }

    def preflight(self) -> dict[str, int]:
        return {
            "non_it_jobs": self.count(
                "MATCH (:RawJob)-[:CURRENT_VERSION]->"
                "(:RawJDVersion {domain_label:'NON_IT'}) RETURN count(*) AS count"
            ),
            "uncertain_jobs": self.count(
                "MATCH (:RawJob)-[:CURRENT_VERSION]->"
                "(:RawJDVersion {domain_label:'UNCERTAIN'}) RETURN count(*) AS count"
            ),
            "uncertain_processed": self.count(
                "MATCH (:RawJob)-[:CURRENT_VERSION]->"
                "(raw:RawJDVersion {domain_label:'UNCERTAIN'})-"
                "[:HAS_PROCESSING_RESULT]->(:ProcessedJD) RETURN count(*) AS count"
            ),
            "historical_versions": self.count(
                "MATCH (raw:RawJDVersion) "
                "WHERE NOT EXISTS { (:RawJob)-[:CURRENT_VERSION]->(raw) } "
                "RETURN count(raw) AS count"
            ),
            "orphan_ability_candidates": self.count(
                "MATCH (ability:AbilityCandidate) "
                "WHERE NOT (ability)<-[:HAS_ABILITY]-() RETURN count(ability) AS count"
            ),
            "archived_normalization_runs": self.count(
                "MATCH (run:NormalizationRun) WHERE run.status <> 'ACTIVE' "
                "RETURN count(run) AS count"
            ),
        }

    def delete_non_it_jobs(self) -> int:
        total = 0
        while True:
            rows = self.query(
                """
                MATCH (job:RawJob)-[:CURRENT_VERSION]->
                      (:RawJDVersion {domain_label:'NON_IT'})
                RETURN job.raw_uid AS raw_uid
                LIMIT $batch_size
                """,
                {"batch_size": self.batch_size},
            )
            raw_uids = [str(row["raw_uid"]) for row in rows if row.get("raw_uid")]
            if not raw_uids:
                break
            deleted = self.query(
                """
                UNWIND $raw_uids AS raw_uid
                MATCH (job:RawJob {raw_uid:raw_uid})
                OPTIONAL MATCH (job)-[:HAS_VERSION]->(raw:RawJDVersion)
                OPTIONAL MATCH (raw)-[:HAS_PROCESSING_RESULT]->(processed:ProcessedJD)
                DETACH DELETE processed, raw, job
                RETURN count(DISTINCT raw_uid) AS count
                """,
                {"raw_uids": raw_uids},
                write=True,
            )
            total += int((deleted[0] if deleted else {}).get("count") or 0)
            print(f"deleted_non_it_jobs={total}", flush=True)
        return total

    def delete_uncertain_processing(self) -> int:
        total = 0
        while True:
            rows = self.query(
                """
                MATCH (:RawJob)-[:CURRENT_VERSION]->
                      (raw:RawJDVersion {domain_label:'UNCERTAIN'})-
                      [:HAS_PROCESSING_RESULT]->(processed:ProcessedJD)
                RETURN processed.version_id AS version_id
                LIMIT $batch_size
                """,
                {"batch_size": self.batch_size},
            )
            version_ids = [
                str(row["version_id"]) for row in rows if row.get("version_id")
            ]
            if not version_ids:
                break
            deleted = self.query(
                """
                UNWIND $version_ids AS version_id
                MATCH (processed:ProcessedJD {version_id:version_id})
                DETACH DELETE processed
                RETURN count(*) AS count
                """,
                {"version_ids": version_ids},
                write=True,
            )
            total += int((deleted[0] if deleted else {}).get("count") or 0)
            print(f"deleted_uncertain_processing={total}", flush=True)
        return total

    def delete_uncertain_jobs(self) -> int:
        total = 0
        while True:
            rows = self.query(
                """
                MATCH (job:RawJob)-[:CURRENT_VERSION]->
                      (:RawJDVersion {domain_label:'UNCERTAIN'})
                RETURN job.raw_uid AS raw_uid
                LIMIT $batch_size
                """,
                {"batch_size": self.batch_size},
            )
            raw_uids = [str(row["raw_uid"]) for row in rows if row.get("raw_uid")]
            if not raw_uids:
                break
            deleted = self.query(
                """
                UNWIND $raw_uids AS raw_uid
                MATCH (job:RawJob {raw_uid:raw_uid})
                OPTIONAL MATCH (job)-[:HAS_VERSION]->(raw:RawJDVersion)
                OPTIONAL MATCH (raw)-[:HAS_PROCESSING_RESULT]->(processed:ProcessedJD)
                DETACH DELETE processed, raw, job
                RETURN count(DISTINCT raw_uid) AS count
                """,
                {"raw_uids": raw_uids},
                write=True,
            )
            total += int((deleted[0] if deleted else {}).get("count") or 0)
            print(f"deleted_uncertain_jobs={total}", flush=True)
        return total

    def delete_historical_versions(self) -> int:
        total = 0
        while True:
            rows = self.query(
                """
                MATCH (raw:RawJDVersion)
                WHERE NOT EXISTS { (:RawJob)-[:CURRENT_VERSION]->(raw) }
                RETURN raw.version_id AS version_id
                LIMIT $batch_size
                """,
                {"batch_size": self.batch_size},
            )
            version_ids = [
                str(row["version_id"]) for row in rows if row.get("version_id")
            ]
            if not version_ids:
                break
            deleted = self.query(
                """
                UNWIND $version_ids AS version_id
                MATCH (raw:RawJDVersion {version_id:version_id})
                OPTIONAL MATCH (raw)-[:HAS_PROCESSING_RESULT]->(processed:ProcessedJD)
                DETACH DELETE processed, raw
                RETURN count(DISTINCT version_id) AS count
                """,
                {"version_ids": version_ids},
                write=True,
            )
            total += int((deleted[0] if deleted else {}).get("count") or 0)
            print(f"deleted_historical_versions={total}", flush=True)
        return total

    def delete_orphan_abilities(self) -> int:
        total = 0
        while True:
            rows = self.query(
                """
                MATCH (ability:AbilityCandidate)
                WHERE NOT (ability)<-[:HAS_ABILITY]-()
                WITH ability LIMIT $batch_size
                DETACH DELETE ability
                RETURN count(*) AS count
                """,
                {"batch_size": self.batch_size},
                write=True,
            )
            deleted = int((rows[0] if rows else {}).get("count") or 0)
            if not deleted:
                break
            total += deleted
            print(f"deleted_orphan_abilities={total}", flush=True)
        return total

    def delete_archived_normalization(self) -> dict[str, int | str]:
        active_rows = self.query(
            """
            MATCH (:NormalizationPointer {name:'core'})-[:ACTIVE]->
                  (run:NormalizationRun)
            RETURN run.run_id AS run_id
            """
        )
        active_run_id = str((active_rows[0] if active_rows else {}).get("run_id") or "")
        if not active_run_id:
            raise RuntimeError("没有活动归一化版本，拒绝清理旧版本")
        core = self.query(
            """
            MATCH ()-[edge:HAS_CORE_SKILL]->()
            WHERE coalesce(edge.run_id, '') <> $active_run_id
            DELETE edge
            RETURN count(edge) AS count
            """,
            {"active_run_id": active_run_id},
            write=True,
        )
        mappings = self.query(
            """
            MATCH ()-[edge:NORMALIZES_TO]->()
            WHERE coalesce(edge.run_id, '') <> $active_run_id
            DELETE edge
            RETURN count(edge) AS count
            """,
            {"active_run_id": active_run_id},
            write=True,
        )
        runs = self.query(
            """
            MATCH (run:NormalizationRun)
            WHERE run.run_id <> $active_run_id
            DETACH DELETE run
            RETURN count(run) AS count
            """,
            {"active_run_id": active_run_id},
            write=True,
        )
        return {
            "active_run_id": active_run_id,
            "deleted_core_edges": int((core[0] if core else {}).get("count") or 0),
            "deleted_mapping_edges": int(
                (mappings[0] if mappings else {}).get("count") or 0
            ),
            "deleted_runs": int((runs[0] if runs else {}).get("count") or 0),
        }

    def execute(self) -> dict[str, Any]:
        started = time.monotonic()
        before = self.snapshot()
        preflight = self.preflight()
        self.metrics["deleted_non_it_jobs"] = self.delete_non_it_jobs()
        self.metrics["deleted_uncertain_jobs"] = self.delete_uncertain_jobs()
        self.metrics["deleted_historical_versions"] = (
            self.delete_historical_versions()
        )
        normalization = self.delete_archived_normalization()
        self.metrics["deleted_orphan_abilities"] = self.delete_orphan_abilities()
        after = self.snapshot()
        return {
            "started_at": utc_now(),
            "finished_at": utc_now(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "policy": {
                "delete_confirmed_non_it": True,
                "delete_uncertain_jobs_and_all_versions": True,
                "delete_non_current_versions": True,
                "keep_active_normalization_only": True,
                "delete_orphan_ability_candidates": True,
            },
            "preflight": preflight,
            "metrics": self.metrics,
            "normalization": normalization,
            "before": before,
            "after": after,
        }

    def execute_non_it_only(self) -> dict[str, Any]:
        started_at = utc_now()
        started = time.monotonic()
        before = self.snapshot()
        non_it_jobs = self.count(
            "MATCH (:RawJob)-[:CURRENT_VERSION]->"
            "(:RawJDVersion {domain_label:'NON_IT'}) RETURN count(*) AS count"
        )
        self.metrics["deleted_non_it_jobs"] = self.delete_non_it_jobs()
        self.metrics["deleted_orphan_abilities"] = self.delete_orphan_abilities()
        after = self.snapshot()
        return {
            "started_at": started_at,
            "finished_at": utc_now(),
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "policy": {
                "scope": "CONFIRMED_NON_IT_ONLY",
                "delete_confirmed_non_it_jobs_and_all_versions": True,
                "delete_non_it_processing_and_ability_mentions": True,
                "delete_newly_orphaned_ability_candidates": True,
                "keep_uncertain": True,
                "keep_other_historical_versions": True,
                "keep_archived_normalization": True,
            },
            "preflight": {"non_it_jobs": non_it_jobs},
            "metrics": self.metrics,
            "before": before,
            "after": after,
        }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="按保留策略清理非 IT、无用派生结果、历史版本和旧归一化版本"
    )
    parser.add_argument(
        "--neo4j-config",
        type=Path,
        default=PROJECT_ROOT / "config" / "neo4j_connection.json",
    )
    parser.add_argument("--batch-size", type=int, default=2000)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument(
        "--scope",
        choices=("non-it", "aggressive"),
        default="non-it",
        help="non-it 仅删除确认非 IT；aggressive 还清理待确认派生、历史和旧版本",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=PROJECT_ROOT / "output" / "cleanup" / "retention_cleanup.json",
    )
    args = parser.parse_args()

    cleanup = RetentionCleanup(args.neo4j_config.resolve(), args.batch_size)
    if not args.execute:
        print(
            json.dumps(
                {"snapshot": cleanup.snapshot(), "preflight": cleanup.preflight()},
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    report = (
        cleanup.execute_non_it_only()
        if args.scope == "non-it"
        else cleanup.execute()
    )
    report_path = args.report.resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print(f"cleanup_report={report_path}", flush=True)


if __name__ == "__main__":
    main()

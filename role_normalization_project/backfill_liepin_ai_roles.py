"""备份并回写通过校验的猎聘大模型岗位映射。"""

from __future__ import annotations

import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(".")
OUTPUT = ROOT / "output" / "liepin_role_normalization"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from trusted_graph_agent.neo4j_repository import Neo4jGraphRepository  # noqa: E402
from trusted_graph_agent.text_utils import stable_id  # noqa: E402


MODEL_VERSION = "codex-role-normalization-liepin-2026-08-12"
BASE_RESOLVER_VERSION = "it-role-taxonomy:2.0.0:title-v2"

FETCH_BACKUP = """
UNWIND $ids AS version_id
MATCH (raw:RawJDVersion {version_id:version_id})
OPTIONAL MATCH (raw)-[:HAS_PROCESSING_RESULT]->(processed:ProcessedJD)
RETURN raw.version_id AS version_id,
       raw.domain_role AS domain_role,
       raw.domain_role_id AS domain_role_id,
       raw.standard_role_id AS standard_role_id,
       raw.domain_role_resolution AS domain_role_resolution,
       raw.domain_role_resolver_version AS domain_role_resolver_version,
       raw.domain_role_ai_model_version AS domain_role_ai_model_version,
       raw.domain_role_provenance AS domain_role_provenance,
       raw.domain_role_locked AS domain_role_locked,
       processed.domain_role AS processed_domain_role,
       processed.domain_role_resolver_version AS processed_domain_role_resolver_version
ORDER BY version_id
"""

WRITE_QUERY = """
UNWIND $rows AS row
MATCH (raw:RawJDVersion {version_id:row.version_id})
SET raw.domain_role = row.canonical_name,
    raw.domain_role_id = row.catalog_role_id,
    raw.standard_role_id = row.graph_role_id,
    raw.domain_role_resolution = row.decision,
    raw.domain_role_resolution_detail = row.provenance,
    raw.domain_role_resolver_version = $base_resolver_version,
    raw.domain_role_ai_model_version = $model_version,
    raw.domain_role_provenance = row.provenance,
    raw.domain_role_locked = true,
    raw.domain_role_resolved_at = $now
WITH raw, row
OPTIONAL MATCH (raw)-[:HAS_PROCESSING_RESULT]->(processed:ProcessedJD)
SET processed.domain_role = row.canonical_name,
    processed.domain_role_resolver_version = $base_resolver_version,
    processed.domain_role_ai_model_version = $model_version
RETURN count(raw) AS updated,
       count(processed) AS processed_updated
"""


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as stream:
        return list(csv.DictReader(stream))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fields = list(rows[0].keys()) if rows else ["version_id"]
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader(); writer.writerows(rows)


def chunks(rows: list[Any], size: int = 500) -> list[list[Any]]:
    return [rows[start:start + size] for start in range(0, len(rows), size)]


def main() -> int:
    validation = json.loads((OUTPUT / "prepublish_validation.json").read_text(encoding="utf-8"))
    if not validation.get("valid"):
        raise ValueError("发布前校验未通过，禁止回写")
    proposed = read_csv(OUTPUT / "proposed_role_assignments.csv")
    mapped = [row for row in proposed if row["assignment_status"] == "MAPPED"]
    if len(mapped) != int(validation["mapped"]):
        raise ValueError("拟回写记录数与校验报告不一致")
    repository = Neo4jGraphRepository(ROOT / "config" / "neo4j_connection.json")
    ids = [row["version_id"] for row in mapped]
    backup: list[dict[str, Any]] = []
    for batch in chunks(ids):
        backup.extend(repository.client.query(FETCH_BACKUP, {"ids": batch}))
    if len(backup) != len(mapped):
        raise ValueError(f"Neo4j 备份记录数异常：{len(backup)} != {len(mapped)}")
    backup_path = OUTPUT / "neo4j_role_backup_before_ai.csv"
    write_csv(backup_path, backup)

    payload = [
        {
            "version_id": row["version_id"],
            "catalog_role_id": row["role_id"],
            "graph_role_id": stable_id("role", row["canonical_name"]),
            "canonical_name": row["canonical_name"],
            "decision": row["decision"],
            "provenance": row["provenance"],
        }
        for row in mapped
    ]
    updated = processed_updated = 0
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for batch in chunks(payload):
        result = repository.client.query(
            WRITE_QUERY,
            {
                "rows": batch, "base_resolver_version": BASE_RESOLVER_VERSION,
                "model_version": MODEL_VERSION, "now": now,
            },
            access_mode="Write",
        )
        updated += int(result[0]["updated"]) if result else 0
        processed_updated += int(result[0]["processed_updated"]) if result else 0
        print(f"updated={updated}/{len(mapped)}", flush=True)
    if updated != len(mapped):
        raise ValueError(f"回写数量异常：{updated} != {len(mapped)}")
    verify = repository.client.query(
        """
        UNWIND $ids AS version_id
        MATCH (raw:RawJDVersion {version_id:version_id})
        RETURN count(raw) AS total,
               count(CASE WHEN raw.domain_role_locked = true THEN 1 END) AS locked,
               count(raw.domain_role) AS roles,
               count(raw.standard_role_id) AS standard_ids
        """,
        {"ids": ids},
    )[0]
    manifest = {
        "model_version": MODEL_VERSION, "updated": updated,
        "processed_updated": processed_updated, "verify": verify,
        "backup": str(backup_path), "rollback_available": True,
    }
    (OUTPUT / "neo4j_backfill_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

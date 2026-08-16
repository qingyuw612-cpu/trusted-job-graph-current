from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from trusted_graph_agent.neo4j_repository import Neo4jGraphRepository  # noqa: E402
from trusted_graph_agent.taxonomy import RoleTaxonomy  # noqa: E402


TAXONOMY_PATH = PROJECT_ROOT / "trusted_graph_agent" / "it_role_taxonomy.json"

FETCH_QUERY = """
MATCH (:RawJob)-[:CURRENT_VERSION]->(raw:RawJDVersion)
WHERE raw.domain_label = 'IT'
  AND coalesce(raw.domain_role_locked, false) = false
  AND coalesce(raw.domain_role_resolver_version, '') <> $resolver_version
RETURN raw.version_id AS version_id,
       raw.title AS title,
       raw.declared_role AS declared_role,
       raw.declared_role_trust AS declared_role_trust
LIMIT $batch_size
"""

WRITE_QUERY = """
UNWIND $rows AS row
MATCH (raw:RawJDVersion {version_id:row.version_id})
SET raw.domain_role = row.domain_role,
    raw.domain_role_resolution = row.resolution,
    raw.domain_role_resolver_version = $resolver_version,
    raw.domain_role_resolved_at = $now
WITH raw, row
OPTIONAL MATCH (raw)-[:HAS_PROCESSING_RESULT]->(processed:ProcessedJD)
SET processed.domain_role = row.domain_role,
    processed.domain_role_resolver_version = $resolver_version
RETURN count(raw) AS updated
"""


def resolve_domain_role(
    taxonomy: RoleTaxonomy,
    title: str,
    declared_role: str,
    declared_role_trust: str,
) -> dict | None:
    """Map actual titles first; search categories are evidence, not a trusted fallback."""
    fallback = "" if declared_role_trust == "SEARCH_CATEGORY" else declared_role
    return taxonomy.resolve_title(title, fallback)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="把通过 IT 准入的实际职位名映射到受控 IT 岗位分类表。"
    )
    parser.add_argument(
        "--neo4j-config",
        type=Path,
        default=PROJECT_ROOT / "config" / "neo4j_connection.json",
    )
    parser.add_argument("--batch-size", type=int, default=1000)
    args = parser.parse_args()

    repository = Neo4jGraphRepository(args.neo4j_config.resolve())
    taxonomy = RoleTaxonomy(TAXONOMY_PATH)
    resolver_version = f"it-role-taxonomy:{taxonomy.version}:title-v2"
    repository.client.query(
        "CREATE INDEX raw_domain_role IF NOT EXISTS "
        "FOR (raw:RawJDVersion) ON (raw.domain_role)",
        access_mode="Write",
    )
    mapped = 0
    unmapped = 0
    batch_size = max(1, min(args.batch_size, 2000))

    while True:
        rows = repository.client.query(
            FETCH_QUERY,
            {"resolver_version": resolver_version, "batch_size": batch_size},
        )
        if not rows:
            break
        output = []
        for row in rows:
            role = resolve_domain_role(
                taxonomy,
                str(row.get("title") or ""),
                str(row.get("declared_role") or ""),
                str(row.get("declared_role_trust") or ""),
            )
            role_name = str(role.get("role_name") or "") if role else ""
            resolution = "MAPPED" if role_name else "UNMAPPED"
            mapped += int(bool(role_name))
            unmapped += int(not role_name)
            output.append(
                {
                    "version_id": str(row.get("version_id") or ""),
                    "domain_role": role_name,
                    "resolution": resolution,
                }
            )
        repository.client.query(
            WRITE_QUERY,
            {
                "rows": output,
                "resolver_version": resolver_version,
                "now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            access_mode="Write",
        )
        print(f"role_mapped={mapped} role_unmapped={unmapped}", flush=True)

    print(
        f"ROLE_BACKFILL_COMPLETE mapped={mapped} unmapped={unmapped} "
        f"version={resolver_version}"
    )


if __name__ == "__main__":
    main()

"""Reclassify Neo4j Role nodes from the checked-in controlled taxonomy.

Dry-run is the default. ``--apply`` replaces obsolete HAS_ROLE relationships,
updates the denormalized Role family fields, and removes the old catch-all
family only when it is empty. The migration is deterministic and idempotent.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from trusted_graph_agent.neo4j_repository import Neo4jGraphRepository  # noqa: E402
from trusted_graph_agent.text_utils import normalize_text  # noqa: E402


def taxonomy_index() -> dict[str, dict[str, str]]:
    payload = json.loads(
        (PROJECT_ROOT / "trusted_graph_agent" / "it_role_taxonomy.json").read_text(
            encoding="utf-8"
        )
    )
    families = {row["family_id"]: row for row in payload["families"]}
    index: dict[str, dict[str, str]] = {}
    for role in payload["roles"]:
        family = families[role["family_id"]]
        target = {
            "family_id": family["family_id"],
            "family_name": family["family_name"],
            "domain_id": payload["domain"]["domain_id"],
            "domain_name": payload["domain"]["domain_name"],
        }
        values = [role["role_name"], *role.get("aliases", [])]
        values.extend(Path(source).stem for source in role.get("sources", []))
        for value in values:
            key = normalize_text(str(value))
            previous = index.setdefault(key, target)
            if previous != target:
                raise ValueError(f"岗位分类别名冲突：{value}")
    return index


def plan_updates(role_rows: list[dict]) -> tuple[list[dict[str, str]], list[str]]:
    index = taxonomy_index()
    updates: list[dict[str, str]] = []
    unresolved: list[str] = []
    for row in role_rows:
        role_name = str(row.get("role_name") or "").strip()
        target = index.get(normalize_text(role_name))
        if not target:
            unresolved.append(role_name or str(row.get("role_id") or "<unnamed>"))
            continue
        updates.append(
            {
                "role_id": str(row["role_id"]),
                "role_name": role_name,
                **target,
            }
        )
    return updates, sorted(set(unresolved))


def run(repository: Neo4jGraphRepository, *, apply: bool, report_path: Path | None = None) -> dict:
    role_rows = repository.client.query(
        """
        MATCH (role:Role)
        RETURN role.role_id AS role_id,
               coalesce(role.role_name, role.name) AS role_name,
               role.family_id AS current_family_id,
               role.family_name AS current_family_name,
               role.domain_id AS current_domain_id,
               role.domain_name AS current_domain_name
        ORDER BY role_id
        """
    )
    updates, unresolved = plan_updates(role_rows)
    before_counts: dict[str, int] = {}
    for row in role_rows:
        family_id = str(row.get("current_family_id") or "<missing>")
        before_counts[family_id] = before_counts.get(family_id, 0) + 1

    if apply and unresolved:
        raise ValueError(
            "岗位族迁移已拒绝执行；以下 Role 未进入受控分类表："
            + "、".join(unresolved)
        )

    if apply and updates:
        repository.client.query(
            """
            UNWIND $rows AS row
            MATCH (role:Role {role_id:row.role_id})
            MERGE (domain:Domain {domain_id:row.domain_id})
            SET domain.name=row.domain_name
            MERGE (family:RoleFamily {family_id:row.family_id})
            SET family.name=row.family_name,
                family.domain_id=row.domain_id,
                family.domain_name=row.domain_name
            MERGE (domain)-[:HAS_FAMILY]->(family)
            WITH row, role, family
            OPTIONAL MATCH (previous:RoleFamily)-[old:HAS_ROLE]->(role)
            WHERE previous.family_id <> family.family_id
            DELETE old
            WITH row, role, family
            MERGE (family)-[:HAS_ROLE]->(role)
            SET role.family_id=row.family_id,
                role.family_name=row.family_name,
                role.domain_id=row.domain_id,
                role.domain_name=row.domain_name,
                role.family_classified_at=$now,
                role.family_classification_source='it_role_taxonomy.json'
            """,
            {
                "rows": updates,
                "now": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            access_mode="Write",
        )
        repository.client.query(
            """
            MATCH (family:RoleFamily {family_id:'extended_roles'})
            WHERE NOT (family)-[:HAS_ROLE]->(:Role)
            DETACH DELETE family
            """,
            access_mode="Write",
        )

    validation_rows = repository.client.query(
        """
        CALL {
            MATCH (fallback:RoleFamily {family_id:'extended_roles'})
            RETURN count(fallback) AS fallback_family_nodes
        }
        MATCH (role:Role)
        OPTIONAL MATCH (family:RoleFamily)-[:HAS_ROLE]->(role)
        WITH role, fallback_family_nodes,
             [family_id IN collect(DISTINCT family.family_id)
              WHERE family_id IS NOT NULL] AS family_ids
        RETURN count(role) AS roles,
               sum(CASE WHEN size(family_ids)=1 THEN 1 ELSE 0 END) AS exactly_one_family,
               sum(CASE WHEN 'extended_roles' IN family_ids THEN 1 ELSE 0 END) AS fallback_roles,
               fallback_family_nodes,
               collect(CASE WHEN size(family_ids) <> 1
                            THEN {role_id:role.role_id, family_ids:family_ids}
                       END)[..20] AS violations
        """
    )
    validation = validation_rows[0] if validation_rows else {}
    if apply and (
        int(validation.get("roles") or 0) != len(role_rows)
        or int(validation.get("exactly_one_family") or 0) != len(role_rows)
        or int(validation.get("fallback_roles") or 0) != 0
        or int(validation.get("fallback_family_nodes") or 0) != 0
    ):
        raise ValueError(f"岗位族迁移后结构校验失败：{validation}")

    family_counts: dict[str, int] = {}
    for row in updates:
        family_counts[row["family_id"]] = family_counts.get(row["family_id"], 0) + 1
    current_by_id = {
        str(row.get("role_id") or ""): row
        for row in role_rows
    }
    changed_roles = [
        {
            "role_id": row["role_id"],
            "role_name": row["role_name"],
            "from_family_id": str(
                current_by_id.get(row["role_id"], {}).get("current_family_id") or ""
            ),
            "from_family_name": str(
                current_by_id.get(row["role_id"], {}).get("current_family_name") or ""
            ),
            "from_domain_id": str(
                current_by_id.get(row["role_id"], {}).get("current_domain_id") or ""
            ),
            "from_domain_name": str(
                current_by_id.get(row["role_id"], {}).get("current_domain_name") or ""
            ),
            "to_family_id": row["family_id"],
        }
        for row in updates
        if str(
            current_by_id.get(row["role_id"], {}).get("current_family_id") or ""
        ) != row["family_id"]
    ]
    result = {
        "status": "APPLIED" if apply else ("BLOCKED" if unresolved else "DRY_RUN"),
        "roles_seen": len(role_rows),
        "classified_roles": len(updates),
        "unresolved_roles": unresolved,
        "before_family_counts": dict(sorted(before_counts.items())),
        "target_family_counts": dict(sorted(family_counts.items())),
        "changed_roles": changed_roles,
        "graph_validation": validation,
    }
    if report_path:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--neo4j-config",
        type=Path,
        default=PROJECT_ROOT / "config" / "neo4j_connection.json",
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    result = run(
        Neo4jGraphRepository(args.neo4j_config.resolve()),
        apply=args.apply,
        report_path=args.report,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

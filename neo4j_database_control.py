from __future__ import annotations

import argparse
import json
from pathlib import Path

from trusted_graph_agent.neo4j_repository import Neo4jHttpClient


PROJECT_ROOT = Path(__file__).resolve().parent


def system_client(config_path: Path) -> Neo4jHttpClient:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    return Neo4jHttpClient(
        config["http_uri"],
        "system",
        config.get("username", "neo4j"),
        config["password"],
        timeout=max(120, int(config.get("timeout_seconds", 120))),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Safely stop, start, or inspect a Neo4j database.")
    parser.add_argument("action", choices=("status", "health", "indexes", "stop", "start"))
    parser.add_argument("--database", default="neo4j")
    parser.add_argument(
        "--neo4j-config",
        type=Path,
        default=PROJECT_ROOT / "config" / "neo4j_connection.json",
    )
    args = parser.parse_args()
    client = system_client(args.neo4j_config.resolve())
    database = args.database.replace("`", "``")
    if args.action == "indexes":
        config = json.loads(args.neo4j_config.resolve().read_text(encoding="utf-8"))
        database_client = Neo4jHttpClient(
            config["http_uri"],
            args.database,
            config.get("username", "neo4j"),
            config["password"],
            timeout=max(120, int(config.get("timeout_seconds", 120))),
        )
        rows = database_client.query(
            "SHOW INDEXES YIELD name, state, populationPercent "
            "RETURN state, count(*) AS index_count, "
            "min(populationPercent) AS min_population ORDER BY state"
        )
        print(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    if args.action == "health":
        config = json.loads(args.neo4j_config.resolve().read_text(encoding="utf-8"))
        database_client = Neo4jHttpClient(
            config["http_uri"],
            args.database,
            config.get("username", "neo4j"),
            config["password"],
            timeout=max(120, int(config.get("timeout_seconds", 120))),
        )
        rows = database_client.query(
            """
            MATCH (:NormalizationPointer {name:'core'})-[:ACTIVE]->(run:NormalizationRun)
            CALL {
              WITH run
              MATCH (role:Role)-[:HAS_CORE_SKILL {run_id:run.run_id}]->(:NormalizedSkill)
              RETURN count(DISTINCT role) AS active_roles
            }
            CALL {
              WITH run
              MATCH (:Role)-[:HAS_CORE_SKILL {run_id:run.run_id}]->(skill:NormalizedSkill)
              RETURN count(DISTINCT skill) AS active_skills
            }
            CALL {
              WITH run
              MATCH ()-[edge:HAS_CORE_SKILL {run_id:run.run_id}]->()
              RETURN count(edge) AS active_core_edges
            }
            RETURN run.run_id AS active_run_id, run.status AS status,
                   active_roles, active_skills, active_core_edges
            """
        )
        print(json.dumps(rows[0] if rows else {}, ensure_ascii=False, indent=2))
        return
    if args.action == "stop":
        client.query(f"STOP DATABASE `{database}` WAIT 120 SECONDS", access_mode="Write")
    elif args.action == "start":
        client.query(f"START DATABASE `{database}` WAIT 120 SECONDS", access_mode="Write")
    rows = client.query(
        "SHOW DATABASES YIELD name, currentStatus, requestedStatus, statusMessage "
        "WHERE name = $database RETURN name, currentStatus, requestedStatus, statusMessage",
        {"database": args.database},
    )
    print(json.dumps(rows[0] if rows else {}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

from __future__ import annotations

import argparse
from pathlib import Path

from trusted_graph_agent.neo4j_repository import Neo4jGraphRepository
from trusted_graph_agent.normalization_experiment import is_noise_phrase


def main() -> None:
    parser = argparse.ArgumentParser(description="清理 Neo4j 中误入能力点的模型说明文字")
    parser.add_argument(
        "--config",
        type=Path,
        default=Path("output/all_it_roles_knowledge_graph_v5_neo4j/neo4j_connection.json"),
    )
    args = parser.parse_args()

    if not args.config.exists():
        raise FileNotFoundError(f"找不到 Neo4j 配置文件：{args.config}")

    repository = Neo4jGraphRepository(args.config)
    rows = repository.client.query(
        "MATCH (s:NormalizedSkill) "
        "RETURN s.canonical_name AS canonical_name, s.skill_id AS skill_id"
    )
    names = sorted(
        {
            str(row.get("canonical_name") or "").strip()
            for row in rows
            if is_noise_phrase(str(row.get("canonical_name") or ""))
        }
    )

    if not names:
        print("CLEANUP_COMPLETE: 没有发现说明性废话能力点。")
        return

    repository.client.query(
        "MATCH (s:NormalizedSkill) "
        "WHERE s.canonical_name IN $names "
        "DETACH DELETE s",
        {"names": names},
        access_mode="Write",
    )
    print(f"CLEANUP_COMPLETE: 已删除 {len(names)} 个说明性废话能力点。")
    for name in names:
        print(f"- {name}")


if __name__ == "__main__":
    main()

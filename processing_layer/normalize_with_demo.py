from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path


PROJECT_ROOT = Path(".")
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from trusted_graph_agent.neo4j_repository import Neo4jGraphRepository  # noqa: E402
from trusted_graph_agent.normalization_experiment import (  # noqa: E402
    NormalizationConfig,
    NormalizationExperiment,
    SentenceTransformerEmbedder,
)
from trusted_graph_agent.taxonomy import RoleTaxonomy  # noqa: E402
from trusted_graph_agent.text_utils import stable_id  # noqa: E402


LOCAL_MODEL = (
    PROJECT_ROOT
    / "models"
    / "hf_cache"
    / "hub"
    / "models--BAAI--bge-small-zh-v1.5"
    / "snapshots"
    / "7999e1d3359715c523056ef9478215996d62a620"
)


FETCH_PAGE_QUERY = """
MATCH (processed:ProcessedJD)
USING INDEX processed:ProcessedJD(version_id)
WHERE processed.version_id > $cursor
  AND processed.status = 'COMPLETED'
MATCH (raw:RawJDVersion {version_id:processed.version_id})
MATCH (:RawJob)-[:CURRENT_VERSION]->(raw)
WHERE raw.domain_label = 'IT'
WITH processed, raw
ORDER BY processed.version_id
LIMIT $batch_size
MATCH (processed)-[mention:HAS_ABILITY]->(ability:AbilityCandidate)
WITH processed, raw, collect({
    skill_id: ability.ability_id,
    skill_name: ability.name,
    raw_term: mention.raw_term,
    requirement_type: mention.requirement_type,
    evidence_quote: mention.evidence_quote,
    evidence_status: mention.evidence_status,
    confidence: mention.confidence,
    competency_category: ability.category
}) AS abilities
RETURN processed.version_id AS version_id,
       raw.declared_role AS declared_role,
       raw.domain_role AS domain_role,
       raw.title AS title,
       raw.company_id AS company_id,
       raw.company_name AS company_name,
       raw.publish_time_raw AS posted_at,
       abilities
ORDER BY processed.version_id
"""

FETCH_ELIGIBLE_ABILITIES_QUERY = """
MATCH (ability:AbilityCandidate)
WHERE ability.ability_id > $cursor
WITH ability, COUNT {
    (ability)<-[:HAS_ABILITY]-(:ProcessedJD)
      <-[:HAS_PROCESSING_RESULT]-(:RawJDVersion {domain_label:'IT'})
} AS mention_count
WHERE mention_count >= $min_mentions
RETURN ability.ability_id AS ability_id
ORDER BY ability.ability_id
LIMIT $batch_size
"""


SCHEMA = """
CREATE TABLE jds (
    jd_id TEXT PRIMARY KEY,
    canonical_role TEXT NOT NULL,
    role_id TEXT NOT NULL,
    company_id TEXT NOT NULL,
    posted_at TEXT,
    time_weight REAL NOT NULL,
    template_weight REAL NOT NULL,
    duplicate_of TEXT NOT NULL
);

CREATE TABLE jd_skill_edges (
    jd_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    skill_name TEXT NOT NULL,
    raw_term TEXT,
    requirement_type TEXT,
    evidence_quote TEXT,
    evidence_status TEXT NOT NULL,
    confidence REAL NOT NULL,
    competency_category TEXT,
    PRIMARY KEY (jd_id, skill_id)
);

CREATE INDEX idx_demo_edges_jd ON jd_skill_edges(jd_id);
CREATE INDEX idx_demo_edges_skill ON jd_skill_edges(skill_id);

CREATE TABLE export_state (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def prepare_target(database_path: Path, output_dir: Path, overwrite: bool, resume: bool) -> bool:
    existing_output = output_dir.exists() and any(
        path.name != ".gitkeep" for path in output_dir.iterdir()
    )
    if resume:
        if not database_path.exists():
            raise FileNotFoundError("无法续跑：目标 SQLite 数据库不存在。")
        output_dir.mkdir(parents=True, exist_ok=True)
        return False
    if (database_path.exists() or existing_output) and not overwrite:
        raise FileExistsError(
            "目标已存在；请换一个 --work-dir、加 --resume，或确认后加 --overwrite。"
        )
    database_path.parent.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    if database_path.exists():
        # Windows 受限运行环境可能允许改写已有文件、但不允许 Python 新建文件。
        database_path.write_bytes(b"")
    return True


def fetch_eligible_ability_ids(
    repository: Neo4jGraphRepository,
    min_mentions: int,
    batch_size: int = 5000,
) -> set[str]:
    eligible: set[str] = set()
    cursor = ""
    while True:
        rows = query_with_retry(
            repository,
            FETCH_ELIGIBLE_ABILITIES_QUERY,
            {
                "cursor": cursor,
                "min_mentions": min_mentions,
                "batch_size": batch_size,
            },
        )
        if not rows:
            break
        ids = [str(row.get("ability_id") or "") for row in rows]
        eligible.update(filter(None, ids))
        cursor = ids[-1]
        print(f"eligible_abilities={len(eligible)}", flush=True)
    return eligible


def query_with_retry(
    repository: Neo4jGraphRepository,
    statement: str,
    parameters: dict | None = None,
    max_attempts: int = 8,
) -> list[dict]:
    for attempt in range(1, max_attempts + 1):
        try:
            return repository.client.query(statement, parameters)
        except (ValueError, TimeoutError, OSError) as error:
            if attempt == max_attempts:
                raise
            delay = min(2 ** attempt, 60)
            print(
                f"neo4j_retry={attempt}/{max_attempts - 1} "
                f"waiting={delay}s error={str(error)[:200]}",
                flush=True,
            )
            time.sleep(delay)
    raise RuntimeError("Neo4j 查询重试状态异常")


def state_value(connection: sqlite3.Connection, key: str, default: str = "") -> str:
    row = connection.execute(
        "SELECT value FROM export_state WHERE key = ?",
        (key,),
    ).fetchone()
    return str(row[0]) if row else default


def save_state(connection: sqlite3.Connection, **values: object) -> None:
    connection.executemany(
        """
        INSERT INTO export_state (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        [(key, str(value)) for key, value in values.items()],
    )


def completed_export_summary(database_path: Path) -> dict[str, int] | None:
    """Return the persisted export counts when a resumable export is complete."""
    if not database_path.exists():
        return None
    connection = sqlite3.connect(database_path)
    try:
        completed = state_value(connection, "completed", "0") == "1"
        if not completed:
            return None
        return {
            "jds": int(state_value(connection, "jd_count", "0")),
            "abilities": int(state_value(connection, "ability_count", "0")),
        }
    except sqlite3.OperationalError:
        return None
    finally:
        connection.close()


def load_exported_ability_ids(database_path: Path) -> set[str]:
    """Reuse a completed export's evidence threshold without re-querying Neo4j."""
    if completed_export_summary(database_path) is None:
        raise ValueError(f"候选能力来源不是已完成的导出数据库：{database_path}")
    connection = sqlite3.connect(database_path)
    try:
        return {
            str(row[0])
            for row in connection.execute("SELECT DISTINCT skill_id FROM jd_skill_edges")
            if row[0]
        }
    finally:
        connection.close()


def export_processed_data(
    repository: Neo4jGraphRepository,
    database_path: Path,
    batch_size: int,
    limit: int,
    eligible_ability_ids: set[str],
    initialize: bool,
    role_taxonomy: RoleTaxonomy,
) -> dict[str, int]:
    connection = sqlite3.connect(database_path)
    try:
        if initialize:
            connection.executescript(SCHEMA)
        completed = state_value(connection, "completed", "0") == "1"
        jd_count = int(state_value(connection, "jd_count", "0"))
        ability_count = int(state_value(connection, "ability_count", "0"))
        cursor = state_value(connection, "cursor", "")
        if completed:
            print(
                f"export_already_complete jds={jd_count} abilities={ability_count}",
                flush=True,
            )
            return {"jds": jd_count, "abilities": ability_count}
        source_exhausted = False
        while not limit or jd_count < limit:
            page_size = min(batch_size, limit - jd_count) if limit else batch_size
            rows = query_with_retry(
                repository,
                FETCH_PAGE_QUERY,
                {"cursor": cursor, "batch_size": page_size},
            )
            if not rows:
                source_exhausted = True
                break
            jd_rows = []
            edge_rows = []
            for row in rows:
                version_id = str(row.get("version_id") or "")
                cursor = version_id
                role_name = str(row.get("domain_role") or "").strip()
                if not role_name:
                    role = role_taxonomy.resolve_title(
                        str(row.get("title") or ""),
                        str(row.get("declared_role") or ""),
                    )
                    role_name = str(role.get("role_name") or "") if role else ""
                if not role_name:
                    continue
                company_name = str(row.get("company_name") or "").strip()
                company_id = str(row.get("company_id") or "").strip() or stable_id(
                    "company", company_name or version_id
                )
                jd_rows.append(
                    (
                        version_id,
                        role_name,
                        stable_id("role", role_name),
                        company_id,
                        str(row.get("posted_at") or ""),
                        1.0,
                        1.0,
                        "",
                    )
                )
                for ability in row.get("abilities") or []:
                    ability_id = str(ability.get("skill_id") or "")
                    if ability_id not in eligible_ability_ids:
                        continue
                    edge_rows.append(
                        (
                            version_id,
                            ability_id,
                            str(ability.get("skill_name") or ""),
                            str(ability.get("raw_term") or ""),
                            str(ability.get("requirement_type") or ""),
                            str(ability.get("evidence_quote") or ""),
                            str(ability.get("evidence_status") or ""),
                            float(ability.get("confidence") or 0.0),
                            str(ability.get("competency_category") or ""),
                        )
                    )
            connection.executemany(
                """
                INSERT OR REPLACE INTO jds (
                    jd_id, canonical_role, role_id, company_id, posted_at,
                    time_weight, template_weight, duplicate_of
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                jd_rows,
            )
            connection.executemany(
                """
                INSERT OR REPLACE INTO jd_skill_edges (
                    jd_id, skill_id, skill_name, raw_term, requirement_type,
                    evidence_quote, evidence_status, confidence, competency_category
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                edge_rows,
            )
            jd_count += len(jd_rows)
            ability_count += len(edge_rows)
            save_state(
                connection,
                cursor=cursor,
                jd_count=jd_count,
                ability_count=ability_count,
                completed=0,
            )
            connection.commit()
            print(
                f"exported_jds={jd_count} exported_abilities={ability_count}",
                flush=True,
            )
        save_state(
            connection,
            cursor=cursor,
            jd_count=jd_count,
            ability_count=ability_count,
            completed=1 if source_exhausted else 0,
        )
        connection.commit()
    finally:
        connection.close()
    return {"jds": jd_count, "abilities": ability_count}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="把 Neo4j 处理层适配给已验证的 Demo 向量归一算法；不修改 Demo 算法。"
    )
    parser.add_argument(
        "--neo4j-config",
        type=Path,
        default=PROJECT_ROOT / "config" / "neo4j_connection.json",
    )
    parser.add_argument(
        "--work-dir",
        type=Path,
        default=PROJECT_ROOT / "output" / "processed_normalization_demo",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=PROJECT_ROOT / "trusted_graph_agent" / "normalization_config_v5.json",
    )
    parser.add_argument("--model", type=Path, default=LOCAL_MODEL)
    parser.add_argument("--device", default="cpu", choices=("cpu", "cuda"))
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument(
        "--min-mentions",
        type=int,
        default=3,
        help="只向量化至少出现在这么多份JD中的候选；默认3不会漏掉可进入榜单的普通候选。",
    )
    parser.add_argument("--export-only", action="store_true")
    parser.add_argument(
        "--eligible-source-database",
        type=Path,
        help="复用一个已完成导出的候选能力集合，避免重新扫描 Neo4j 计数。",
    )
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    if args.overwrite and args.resume:
        parser.error("--overwrite 和 --resume 不能同时使用")

    work_dir = args.work_dir
    database_path = work_dir / "knowledge_graph.db"
    output_dir = work_dir / "skill_reports"
    initialize = prepare_target(database_path, output_dir, args.overwrite, args.resume)

    exported = completed_export_summary(database_path) if args.resume else None
    if exported is not None:
        print(
            f"export_already_complete jds={exported['jds']} "
            f"abilities={exported['abilities']}",
            flush=True,
        )
    else:
        repository = Neo4jGraphRepository(args.neo4j_config)
        min_mentions = max(1, args.min_mentions)
        if args.eligible_source_database:
            eligible_ability_ids = load_exported_ability_ids(
                args.eligible_source_database
            )
        else:
            eligible_ability_ids = fetch_eligible_ability_ids(repository, min_mentions)
        print(
            f"eligible_filter min_mentions={min_mentions} "
            f"abilities={len(eligible_ability_ids)}",
            flush=True,
        )
        exported = export_processed_data(
            repository,
            database_path,
            max(1, min(args.batch_size, 500)),
            max(0, args.limit),
            eligible_ability_ids,
            initialize,
            RoleTaxonomy(PROJECT_ROOT / "trusted_graph_agent" / "it_role_taxonomy.json"),
        )
    if args.export_only:
        print(json.dumps({"exported": exported, "database": str(database_path)}, ensure_ascii=False, indent=2))
        return
    if exported["abilities"] == 0:
        raise ValueError("没有可用于向量归一的 AbilityCandidate。")

    model_path = args.model
    if not model_path.exists():
        raise FileNotFoundError(f"本地 BGE 模型不存在：{model_path}")
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

    config = NormalizationConfig.load(args.config)
    embedder = SentenceTransformerEmbedder(str(model_path), config.embedding_batch_size, args.device)
    report = NormalizationExperiment(
        database_path,
        output_dir,
        config,
        embedder,
    ).run()
    summary = {
        "exported": exported,
        "normalization": {
            key: value
            for key, value in report.items()
            if key != "top_skills"
        },
        "report": str(output_dir / "normalization_report.md"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

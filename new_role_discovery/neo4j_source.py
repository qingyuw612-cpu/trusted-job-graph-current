"""Read-only Neo4j data source for role-evolution analysis.

The source reads the global raw/processing/normalization chain directly.  It
never creates a SQLite compatibility database and never writes to Neo4j.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

from trusted_graph_agent.neo4j_repository import Neo4jGraphRepository


SOURCE_INFO_QUERY = """
OPTIONAL MATCH (:NormalizationPointer {name:'core'})-[:ACTIVE]->
               (normalization:NormalizationRun)
WITH normalization
CALL {
    MATCH (raw:RawJDVersion)
    USING INDEX raw:RawJDVersion(domain_role)
    WHERE raw.domain_role IS NOT NULL
      AND raw.domain_role <> ''
      AND raw.domain_label = 'IT'
    MATCH (:RawJob)-[:CURRENT_VERSION]->(raw)
    MATCH (raw)-[:HAS_PROCESSING_RESULT]->
          (processed:ProcessedJD {status:'COMPLETED'})
    WITH raw, processed,
         split(
             replace(
                 split(trim(raw.publish_time_raw), ' ')[0],
                 '-',
                 '/'
             ),
             '/'
         ) AS date_parts
    WITH raw, processed,
         CASE WHEN size(date_parts) = 3
              THEN date({
                  year:toInteger(date_parts[0]),
                  month:toInteger(date_parts[1]),
                  day:toInteger(date_parts[2])
              })
              ELSE null
         END AS posted_date
    RETURN count(raw) AS usable_jds,
           max(raw.observed_epoch) AS max_observed_epoch,
           max(raw.domain_classified_at) AS max_domain_classified_at,
           max(processed.processed_at) AS max_processed_at,
           toString(min(posted_date)) AS min_posted_at,
           toString(max(posted_date)) AS max_posted_at
}
CALL {
    OPTIONAL MATCH (run:RawIngestionRun)
    WHERE run.status IN ['RUNNING', 'STARTED', 'PROCESSING']
    RETURN count(run) AS active_ingestion_runs
}
CALL {
    OPTIONAL MATCH (run:JDProcessingRun)
    WHERE run.status IN ['RUNNING', 'STARTED', 'PROCESSING']
    RETURN count(run) AS active_processing_runs
}
RETURN normalization.run_id AS active_normalization_run_id,
       normalization.status AS normalization_status,
       usable_jds,
       max_observed_epoch,
       max_domain_classified_at,
       max_processed_at,
       min_posted_at,
       max_posted_at,
       active_ingestion_runs,
       active_processing_runs
"""


JD_PAGE_QUERY = """
MATCH (processed:ProcessedJD)
USING INDEX processed:ProcessedJD(version_id)
WHERE processed.version_id > $cursor
  AND processed.status = 'COMPLETED'
MATCH (raw:RawJDVersion {version_id:processed.version_id})
MATCH (:RawJob)-[:CURRENT_VERSION]->(raw)
WHERE raw.domain_label = 'IT'
  AND coalesce(raw.domain_role, '') <> ''
WITH processed, raw
ORDER BY processed.version_id
LIMIT $batch_size
RETURN processed.version_id AS jd_id,
       raw.domain_role AS canonical_role,
       raw.company_id AS company_id,
       raw.company_name AS company_name,
       raw.title AS title,
       raw.publish_time_raw AS posted_at,
       raw.industry AS industry,
       coalesce(raw.source_platform, 'UNKNOWN') + '/' +
           coalesce(raw.source_category, '') AS source_file,
       raw.content_hash AS template_cluster_id,
       raw.observed_epoch AS observed_epoch
ORDER BY jd_id
"""


DESCRIPTION_QUERY = """
UNWIND $jd_ids AS jd_id
MATCH (raw:RawJDVersion {version_id:jd_id})
RETURN raw.version_id AS jd_id,
       coalesce(raw.description, '') AS description
"""


SKILL_CHUNK_QUERY = """
MATCH (run:NormalizationRun {run_id:$run_id})
UNWIND $jd_ids AS jd_id
MATCH (processed:ProcessedJD {version_id:jd_id})
      -[mention:HAS_ABILITY]->(ability:AbilityCandidate)
WHERE mention.evidence_status = $verified_status
OPTIONAL MATCH (ability)-[:NORMALIZES_TO {run_id:run.run_id}]->
               (skill:NormalizedSkill)
WITH jd_id, mention, ability, skill,
     coalesce(skill.concept_id, ability.ability_id) AS skill_id,
     coalesce(skill.canonical_name, ability.name) AS skill_name,
     coalesce(skill.category, ability.category) AS competency_category,
     CASE WHEN skill IS NULL
          THEN 'raw_skill_fallback'
          ELSE 'neo4j_active_normalization'
     END AS normalization_source
WHERE competency_category IN $categories
WITH *
ORDER BY jd_id,
         skill_id,
         CASE WHEN skill IS NULL THEN 1 ELSE 0 END,
         CASE WHEN trim(coalesce(mention.evidence_quote, '')) <> ''
              THEN 0 ELSE 1 END,
         mention.confidence DESC,
         ability.ability_id
WITH jd_id,
     skill_id,
     head(collect({
         raw_skill_id: ability.ability_id,
         skill_name: skill_name,
         requirement_type: coalesce(mention.requirement_type, ''),
         evidence_quote: coalesce(mention.evidence_quote, ''),
         evidence_status: mention.evidence_status,
         confidence: coalesce(mention.confidence, 0.0),
         competency_category: competency_category,
         normalization_source: normalization_source
     })) AS evidence
RETURN jd_id,
       skill_id,
       evidence.raw_skill_id AS raw_skill_id,
       evidence.skill_name AS skill_name,
       evidence.requirement_type AS requirement_type,
       evidence.evidence_quote AS evidence_quote,
       evidence.evidence_status AS evidence_status,
       evidence.confidence AS confidence,
       evidence.competency_category AS competency_category,
       evidence.normalization_source AS normalization_source
ORDER BY jd_id, skill_id
"""


KNOWN_TITLES_QUERY = """
MATCH (role:Role {normalization_run_id:$run_id})
OPTIONAL MATCH (alias:RoleAlias)-[:ALIAS_OF]->(role)
RETURN role.name AS role_name,
       coalesce(role.source_role_names, []) AS source_role_names,
       collect(DISTINCT alias.name) AS aliases
"""


class Neo4jEvolutionSourceError(RuntimeError):
    """Raised when the live Neo4j source cannot provide a coherent read."""


class Neo4jSourceChangedError(Neo4jEvolutionSourceError):
    """Raised when the graph changes while a paged analysis read is running."""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _fingerprint(info: dict[str, Any]) -> str:
    payload = {
        "active_normalization_run_id": info.get(
            "active_normalization_run_id"
        ),
        "usable_jds": int(info.get("usable_jds") or 0),
        "max_observed_epoch": int(info.get("max_observed_epoch") or 0),
        "max_domain_classified_at": str(
            info.get("max_domain_classified_at") or ""
        ),
        "max_processed_at": str(info.get("max_processed_at") or ""),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:24]


def _safe_source_info(row: dict[str, Any]) -> dict[str, Any]:
    info = {
        "type": "neo4j",
        "label": "Neo4j 全局岗位图谱",
        "connected": True,
        "schema_ready": bool(
            row.get("active_normalization_run_id")
            and int(row.get("usable_jds") or 0) > 0
        ),
        "active_normalization_run_id": str(
            row.get("active_normalization_run_id") or ""
        ),
        "normalization_status": str(
            row.get("normalization_status") or ""
        ),
        "usable_jds": int(row.get("usable_jds") or 0),
        "min_posted_at": str(row.get("min_posted_at") or ""),
        "max_posted_at": str(row.get("max_posted_at") or ""),
        "max_observed_epoch": int(row.get("max_observed_epoch") or 0),
        "max_domain_classified_at": str(
            row.get("max_domain_classified_at") or ""
        ),
        "max_processed_at": str(row.get("max_processed_at") or ""),
        "active_ingestion_runs": int(
            row.get("active_ingestion_runs") or 0
        ),
        "active_processing_runs": int(
            row.get("active_processing_runs") or 0
        ),
        "checked_at": _now(),
    }
    info["fingerprint"] = _fingerprint(info)
    return info


def inspect_neo4j_source(
    config_path: Path,
    *,
    client: Any | None = None,
) -> dict[str, Any]:
    """Return safe source health without exposing connection credentials."""
    path = Path(config_path).expanduser().resolve()
    if not path.is_file():
        raise Neo4jEvolutionSourceError("Neo4j 连接配置文件不存在。")
    try:
        active_client = (
            client
            if client is not None
            else Neo4jGraphRepository(path).client
        )
        rows = active_client.query(SOURCE_INFO_QUERY, access_mode="Read")
    except Exception as error:
        raise Neo4jEvolutionSourceError(
            f"无法只读连接 Neo4j：{type(error).__name__}: {error}"
        ) from error
    if not rows:
        raise Neo4jEvolutionSourceError("Neo4j 未返回全局图谱状态。")
    return _safe_source_info(rows[0])


class Neo4jEvolutionSource:
    """Paged, read-only source backed by the live global Neo4j graph."""

    def __init__(
        self,
        config_path: Path,
        *,
        batch_size: int = 2000,
        client: Any | None = None,
        progress_callback: Callable[[str, int, str], None] | None = None,
    ) -> None:
        self.config_path = Path(config_path).expanduser().resolve()
        self.batch_size = max(100, min(int(batch_size), 5000))
        self.client = (
            client
            if client is not None
            else Neo4jGraphRepository(self.config_path).client
        )
        self.progress_callback = progress_callback
        self.captured_info: dict[str, Any] = {}
        self.edge_count = 0
        self.skill_names: dict[str, str] = {}
        self.standardized_skill_ids: set[str] = set()
        self.raw_fallback_skill_ids: set[str] = set()
        self.mapping_quality: dict[str, Any] = {
            "strategy": "neo4j_active_normalization_with_raw_fallback",
            "normalization_tables_available": False,
            "eligible_edges": 0,
            "normalized_edges": 0,
            "normalization_coverage": 0.0,
        }
        self._loaded_jd_ids: list[str] = []

    def set_progress_callback(
        self,
        callback: Callable[[str, int, str], None] | None,
    ) -> None:
        self.progress_callback = callback

    def identity(self) -> str:
        run_id = self.captured_info.get(
            "active_normalization_run_id",
            "",
        )
        fingerprint = self.captured_info.get("fingerprint", "")
        return f"neo4j-global:{run_id}:{fingerprint}"

    def capture(self) -> dict[str, Any]:
        info = inspect_neo4j_source(self.config_path, client=self.client)
        if not info["schema_ready"]:
            raise Neo4jEvolutionSourceError(
                "Neo4j 缺少可用全局 JD 或活动归一化版本。"
            )
        if (
            info["active_ingestion_runs"] > 0
            or info["active_processing_runs"] > 0
        ):
            raise Neo4jEvolutionSourceError(
                "Neo4j 正在导入或处理数据，请等待上游任务完成后再运行。"
            )
        self.captured_info = info
        return dict(info)

    def load_jd_rows(self) -> list[dict[str, Any]]:
        if not self.captured_info:
            self.capture()
        self._loaded_jd_ids.clear()
        expected_count = int(
            self.captured_info.get("usable_jds") or 0
        )
        expected = max(expected_count, 1)
        rows: list[dict[str, Any]] = []
        seen_jd_ids: set[str] = set()
        cursor = ""
        while True:
            page = self.client.query(
                JD_PAGE_QUERY,
                {
                    "cursor": cursor,
                    "batch_size": self.batch_size,
                },
                access_mode="Read",
            )
            if not page:
                break
            for raw in page:
                jd_id = str(raw.get("jd_id") or "")
                if not jd_id:
                    continue
                if jd_id in seen_jd_ids:
                    raise Neo4jEvolutionSourceError(
                        f"Neo4j JD 分页返回重复标识：{jd_id}"
                    )
                seen_jd_ids.add(jd_id)
                company_id = str(raw.get("company_id") or "").strip()
                if not company_id:
                    company_name = str(
                        raw.get("company_name") or "UNKNOWN"
                    ).strip()
                    digest = hashlib.sha256(
                        company_name.encode("utf-8")
                    ).hexdigest()[:20]
                    company_id = f"company:{digest}"
                rows.append(
                    {
                        "jd_id": jd_id,
                        "canonical_role": str(
                            raw.get("canonical_role") or ""
                        ),
                        "role_id": "",
                        "company_id": company_id,
                        "title": str(raw.get("title") or ""),
                        "posted_at": str(raw.get("posted_at") or ""),
                        "description": "",
                        "industry_detail": str(
                            raw.get("industry") or ""
                        ),
                        "industry_name": str(
                            raw.get("industry") or ""
                        ),
                        "source_file": str(
                            raw.get("source_file") or ""
                        ),
                        "template_cluster_id": str(
                            raw.get("template_cluster_id") or ""
                        ),
                        "duplicate_of": "",
                    }
                )
                self._loaded_jd_ids.append(jd_id)
            next_cursor = str(page[-1].get("jd_id") or "")
            if not next_cursor or next_cursor <= cursor:
                raise Neo4jEvolutionSourceError(
                    "Neo4j JD 分页游标未向前推进。"
                )
            cursor = next_cursor
            self._progress(
                "READING_NEO4J_JDS",
                5 + min(10, int(len(rows) / expected * 10)),
                f"正在读取 Neo4j 全局 JD：{len(rows):,}/{expected:,}",
            )
            if len(page) < self.batch_size:
                break
        if len(rows) != expected_count:
            raise Neo4jSourceChangedError(
                "Neo4j 可用 JD 数与分页读取结果不一致；"
                "本轮结果已丢弃，请重新运行。"
            )
        return rows

    def load_descriptions(
        self,
        jd_ids: Iterable[str],
        *,
        chunk_size: int = 500,
    ) -> dict[str, str]:
        identifiers = list(dict.fromkeys(str(value) for value in jd_ids))
        result: dict[str, str] = {}
        total = max(len(identifiers), 1)
        for index in range(0, len(identifiers), max(1, chunk_size)):
            chunk = identifiers[index : index + chunk_size]
            rows = self.client.query(
                DESCRIPTION_QUERY,
                {"jd_ids": chunk},
                access_mode="Read",
            )
            for row in rows:
                jd_id = str(row.get("jd_id") or "")
                if jd_id:
                    result[jd_id] = str(row.get("description") or "")
            self._progress(
                "READING_NEO4J_DESCRIPTIONS",
                15 + min(5, int((index + len(chunk)) / total * 5)),
                (
                    "正在按新数据窗口读取职责原文："
                    f"{min(index + len(chunk), len(identifiers)):,}/"
                    f"{len(identifiers):,}"
                ),
            )
        return result

    def iter_skill_edges(
        self,
        *,
        allowed_jd_ids: set[str],
        verified_status: str,
        categories: tuple[str, ...],
    ) -> Iterator[dict[str, Any]]:
        if not self.captured_info:
            self.capture()
        run_id = str(
            self.captured_info["active_normalization_run_id"]
        )
        expected = max(int(self.captured_info.get("usable_jds") or 0), 1)
        identifiers = [
            jd_id
            for jd_id in self._loaded_jd_ids
            if jd_id in allowed_jd_ids
        ]
        if not identifiers:
            identifiers = sorted(allowed_jd_ids)
        scanned_jds = 0
        eligible = 0
        normalized = 0
        skill_batch_size = min(self.batch_size, 1000)
        for index in range(0, len(identifiers), skill_batch_size):
            chunk = identifiers[index : index + skill_batch_size]
            page = self.client.query(
                SKILL_CHUNK_QUERY,
                {
                    "jd_ids": chunk,
                    "verified_status": verified_status,
                    "categories": list(categories),
                    "run_id": run_id,
                },
                access_mode="Read",
            )
            for row in page:
                jd_id = str(row.get("jd_id") or "")
                if jd_id not in allowed_jd_ids:
                    continue
                skill_id = str(row.get("skill_id") or "").strip()
                if not skill_id:
                    continue
                source = str(row.get("normalization_source") or "")
                skill_name = str(row.get("skill_name") or skill_id)
                eligible += 1
                if source == "neo4j_active_normalization":
                    normalized += 1
                    self.standardized_skill_ids.add(skill_id)
                else:
                    self.raw_fallback_skill_ids.add(skill_id)
                self.skill_names[skill_id] = skill_name
                yield {
                    "jd_id": jd_id,
                    "skill_id": skill_id,
                    "skill_name": skill_name,
                    "evidence_status": str(
                        row.get("evidence_status") or ""
                    ),
                    "competency_category": str(
                        row.get("competency_category") or ""
                    ),
                    "requirement_type": str(
                        row.get("requirement_type") or ""
                    ),
                    "evidence_quote": str(
                        row.get("evidence_quote") or ""
                    ),
                    "confidence": float(row.get("confidence") or 0.0),
                    "normalization_source": source,
                }
            scanned_jds += len(chunk)
            self._progress(
                "READING_NEO4J_SKILLS",
                20 + min(15, int(scanned_jds / expected * 15)),
                (
                    "正在流式聚合 Neo4j 能力证据："
                    f"{scanned_jds:,}/{expected:,} JD"
                ),
            )
        self.edge_count = eligible
        self.mapping_quality = {
            "strategy": "neo4j_active_normalization_with_raw_fallback",
            "normalization_tables_available": True,
            "active_normalization_run_id": run_id,
            "eligible_edges": eligible,
            "normalized_edges": normalized,
            "normalization_coverage": (
                normalized / eligible if eligible else 0.0
            ),
        }

    def load_known_titles(self) -> set[str]:
        if not self.captured_info:
            self.capture()
        rows = self.client.query(
            KNOWN_TITLES_QUERY,
            {
                "run_id": self.captured_info[
                    "active_normalization_run_id"
                ]
            },
            access_mode="Read",
        )
        titles: set[str] = set()
        for row in rows:
            values = [
                row.get("role_name"),
                *(row.get("source_role_names") or []),
                *(row.get("aliases") or []),
            ]
            titles.update(
                str(value).strip()
                for value in values
                if str(value or "").strip()
            )
        return titles

    def verify_unchanged(self) -> None:
        if not self.captured_info:
            return
        current = inspect_neo4j_source(
            self.config_path,
            client=self.client,
        )
        if current["fingerprint"] != self.captured_info["fingerprint"]:
            raise Neo4jSourceChangedError(
                "Neo4j 在分页读取期间发生变化；本轮结果已丢弃，请重新运行。"
            )

    def public_metadata(self) -> dict[str, Any]:
        return dict(self.captured_info)

    def _progress(self, stage: str, percent: int, message: str) -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(stage, percent, message)
        except Exception:
            pass


__all__ = [
    "Neo4jEvolutionSource",
    "Neo4jEvolutionSourceError",
    "Neo4jSourceChangedError",
    "inspect_neo4j_source",
]

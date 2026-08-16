from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from trusted_graph_agent.extractors import (  # noqa: E402
    AbilityAnalysisExtractor,
    EvidenceVerifier,
    WebhookLLMExtractor,
)
from trusted_graph_agent.models import JobDocument, SkillCandidate  # noqa: E402
from trusted_graph_agent.neo4j_repository import Neo4jGraphRepository  # noqa: E402
from trusted_graph_agent.normalization_experiment import (  # noqa: E402
    corrected_category,
    normalize_surface,
)
from trusted_graph_agent.registry import SkillRegistry  # noqa: E402
from trusted_graph_agent.text_utils import (  # noqa: E402
    normalize_text,
    parse_datetime,
    simhash64,
    stable_id,
    text_hash,
)
from extract_five_dimension_abilities import (  # noqa: E402
    IFLYTEK_SPARK_BASE_URL,
    JsonlCache,
    OpenAICompatibleClient,
    read_environment,
    record_key,
    validate_result,
)


class SparkFiveDimensionExtractor:
    """Call Spark only after the domain filter has admitted an IT JD."""

    def __init__(self, analysis_extractor: AbilityAnalysisExtractor, cache_path: Path):
        api_key = read_environment("IFLYTEK_SPARK_API_PASSWORD")
        model = read_environment("IFLYTEK_SPARK_MODEL")
        base_url = read_environment("IFLYTEK_SPARK_BASE_URL") or IFLYTEK_SPARK_BASE_URL
        if not api_key:
            raise ValueError("缺少 IFLYTEK_SPARK_API_PASSWORD，无法调用讯飞星火。")
        if not model:
            raise ValueError("缺少 IFLYTEK_SPARK_MODEL，Spark Lite 请设置为 lite。")
        self.analysis_extractor = analysis_extractor
        self.client = OpenAICompatibleClient(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout=90,
            retries=3,
            response_mode="prompt-only",
        )
        self.cache = JsonlCache(cache_path)

    def extract(self, document: JobDocument) -> list[SkillCandidate]:
        key = record_key(document.title, document.description, document.tags, self.client.model)
        result = self.cache.get(key)
        if result is None:
            raw_result, _usage = self.client.extract(
                document.title,
                document.description,
                document.tags,
            )
            result = validate_result(raw_result, document.evidence_text)
            self.cache.put(key, result)
        else:
            result = validate_result(result, document.evidence_text)
        enriched = replace(
            document,
            ability_analysis=json.dumps(result, ensure_ascii=False, separators=(",", ":")),
        )
        candidates = self.analysis_extractor.extract(enriched)
        for candidate in candidates:
            candidate.source = "IFLYTEK_SPARK_LLM"
        return candidates


PROCESSOR_VERSION = "raw-ability-evidence-v1"
ACCEPTED_EVIDENCE = {"VERIFIED", "LOW_CONFIDENCE", "ANALYSIS_ONLY"}
META_NOISE = re.compile(
    r"没有对应描述|无对应描述|没有相关描述|无相关描述|未出现直接对应|"
    r"未出现明确对应|未找到直接对应|未提及|未明确提及|原文中无|"
    r"所有输出要素.*对应原文|严格对应原文|能力要素描述|直接描述要素|"
    r"所有(?:能力)?要素均直接提取|所有短语均直接来自招聘文本|"
    r"无明确对应要素|无直接对应要素|无明确(?:动机|技术|知识|技能|特质|自我概念)?(?:要素|要求)|"
    r"未明确体现|未涉及(?:动机|特质|自我概念)|"
    r"(?:采用|并以).*?(?:短词组|形式呈现)|"
    r"(?:故|因此)(?:未|保持|留空|标注|输出|在两个维度)|"
    r"按(?:原文|维度).*?(?:呈现|列出|分组)|归类为.*?维度|"
    r"如果您有其他|我可以进一步|严格按胜任力维度|严格提取招聘文本|"
    r"(?:知识|技术|技能|动机|特质|自我概念)(?:和|及|、)?"
    r"(?:知识|技术|技能|动机|特质|自我概念)?维度的具体要求|"
    r"无法判断|不适用|暂无相关|网络错误",
    re.IGNORECASE,
)


CONSTRAINTS = (
    "CREATE CONSTRAINT processed_jd_version IF NOT EXISTS FOR (n:ProcessedJD) REQUIRE n.version_id IS UNIQUE",
    "CREATE CONSTRAINT ability_candidate_id IF NOT EXISTS FOR (n:AbilityCandidate) REQUIRE n.ability_id IS UNIQUE",
    "CREATE CONSTRAINT processing_run_id IF NOT EXISTS FOR (n:JDProcessingRun) REQUIRE n.run_id IS UNIQUE",
    "CREATE INDEX raw_jd_ingest_cursor IF NOT EXISTS FOR (n:RawJDVersion) "
    "ON (n.last_ingest_run_id, n.version_id)",
)


FETCH_CURRENT_QUERY = """
MATCH (job:RawJob)-[:CURRENT_VERSION]->(raw:RawJDVersion)
WHERE raw.domain_label = 'IT'
  AND coalesce(raw.processing_version, '') <> $processor_version
  AND ($ingest_run_id = '' OR raw.last_ingest_run_id = $ingest_run_id)
RETURN job.raw_uid AS raw_uid, properties(raw) AS raw
LIMIT $batch_size
"""


FETCH_ALL_VERSIONS_QUERY = """
MATCH (raw:RawJDVersion)
WHERE raw.domain_label = 'IT'
  AND coalesce(raw.processing_version, '') <> $processor_version
  AND ($ingest_run_id = '' OR raw.last_ingest_run_id = $ingest_run_id)
RETURN raw.raw_uid AS raw_uid, properties(raw) AS raw
LIMIT $batch_size
"""


FETCH_CURRENT_FORCE_QUERY = """
MATCH (raw:RawJDVersion)
USING INDEX raw:RawJDVersion(version_id)
WHERE raw.version_id > $cursor
  AND raw.domain_label = 'IT'
  AND ($ingest_run_id = '' OR raw.last_ingest_run_id = $ingest_run_id)
MATCH (job:RawJob)-[:CURRENT_VERSION]->(raw)
RETURN job.raw_uid AS raw_uid, properties(raw) AS raw
ORDER BY raw.version_id
LIMIT $batch_size
"""


FETCH_ALL_VERSIONS_FORCE_QUERY = """
MATCH (raw:RawJDVersion)
USING INDEX raw:RawJDVersion(version_id)
WHERE raw.version_id > $cursor
  AND raw.domain_label = 'IT'
  AND ($ingest_run_id = '' OR raw.last_ingest_run_id = $ingest_run_id)
RETURN raw.raw_uid AS raw_uid, properties(raw) AS raw
ORDER BY raw.version_id
LIMIT $batch_size
"""


FETCH_CURRENT_INGEST_FORCE_QUERY = """
MATCH (raw:RawJDVersion)
USING INDEX raw:RawJDVersion(last_ingest_run_id, version_id)
WHERE raw.last_ingest_run_id = $ingest_run_id
  AND raw.version_id > $cursor
  AND raw.domain_label = 'IT'
MATCH (job:RawJob)-[:CURRENT_VERSION]->(raw)
RETURN job.raw_uid AS raw_uid, properties(raw) AS raw
ORDER BY raw.version_id
LIMIT $batch_size
"""


FETCH_ALL_VERSIONS_INGEST_FORCE_QUERY = """
MATCH (raw:RawJDVersion)
USING INDEX raw:RawJDVersion(last_ingest_run_id, version_id)
WHERE raw.last_ingest_run_id = $ingest_run_id
  AND raw.version_id > $cursor
  AND raw.domain_label = 'IT'
RETURN raw.raw_uid AS raw_uid, properties(raw) AS raw
ORDER BY raw.version_id
LIMIT $batch_size
"""


WRITE_BATCH_QUERY = """
UNWIND $rows AS row
MATCH (raw:RawJDVersion {version_id: row.version_id})
MERGE (processed:ProcessedJD {version_id: row.version_id})
SET processed += row.processed_props
MERGE (raw)-[:HAS_PROCESSING_RESULT]->(processed)
SET raw.processing_version = $processor_version,
    raw.processing_status = row.processed_props.status,
    raw.processed_at = $now
WITH row, processed
OPTIONAL MATCH (processed)-[oldAbility:HAS_ABILITY]->(:AbilityCandidate)
WITH row, processed, collect(oldAbility) AS oldAbilities
FOREACH (relationship IN oldAbilities | DELETE relationship)
FOREACH (ability IN row.abilities |
    MERGE (candidate:AbilityCandidate {ability_id: ability.ability_id})
    SET candidate.name = ability.name,
        candidate.normalized_name = ability.normalized_name,
        candidate.category = ability.category,
        candidate.tech_stack = ability.tech_stack,
        candidate.updated_at = $now
    CREATE (processed)-[:HAS_ABILITY {
        mention_id: ability.mention_id,
        raw_term: ability.raw_term,
        requirement_type: ability.requirement_type,
        evidence_quote: ability.evidence_quote,
        evidence_status: ability.evidence_status,
        confidence: ability.confidence,
        source: ability.source
    }]->(candidate)
)
RETURN count(*) AS processed
"""


@dataclass(slots=True)
class ProcessingMetrics:
    rows_read: int = 0
    completed: int = 0
    needs_llm: int = 0
    no_valid_abilities: int = 0
    failed: int = 0
    abilities_written: int = 0
    reviews_written: int = 0
    batches_written: int = 0
    errors: list[str] = field(default_factory=list)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_meta_noise(value: str) -> bool:
    text = re.sub(r"\s+", "", value or "").strip("，。；;：:、- ")
    if not text or META_NOISE.search(text):
        return True
    normalized = normalize_text(text)
    return len(normalized) < 2 or normalized in {"无", "没有", "暂无", "未知", "none", "null"}


def build_document(raw_uid: str, raw: dict[str, Any]) -> JobDocument:
    title = str(raw.get("title") or raw.get("declared_role") or "").strip()
    description = str(raw.get("description") or "").strip()
    company_name = str(raw.get("company_name") or "").strip()
    version_id = str(raw.get("version_id") or "").strip()
    return JobDocument(
        jd_id=version_id,
        source_file=str(raw.get("source_category") or ""),
        source_category=str(raw.get("source_category") or ""),
        raw_job_id=raw_uid,
        company_id=str(raw.get("company_id") or ""),
        company_name=company_name,
        title=title,
        canonical_role=str(raw.get("declared_role") or title),
        description=description,
        tags=str(raw.get("tags") or ""),
        ability_analysis=str(raw.get("ability_analysis_raw") or ""),
        industry=str(raw.get("industry") or ""),
        education=str(raw.get("education") or ""),
        experience=str(raw.get("experience") or ""),
        salary=str(raw.get("salary") or ""),
        location=str(raw.get("location") or ""),
        posted_at=parse_datetime(str(raw.get("publish_time_raw") or "")),
        level="",
        exact_hash=text_hash(company_name, title, description),
        simhash=simhash64(description),
    )


def clean_candidates(candidates: list[SkillCandidate]) -> tuple[list[SkillCandidate], int]:
    cleaned: list[SkillCandidate] = []
    seen: set[tuple[str, str]] = set()
    removed = 0
    for candidate in candidates:
        name = normalize_surface(candidate.skill_name or candidate.raw_term)
        category = corrected_category(name, candidate.competency_category)
        if is_meta_noise(name) or category == "噪声":
            removed += 1
            continue
        key = (normalize_text(name), category)
        if key in seen:
            removed += 1
            continue
        seen.add(key)
        candidate.skill_name = name
        candidate.competency_category = category
        cleaned.append(candidate)
    return cleaned, removed


def processing_row(
    raw_uid: str,
    raw: dict[str, Any],
    extractor: AbilityAnalysisExtractor,
    verifier: EvidenceVerifier,
    llm_extractor: WebhookLLMExtractor | SparkFiveDimensionExtractor | None = None,
) -> dict[str, Any]:
    document = build_document(raw_uid, raw)
    now = utc_now()
    analysis_present = bool(document.ability_analysis.strip())
    if isinstance(llm_extractor, SparkFiveDimensionExtractor):
        source_type = "IFLYTEK_SPARK_LLM"
        candidates = llm_extractor.extract(document)
    elif analysis_present:
        source_type = "EXISTING_ABILITY_ANALYSIS"
        candidates = extractor.extract(document)
    elif llm_extractor is not None:
        source_type = "LLM_WEBHOOK"
        candidates = llm_extractor.extract(document)
    else:
        return {
            "version_id": document.jd_id,
            "processed_props": {
                "raw_uid": raw_uid,
                "title": document.title,
                "company_name": document.company_name,
                "source_platform": str(raw.get("source_platform") or ""),
                "status": "NEEDS_LLM",
                "source_type": "MISSING_ABILITY_ANALYSIS",
                "processor_version": PROCESSOR_VERSION,
                "processed_at": now,
                "candidate_count": 0,
                "accepted_count": 0,
                "review_count": 0,
                "noise_removed": 0,
            },
            "abilities": [],
            "reviews": [],
        }

    candidates, noise_removed = clean_candidates(candidates)
    verification = verifier.verify(document, candidates)
    accepted = [item for item in verification.evidences if item.evidence_status in ACCEPTED_EVIDENCE]
    abilities = []
    for evidence in accepted:
        normalized_name = normalize_text(evidence.skill_name)
        ability_id = stable_id("ability", evidence.competency_category, normalized_name)
        abilities.append(
            {
                "ability_id": ability_id,
                "mention_id": stable_id("mention", document.jd_id, ability_id),
                "name": evidence.skill_name,
                "normalized_name": normalized_name,
                "category": evidence.competency_category,
                "tech_stack": evidence.tech_stack,
                "raw_term": evidence.raw_term,
                "requirement_type": evidence.requirement_type,
                "evidence_quote": evidence.evidence_quote,
                "evidence_status": evidence.evidence_status,
                "confidence": evidence.confidence,
                "source": evidence.source,
            }
        )
    reviews = [
        {
            "review_id": item.task_id,
            "properties": {
                "skill_id": item.skill_id,
                "skill_name": item.skill_name,
                "reason": item.reason,
                "evidence_status": item.evidence_status,
                "confidence": item.confidence,
                "evidence_quote": item.evidence_quote,
                "status": item.status,
                "updated_at": now,
            },
        }
        for item in verification.reviews
    ]
    status = "COMPLETED" if abilities else "NO_VALID_ABILITIES"
    return {
        "version_id": document.jd_id,
        "processed_props": {
            "raw_uid": raw_uid,
            "title": document.title,
            "company_name": document.company_name,
            "source_platform": str(raw.get("source_platform") or ""),
            "status": status,
            "source_type": source_type,
            "processor_version": PROCESSOR_VERSION,
            "processed_at": now,
            "candidate_count": len(candidates),
            "accepted_count": len(abilities),
            "review_count": len(reviews),
            "noise_removed": noise_removed,
        },
        "abilities": abilities,
        "reviews": reviews,
    }


class IncrementalProcessor:
    def __init__(
        self,
        repository: Neo4jGraphRepository,
        registry: SkillRegistry,
        batch_size: int,
        all_versions: bool,
        force: bool,
        llm_endpoint: str,
        iflytek_spark: bool = False,
        llm_cache: Path | None = None,
        ingest_run_id: str = "",
    ):
        self.client = repository.client
        self.extractor = AbilityAnalysisExtractor(registry)
        self.verifier = EvidenceVerifier(registry)
        if iflytek_spark and llm_endpoint:
            raise ValueError("--iflytek-spark 与 --llm-endpoint 不能同时使用。")
        if iflytek_spark:
            cache_path = llm_cache or (PROJECT_ROOT / "output" / "jd_processing" / "spark_ability_cache.jsonl")
            self.llm_extractor = SparkFiveDimensionExtractor(self.extractor, cache_path.resolve())
        else:
            self.llm_extractor = WebhookLLMExtractor(llm_endpoint) if llm_endpoint else None
        self.batch_size = max(1, min(batch_size, 500))
        self.ingest_run_id = ingest_run_id.strip()
        if force and self.ingest_run_id:
            self.fetch_query = (
                FETCH_ALL_VERSIONS_INGEST_FORCE_QUERY
                if all_versions
                else FETCH_CURRENT_INGEST_FORCE_QUERY
            )
        elif force:
            self.fetch_query = (
                FETCH_ALL_VERSIONS_FORCE_QUERY
                if all_versions
                else FETCH_CURRENT_FORCE_QUERY
            )
        else:
            self.fetch_query = (
                FETCH_ALL_VERSIONS_QUERY if all_versions else FETCH_CURRENT_QUERY
            )
        self.force = force

    def query_with_retry(
        self,
        statement: str,
        parameters: dict[str, Any] | None = None,
        access_mode: str = "Read",
        max_attempts: int = 6,
    ) -> list[dict]:
        for attempt in range(1, max_attempts + 1):
            try:
                return self.client.query(statement, parameters, access_mode=access_mode)
            except (ValueError, TimeoutError, OSError) as error:
                message = str(error)
                transient = (
                    "无法连接 Neo4j" in message
                    or "timed out" in message.lower()
                    or "timeout" in message.lower()
                    or "HTTP 502" in message
                    or "HTTP 503" in message
                    or "HTTP 504" in message
                )
                if not transient or attempt == max_attempts:
                    raise
                delay = min(2 ** attempt, 30)
                print(
                    f"neo4j_connection_retry={attempt}/{max_attempts - 1} "
                    f"waiting={delay}s error={message[:200]}",
                    flush=True,
                )
                time.sleep(delay)
        raise RuntimeError("Neo4j 查询重试状态异常")

    def initialize(self) -> None:
        for statement in CONSTRAINTS:
            self.query_with_retry(statement, access_mode="Write")

    def run(self, limit: int = 0) -> ProcessingMetrics:
        metrics = ProcessingMetrics()
        cursor = ""
        while not limit or metrics.rows_read < limit:
            page_size = min(self.batch_size, limit - metrics.rows_read) if limit else self.batch_size
            rows = self.query_with_retry(
                self.fetch_query,
                {
                    "cursor": cursor,
                    "batch_size": page_size,
                    "force": self.force,
                    "processor_version": PROCESSOR_VERSION,
                    "ingest_run_id": self.ingest_run_id,
                },
            )
            if not rows:
                break
            output_rows = []
            for item in rows:
                raw = item.get("raw") or {}
                cursor = str(raw.get("version_id") or cursor)
                metrics.rows_read += 1
                try:
                    result = processing_row(
                        str(item.get("raw_uid") or raw.get("raw_uid") or ""),
                        raw,
                        self.extractor,
                        self.verifier,
                        self.llm_extractor,
                    )
                except Exception as error:
                    metrics.failed += 1
                    metrics.errors.append(f"{cursor}: {error}")
                    result = self.failed_row(item, error)
                output_rows.append(result)
                status = result["processed_props"]["status"]
                if status == "COMPLETED":
                    metrics.completed += 1
                elif status == "NEEDS_LLM":
                    metrics.needs_llm += 1
                elif status == "NO_VALID_ABILITIES":
                    metrics.no_valid_abilities += 1
                metrics.abilities_written += len(result["abilities"])
                metrics.reviews_written += len(result["reviews"])
            self.query_with_retry(
                WRITE_BATCH_QUERY,
                {
                    "rows": output_rows,
                    "processor_version": PROCESSOR_VERSION,
                    "now": utc_now(),
                },
                access_mode="Write",
            )
            metrics.batches_written += 1
            print(
                f"processed={metrics.rows_read} completed={metrics.completed} "
                f"needs_llm={metrics.needs_llm} abilities={metrics.abilities_written}",
                flush=True,
            )
        return metrics

    @staticmethod
    def failed_row(item: dict[str, Any], error: Exception) -> dict[str, Any]:
        raw = item.get("raw") or {}
        return {
            "version_id": str(raw.get("version_id") or ""),
            "processed_props": {
                "raw_uid": str(item.get("raw_uid") or raw.get("raw_uid") or ""),
                "title": str(raw.get("title") or ""),
                "status": "FAILED",
                "source_type": "PROCESSING_ERROR",
                "processor_version": PROCESSOR_VERSION,
                "processed_at": utc_now(),
                "error": str(error)[:1000],
                "candidate_count": 0,
                "accepted_count": 0,
                "review_count": 0,
                "noise_removed": 0,
            },
            "abilities": [],
            "reviews": [],
        }


def graph_status(repository: Neo4jGraphRepository) -> dict[str, Any]:
    rows = repository.client.query(
        """
        CALL { MATCH (n:RawJob) RETURN count(n) AS raw_jobs }
        CALL { MATCH (n:ProcessedJD) RETURN count(n) AS processed_jds }
        CALL { MATCH (n:ProcessedJD {status:'COMPLETED'}) RETURN count(n) AS completed }
        CALL { MATCH (n:ProcessedJD {status:'NEEDS_LLM'}) RETURN count(n) AS needs_llm }
        CALL { MATCH (n:ProcessedJD {status:'NO_VALID_ABILITIES'}) RETURN count(n) AS no_valid_abilities }
        CALL { MATCH (n:ProcessedJD {status:'FAILED'}) RETURN count(n) AS failed }
        CALL { MATCH (n:AbilityCandidate) RETURN count(n) AS ability_candidates }
        CALL { MATCH (:ProcessedJD)-[r:HAS_ABILITY]->() RETURN count(r) AS ability_mentions }
        RETURN raw_jobs, processed_jds, completed, needs_llm, no_valid_abilities,
               failed, ability_candidates, ability_mentions
        """
    )
    return rows[0] if rows else {}


def export_llm_queue(
    repository: Neo4jGraphRepository,
    output_path: Path,
    limit: int,
    source_platform: str = "",
    domain_label: str = "",
    page_size: int = 2000,
) -> int:
    page_size = max(1, min(page_size, 10000))
    max_rows = max(0, limit)
    cursor = ""
    written = 0
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as stream:
        while True:
            if max_rows and written >= max_rows:
                break
            batch_size = min(page_size, max_rows - written) if max_rows else page_size
            rows = repository.client.query(
                """
                MATCH (raw:RawJDVersion)-[:HAS_PROCESSING_RESULT]->(:ProcessedJD {status:'NEEDS_LLM'})
                WHERE raw.version_id > $cursor
                  AND ($source_platform = '' OR raw.source_platform = $source_platform)
                  AND ($domain_label = '' OR raw.domain_label = $domain_label)
                RETURN raw.version_id AS version_id, raw.raw_uid AS raw_uid, raw.title AS title,
                       raw.description AS description, raw.tags AS tags,
                       raw.company_name AS company_name, raw.source_platform AS source_platform,
                       raw.declared_role AS source_role, raw.domain_role AS canonical_role
                ORDER BY raw.version_id
                LIMIT $batch_size
                """,
                {
                    "cursor": cursor,
                    "batch_size": batch_size,
                    "source_platform": source_platform.strip(),
                    "domain_label": domain_label.strip(),
                },
            )
            if not rows:
                break
            for row in rows:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            written += len(rows)
            next_cursor = str(rows[-1].get("version_id") or "")
            if not next_cursor or next_cursor <= cursor:
                raise RuntimeError("LLM queue pagination cursor did not advance")
            cursor = next_cursor
    return written


def write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="增量解析原始JD能力分析、回标原文并写入Neo4j")
    parser.add_argument("--neo4j-config", type=Path, default=PROJECT_ROOT / "config" / "neo4j_connection.json")
    parser.add_argument("--registry", type=Path, default=PROJECT_ROOT / "trusted_graph_agent" / "skills_registry.json")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--all-versions", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--llm-endpoint", default="")
    parser.add_argument(
        "--iflytek-spark",
        action="store_true",
        help="领域准入后仅对 IT 岗位调用讯飞星火五维能力提取",
    )
    parser.add_argument("--llm-cache", type=Path, help="讯飞提取断点缓存；默认写入 output/jd_processing")
    parser.add_argument("--ingest-run-id", default="", help="只处理指定原始导入批次涉及的版本")
    parser.add_argument("--status-only", action="store_true")
    parser.add_argument("--export-llm-queue", type=Path)
    parser.add_argument("--queue-limit", type=int, default=0, help="0 means export all matching rows")
    parser.add_argument("--queue-page-size", type=int, default=2000)
    parser.add_argument("--queue-source-platform", default="")
    parser.add_argument("--queue-domain-label", default="")
    parser.add_argument("--report", type=Path, default=PROJECT_ROOT / "output" / "jd_processing" / "last_run.json")
    args = parser.parse_args()

    repository = Neo4jGraphRepository(args.neo4j_config.resolve())
    if args.status_only:
        print(json.dumps(graph_status(repository), ensure_ascii=False, indent=2))
        return
    if args.export_llm_queue:
        count = export_llm_queue(
            repository,
            args.export_llm_queue.resolve(),
            args.queue_limit,
            args.queue_source_platform,
            args.queue_domain_label,
            args.queue_page_size,
        )
        print(f"exported={count} path={args.export_llm_queue.resolve()}")
        return

    processor = IncrementalProcessor(
        repository=repository,
        registry=SkillRegistry(args.registry.resolve()),
        batch_size=args.batch_size,
        all_versions=args.all_versions,
        force=args.force,
        llm_endpoint=args.llm_endpoint.strip(),
        iflytek_spark=args.iflytek_spark,
        llm_cache=args.llm_cache,
        ingest_run_id=args.ingest_run_id,
    )
    processor.initialize()
    run_id = "processrun:" + datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    started_at = utc_now()
    # Keep one lightweight run status instead of accumulating process history.
    repository.client.query(
        "MATCH (old:JDProcessingRun) DETACH DELETE old",
        access_mode="Write",
    )
    repository.client.query(
        "MERGE (r:JDProcessingRun {run_id:$run_id}) SET r.status='RUNNING', "
        "r.started_at=$started_at, r.processor_version=$processor_version",
        {"run_id": run_id, "started_at": started_at, "processor_version": PROCESSOR_VERSION},
        access_mode="Write",
    )
    try:
        metrics = processor.run(args.limit)
        status = "COMPLETED"
    except Exception:
        repository.client.query(
            "MATCH (r:JDProcessingRun {run_id:$run_id}) SET r.status='FAILED', r.finished_at=$finished_at",
            {"run_id": run_id, "finished_at": utc_now()},
            access_mode="Write",
        )
        raise
    payload = {
        "run_id": run_id,
        "status": status,
        "processor_version": PROCESSOR_VERSION,
        "started_at": started_at,
        "finished_at": utc_now(),
        "current_versions_only": not args.all_versions,
        "metrics": asdict(metrics),
        "graph_status": graph_status(repository),
    }
    run_properties = {
        "status": status,
        "finished_at": payload["finished_at"],
        "current_versions_only": payload["current_versions_only"],
        "metrics_json": json.dumps(payload["metrics"], ensure_ascii=False),
        "graph_status_json": json.dumps(payload["graph_status"], ensure_ascii=False),
    }
    repository.client.query(
        "MATCH (r:JDProcessingRun {run_id:$run_id}) SET r += $properties",
        {"run_id": run_id, "properties": run_properties},
        access_mode="Write",
    )
    write_report(args.report.resolve(), payload)
    print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

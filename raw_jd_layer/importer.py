from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import sys
import time
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from trusted_graph_agent.neo4j_repository import Neo4jGraphRepository  # noqa: E402


FIELD_ALIASES = {
    "source_job_id": ["职位ID", "jobID", "job_id", "jobId", "position_id", "positionId", "id"],
    "company_id": ["companyID", "company_id", "companyId"],
    "title": ["原始职位名称", "职位名称", "job_name", "job_title", "title", "position_name"],
    "normalized_role": ["岗位名称", "归一化岗位名称", "normalized_role", "canonical_role"],
    "declared_role": ["岗位关键词", "declared_role", "source_role", "source_job_category"],
    "description": ["JD全文", "职位描述", "jd", "job_description", "description", "content"],
    "salary_max": ["最高工资", "salary_max", "max_salary"],
    "salary_min": ["最低工资", "salary_min", "min_salary"],
    "salary": ["薪水", "salary", "salary_text"],
    "location": ["工作地区", "location", "city", "work_location"],
    "city": ["城市", "job_city"],
    "tags": ["职位标签", "tags", "tags_list", "job_tags"],
    "company_name": ["公司全称", "company", "company_name"],
    "company_type": ["公司类型", "company_type"],
    "company_size": ["公司规模", "company_size"],
    "company_qualification": ["公司资历", "company_qualification"],
    "industry": ["公司行业", "行业类型", "industry", "industry_name"],
    "province": ["省份", "province"],
    "education": ["学历要求", "education", "education_requirement"],
    "experience": ["工作经验", "经验要求", "experience", "experience_requirement"],
    "publish_time": ["发布日期", "时间", "publish_time", "posted_at", "publishDate", "update_time"],
    "collected_at": ["采集时间", "collected_at", "crawled_at", "scraped_at"],
    "ability_analysis": ["能力提取结果", "能力分析结果", "ability_analysis", "analysis_result", "extracted_abilities"],
    "job_link": ["职位详情链接", "job_link", "link", "url", "job_url"],
    "search_city": ["搜索城市", "search_city"],
    "search_keyword": ["搜索关键词", "search_keyword"],
    "skill_tags": ["skill_tags", "skills"],
    "source_platform": ["source_platform", "platform", "source", "site"],
}


def normalize_key(value: Any) -> str:
    return re.sub(r"[\s_\-]+", "", str(value or "").replace("\ufeff", "")).lower()


NORMALIZED_ALIASES = {
    field_name: {normalize_key(alias) for alias in aliases}
    for field_name, aliases in FIELD_ALIASES.items()
}
KNOWN_KEYS = set().union(*NORMALIZED_ALIASES.values())


def stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value).strip()


def normalized_record(record: dict[str, Any]) -> dict[str, Any]:
    return {normalize_key(key): value for key, value in record.items()}


def pick(record: dict[str, Any], field_name: str) -> str:
    for alias in NORMALIZED_ALIASES[field_name]:
        value = stringify(record.get(alias))
        if value and value.lower() not in {"nan", "none", "null"}:
            return value
    return ""


def stable_hash(value: str, length: int = 32) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="replace")).hexdigest()[:length]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_observed_epoch(value: str, fallback: int) -> int:
    text = value.strip()
    if not text:
        return fallback
    formats = (
        "%Y%m%d%H%M%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y/%m/%d %H:%M:%S",
        "%Y-%m-%d",
        "%Y/%m/%d",
    )
    for pattern in formats:
        try:
            return int(datetime.strptime(text, pattern).replace(tzinfo=timezone.utc).timestamp())
        except ValueError:
            continue
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return fallback


def infer_platform(relative_path: str, explicit: str, default_platform: str) -> str:
    if explicit:
        return explicit
    path = relative_path.lower()
    rules = {
        "liepin": "猎聘",
        "猎聘": "猎聘",
        "zhaopin": "智联招聘",
        "智联": "智联招聘",
        "boss": "BOSS直聘",
        "前程": "前程无忧",
        "51job": "前程无忧",
        "58": "58同城",
    }
    return next((platform for marker, platform in rules.items() if marker in path), default_platform)


def source_category(relative_path: Path) -> str:
    if len(relative_path.parts) < 2:
        return ""
    return re.sub(r"_output_folder$", "", relative_path.parts[-2], flags=re.IGNORECASE)


@dataclass(slots=True)
class FileContext:
    relative_path: str
    source_file_id: str
    file_signature: str
    file_mtime_epoch: int
    declared_role: str
    source_category: str
    default_platform: str


@dataclass(slots=True)
class ImportMetrics:
    files_found: int = 0
    files_imported: int = 0
    files_skipped: int = 0
    rows_seen: int = 0
    rows_valid: int = 0
    rows_invalid: int = 0
    batches_written: int = 0
    errors: list[str] = field(default_factory=list)


def prepare_row(record: dict[str, Any], context: FileContext, now: str) -> dict[str, Any]:
    normalized = normalized_record(record)
    values = {field_name: pick(normalized, field_name) for field_name in FIELD_ALIASES}
    platform = infer_platform(context.relative_path, values["source_platform"], context.default_platform)
    declared_role = values["normalized_role"] or values["declared_role"] or context.declared_role
    if values["normalized_role"]:
        declared_role_trust = "PROCESSED_NORMALIZATION"
    elif values["declared_role"]:
        declared_role_trust = "SEARCH_CATEGORY"
    else:
        declared_role_trust = "CURATED_FILE"
    source_job_id = values["source_job_id"]
    company_id = values["company_id"]
    if not company_id and values["company_name"]:
        company_id = f"sourcecompany:{stable_hash(platform + '|' + values['company_name'], 40)}"
    salary = values["salary"]
    if not salary and (values["salary_min"] or values["salary_max"]):
        salary = f"{values['salary_min']}-{values['salary_max']}".strip("-")
    fallback_identity = "|".join(
        [values["title"], values["company_name"], values["description"], values["location"]]
    )
    raw_uid_seed = f"{platform}|{source_job_id}" if source_job_id else f"{platform}|fallback|{stable_hash(fallback_identity, 40)}"
    raw_uid = f"rawjob:{stable_hash(raw_uid_seed, 40)}"
    extras = {
        str(key): value
        for key, value in record.items()
        if normalize_key(key) not in KNOWN_KEYS and value not in (None, "", [], {})
    }
    version_properties = {
        "title": values["title"],
        "description": values["description"],
        "company_id": company_id,
        "company_name": values["company_name"],
        "company_type": values["company_type"],
        "company_size": values["company_size"],
        "company_qualification": values["company_qualification"],
        "industry": values["industry"],
        "province": values["province"],
        "city": values["city"],
        "location": values["location"],
        "education": values["education"],
        "experience": values["experience"],
        "salary": salary,
        "salary_min": values["salary_min"],
        "salary_max": values["salary_max"],
        "tags": values["tags"],
        "publish_time_raw": values["publish_time"],
        "collected_at_raw": values["collected_at"],
        "job_link": values["job_link"],
        "search_city": values["search_city"],
        "search_keyword": values["search_keyword"],
        "skill_tags_raw": values["skill_tags"],
        "ability_analysis_raw": values["ability_analysis"],
        "source_category": context.source_category,
        "declared_role": declared_role,
        "declared_role_trust": declared_role_trust,
        "source_normalized_role": values["normalized_role"],
        "source_platform": platform,
        "source_job_id": source_job_id,
        "extra_json": json.dumps(extras, ensure_ascii=False, separators=(",", ":")) if extras else "",
    }
    # A version represents a business-content change. Collection metadata such
    # as crawl time, search route, URL parameters and extra fields is updated
    # on the same version instead of creating a duplicate version.
    version_identity = {
        key: version_properties[key]
        for key in (
            "title", "description", "company_id", "company_name",
            "company_type", "company_size", "company_qualification", "industry",
            "province", "city", "location", "education", "experience",
            "salary", "salary_min", "salary_max", "tags", "publish_time_raw",
            "skill_tags_raw",
        )
    }
    content_hash = stable_hash(
        json.dumps(version_identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        64,
    )
    version_id = f"rawversion:{stable_hash(raw_uid + '|' + content_hash, 48)}"
    observed_at_raw = values["collected_at"] or values["publish_time"]
    observed_epoch = parse_observed_epoch(observed_at_raw, context.file_mtime_epoch)
    version_properties.update(
        {
            "version_id": version_id,
            "raw_uid": raw_uid,
            "content_hash": content_hash,
            "observed_epoch": observed_epoch,
            "observed_at_raw": observed_at_raw,
        }
    )
    return {
        "raw_uid": raw_uid,
        "version_id": version_id,
        "observed_epoch": observed_epoch,
        "job_props": {
            "source_platform": platform,
            "source_job_id": source_job_id,
            "current_title": values["title"],
            "current_company_name": values["company_name"],
        },
        "version_props": version_properties,
    }


def detect_encoding(path: Path) -> str:
    with path.open("rb") as stream:
        sample = stream.read(8192)
    if sample.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    header = sample.split(b"\n", 1)[0]
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            header.decode(encoding)
            return encoding
        except UnicodeError:
            continue
    return "utf-8-sig"


def iter_json_array(stream: TextIO) -> Iterator[Any]:
    decoder = json.JSONDecoder()
    buffer = ""
    started = False
    finished = False
    while not finished:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            finished = True
        buffer += chunk
        while True:
            stripped = buffer.lstrip()
            buffer = stripped
            if not started:
                if not buffer and not finished:
                    break
                if not buffer.startswith("["):
                    raise ValueError("大型JSON必须是顶层数组，或改成JSONL格式")
                buffer = buffer[1:]
                started = True
                continue
            buffer = buffer.lstrip()
            if buffer.startswith(","):
                buffer = buffer[1:]
                continue
            if buffer.startswith("]"):
                return
            if not buffer:
                break
            try:
                value, end = decoder.raw_decode(buffer)
            except json.JSONDecodeError:
                if finished:
                    raise
                break
            yield value
            buffer = buffer[end:]
    if started:
        raise ValueError("JSON数组未正常结束")


def extract_json_records(value: Any) -> Iterable[dict[str, Any]]:
    if isinstance(value, list):
        return (item for item in value if isinstance(item, dict))
    if isinstance(value, dict):
        for key in ("data", "jobs", "items", "results", "list"):
            nested = value.get(key)
            if isinstance(nested, list):
                return (item for item in nested if isinstance(item, dict))
        return iter((value,))
    return iter(())


def iter_records(path: Path) -> Iterator[dict[str, Any]]:
    encoding = detect_encoding(path)
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding=encoding, newline="", errors="replace") as stream:
            for row in csv.DictReader(stream):
                if row and any(value not in (None, "") for value in row.values()):
                    yield row
        return
    if path.suffix.lower() == ".jsonl":
        with path.open("r", encoding=encoding, errors="replace") as stream:
            for line_number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError(f"JSONL第{line_number}行不是对象")
                yield value
        return
    if path.stat().st_size <= 64 * 1024 * 1024:
        with path.open("r", encoding=encoding, errors="replace") as stream:
            yield from extract_json_records(json.load(stream))
        return
    with path.open("r", encoding=encoding, errors="replace") as stream:
        yield from (value for value in iter_json_array(stream) if isinstance(value, dict))


def discover_files(source_root: Path, extensions: set[str]) -> list[Path]:
    return sorted(
        path
        for path in source_root.rglob("*")
        if path.is_file() and path.suffix.lower() in extensions and not path.name.startswith("~$")
    )


def file_context(
    path: Path,
    source_root: Path,
    default_platform: str,
    source_id_prefix: str = "",
) -> FileContext:
    relative = path.relative_to(source_root)
    stat = path.stat()
    relative_text = relative.as_posix()
    source_identity = (
        f"{source_id_prefix}|{relative_text}"
        if source_id_prefix
        else relative_text
    )
    return FileContext(
        relative_path=relative_text,
        source_file_id=f"rawsource:{stable_hash(source_identity.lower(), 40)}",
        file_signature=f"{stat.st_size}:{stat.st_mtime_ns}",
        file_mtime_epoch=int(stat.st_mtime),
        declared_role=path.stem,
        source_category=source_category(relative),
        default_platform=default_platform,
    )


CONSTRAINTS = (
    "CREATE CONSTRAINT raw_job_uid IF NOT EXISTS FOR (n:RawJob) REQUIRE n.raw_uid IS UNIQUE",
    "CREATE CONSTRAINT raw_jd_version_id IF NOT EXISTS FOR (n:RawJDVersion) REQUIRE n.version_id IS UNIQUE",
    "CREATE CONSTRAINT raw_source_file_id IF NOT EXISTS FOR (n:RawSourceFile) REQUIRE n.source_file_id IS UNIQUE",
    "CREATE CONSTRAINT raw_ingestion_run_id IF NOT EXISTS FOR (n:RawIngestionRun) REQUIRE n.run_id IS UNIQUE",
)


BATCH_QUERY = """
UNWIND $rows AS row
MERGE (job:RawJob {raw_uid:row.raw_uid})
ON CREATE SET job.first_seen_at = $now
SET job.last_seen_at = $now
WITH row, job
OPTIONAL MATCH (job)-[:HAS_VERSION]->(matching:RawJDVersion)
WHERE coalesce(toString(matching.title), '') = coalesce(toString(row.version_props.title), '')
  AND coalesce(toString(matching.description), '') = coalesce(toString(row.version_props.description), '')
  AND coalesce(toString(matching.company_id), '') = coalesce(toString(row.version_props.company_id), '')
  AND coalesce(toString(matching.company_name), '') = coalesce(toString(row.version_props.company_name), '')
  AND coalesce(toString(matching.company_type), '') = coalesce(toString(row.version_props.company_type), '')
  AND coalesce(toString(matching.company_size), '') = coalesce(toString(row.version_props.company_size), '')
  AND coalesce(toString(matching.company_qualification), '') = coalesce(toString(row.version_props.company_qualification), '')
  AND coalesce(toString(matching.industry), '') = coalesce(toString(row.version_props.industry), '')
  AND coalesce(toString(matching.province), '') = coalesce(toString(row.version_props.province), '')
  AND coalesce(toString(matching.city), '') = coalesce(toString(row.version_props.city), '')
  AND coalesce(toString(matching.location), '') = coalesce(toString(row.version_props.location), '')
  AND coalesce(toString(matching.education), '') = coalesce(toString(row.version_props.education), '')
  AND coalesce(toString(matching.experience), '') = coalesce(toString(row.version_props.experience), '')
  AND coalesce(toString(matching.salary), '') = coalesce(toString(row.version_props.salary), '')
  AND coalesce(toString(matching.salary_min), '') = coalesce(toString(row.version_props.salary_min), '')
  AND coalesce(toString(matching.salary_max), '') = coalesce(toString(row.version_props.salary_max), '')
  AND coalesce(toString(matching.tags), '') = coalesce(toString(row.version_props.tags), '')
  AND coalesce(toString(matching.publish_time_raw), '') = coalesce(toString(row.version_props.publish_time_raw), '')
  AND coalesce(toString(matching.skill_tags_raw), '') = coalesce(toString(row.version_props.skill_tags_raw), '')
WITH row, job, head(collect(matching)) AS existing
CALL (row, existing) {
  WITH row, existing
  WHERE existing IS NOT NULL
  SET existing.collected_at_raw = row.version_props.collected_at_raw,
      existing.observed_at_raw = row.version_props.observed_at_raw,
      existing.observed_epoch = row.version_props.observed_epoch,
      existing.job_link = row.version_props.job_link,
      existing.search_city = row.version_props.search_city,
      existing.search_keyword = row.version_props.search_keyword,
      existing.source_category = row.version_props.source_category,
      existing.declared_role = row.version_props.declared_role,
      existing.declared_role_trust = row.version_props.declared_role_trust,
      existing.source_normalized_role = row.version_props.source_normalized_role,
      existing.ability_analysis_raw = row.version_props.ability_analysis_raw,
      existing.extra_json = row.version_props.extra_json,
      existing.business_content_hash = row.version_props.content_hash,
      existing.last_seen_at = $now,
      existing.last_ingest_run_id = $run_id
  RETURN existing AS version
  UNION
  WITH row, existing
  WHERE existing IS NULL
  MERGE (created:RawJDVersion {version_id:row.version_id})
  ON CREATE SET created.created_at = $now, created.first_ingested_at = $now
  SET created += row.version_props,
      created.business_content_hash = row.version_props.content_hash,
      created.last_seen_at = $now,
      created.last_ingest_run_id = $run_id
  RETURN created AS version
}
MERGE (job)-[:HAS_VERSION]->(version)
WITH job, version, row,
     row.observed_epoch >= coalesce(job.current_observed_epoch, -1) AS make_current
OPTIONAL MATCH (job)-[old:CURRENT_VERSION]->(oldVersion:RawJDVersion)
WHERE make_current AND oldVersion.version_id <> version.version_id
DELETE old
FOREACH (_ IN CASE WHEN make_current THEN [1] ELSE [] END |
    SET job += row.job_props,
        job.current_version_id = version.version_id,
        job.current_observed_epoch = row.observed_epoch,
        job.last_ingest_run_id = $run_id
)
FOREACH (_ IN CASE WHEN make_current THEN [1] ELSE [] END |
    MERGE (job)-[:CURRENT_VERSION]->(version)
)
RETURN count(*) AS processed
"""


class RawJDImporter:
    def __init__(self, repository: Neo4jGraphRepository, batch_size: int, max_batch_bytes: int):
        self.client = repository.client
        self.batch_size = max(1, min(batch_size, 1000))
        self.max_batch_bytes = max(256 * 1024, max_batch_bytes)

    def initialize(self) -> None:
        for statement in CONSTRAINTS:
            self.client.query(statement, access_mode="Write")

    def start_run(self, run_id: str, source_root: Path, now: str) -> None:
        # Only the latest status is useful here; detailed history is already in
        # the small last_run.json report.
        self.client.query(
            "MATCH (old:RawIngestionRun) DETACH DELETE old",
            access_mode="Write",
        )
        self.client.query(
            "MERGE (r:RawIngestionRun {run_id:$run_id}) "
            "SET r.status='RUNNING', r.started_at=$now, r.source_root=$source_root",
            {"run_id": run_id, "now": now, "source_root": str(source_root)},
            access_mode="Write",
        )

    def finish_run(self, run_id: str, status: str, metrics: ImportMetrics, now: str) -> None:
        properties = asdict(metrics)
        properties["errors"] = json.dumps(properties["errors"], ensure_ascii=False)
        self.client.query(
            "MATCH (r:RawIngestionRun {run_id:$run_id}) "
            "SET r += $properties, r.status=$status, r.finished_at=$now",
            {"run_id": run_id, "properties": properties, "status": status, "now": now},
            access_mode="Write",
        )

    def source_is_current(self, context: FileContext, force: bool) -> bool:
        if force:
            return False
        rows = self.client.query(
            "MATCH (f:RawSourceFile {source_file_id:$source_file_id}) "
            "RETURN f.file_signature AS signature, f.status AS status",
            {"source_file_id": context.source_file_id},
        )
        return bool(
            rows
            and rows[0].get("signature") == context.file_signature
            and rows[0].get("status") == "COMPLETED"
        )

    def start_source(self, context: FileContext, run_id: str, now: str) -> None:
        self.client.query(
            """
            MERGE (f:RawSourceFile {source_file_id:$source_file_id})
            SET f.relative_path=$relative_path, f.file_signature=$file_signature,
                f.status='IMPORTING', f.started_at=$now
            WITH f
            MATCH (r:RawIngestionRun {run_id:$run_id})
            MERGE (r)-[:TOUCHED_FILE]->(f)
            """,
            {
                "source_file_id": context.source_file_id,
                "relative_path": context.relative_path,
                "file_signature": context.file_signature,
                "run_id": run_id,
                "now": now,
            },
            access_mode="Write",
        )

    def finish_source(
        self,
        context: FileContext,
        row_count: int,
        run_id: str,
        now: str,
        completed: bool = True,
    ) -> None:
        self.client.query(
            "MATCH (f:RawSourceFile {source_file_id:$source_file_id}) "
            "SET f.status=$status, f.row_count=$row_count, f.finished_at=$now, f.last_run_id=$run_id",
            {
                "source_file_id": context.source_file_id,
                "row_count": row_count,
                "run_id": run_id,
                "now": now,
                "status": "COMPLETED" if completed else "PARTIAL",
            },
            access_mode="Write",
        )

    def write_batch(self, context: FileContext, run_id: str, rows: list[dict[str, Any]], now: str) -> None:
        self.client.query(
            BATCH_QUERY,
            {
                "source_file_id": context.source_file_id,
                "run_id": run_id,
                "rows": rows,
                "now": now,
            },
            access_mode="Write",
        )

    def import_file(
        self,
        path: Path,
        context: FileContext,
        run_id: str,
        metrics: ImportMetrics,
        max_rows: int = 0,
    ) -> None:
        now = utc_now()
        self.start_source(context, run_id, now)
        batch: list[dict[str, Any]] = []
        batch_bytes = 0
        row_count = 0
        for record in iter_records(path):
            metrics.rows_seen += 1
            row_count += 1
            if max_rows and row_count > max_rows:
                break
            if not isinstance(record, dict):
                metrics.rows_invalid += 1
                continue
            row = prepare_row(record, context, now)
            row_size = len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            if batch and (len(batch) >= self.batch_size or batch_bytes + row_size > self.max_batch_bytes):
                self.write_batch(context, run_id, batch, now)
                metrics.batches_written += 1
                batch = []
                batch_bytes = 0
            batch.append(row)
            batch_bytes += row_size
            metrics.rows_valid += 1
        if batch:
            self.write_batch(context, run_id, batch, now)
            metrics.batches_written += 1
        self.finish_source(
            context,
            min(row_count, max_rows) if max_rows else row_count,
            run_id,
            utc_now(),
            completed=not bool(max_rows),
        )


def check_sources(
    files: list[Path],
    source_root: Path,
    default_platform: str,
    sample_rows: int,
    source_id_prefix: str = "",
) -> dict:
    metrics = ImportMetrics(files_found=len(files))
    formats: dict[str, int] = {}
    samples: list[dict[str, Any]] = []
    for path in files:
        context = file_context(path, source_root, default_platform, source_id_prefix)
        count = 0
        try:
            for record in iter_records(path):
                metrics.rows_seen += 1
                count += 1
                if len(samples) < 10:
                    row = prepare_row(record, context, utc_now())
                    samples.append(
                        {
                            "file": context.relative_path,
                            "source_platform": row["job_props"]["source_platform"],
                            "title": row["job_props"]["current_title"],
                            "description_length": len(row["version_props"]["description"]),
                        }
                    )
                if sample_rows and count >= sample_rows:
                    break
            metrics.rows_valid += count
            formats[path.suffix.lower()] = formats.get(path.suffix.lower(), 0) + 1
        except Exception as error:
            metrics.errors.append(f"{context.relative_path}: {error}")
    return {
        "source_root": str(source_root),
        "sample_rows_per_file": sample_rows,
        "metrics": asdict(metrics),
        "formats": formats,
        "samples": samples,
    }


def status(repository: Neo4jGraphRepository) -> dict:
    rows = repository.client.query(
        """
        CALL { MATCH (n:RawJob) RETURN count(n) AS raw_jobs }
        CALL { MATCH (n:RawJDVersion) RETURN count(n) AS raw_versions }
        CALL { MATCH (n:RawSourceFile) RETURN count(n) AS source_files }
        CALL { MATCH (n:RawIngestionRun) RETURN count(n) AS ingestion_runs }
        RETURN raw_jobs, raw_versions, source_files, ingestion_runs
        """
    )
    latest = repository.client.query(
        "MATCH (r:RawIngestionRun) RETURN properties(r) AS run ORDER BY r.started_at DESC LIMIT 1"
    )
    return {"counts": rows[0] if rows else {}, "latest_run": latest[0]["run"] if latest else {}}


def load_config(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    csv.field_size_limit(2**31 - 1)
    parser = argparse.ArgumentParser(description="将全量原始JD以流式、增量方式导入Neo4j")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "raw_jd_layer" / "config.json")
    parser.add_argument("--neo4j-config", type=Path, default=PROJECT_ROOT / "config" / "neo4j_connection.json")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--default-platform")
    parser.add_argument(
        "--source-id-prefix",
        default="",
        help="可选来源命名空间；多个平台存在同名文件时用于避免 RawSourceFile ID 冲突。",
    )
    parser.add_argument("--batch-size", type=int)
    parser.add_argument("--max-batch-bytes", type=int)
    parser.add_argument("--limit-files", type=int, default=0)
    parser.add_argument("--max-rows-per-file", type=int, default=0)
    parser.add_argument("--sample-rows-per-file", type=int, default=10)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--status-only", action="store_true")
    parser.add_argument("--report", type=Path, default=PROJECT_ROOT / "output" / "raw_jd_ingestion" / "last_run.json")
    args = parser.parse_args()

    config = load_config(args.config.resolve())
    source_input = (args.source or (PROJECT_ROOT / config["source_root"])).resolve()
    source_root = source_input.parent if source_input.is_file() else source_input
    default_platform = args.default_platform or config.get("default_platform", "未知平台")
    extensions = {value.lower() for value in config.get("extensions", [".csv", ".json", ".jsonl"])}
    if source_input.is_file():
        files = (
            [source_input]
            if source_input.suffix.lower() in extensions and not source_input.name.startswith("~$")
            else []
        )
    else:
        files = discover_files(source_root, extensions) if source_root.exists() else []
    if args.limit_files:
        files = files[: args.limit_files]

    if args.check_only:
        report = check_sources(
            files,
            source_root,
            default_platform,
            args.sample_rows_per_file,
            args.source_id_prefix,
        )
        write_report(args.report.resolve(), report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    repository = Neo4jGraphRepository(args.neo4j_config.resolve())
    if args.status_only:
        print(json.dumps(status(repository), ensure_ascii=False, indent=2))
        return

    if not files:
        raise FileNotFoundError(f"没有找到可导入的CSV/JSON：{source_root}")
    importer = RawJDImporter(
        repository,
        args.batch_size or int(config.get("batch_size", 100)),
        args.max_batch_bytes or int(config.get("max_batch_bytes", 4 * 1024 * 1024)),
    )
    importer.initialize()
    run_id = f"rawrun:{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}"
    started_at = utc_now()
    metrics = ImportMetrics(files_found=len(files))
    importer.start_run(run_id, source_root, started_at)
    started = time.monotonic()
    try:
        for index, path in enumerate(files, 1):
            context = file_context(
                path,
                source_root,
                default_platform,
                args.source_id_prefix,
            )
            if importer.source_is_current(context, args.force):
                metrics.files_skipped += 1
                print(f"[{index}/{len(files)}] 跳过未变化文件：{context.relative_path}")
                continue
            print(f"[{index}/{len(files)}] 导入：{context.relative_path}")
            importer.import_file(path, context, run_id, metrics, args.max_rows_per_file)
            metrics.files_imported += 1
            print(f"  已累计处理 {metrics.rows_valid:,} 条，写入 {metrics.batches_written:,} 批")
        importer.finish_run(run_id, "COMPLETED", metrics, utc_now())
    except Exception as error:
        metrics.errors.append(str(error))
        importer.finish_run(run_id, "FAILED", metrics, utc_now())
        raise
    report = {
        "run_id": run_id,
        "status": "COMPLETED",
        "source_root": str(source_root),
        "elapsed_seconds": round(time.monotonic() - started, 2),
        "metrics": asdict(metrics),
        "neo4j_status": status(repository),
    }
    write_report(args.report.resolve(), report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

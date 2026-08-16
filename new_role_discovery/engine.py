from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import traceback
from calendar import monthrange
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Callable, Iterable

from .config import EvolutionConfig
from .qwen_reviewer import SemanticReviewer
from .statistics import (
    add_bh_qvalues,
    compare_proportions,
    jensen_shannon_divergence,
    weighted_jaccard,
    wilson_interval,
)


TITLE_NOISE = re.compile(
    r"(?i)(急聘|急招|诚聘|高薪|直招|热招|五险一金|五险|一金|双休|"
    r"周末双休|朝九晚五|不加班|包吃住|包吃包住|专人培训|带薪培训|"
    r"校招|社招|应届生|实习生|储备)"
)
TITLE_PREFIX_LEVEL = re.compile(
    r"(?i)^\s*(?:(?:首席|资深|高级|中级|初级|助理|见习|"
    r"senior|junior|lead|principal|sr\.?|jr\.?)\s*)+"
)
TITLE_CODE = re.compile(r"(?i)[（(]?\s*[JZ]\d{3,}\s*[)）]?")
TITLE_SALARY = re.compile(r"(?i)\d+(?:-\d+)?(?:k|千|万)(?:/月|·\d+薪)?")
TITLE_BRACKETED_QUALIFIER = re.compile(
    r"[（(【\[][^()（）【】\[\]]{0,60}[)）】\]]"
)
TITLE_TRAILING_DIRECTION = re.compile(
    r"(?i)\s*[-—–|｜]\s*[^-—–|｜]{1,40}"
    r"(?:方向|业务|项目|岗位|团队|城市|地区)\s*$"
)
TITLE_INFIX_LEVEL = re.compile(
    r"(?i)(?:首席|资深|高级|中级|初级|助理|见习|"
    r"senior|junior|lead|principal|sr\.?|jr\.?)"
    r"(?=(?:工程师|开发|测试|设计|架构|算法|顾问|专员))"
)
TITLE_ROLE_ENDING = re.compile(
    r"(?i)(?:工程师|开发|测试|架构师|设计师|分析师|算法|顾问|"
    r"产品经理|项目经理|运维|实施|运营|专员|负责人|专家|主管|总监)$"
)
TITLE_ENGINEERING_LEVEL_SUFFIX = re.compile(r"(?:负责人|专家)$")
TITLE_COMPLETE_ROLE_WITH_SUFFIX = re.compile(
    r"(?i)^(.+?(?:工程师|架构师|设计师|分析师|产品经理|项目经理|"
    r"顾问|专员|负责人|专家|主管|总监|开发|测试|运维|实施|运营))"
    r"\s*[-—–|｜/／\\、,，]\s*"
    r"(?!.*(?:工程师|架构师|设计师|分析师|产品经理|项目经理|"
    r"顾问|专员|负责人|专家|主管|总监)\s*$).+$"
)
TITLE_TECH_PREFIX = re.compile(r"(?i)^(?:python|java|c\+\+|go)(?=.+)")
TITLE_PUNCT = re.compile(r"[\s\-—–·|/\\,，、;；:：()（）\[\]【】{}<>《》&＆]+")
GENERIC_TITLES = {
    "工程师",
    "开发工程师",
    "技术工程师",
    "技术员",
    "专员",
    "助理",
    "主管",
    "经理",
    "python",
    "java",
    "ai",
    "it",
    "qa",
    "人工智能",
    "c",
    "c++",
}
ENGLISH_ROLE_TITLE_ALIASES = {
    "electricalengineer": "电气工程师",
    "dataengineer": "数据工程师",
    "testingengineer": "测试工程师",
    "testengineer": "测试工程师",
    "softwareengineer": "软件工程师",
    "softwaredeveloper": "软件开发工程师",
    "hardwareengineer": "硬件工程师",
    "frontendengineer": "前端开发工程师",
    "frontenddeveloper": "前端开发工程师",
    "backendengineer": "后端开发工程师",
    "backenddeveloper": "后端开发工程师",
    "fullstackengineer": "全栈开发工程师",
    "productmanager": "产品经理",
    "projectmanager": "项目经理",
    "machinelearningengineer": "机器学习工程师",
    "deeplearningengineer": "深度学习工程师",
    "algorithmengineer": "算法工程师",
}
RESPONSIBILITY_CUTOFF = re.compile(
    r"(任职要求|岗位要求|职位要求|任职资格|我们希望你|资格要求)",
    flags=re.IGNORECASE,
)
RESPONSIBILITY_HEADING = re.compile(
    r"^(岗位职责|工作职责|工作内容|主要职责|职位描述|职责描述)[:：]?\s*",
    flags=re.IGNORECASE,
)
RESPONSIBILITY_EXCLUDE = re.compile(
    r"(薪资|福利|五险|公积金|双休|团建|年龄|投递|联系方式|招聘人数|上班时间)"
)


@dataclass(slots=True)
class JDRecord:
    jd_id: str
    canonical_role: str
    role_id: str
    company_id: str
    title: str
    normalized_title: str
    posted_at: datetime
    description: str
    industry: str
    source_file: str
    source_family: str
    template_key: str
    month: str
    period: str


def parse_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    normalized = text.replace("/", "-").replace("T", " ").replace("Z", "")
    for pattern in (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(normalized, pattern)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def normalize_title(value: str) -> str:
    text = str(value or "").strip().lower()
    text = TITLE_BRACKETED_QUALIFIER.sub("", text)
    text = TITLE_TRAILING_DIRECTION.sub("", text)
    complete_role = TITLE_COMPLETE_ROLE_WITH_SUFFIX.match(text)
    if complete_role:
        text = complete_role.group(1)
    parts = [
        part.strip()
        for part in re.split(r"[/／|｜]", text)
        if part.strip()
    ]
    role_parts = [part for part in parts if TITLE_ROLE_ENDING.search(part)]
    if len(role_parts) >= 2:
        # A slash-separated title usually advertises multiple aliases/roles.
        # Use the shortest complete role phrase as the clustering key instead
        # of concatenating every phrase into a false new concept.
        text = min(role_parts, key=lambda part: (len(part), part))
    text = TITLE_NOISE.sub("", text)
    text = TITLE_CODE.sub("", text)
    text = TITLE_SALARY.sub("", text)
    text = TITLE_PREFIX_LEVEL.sub("", text)
    text = TITLE_INFIX_LEVEL.sub("", text)
    text = TITLE_PUNCT.sub("", text)
    text = re.sub(r"(工程师)(?:部门)?工程师$", r"\1", text)
    text = text.strip("+＋")
    text = re.sub(r"(岗位|职位|招聘)$", "", text)
    if (
        TITLE_ENGINEERING_LEVEL_SUFFIX.search(text)
        and re.search(r"(?:软件|硬件|测试|研发|开发|算法|系统|平台|数据)", text)
        and "项目" not in text
    ):
        text = TITLE_ENGINEERING_LEVEL_SUFFIX.sub("工程师", text)
    return text[:80]


def display_title(value: str) -> str:
    """Return a stable human-facing name from a cleaned clustering key."""
    text = str(value or "").strip()
    replacements = (
        (r"(?i)java", "Java"),
        (r"(?i)python", "Python"),
        (r"(?i)dataops", "DataOps"),
        (r"(?i)devops", "DevOps"),
        (r"(?i)fae", "FAE"),
        (r"(?i)ae", "AE"),
        (r"(?i)ntn", "NTN"),
        (r"(?i)ai", "AI"),
        (r"(?i)bi", "BI"),
        (r"(?i)cad", "CAD"),
        (r"(?i)cae", "CAE"),
        (r"(?i)cam", "CAM"),
        (r"(?i)\.net", ".NET"),
        (r"(?i)c\+\+", "C++"),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text)
    return text[:80]


def normalized_text_hash(value: str) -> str:
    normalized = re.sub(r"\s+", "", str(value or "").lower())
    normalized = re.sub(r"\d+", "#", normalized)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:20]


def stable_id(prefix: str, *parts: Any) -> str:
    payload = "\x1f".join(str(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:20]
    return f"{prefix}:{digest}"


def source_group(source_file: str) -> str:
    normalized = str(source_file or "").replace("\\", "/")
    family = normalized.split("/", 1)[0].strip()
    return family or "UNKNOWN"


def extract_responsibility_sentences(description: str) -> list[str]:
    if not description:
        return []
    prefix = RESPONSIBILITY_CUTOFF.split(description, maxsplit=1)[0]
    prefix = RESPONSIBILITY_HEADING.sub("", prefix.strip())
    segments = re.split(r"[\r\n]+|(?<=[。；;])", prefix)
    output: list[str] = []
    for segment in segments:
        sentence = RESPONSIBILITY_HEADING.sub("", segment).strip(" \t-—•·0123456789.、）)")
        sentence = re.sub(r"\s+", " ", sentence)
        if not 8 <= len(sentence) <= 220:
            continue
        if RESPONSIBILITY_EXCLUDE.search(sentence):
            continue
        if sentence not in output:
            output.append(sentence)
    return output[:8]


class EvolutionEngine:
    def __init__(
        self,
        config: EvolutionConfig,
        progress_callback: Callable[[str, int, str], None] | None = None,
        data_source: Any | None = None,
    ):
        self.config = config
        self.progress_callback = progress_callback
        self.data_source = data_source
        self.output_dir: Path | None = None
        self.run_manifest: dict[str, Any] = {}
        self.skill_mapping_quality: dict[str, Any] = {
            "strategy": "raw_skill_fallback",
            "eligible_edges": 0,
            "normalized_edges": 0,
            "normalization_coverage": 0.0,
        }
        self.jd_skill_evidence: dict[tuple[str, str], str] = {}
        self.standardized_skill_ids: set[str] = set()
        self.raw_fallback_skill_ids: set[str] = set()

    def _progress(self, stage: str, percent: int, message: str) -> None:
        if self.progress_callback is None:
            return
        try:
            self.progress_callback(stage, max(0, min(100, percent)), message)
        except Exception:  # noqa: BLE001 - progress reporting cannot fail a run
            pass

    def run(self) -> dict[str, Any]:
        self._progress("PREPARING", 2, "正在准备本轮分析")
        started = datetime.now().astimezone()
        source_metadata: dict[str, Any] = {}
        if self.data_source is not None:
            set_progress = getattr(
                self.data_source,
                "set_progress_callback",
                None,
            )
            if callable(set_progress):
                set_progress(self._progress)
            source_metadata = dict(self.data_source.capture())
            source_identity = str(self.data_source.identity())
        else:
            if self.config.database_path is None:
                raise ValueError("database_path is required for the SQLite source")
            source_identity = str(self.config.database_path.resolve())
        run_id = stable_id(
            "evolution_run",
            started.isoformat(timespec="microseconds"),
            source_identity,
        )
        self.output_dir = self.config.output_root / run_id.replace(":", "_")
        self.output_dir.mkdir(parents=True, exist_ok=False)
        self.run_manifest = {
            "run_id": run_id,
            "state": "CREATED",
            "started_at": started.isoformat(timespec="seconds"),
            "completed_at": "",
            "config": self.config.to_dict(),
            "warnings": [],
            "summary": {},
            "output_dir": str(self.output_dir),
            "source": source_metadata,
        }
        self._write_json("run_manifest.json", self.run_manifest)

        try:
            self._progress("READING_DATA", 5, "正在校验并读取知识图谱")
            if self.data_source is not None:
                raw_rows = self.data_source.load_jd_rows()
                rows, date_quality = self._prepare_jd_rows(raw_rows)
                cutoff, as_of, baseline_start = self._resolve_windows(rows)
                jds = self._build_jd_records(rows, cutoff, as_of, baseline_start)
                if not jds:
                    raise ValueError("No usable JD records fall inside the configured windows")
                if self.config.skill_review_limit > 0 and self.config.skill_change_source_family:
                    source_family = self.config.skill_change_source_family
                    jds = {
                        jd_id: jd
                        for jd_id, jd in jds.items()
                        if jd.source_family == source_family
                    }
                    if not jds:
                        raise ValueError(
                            f"No usable JD records for skill-change source {source_family}"
                        )
                descriptions = (
                    self.data_source.load_descriptions(
                        jd_id
                        for jd_id, jd in jds.items()
                        if jd.period == "current"
                    )
                    if self.config.role_review_limit > 0
                    else {}
                )
                for jd_id, description in descriptions.items():
                    if jd_id in jds:
                        jds[jd_id].description = description
                del raw_rows
                del rows
                known_titles = (
                    {
                        normalize_title(title)
                        for title in self.data_source.load_known_titles()
                        if normalize_title(title)
                    }
                    if self.config.role_review_limit > 0
                    else set()
                )
                edges: Iterable[dict[str, Any]] = (
                    self.data_source.iter_skill_edges(
                        allowed_jd_ids=set(jds),
                        verified_status=self.config.verified_status,
                        categories=self.config.focus_categories,
                    )
                )
            else:
                connection = self._connect()
                try:
                    schema = self._validate_schema(connection)
                    rows, date_quality = self._read_jd_rows(connection, schema)
                    cutoff, as_of, baseline_start = self._resolve_windows(rows)
                    jds = self._build_jd_records(
                        rows,
                        cutoff,
                        as_of,
                        baseline_start,
                    )
                    if not jds:
                        raise ValueError(
                            "No usable JD records fall inside the configured windows"
                        )
                    edges = self._load_skill_edges(connection, jds)
                    known_titles = self._load_known_titles(connection)
                    skill_names = self._load_skill_names(connection, edges)
                finally:
                    connection.close()

            if self.data_source is None:
                self._progress(
                    "AGGREGATING",
                    38,
                    "正在计算企业覆盖与能力变化",
                )
            (
                role_company,
                role_jd,
                skill_company,
                skill_jd,
                skill_evidence,
                jd_skills,
            ) = self._aggregate_skill_evidence(jds, edges)
            if self.data_source is not None:
                self._progress(
                    "AGGREGATING",
                    38,
                    "Neo4j 能力证据读取完成，正在计算覆盖率",
                )
                self.skill_mapping_quality = dict(
                    self.data_source.mapping_quality
                )
                self.standardized_skill_ids.update(
                    self.data_source.standardized_skill_ids
                )
                self.raw_fallback_skill_ids.update(
                    self.data_source.raw_fallback_skill_ids
                )
                skill_names = dict(self.data_source.skill_names)
                verified_skill_edges = int(self.data_source.edge_count)
                self.data_source.verify_unchanged()
                self.run_manifest["source"] = dict(
                    self.data_source.public_metadata()
                )
            else:
                verified_skill_edges = len(edges)  # type: ignore[arg-type]
            quality = self._quality_report(
                jds,
                date_quality,
                cutoff,
                as_of,
                baseline_start,
            )

            discovery_message = (
                "正在聚类岗位名称并比较历史与近期数据"
                if self.config.skill_review_limit <= 0
                else "正在筛选新岗位与能力变化候选"
            )
            self._progress("DISCOVERING", 52, discovery_message)
            role_candidates = (
                self._discover_roles(
                    jds,
                    known_titles,
                    role_company,
                    skill_company,
                    jd_skills,
                    skill_names,
                )
                if self.config.role_review_limit > 0
                else []
            )
            skill_changes = (
                self._discover_skill_changes(
                    jds,
                    role_company,
                    role_jd,
                    skill_company,
                    skill_jd,
                    skill_evidence,
                    skill_names,
                )
                if self.config.skill_review_limit > 0
                else []
            )

            role_review = [
                row for row in role_candidates if row["rule_state"] == "REVIEW"
            ][: self.config.role_review_limit]
            skill_review = [
                row for row in skill_changes if row["rule_state"] == "REVIEW"
            ][: self.config.skill_review_limit]
            if self.config.llm_enabled:
                self._attach_nearest_skill_names(skill_review, skill_names)

            self._progress("SEMANTIC_REVIEW", 70, "正在执行低成本语义复核")
            reviewer = SemanticReviewer(
                self.config,
                self.config.output_root / "_llm_cache",
            )
            for candidate in role_review:
                packet = self._role_llm_packet(candidate)
                candidate["semantic_review"] = reviewer.review_role(packet).to_dict()
            for candidate in skill_review:
                packet = self._skill_llm_packet(candidate)
                candidate["semantic_review"] = reviewer.review_skill(packet).to_dict()

            self._progress("ASSEMBLING", 84, "正在组装审核任务与图谱草案")
            selected_role_ids = {row["candidate_id"] for row in role_review}
            selected_skill_ids = {row["candidate_id"] for row in skill_review}
            for candidate in role_candidates:
                if candidate["rule_state"] == "REVIEW" and candidate["candidate_id"] not in selected_role_ids:
                    candidate["rule_state"] = "WATCH"
                    candidate["rule_reasons"].append("REVIEW_BUDGET_EXCEEDED")
            for candidate in skill_changes:
                if candidate["rule_state"] == "REVIEW" and candidate["candidate_id"] not in selected_skill_ids:
                    candidate["rule_state"] = "WATCH"
                    candidate["rule_reasons"].append("REVIEW_BUDGET_EXCEEDED")

            review_queue = self._review_queue(role_review, skill_review)
            watchlist = {
                "new_roles": [
                    row for row in role_candidates if row["rule_state"] == "WATCH"
                ],
                "skill_changes": [
                    row for row in skill_changes if row["rule_state"] == "WATCH"
                ],
            }
            graph_patch = self._graph_patch_draft(run_id, role_review, skill_review)
            llm_usage = reviewer.usage_summary()
            evaluation_sample = self._evaluation_sample(role_candidates)
            evaluation_report = {
                "status": "AWAITING_EXPERT_LABELS",
                "sample_size": len(evaluation_sample),
                "labeled_items": 0,
                "label_classes": [
                    "NEW_ROLE",
                    "SPECIALIZATION",
                    "ALIAS",
                    "NOISE",
                    "UNCERTAIN",
                ],
                "sampling": (
                    "按 REVIEW、WATCH、AUTO_REJECT 分层并在层内按涌现分排序；"
                    "正式评估须由两名专家独立标注，冲突交第三人裁决。"
                ),
                "temporal_validation": (
                    "参数校准使用较早月份，Precision@K 等最终指标只在"
                    "后续不可见月份计算，禁止随机拆分同模板 JD。"
                ),
                "metrics": {
                    "precision_at_2": None,
                    "precision_at_5": None,
                    "precision_at_10": None,
                    "false_positive_rate": None,
                    "expert_acceptance_rate": None,
                    "unsupported_claim_rate": None,
                    "top_k_stability_jaccard": None,
                },
            }

            summary = {
                "cutoff": cutoff.isoformat(timespec="seconds"),
                "as_of": as_of.isoformat(timespec="seconds"),
                "baseline_start": baseline_start.isoformat(timespec="seconds"),
                "jds": len(jds),
                "historical_jds": sum(row.period == "historical" for row in jds.values()),
                "baseline_jds": sum(row.period == "baseline" for row in jds.values()),
                "current_jds": sum(row.period == "current" for row in jds.values()),
                "companies": len({row.company_id for row in jds.values() if row.company_id}),
                "roles": len({row.canonical_role for row in jds.values()}),
                "verified_skill_edges": verified_skill_edges,
                "normalized_skill_edges": self.skill_mapping_quality["normalized_edges"],
                "new_role_candidates": len(role_candidates),
                "new_role_review_tasks": len(role_review),
                "new_role_watch_candidates": sum(
                    row["rule_state"] == "WATCH"
                    for row in role_candidates
                ),
                "multi_source_candidates": sum(
                    int(row.get("independent_source_count") or 0)
                    >= self.config.min_independent_sources
                    for row in role_candidates
                ),
                "persistent_growth_candidates": sum(
                    int(row.get("growth_windows") or 0)
                    >= self.config.min_consecutive_months
                    for row in role_candidates
                ),
                "skill_change_candidates": len(skill_changes),
                "skill_review_tasks": len(skill_review),
                "review_tasks": len(review_queue),
                "llm_requests": llm_usage["requests"],
                "llm_cache_hits": llm_usage["cache_hits"],
                "graph_patch_actions": len(graph_patch["actions"]),
                "dry_run": self.config.dry_run,
            }

            self._write_json("data_quality_report.json", quality)
            self._write_json("new_role_candidates.json", role_candidates)
            self._write_json("role_skill_changes.json", skill_changes)
            self._write_json("watchlist.json", watchlist)
            self._write_json(
                "llm_role_analysis.json",
                [
                    {
                        "candidate_id": row["candidate_id"],
                        "candidate_title": row["candidate_title"],
                        "semantic_review": row.get("semantic_review", {}),
                    }
                    for row in role_review
                ],
            )
            self._write_json(
                "llm_skill_analysis.json",
                [
                    {
                        "candidate_id": row["candidate_id"],
                        "role": row["role"],
                        "skill": row["skill"],
                        "semantic_review": row.get("semantic_review", {}),
                    }
                    for row in skill_review
                ],
            )
            self._write_json("llm_usage.json", llm_usage)
            self._write_json("review_queue.json", review_queue)
            self._write_review_csv(review_queue)
            self._write_json("graph_patch_draft.json", graph_patch)
            self._write_json("evaluation_sample.json", evaluation_sample)
            self._write_json("evaluation_report.json", evaluation_report)

            self._progress("REPORTING", 94, "正在生成结论报告")
            from .reporting import write_markdown_report

            write_markdown_report(
                self.output_dir / "conclusion_report.md",
                summary=summary,
                quality=quality,
                role_candidates=role_candidates,
                skill_changes=skill_changes,
                review_queue=review_queue,
                llm_usage=llm_usage,
            )

            self.run_manifest["state"] = "REVIEW_READY"
            self.run_manifest["completed_at"] = datetime.now().astimezone().isoformat(
                timespec="seconds"
            )
            self.run_manifest["summary"] = summary
            self.run_manifest["resolved_windows"] = {
                "cutoff": cutoff.isoformat(timespec="seconds"),
                "as_of": as_of.isoformat(timespec="seconds"),
                "baseline_start": baseline_start.isoformat(timespec="seconds"),
            }
            self.run_manifest["warnings"].extend(quality["warnings"])
            if reviewer.disabled_reason:
                self.run_manifest["warnings"].append(reviewer.disabled_reason)
            self._write_json("run_manifest.json", self.run_manifest)
            self._progress("COMPLETED", 100, "分析完成，等待人工复核")
            return {
                "run_id": run_id,
                "output_dir": str(self.output_dir),
                "summary": summary,
                "warnings": self.run_manifest["warnings"],
            }
        except Exception as error:
            self._progress("FAILED", 100, f"运行失败：{type(error).__name__}")
            self.run_manifest["state"] = "FAILED"
            self.run_manifest["completed_at"] = datetime.now().astimezone().isoformat(
                timespec="seconds"
            )
            self.run_manifest["error"] = f"{type(error).__name__}: {error}"
            self.run_manifest["traceback"] = traceback.format_exc()
            self._write_json("run_manifest.json", self.run_manifest)
            raise

    def _connect(self) -> sqlite3.Connection:
        if self.config.database_path is None:
            raise ValueError("database_path is required for the SQLite source")
        path = self.config.database_path.resolve()
        if not path.exists():
            raise FileNotFoundError(path)
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        return bool(row)

    def _validate_schema(self, connection: sqlite3.Connection) -> dict[str, set[str]]:
        required_tables = {"jds", "jd_skill_edges"}
        missing_tables = [
            table for table in required_tables if not self._table_exists(connection, table)
        ]
        if missing_tables:
            raise ValueError(f"Missing required tables: {missing_tables}")
        schema = {
            table: {
                str(row["name"])
                for row in connection.execute(f"PRAGMA table_info({table})")
            }
            for table in ("jds", "jd_skill_edges")
        }
        required_jd = {"jd_id", "canonical_role", "company_id", "posted_at", "title"}
        required_edge = {
            "jd_id",
            "skill_id",
            "skill_name",
            "evidence_status",
            "competency_category",
        }
        missing_jd = required_jd - schema["jds"]
        missing_edge = required_edge - schema["jd_skill_edges"]
        if missing_jd or missing_edge:
            raise ValueError(
                f"Incompatible graph database. Missing JD columns={sorted(missing_jd)}, "
                f"edge columns={sorted(missing_edge)}"
            )
        return schema

    def _read_jd_rows(
        self,
        connection: sqlite3.Connection,
        schema: dict[str, set[str]],
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        jd_columns = schema["jds"]

        def expression(column: str, fallback: str = "''") -> str:
            return column if column in jd_columns else f"{fallback} AS {column}"

        columns = [
            "jd_id",
            "canonical_role",
            expression("role_id"),
            "company_id",
            "title",
            "posted_at",
            expression("description"),
            expression("industry_detail"),
            expression("industry_name"),
            expression("source_file"),
            expression("template_cluster_id"),
            expression("duplicate_of"),
        ]
        return self._prepare_jd_rows(
            connection.execute(f"SELECT {','.join(columns)} FROM jds")
        )

    @staticmethod
    def _prepare_jd_rows(
        raw_rows: Iterable[Any],
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        rows: list[dict[str, Any]] = []
        total = 0
        invalid_dates = 0
        for raw in raw_rows:
            total += 1
            posted_at = parse_datetime(raw["posted_at"])
            if posted_at is None:
                invalid_dates += 1
                continue
            row = dict(raw)
            row["_posted_at"] = posted_at
            rows.append(row)
        return rows, {
            "database_jds": total,
            "valid_date_jds": len(rows),
            "invalid_date_jds": invalid_dates,
        }

    def _resolve_windows(
        self,
        rows: list[dict[str, Any]],
    ) -> tuple[datetime, datetime, datetime]:
        if not rows:
            raise ValueError("Database has no JD with a valid posted_at")
        maximum = max(row["_posted_at"] for row in rows)
        minimum = min(row["_posted_at"] for row in rows)
        as_of = self.config.as_of or maximum
        cutoff = self.config.cutoff or (as_of - timedelta(days=self.config.current_days))
        if cutoff >= as_of:
            raise ValueError("cutoff must be earlier than as_of")
        baseline_start = (
            cutoff - timedelta(days=self.config.baseline_days)
            if self.config.baseline_days > 0
            else minimum
        )
        return cutoff, as_of, baseline_start

    def _build_jd_records(
        self,
        rows: list[dict[str, Any]],
        cutoff: datetime,
        as_of: datetime,
        baseline_start: datetime,
    ) -> dict[str, JDRecord]:
        output: dict[str, JDRecord] = {}
        for row in rows:
            when = row["_posted_at"]
            if when > as_of:
                continue
            duplicate_of = str(row.get("duplicate_of") or "").strip()
            if duplicate_of:
                continue
            role = str(row.get("canonical_role") or "").strip()
            title = str(row.get("title") or "").strip()
            company_id = str(row.get("company_id") or "").strip()
            if not role or not title or not company_id:
                continue
            description = str(row.get("description") or "").strip()
            template_cluster = str(row.get("template_cluster_id") or "").strip()
            template_key = template_cluster or f"text:{normalized_text_hash(description or title)}"
            if when >= cutoff:
                period = "current"
            elif when >= baseline_start:
                period = "baseline"
            else:
                # Earlier rows do not enter capability-change denominators, but they
                # are retained to prevent an old/rare title from being called "new".
                period = "historical"
            output[str(row["jd_id"])] = JDRecord(
                jd_id=str(row["jd_id"]),
                canonical_role=role,
                role_id=str(row.get("role_id") or ""),
                company_id=company_id,
                title=title,
                normalized_title=normalize_title(title),
                posted_at=when,
                description=description,
                industry=(
                    str(row.get("industry_detail") or "").strip()
                    or str(row.get("industry_name") or "").strip()
                    or "UNKNOWN"
                ),
                source_file=str(row.get("source_file") or ""),
                source_family=source_group(str(row.get("source_file") or "")),
                template_key=template_key,
                month=when.strftime("%Y-%m"),
                period=period,
            )
        return output

    def _load_skill_edges(
        self,
        connection: sqlite3.Connection,
        jds: dict[str, JDRecord],
    ) -> list[dict[str, Any]]:
        edge_columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(jd_skill_edges)")
        }

        def expression(column: str, fallback: str = "''") -> str:
            return column if column in edge_columns else f"{fallback} AS {column}"

        normalized_map: dict[tuple[str, str], tuple[str, str]] = {}
        has_normalized_map = self._table_exists(connection, "normalized_evidence_map")
        has_normalized_skills = self._table_exists(connection, "normalized_skills")
        if has_normalized_map and has_normalized_skills:
            concept_names = {
                str(concept_id): str(canonical_name)
                for concept_id, canonical_name in connection.execute(
                    "SELECT concept_id,canonical_name FROM normalized_skills"
                )
                if concept_id and canonical_name
            }
            for jd_id, original_skill_id, concept_id in connection.execute(
                "SELECT jd_id,original_skill_id,concept_id "
                "FROM normalized_evidence_map"
            ):
                normalized_jd_id = str(jd_id or "")
                normalized_original_id = str(original_skill_id or "")
                normalized_concept_id = str(concept_id or "")
                canonical_name = concept_names.get(normalized_concept_id, "")
                if (
                    normalized_jd_id
                    and normalized_original_id
                    and normalized_concept_id
                    and canonical_name
                ):
                    normalized_map[
                        (normalized_jd_id, normalized_original_id)
                    ] = (normalized_concept_id, canonical_name)

        categories = set(self.config.focus_categories)
        seen: set[tuple[str, str]] = set()
        edges: list[dict[str, Any]] = []
        eligible_edges = 0
        normalized_edges = 0
        query = (
            "SELECT jd_id,skill_id,skill_name,evidence_status,competency_category,"
            f"{expression('requirement_type')},{expression('evidence_quote')},"
            f"{expression('confidence', '0.0')} FROM jd_skill_edges "
            "WHERE evidence_status=?"
        )
        for row in connection.execute(query, (self.config.verified_status,)):
            jd_id = str(row["jd_id"])
            original_skill_id = str(row["skill_id"] or "").strip()
            if jd_id not in jds or not original_skill_id:
                continue
            if str(row["competency_category"] or "") not in categories:
                continue
            eligible_edges += 1
            mapped = normalized_map.get((jd_id, original_skill_id))
            skill_id = mapped[0] if mapped else original_skill_id
            if mapped:
                normalized_edges += 1
            key = (jd_id, skill_id)
            if key in seen:
                continue
            seen.add(key)
            edge = dict(row)
            edge["raw_skill_id"] = original_skill_id
            edge["skill_id"] = skill_id
            edge["normalization_source"] = (
                "normalized_evidence_map" if mapped else "raw_skill_fallback"
            )
            if mapped:
                edge["skill_name"] = mapped[1]
                self.standardized_skill_ids.add(skill_id)
            else:
                self.raw_fallback_skill_ids.add(skill_id)
            edges.append(edge)
        self.skill_mapping_quality = {
            "strategy": (
                "normalized_evidence_map_with_raw_fallback"
                if normalized_map
                else "raw_skill_fallback"
            ),
            "normalization_tables_available": bool(
                has_normalized_map and has_normalized_skills
            ),
            "eligible_edges": eligible_edges,
            "normalized_edges": normalized_edges,
            "normalization_coverage": (
                normalized_edges / eligible_edges if eligible_edges else 0.0
            ),
        }
        return edges

    def _load_known_titles(self, connection: sqlite3.Connection) -> set[str]:
        values: set[str] = set()
        if self._table_exists(connection, "roles"):
            for row in connection.execute("SELECT role_name FROM roles"):
                normalized = normalize_title(str(row[0] or ""))
                if normalized:
                    values.add(normalized)
        if self._table_exists(connection, "role_aliases"):
            for row in connection.execute("SELECT alias FROM role_aliases"):
                normalized = normalize_title(str(row[0] or ""))
                if normalized:
                    values.add(normalized)
        return values

    def _load_skill_names(
        self,
        connection: sqlite3.Connection,
        edges: list[dict[str, Any]],
    ) -> dict[str, str]:
        names = {
            str(row["skill_id"]): str(row["skill_name"] or row["skill_id"])
            for row in edges
        }
        if self._table_exists(connection, "skills"):
            columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(skills)")
            }
            name_column = "canonical_name" if "canonical_name" in columns else "skill_name"
            if "skill_id" in columns and name_column in columns:
                for skill_id, name in connection.execute(
                    f"SELECT skill_id,{name_column} FROM skills"
                ):
                    if skill_id and name:
                        names[str(skill_id)] = str(name)
        if self._table_exists(connection, "normalized_skills"):
            for concept_id, canonical_name in connection.execute(
                "SELECT concept_id,canonical_name FROM normalized_skills"
            ):
                if concept_id and canonical_name:
                    names[str(concept_id)] = str(canonical_name)
        return names

    def _skill_normalization_status(self, skill_id: str) -> str:
        standardized = skill_id in self.standardized_skill_ids
        fallback = skill_id in self.raw_fallback_skill_ids
        if standardized and fallback:
            return "MIXED_STANDARDIZED_AND_RAW"
        if standardized:
            return "STANDARDIZED_CONCEPT"
        return "RAW_SKILL_FALLBACK"

    def _aggregate_skill_evidence(
        self,
        jds: dict[str, JDRecord],
        edges: Iterable[dict[str, Any]],
    ):
        role_company: dict[tuple[str, str], set[str]] = defaultdict(set)
        role_jd: dict[tuple[str, str], set[str]] = defaultdict(set)
        skill_company: dict[tuple[str, str, str], set[str]] = defaultdict(set)
        skill_jd: dict[tuple[str, str, str], int] = defaultdict(int)
        skill_evidence: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
        jd_skills: dict[str, set[str]] = defaultdict(set)
        jd_skill_evidence: dict[tuple[str, str], str] = {}
        title_skill_evidence_companies: dict[
            tuple[str, str],
            set[str],
        ] = defaultdict(set)

        for jd_id, jd in jds.items():
            role_company[(jd.canonical_role, jd.period)].add(jd.company_id)
            role_jd[(jd.canonical_role, jd.period)].add(jd_id)

        for edge in edges:
            jd = jds.get(str(edge["jd_id"]))
            if jd is None:
                continue
            skill_id = str(edge["skill_id"])
            if skill_id in jd_skills[jd.jd_id]:
                continue
            key = (jd.canonical_role, jd.period, skill_id)
            skill_company[key].add(jd.company_id)
            skill_jd[key] += 1
            jd_skills[jd.jd_id].add(skill_id)
            quote = str(edge.get("evidence_quote") or "").strip()
            title_skill_key = (jd.normalized_title, skill_id)
            title_evidence_companies = title_skill_evidence_companies[
                title_skill_key
            ]
            if (
                quote
                and jd.period == "current"
                and jd.company_id not in title_evidence_companies
                and len(title_evidence_companies) < 3
            ):
                jd_skill_evidence[(jd.jd_id, skill_id)] = quote[:500]
                title_evidence_companies.add(jd.company_id)
            if len(skill_evidence[key]) < 8:
                if quote:
                    skill_evidence[key].append(
                        {
                            "jd_id": jd.jd_id,
                            "text": quote[:500],
                            "requirement_type": str(edge.get("requirement_type") or ""),
                        }
                    )
        self.jd_skill_evidence = jd_skill_evidence
        return (
            role_company,
            role_jd,
            skill_company,
            skill_jd,
            skill_evidence,
            jd_skills,
        )

    def _discover_roles(
        self,
        jds: dict[str, JDRecord],
        known_titles: set[str],
        role_company,
        skill_company,
        jd_skills,
        skill_names: dict[str, str],
    ) -> list[dict[str, Any]]:
        """Find clustered titles absent before cutoff but frequent in recent data.

        Deterministic title/description/skill clustering and historical/recent
        frequency checks deliberately precede the bounded LLM review.
        """
        title_stats: dict[str, dict[str, Any]] = {}
        for jd in jds.values():
            normalized = jd.normalized_title
            if len(normalized) < 2:
                continue
            stat = title_stats.setdefault(
                normalized,
                {
                    "raw_titles": Counter(),
                    "current_raw_titles": Counter(),
                    "historical_jds": set(),
                    "baseline_jds": set(),
                    "current_jds": set(),
                    "historical_companies": set(),
                    "baseline_companies": set(),
                    "current_companies": set(),
                    "current_templates": set(),
                    "roles": Counter(),
                    "industries": Counter(),
                    "sources": Counter(),
                    "source_companies": defaultdict(set),
                    "month_jds": defaultdict(set),
                    "month_companies": defaultdict(set),
                    "month_templates": defaultdict(set),
                    "month_source_companies": defaultdict(set),
                    "responsibility_terms": set(),
                },
            )
            stat["raw_titles"][jd.title] += 1
            if jd.period == "current":
                stat["current_raw_titles"][jd.title] += 1
            stat[f"{jd.period}_jds"].add(jd.jd_id)
            stat[f"{jd.period}_companies"].add(jd.company_id)
            if jd.period == "current":
                stat["current_templates"].add(jd.template_key)
                stat["industries"][jd.industry] += 1
                stat["sources"][jd.source_family] += 1
                stat["source_companies"][jd.source_family].add(jd.company_id)
                stat["month_jds"][jd.month].add(jd.jd_id)
                stat["month_companies"][jd.month].add(jd.company_id)
                stat["month_templates"][jd.month].add(jd.template_key)
                stat["month_source_companies"][
                    (jd.month, jd.source_family)
                ].add(jd.company_id)
                for sentence in extract_responsibility_sentences(jd.description)[:3]:
                    compact = re.sub(r"\s+", "", sentence.lower())
                    stat["responsibility_terms"].update(
                        compact[index : index + 2]
                        for index in range(max(0, len(compact) - 1))
                    )
            stat["roles"][jd.canonical_role] += 1

        baseline_role_profiles: dict[str, dict[str, float]] = {}
        roles = {role for role, period in role_company if period == "baseline"}
        for role in roles:
            denominator = len(role_company[(role, "baseline")])
            if denominator <= 0:
                continue
            weights = {
                skill_id: len(companies) / denominator
                for (candidate_role, period, skill_id), companies in skill_company.items()
                if candidate_role == role and period == "baseline"
            }
            baseline_role_profiles[role] = dict(
                sorted(weights.items(), key=lambda item: (-item[1], item[0]))[:15]
            )

        # Build one compact skill profile per cleaned title for concept clustering.
        title_skill_companies: dict[str, dict[str, set[str]]] = defaultdict(
            lambda: defaultdict(set)
        )
        for jd in jds.values():
            if jd.period != "current" or jd.normalized_title not in title_stats:
                continue
            for skill_id in jd_skills.get(jd.jd_id, set()):
                title_skill_companies[jd.normalized_title][skill_id].add(
                    jd.company_id
                )
        title_skill_profiles: dict[str, dict[str, float]] = {}
        for title, skills in title_skill_companies.items():
            denominator = max(
                1,
                len(title_stats[title]["current_companies"]),
            )
            title_skill_profiles[title] = {
                skill_id: len(companies) / denominator
                for skill_id, companies in skills.items()
            }

        current_titles = sorted(
            title
            for title, stat in title_stats.items()
            if stat["current_jds"]
        )
        all_titles = sorted(title_stats)
        current_title_set = set(current_titles)
        parents = {title: title for title in all_titles}

        def find(title: str) -> str:
            root = title
            while parents[root] != root:
                root = parents[root]
            while parents[title] != title:
                parent = parents[title]
                parents[title] = root
                title = parent
            return root

        def union(left: str, right: str) -> None:
            left_root = find(left)
            right_root = find(right)
            if left_root != right_root:
                parents[max(left_root, right_root)] = min(left_root, right_root)

        def jaccard(left: set[str], right: set[str]) -> float:
            if not left or not right:
                return 0.0
            return len(left & right) / len(left | right)

        def title_grams(title: str) -> set[str]:
            compact = re.sub(r"\s+", "", title.lower())
            return {
                compact[index : index + 2]
                for index in range(max(0, len(compact) - 1))
            }

        # Historical titles participate in clustering so a recently used alias
        # cannot be misreported as "historically absent".  Only pairs touching
        # a current title need comparison; historical-only concepts are never
        # candidates.  Blocking on the upstream coarse role prevents unrelated
        # short titles from being joined merely because both end with "工程师".
        role_blocks: dict[str, list[str]] = defaultdict(list)
        for title in all_titles:
            roles = title_stats[title]["roles"]
            block = roles.most_common(1)[0][0] if roles else "UNKNOWN"
            role_blocks[block].append(title)
        comparison_count = 0
        for block_titles in role_blocks.values():
            current_block_titles = [
                title for title in block_titles if title in current_title_set
            ]
            gram_index: dict[str, set[str]] = defaultdict(set)
            for title in block_titles:
                for gram in title_grams(title):
                    gram_index[gram].add(title)
            compared_pairs: set[tuple[str, str]] = set()
            for left in current_block_titles:
                grams = title_grams(left)
                # SequenceMatcher >= 0.78 implies substantial character
                # overlap.  Retrieve candidates through the four rarest
                # shared bigrams first instead of comparing against every
                # historical title in the coarse role block.
                informative_grams = sorted(
                    grams,
                    key=lambda gram: (len(gram_index[gram]), gram),
                )[:4]
                candidate_titles: set[str] = set()
                for gram in informative_grams:
                    candidate_titles.update(gram_index[gram])
                for right in sorted(candidate_titles):
                    if left == right:
                        continue
                    pair = tuple(sorted((left, right)))
                    if pair in compared_pairs:
                        continue
                    compared_pairs.add(pair)
                    comparison_count += 1
                    length_ratio = min(len(left), len(right)) / max(
                        len(left),
                        len(right),
                    )
                    if length_ratio < 0.72:
                        continue
                    title_similarity = SequenceMatcher(
                        None,
                        left,
                        right,
                        autojunk=False,
                    ).ratio()
                    if title_similarity < self.config.concept_title_similarity:
                        continue
                    skill_similarity = weighted_jaccard(
                        title_skill_profiles.get(left, {}),
                        title_skill_profiles.get(right, {}),
                    )
                    responsibility_similarity = jaccard(
                        title_stats[left]["responsibility_terms"],
                        title_stats[right]["responsibility_terms"],
                    )
                    if (
                        title_similarity >= 0.92
                        or (
                            right not in current_title_set
                            and title_similarity
                            >= self.config.concept_title_similarity
                        )
                        or skill_similarity >= self.config.concept_skill_jaccard
                        or responsibility_similarity >= 0.45
                    ):
                        union(left, right)

        self._progress(
            "DISCOVERING",
            57,
            f"岗位名称聚类完成：精确比较 {comparison_count:,} 对标题",
        )

        current_roots = {find(title) for title in current_titles}
        concept_members: dict[str, list[str]] = defaultdict(list)
        for title in all_titles:
            root = find(title)
            if root in current_roots:
                concept_members[root].append(title)

        month_source_all_companies: dict[tuple[str, str], set[str]] = (
            defaultdict(set)
        )
        for jd in jds.values():
            if jd.period == "current":
                month_source_all_companies[
                    (jd.month, jd.source_family)
                ].add(jd.company_id)

        current_records = [jd for jd in jds.values() if jd.period == "current"]
        window_start = (
            self.config.cutoff.date()
            if self.config.cutoff
            else min(jd.posted_at for jd in current_records).date()
        )
        window_end = (
            self.config.as_of.date()
            if self.config.as_of
            else max(jd.posted_at for jd in current_records).date()
        )

        def closed_month(month: str) -> bool:
            year, number = (int(part) for part in month.split("-"))
            start = datetime(year, number, 1).date()
            end = datetime(
                year,
                number,
                monthrange(year, number)[1],
            ).date()
            return start >= window_start and end <= window_end

        def consecutive(previous: str, current: str) -> bool:
            previous_date = datetime.strptime(previous, "%Y-%m")
            current_date = datetime.strptime(current, "%Y-%m")
            next_month = (
                datetime(previous_date.year + 1, 1, 1)
                if previous_date.month == 12
                else datetime(previous_date.year, previous_date.month + 1, 1)
            )
            return current_date == next_month

        candidates: list[dict[str, Any]] = []
        for members in concept_members.values():
            member_stats = [title_stats[title] for title in members]
            normalized = sorted(
                members,
                key=lambda title: (
                    -len(title_stats[title]["current_jds"]),
                    -sum(title_stats[title]["raw_titles"].values()),
                    len(title),
                    title,
                ),
            )[0]

            stat: dict[str, Any] = {
                "raw_titles": Counter(),
                "current_raw_titles": Counter(),
                "historical_jds": set(),
                "baseline_jds": set(),
                "current_jds": set(),
                "historical_companies": set(),
                "baseline_companies": set(),
                "current_companies": set(),
                "current_templates": set(),
                "roles": Counter(),
                "industries": Counter(),
                "sources": Counter(),
                "source_companies": defaultdict(set),
                "month_jds": defaultdict(set),
                "month_companies": defaultdict(set),
                "month_templates": defaultdict(set),
                "month_source_companies": defaultdict(set),
            }
            for member_stat in member_stats:
                for field in (
                    "raw_titles",
                    "current_raw_titles",
                    "roles",
                    "industries",
                    "sources",
                ):
                    stat[field].update(member_stat[field])
                for field in (
                    "historical_jds",
                    "baseline_jds",
                    "current_jds",
                    "historical_companies",
                    "baseline_companies",
                    "current_companies",
                    "current_templates",
                ):
                    stat[field].update(member_stat[field])
                for field in (
                    "source_companies",
                    "month_jds",
                    "month_companies",
                    "month_templates",
                    "month_source_companies",
                ):
                    for key, values in member_stat[field].items():
                        stat[field][key].update(values)

            prebaseline_historical_jds = len(stat["historical_jds"])
            baseline_jds = len(stat["baseline_jds"])
            historical_jd_ids = stat["historical_jds"] | stat["baseline_jds"]
            historical_company_ids = (
                stat["historical_companies"] | stat["baseline_companies"]
            )
            historical_jds = len(historical_jd_ids)
            current_jds = len(stat["current_jds"])
            current_companies = len(stat["current_companies"])
            unseen_before_cutoff = historical_jds == 0
            # The product's only candidate definition:
            # this clustered title did not exist before the cutoff and has
            # recently reached the configured JD/company/template support.
            if (
                not unseen_before_cutoff
                or current_jds < self.config.min_new_role_jds
                or current_companies < self.config.min_new_role_companies
                or len(stat["current_templates"])
                < self.config.min_new_role_templates
            ):
                continue

            monthly_trend: list[dict[str, Any]] = []
            for month in sorted(stat["month_jds"]):
                source_breakdown: list[dict[str, Any]] = []
                source_rates: list[float] = []
                for source in sorted(
                    {
                        key[1]
                        for key in stat["month_source_companies"]
                        if key[0] == month
                    }
                ):
                    companies = stat["month_source_companies"][(month, source)]
                    denominator = len(
                        month_source_all_companies[(month, source)]
                    )
                    rate = len(companies) / denominator if denominator else 0.0
                    source_rates.append(rate)
                    source_breakdown.append(
                        {
                            "source_family": source,
                            "company_count": len(companies),
                            "market_company_count": denominator,
                            "company_rate": rate,
                        }
                    )
                monthly_trend.append(
                    {
                        "month": month,
                        "closed": closed_month(month),
                        "jd_count": len(stat["month_jds"][month]),
                        "company_count": len(stat["month_companies"][month]),
                        "template_count": len(stat["month_templates"][month]),
                        "source_count": len(source_breakdown),
                        "source_normalized_company_rate": (
                            sum(source_rates) / len(source_rates)
                            if source_rates
                            else 0.0
                        ),
                        "sources": source_breakdown,
                    }
                )

            eligible_months = [
                row
                for row in monthly_trend
                if row["closed"]
                and row["company_count"] >= self.config.min_month_companies
            ]
            continuous_windows = 0
            current_run = 0
            previous_month = ""
            for row in eligible_months:
                if previous_month and consecutive(previous_month, row["month"]):
                    current_run += 1
                else:
                    current_run = 1
                continuous_windows = max(continuous_windows, current_run)
                previous_month = row["month"]

            growth_windows = 0
            current_growth_run = 0
            previous: dict[str, Any] | None = None
            for row in eligible_months:
                is_growth = bool(
                    previous
                    and consecutive(previous["month"], row["month"])
                    and (
                        row["company_count"] > previous["company_count"]
                        or row["source_normalized_company_rate"]
                        > previous["source_normalized_company_rate"] + 1e-12
                    )
                )
                if is_growth:
                    current_growth_run = (
                        current_growth_run + 1
                        if current_growth_run
                        else 2
                    )
                else:
                    current_growth_run = 1
                growth_windows = max(growth_windows, current_growth_run)
                previous = row

            source_distribution = [
                {
                    "source_family": source,
                    "jd_count": count,
                    "company_count": len(stat["source_companies"][source]),
                    "months": sorted(
                        {
                            month
                            for month, candidate_source in stat[
                                "month_source_companies"
                            ]
                            if candidate_source == source
                            and stat["month_source_companies"][
                                (month, candidate_source)
                            ]
                        }
                    ),
                }
                for source, count in stat["sources"].most_common()
            ]
            independent_sources = sum(
                row["company_count"] >= self.config.min_source_companies
                for row in source_distribution
            )

            # The cluster representative is already weighted by recent JD
            # support, total support and title length above.  Expose its clean
            # concept form rather than leaking the most frequent raw advert
            # title (which may contain a city, seniority or business suffix).
            canonical_title = display_title(normalized)
            candidate_skill_companies: dict[str, set[str]] = defaultdict(set)
            for jd_id in stat["current_jds"]:
                for skill_id in jd_skills.get(jd_id, set()):
                    candidate_skill_companies[skill_id].add(jds[jd_id].company_id)
            candidate_weights = {
                skill_id: len(companies) / current_companies
                for skill_id, companies in candidate_skill_companies.items()
            }
            candidate_weights = dict(
                sorted(candidate_weights.items(), key=lambda item: (-item[1], item[0]))[:15]
            )
            nearest = sorted(
                (
                    {
                        "role": role,
                        "weighted_skill_jaccard": weighted_jaccard(
                            candidate_weights,
                            profile,
                        ),
                        "title_similarity": SequenceMatcher(
                            None,
                            normalized,
                            normalize_title(role),
                            autojunk=False,
                        ).ratio(),
                        "composite_similarity": (
                            0.60
                            * weighted_jaccard(candidate_weights, profile)
                            + 0.40
                            * SequenceMatcher(
                                None,
                                normalized,
                                normalize_title(role),
                                autojunk=False,
                            ).ratio()
                        ),
                    }
                    for role, profile in baseline_role_profiles.items()
                ),
                key=lambda row: (-row["composite_similarity"], row["role"]),
            )[:3]
            nearest_score = (
                nearest[0]["weighted_skill_jaccard"] if nearest else 0.0
            )
            nearest_title_score = (
                nearest[0]["title_similarity"] if nearest else 0.0
            )

            skill_detail: list[dict[str, Any]] = []
            for skill_id in candidate_weights:
                evidence: list[dict[str, str]] = []
                evidence_companies: set[str] = set()
                for jd_id in sorted(stat["current_jds"]):
                    jd = jds[jd_id]
                    quote = self.jd_skill_evidence.get((jd_id, skill_id), "")
                    if (
                        not quote
                        or jd.company_id in evidence_companies
                        or skill_id not in jd_skills.get(jd_id, set())
                    ):
                        continue
                    evidence.append({"jd_id": jd_id, "text": quote})
                    evidence_companies.add(jd.company_id)
                    if len(evidence) >= 3:
                        break
                skill_detail.append(
                    {
                        "skill_id": skill_id,
                        "skill": skill_names.get(skill_id, skill_id),
                        "normalization_status": self._skill_normalization_status(
                            skill_id
                        ),
                        "company_count": len(candidate_skill_companies[skill_id]),
                        "company_coverage": len(candidate_skill_companies[skill_id])
                        / current_companies,
                        "evidence": evidence,
                    }
                )
            essential = [
                row
                for row in skill_detail
                if row["company_count"] >= max(2, math.ceil(current_companies * 0.5))
            ][:8]
            bonus = [row for row in skill_detail if row not in essential][:8]

            responsibility_evidence: list[dict[str, Any]] = []
            seen_companies: set[str] = set()
            for jd_id in sorted(
                stat["current_jds"],
                key=lambda item: (
                    jds[item].company_id,
                    jds[item].posted_at,
                    item,
                ),
            ):
                jd = jds[jd_id]
                if jd.company_id in seen_companies:
                    continue
                sentences = extract_responsibility_sentences(jd.description)
                if not sentences:
                    continue
                responsibility_evidence.append(
                    {
                        "jd_id": jd.jd_id,
                        "company_id": jd.company_id,
                        "source_family": jd.source_family,
                        "text": sentences[0][:500],
                    }
                )
                seen_companies.add(jd.company_id)
                if len(responsibility_evidence) >= self.config.llm_max_evidence_per_candidate:
                    break

            reasons: list[str] = [
                "TITLE_CLUSTER_UNSEEN_BEFORE_CUTOFF",
                "RECENT_FREQUENCY_THRESHOLD_PASSED",
            ]
            translated_alias = ENGLISH_ROLE_TITLE_ALIASES.get(normalized, "")
            dominant_source_role = (
                stat["roles"].most_common(1)[0][0]
                if stat["roles"]
                else ""
            )
            translated_source_similarity = (
                SequenceMatcher(
                    None,
                    normalize_title(translated_alias),
                    normalize_title(dominant_source_role),
                    autojunk=False,
                ).ratio()
                if translated_alias and dominant_source_role
                else 0.0
            )
            if normalized in known_titles:
                continue
            elif TITLE_TECH_PREFIX.sub("", normalized) in known_titles:
                # A programming-language prefix does not create a separate
                # role when the remaining concept is already controlled.
                continue
            elif translated_alias and (
                normalize_title(translated_alias) in known_titles
                or translated_source_similarity >= 0.55
            ):
                continue
            elif normalized in GENERIC_TITLES:
                continue
            if (
                len(candidate_skill_companies)
                < self.config.min_candidate_skills
                or len(essential)
                < self.config.min_shared_candidate_skills
                or len(responsibility_evidence) < 2
            ):
                continue

            state = "REVIEW"
            reasons.append("PASSED_RECENT_NEW_TITLE_CLUSTER_GATE")
            if (
                nearest_score >= self.config.new_role_jaccard_max
                or nearest_title_score >= self.config.alias_jaccard_min
            ):
                reasons.append("QWEN_REVIEW_EXISTING_ROLE_SIMILARITY")

            typical_industries: list[dict[str, Any]] = []
            for industry, count in stat["industries"].most_common(5):
                evidence_ids = [
                    jd_id
                    for jd_id in sorted(stat["current_jds"])
                    if jds[jd_id].industry == industry
                ][:3]
                typical_industries.append(
                    {
                        "industry": industry,
                        "jd_count": count,
                        "evidence_jd_ids": evidence_ids,
                    }
                )

            support_score = min(
                1.0,
                current_companies
                / max(1, self.config.min_new_role_companies),
            )
            novelty_score = max(
                0.0,
                1.0 - max(nearest_score, nearest_title_score),
            )
            template_score = min(
                1.0,
                len(stat["current_templates"])
                / max(1, self.config.min_new_role_templates),
            )
            evidence_score = min(1.0, len(responsibility_evidence) / 2)
            emergence_score = 100 * math.prod(
                max(0.01, value)
                for value in (
                    support_score,
                    novelty_score,
                    template_score,
                    evidence_score,
                )
            ) ** (1 / 4)

            confirmation_state = "RECENT_NEW_TITLE_CLUSTER_CANDIDATE"

            candidates.append(
                {
                    "candidate_id": stable_id("role_candidate", normalized),
                    "concept_key": normalized,
                    "candidate_title": canonical_title,
                    "normalized_title": normalized,
                    "canonical_name_source": "weighted_clean_title",
                    "concept_member_count": len(members),
                    "concept_title_members": sorted(members),
                    "rule_state": state,
                    "rule_reasons": reasons,
                    "emergence_score": round(emergence_score, 4),
                    "score_calibration": "UNCALIBRATED_DEMO",
                    "historical_jd_count": historical_jds,
                    "historical_company_count": len(historical_company_ids),
                    "prebaseline_historical_jd_count": prebaseline_historical_jds,
                    "baseline_jd_count": baseline_jds,
                    "current_jd_count": current_jds,
                    "current_company_count": current_companies,
                    "current_template_count": len(stat["current_templates"]),
                    "continuous_windows": continuous_windows,
                    "growth_windows": growth_windows,
                    "monthly_trend": monthly_trend,
                    "independent_source_count": independent_sources,
                    "source_distribution": source_distribution,
                    "confirmation_state": confirmation_state,
                    "raw_title_variants": [
                        {"title": title, "count": count}
                        for title, count in stat["raw_titles"].most_common(8)
                    ],
                    "source_roles": [
                        {"role": role, "jd_count": count}
                        for role, count in stat["roles"].most_common(5)
                    ],
                    "nearest_existing_roles": nearest,
                    "candidate_skills_detail": skill_detail,
                    "required_skill_draft": essential,
                    "bonus_skill_draft": bonus,
                    "typical_industries": typical_industries,
                    "responsibility_evidence": responsibility_evidence,
                    "semantic_review": {},
                }
            )

        priority = {"REVIEW": 0, "WATCH": 1, "AUTO_REJECT": 2}
        candidates.sort(
            key=lambda row: (
                priority[row["rule_state"]],
                -row["current_company_count"],
                -row["emergence_score"],
                -row["current_template_count"],
                -row["current_jd_count"],
                row["nearest_existing_roles"][0]["weighted_skill_jaccard"]
                if row["nearest_existing_roles"]
                else 0.0,
                row["candidate_title"],
            )
        )
        return candidates

    @staticmethod
    def _evaluation_sample(
        candidates: list[dict[str, Any]],
        maximum: int = 300,
    ) -> list[dict[str, Any]]:
        """Build a deterministic, stratified expert-labeling queue."""
        by_state: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for candidate in candidates:
            by_state[str(candidate.get("rule_state") or "UNKNOWN")].append(
                candidate
            )
        order = ("REVIEW", "WATCH", "AUTO_REJECT", "UNKNOWN")
        selected: list[dict[str, Any]] = []
        per_state = max(1, maximum // max(1, len(order)))
        for state in order:
            rows = sorted(
                by_state.get(state, []),
                key=lambda row: (
                    -float(row.get("emergence_score") or 0.0),
                    str(row.get("candidate_id") or ""),
                ),
            )
            selected.extend(rows[:per_state])
        if len(selected) < maximum:
            selected_ids = {
                str(row.get("candidate_id") or "") for row in selected
            }
            remainder = [
                row
                for row in candidates
                if str(row.get("candidate_id") or "") not in selected_ids
            ]
            remainder.sort(
                key=lambda row: (
                    -float(row.get("emergence_score") or 0.0),
                    str(row.get("candidate_id") or ""),
                )
            )
            selected.extend(remainder[: maximum - len(selected)])
        return [
            {
                "candidate_id": row.get("candidate_id"),
                "candidate_title": row.get("candidate_title"),
                "concept_title_members": row.get("concept_title_members"),
                "rule_state": row.get("rule_state"),
                "emergence_score": row.get("emergence_score"),
                "monthly_trend": row.get("monthly_trend"),
                "source_distribution": row.get("source_distribution"),
                "nearest_existing_roles": row.get("nearest_existing_roles"),
                "responsibility_evidence": row.get(
                    "responsibility_evidence"
                ),
                "candidate_skills_detail": row.get(
                    "candidate_skills_detail"
                ),
                "expert_1_label": "",
                "expert_1_comment": "",
                "expert_2_label": "",
                "expert_2_comment": "",
                "adjudicated_label": "",
                "adjudicator_comment": "",
            }
            for row in selected[:maximum]
        ]

    def _discover_skill_changes(
        self,
        jds: dict[str, JDRecord],
        role_company,
        role_jd,
        skill_company,
        skill_jd,
        skill_evidence,
        skill_names: dict[str, str],
    ) -> list[dict[str, Any]]:
        role_skills: dict[str, set[str]] = defaultdict(set)
        for role, period, skill_id in skill_company:
            if period in {"baseline", "current"}:
                role_skills[role].add(skill_id)
        tests: list[dict[str, Any]] = []
        for role, skills in role_skills.items():
            baseline_companies = len(role_company[(role, "baseline")])
            current_companies = len(role_company[(role, "current")])
            if (
                baseline_companies < self.config.min_role_companies_per_window
                or current_companies < self.config.min_role_companies_per_window
            ):
                continue
            for skill_id in skills:
                baseline_hits = len(skill_company[(role, "baseline", skill_id)])
                current_hits = len(skill_company[(role, "current", skill_id)])
                distinct_support = len(
                    skill_company[(role, "baseline", skill_id)]
                    | skill_company[(role, "current", skill_id)]
                )
                # Independent low-information filtering: singleton/doubleton terms
                # cannot pass the review gate and only inflate the BH test family.
                if distinct_support < self.config.min_skill_review_companies:
                    continue
                baseline_coverage = baseline_hits / baseline_companies
                current_coverage = current_hits / current_companies
                delta = current_coverage - baseline_coverage
                # Only effects large enough to enter the product result need an
                # expensive exact/proportion test.  The remaining hypotheses
                # stay in the BH family with p=1, which is conservative and
                # avoids spending minutes testing immaterial fluctuations.
                if abs(delta) >= self.config.min_skill_delta:
                    test_name, p_value = compare_proportions(
                        baseline_hits,
                        baseline_companies,
                        current_hits,
                        current_companies,
                    )
                else:
                    test_name, p_value = "effect_size_prefilter", 1.0
                baseline_ci = wilson_interval(baseline_hits, baseline_companies)
                current_ci = wilson_interval(current_hits, current_companies)
                tests.append(
                    {
                        "candidate_id": stable_id("skill_change", role, skill_id),
                        "role": role,
                        "skill_id": skill_id,
                        "skill": skill_names.get(skill_id, skill_id),
                        "baseline_company_count": baseline_hits,
                        "baseline_role_companies": baseline_companies,
                        "baseline_coverage": baseline_coverage,
                        "baseline_coverage_ci95": list(baseline_ci),
                        "current_company_count": current_hits,
                        "current_role_companies": current_companies,
                        "current_coverage": current_coverage,
                        "current_coverage_ci95": list(current_ci),
                        "delta": delta,
                        "baseline_jd_count": skill_jd[
                            (role, "baseline", skill_id)
                        ],
                        "current_jd_count": skill_jd[
                            (role, "current", skill_id)
                        ],
                        "test": test_name,
                        "p_value": p_value,
                        "q_value": 1.0,
                    }
                )

        by_role: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in tests:
            by_role[row["role"]].append(row)
        for rows in by_role.values():
            add_bh_qvalues(rows)

        changes: list[dict[str, Any]] = []
        for row in tests:
            baseline_hits = row["baseline_company_count"]
            current_hits = row["current_company_count"]
            delta = row["delta"]
            q_value = row["q_value"]
            current_coverage = row["current_coverage"]
            change_type = ""
            state = ""
            reasons: list[str] = []

            if (
                baseline_hits == 0
                and current_hits >= self.config.min_skill_review_companies
                and current_coverage >= self.config.min_skill_coverage
                and delta >= self.config.min_skill_delta
            ):
                change_type = "ADDED_CANDIDATE"
                reasons.append("BASELINE_ABSENT_MULTI_COMPANY_CURRENT_SUPPORT")
                if q_value <= self.config.q_value_threshold:
                    state = "REVIEW"
                    reasons.append("FDR_THRESHOLD_PASSED")
                else:
                    state = "WATCH"
                    reasons.append("FDR_THRESHOLD_NOT_PASSED")
            elif (
                baseline_hits > 0
                and current_hits > 0
                and delta >= self.config.min_skill_delta
                and q_value <= self.config.q_value_threshold
            ):
                change_type = "INCREASED"
                state = "REVIEW"
                reasons.append("SIGNIFICANT_COVERAGE_INCREASE")
            elif (
                baseline_hits > 0
                and current_hits > 0
                and delta <= -self.config.min_skill_delta
                and q_value <= self.config.q_value_threshold
            ):
                change_type = "DECREASED"
                state = "REVIEW"
                reasons.append("SIGNIFICANT_COVERAGE_DECREASE")
            elif (
                current_hits == 0
                and baseline_hits >= self.config.min_skill_review_companies
                and row["baseline_coverage"] >= self.config.min_skill_coverage
            ):
                change_type = "MISSING_WATCH"
                state = "REVIEW"
                reasons.append("CURRENT_WINDOW_ABSENT_REQUIRES_HUMAN_REVIEW")
                reasons.append("CURRENT_WINDOW_ABSENT_NEVER_AUTO_DELETE")
            else:
                continue

            normalization_status = self._skill_normalization_status(
                row["skill_id"]
            )
            if normalization_status == "RAW_SKILL_FALLBACK":
                reasons.append("RAW_SKILL_FALLBACK_REQUIRES_SEMANTIC_REVIEW")
            row["change_type"] = change_type
            row["rule_state"] = state
            row["rule_reasons"] = reasons
            row["normalization_status"] = normalization_status
            row["confirmation_eligible"] = bool(
                change_type == "ADDED_CANDIDATE"
                and current_hits >= self.config.min_skill_confirm_companies
                and current_coverage >= self.config.min_skill_confirm_coverage
                and q_value <= self.config.q_value_threshold
            ) or bool(
                change_type in {"INCREASED", "DECREASED"}
                and q_value <= self.config.q_value_threshold
            )
            evidence_period = (
                "current"
                if change_type in {"ADDED_CANDIDATE", "INCREASED"}
                else "baseline"
            )
            row["evidence_period"] = evidence_period
            row["evidence"] = skill_evidence[
                (row["role"], evidence_period, row["skill_id"])
            ][: self.config.llm_max_evidence_per_candidate]
            row["nearest_existing_skills"] = []
            row["semantic_review"] = {}
            changes.append(row)

        priority = {
            "ADDED_CANDIDATE": 0,
            "INCREASED": 1,
            "DECREASED": 2,
            "MISSING_WATCH": 3,
        }
        changes.sort(
            key=lambda row: (
                0 if row["rule_state"] == "REVIEW" else 1,
                priority[row["change_type"]],
                -abs(row["delta"]),
                -max(
                    row["baseline_company_count"],
                    row["current_company_count"],
                ),
                row["q_value"],
                row["role"],
                row["skill"],
            )
        )
        return changes

    @staticmethod
    def _attach_nearest_skill_names(
        candidates: list[dict[str, Any]],
        skill_names: dict[str, str],
    ) -> None:
        """Run lexical synonym hints only for the bounded semantic-review queue."""
        all_names = list(skill_names.items())
        for row in candidates:
            nearest_skill_matches = sorted(
                [
                    (
                        SequenceMatcher(
                            None,
                            str(row["skill"]).lower(),
                            name.lower(),
                            autojunk=False,
                        ).ratio(),
                        skill_id,
                        name,
                    )
                    for skill_id, name in all_names
                    if skill_id != row["skill_id"]
                ],
                reverse=True,
            )[:5]
            row["nearest_existing_skills"] = [
                {
                    "skill_id": skill_id,
                    "skill": name,
                    "name_similarity": similarity,
                }
                for similarity, skill_id, name in nearest_skill_matches
            ]

    def _quality_report(
        self,
        jds: dict[str, JDRecord],
        date_quality: dict[str, int],
        cutoff: datetime,
        as_of: datetime,
        baseline_start: datetime,
    ) -> dict[str, Any]:
        historical = [row for row in jds.values() if row.period == "historical"]
        baseline = [row for row in jds.values() if row.period == "baseline"]
        current = [row for row in jds.values() if row.period == "current"]
        source_baseline = Counter(source_group(row.source_file) for row in baseline)
        source_current = Counter(source_group(row.source_file) for row in current)
        industry_baseline = Counter(row.industry for row in baseline)
        industry_current = Counter(row.industry for row in current)
        source_jsd = jensen_shannon_divergence(source_baseline, source_current)
        industry_jsd = jensen_shannon_divergence(industry_baseline, industry_current)
        warnings: list[str] = []
        if not baseline:
            warnings.append("NO_BASELINE_JDS")
        if not current:
            warnings.append("NO_CURRENT_JDS")
        if len(source_current) < self.config.min_independent_sources:
            warnings.append(
                "INSUFFICIENT_INDEPENDENT_SOURCES:"
                f"{len(source_current)}/{self.config.min_independent_sources}"
            )
        if source_jsd > self.config.source_drift_warning:
            warnings.append(f"SOURCE_DISTRIBUTION_DRIFT:{source_jsd:.4f}")
        if industry_jsd > self.config.industry_drift_warning:
            warnings.append(f"INDUSTRY_DISTRIBUTION_DRIFT:{industry_jsd:.4f}")
        date_completeness = (
            date_quality["valid_date_jds"] / date_quality["database_jds"]
            if date_quality["database_jds"]
            else 0.0
        )
        if date_completeness < 0.95:
            warnings.append(f"POSTED_AT_COMPLETENESS_LOW:{date_completeness:.4f}")
        mapping_coverage = float(
            self.skill_mapping_quality.get("normalization_coverage") or 0.0
        )
        if not self.skill_mapping_quality.get("normalization_tables_available"):
            warnings.append("SKILL_NORMALIZATION_TABLES_MISSING:RAW_SKILL_FALLBACK")
        elif mapping_coverage < 0.80:
            warnings.append(f"SKILL_NORMALIZATION_PARTIAL:{mapping_coverage:.4f}")
        return {
            "status": "PASS_WITH_WARNINGS" if warnings else "PASS",
            "warnings": warnings,
            "windows": {
                "baseline_start": baseline_start.isoformat(timespec="seconds"),
                "cutoff": cutoff.isoformat(timespec="seconds"),
                "as_of": as_of.isoformat(timespec="seconds"),
            },
            "database_jds": date_quality["database_jds"],
            "valid_date_jds": date_quality["valid_date_jds"],
            "invalid_date_jds": date_quality["invalid_date_jds"],
            "date_completeness": date_completeness,
            "usable_jds": len(jds),
            "historical_jds": len(historical),
            "baseline_jds": len(baseline),
            "current_jds": len(current),
            "historical_companies": len({row.company_id for row in historical}),
            "baseline_companies": len({row.company_id for row in baseline}),
            "current_companies": len({row.company_id for row in current}),
            "independent_source_families": len(source_current),
            "skill_normalization": self.skill_mapping_quality,
            "source_js_divergence": source_jsd,
            "industry_js_divergence": industry_jsd,
            "source_distribution": {
                "baseline": dict(source_baseline),
                "current": dict(source_current),
            },
            "industry_distribution_top20": {
                "baseline": dict(industry_baseline.most_common(20)),
                "current": dict(industry_current.most_common(20)),
            },
        }

    def _role_llm_packet(self, candidate: dict[str, Any]) -> dict[str, Any]:
        return {
            "candidate_id": candidate["candidate_id"],
            "candidate_title": candidate["candidate_title"],
            "statistics": {
                "historical_jds": candidate["historical_jd_count"],
                "current_jds": candidate["current_jd_count"],
                "current_companies": candidate["current_company_count"],
                "current_templates": candidate["current_template_count"],
                "continuous_windows": candidate["continuous_windows"],
                "growth_windows": candidate["growth_windows"],
                "independent_sources": candidate[
                    "independent_source_count"
                ],
                "confirmation_state": candidate["confirmation_state"],
                "skill_normalization_coverage": self.skill_mapping_quality.get(
                    "normalization_coverage",
                    0.0,
                ),
            },
            "source_roles": candidate["source_roles"],
            "title_variants": candidate["raw_title_variants"],
            "monthly_trend": candidate["monthly_trend"],
            "source_distribution": candidate["source_distribution"],
            "nearest_roles": candidate["nearest_existing_roles"],
            "candidate_skills": candidate["candidate_skills_detail"][:15],
            "required_skill_draft": candidate["required_skill_draft"][:5],
            "bonus_skill_draft": candidate["bonus_skill_draft"][:8],
            "industries": candidate["typical_industries"],
            "responsibility_evidence": candidate["responsibility_evidence"],
            "quality_flags": candidate["rule_reasons"],
        }

    @staticmethod
    def _skill_llm_packet(candidate: dict[str, Any]) -> dict[str, Any]:
        return {
            "candidate_id": candidate["candidate_id"],
            "role": candidate["role"],
            "change_type": candidate["change_type"],
            "candidate_skill": {
                "skill_id": candidate["skill_id"],
                "skill": candidate["skill"],
                "normalization_status": candidate["normalization_status"],
            },
            "statistics": {
                "baseline_company_count": candidate["baseline_company_count"],
                "baseline_role_companies": candidate["baseline_role_companies"],
                "baseline_coverage": candidate["baseline_coverage"],
                "current_company_count": candidate["current_company_count"],
                "current_role_companies": candidate["current_role_companies"],
                "current_coverage": candidate["current_coverage"],
                "delta": candidate["delta"],
                "p_value": candidate["p_value"],
                "q_value": candidate["q_value"],
                "test": candidate["test"],
            },
            "nearest_existing_skills": candidate["nearest_existing_skills"],
            "evidence": candidate["evidence"],
            "quality_flags": candidate["rule_reasons"],
        }

    @staticmethod
    def _review_queue(
        role_review: list[dict[str, Any]],
        skill_review: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        for candidate in role_review:
            tasks.append(
                {
                    "task_id": stable_id("review_task", candidate["candidate_id"]),
                    "task_type": "NEW_ROLE_REVIEW",
                    "candidate_id": candidate["candidate_id"],
                    "title": candidate["candidate_title"],
                    "metrics": {
                        "emergence_score": candidate["emergence_score"],
                        "current_jds": candidate["current_jd_count"],
                        "current_companies": candidate["current_company_count"],
                        "current_templates": candidate["current_template_count"],
                        "continuous_windows": candidate[
                            "continuous_windows"
                        ],
                        "growth_windows": candidate["growth_windows"],
                        "independent_sources": candidate[
                            "independent_source_count"
                        ],
                        "nearest_role": (
                            candidate["nearest_existing_roles"][0]
                            if candidate["nearest_existing_roles"]
                            else {}
                        ),
                    },
                    "rule_reasons": candidate["rule_reasons"],
                    "semantic_review": candidate.get("semantic_review", {}),
                    "definition_draft": {
                        "name": candidate["candidate_title"],
                        "parent_role_id": "",
                        "core_responsibilities": [
                            item["text"]
                            for item in candidate[
                                "responsibility_evidence"
                            ]
                        ],
                        "required_skills": [
                            item["skill"]
                            for item in candidate[
                                "required_skill_draft"
                            ]
                        ],
                        "bonus_skills": [
                            item["skill"]
                            for item in candidate[
                                "bonus_skill_draft"
                            ]
                        ],
                        "industry_scenarios": [
                            item["industry"]
                            for item in candidate["typical_industries"]
                        ],
                    },
                    "status": "PENDING",
                    "human_decision": "",
                    "human_comment": "",
                }
            )
        for candidate in skill_review:
            tasks.append(
                {
                    "task_id": stable_id("review_task", candidate["candidate_id"]),
                    "task_type": "SKILL_CHANGE_REVIEW",
                    "candidate_id": candidate["candidate_id"],
                    "title": f"{candidate['role']}｜{candidate['skill']}",
                    "metrics": {
                        "change_type": candidate["change_type"],
                        "baseline_company_count": candidate["baseline_company_count"],
                        "baseline_coverage": candidate["baseline_coverage"],
                        "current_company_count": candidate["current_company_count"],
                        "current_coverage": candidate["current_coverage"],
                        "delta": candidate["delta"],
                        "p_value": candidate["p_value"],
                        "q_value": candidate["q_value"],
                    },
                    "rule_reasons": candidate["rule_reasons"],
                    "semantic_review": candidate.get("semantic_review", {}),
                    "status": "PENDING",
                    "human_decision": "",
                    "human_comment": "",
                }
            )
        return tasks

    @staticmethod
    def _graph_patch_draft(
        run_id: str,
        role_review: list[dict[str, Any]],
        skill_review: list[dict[str, Any]],
    ) -> dict[str, Any]:
        actions: list[dict[str, Any]] = []
        role_action = {
            "NEW_ROLE": "CREATE_ROLE",
            "SPECIALIZATION": "CREATE_ROLE_SPECIALIZATION",
            "ALIAS": "ADD_ROLE_ALIAS",
        }
        skill_action = {
            "TRUE_NEW_SKILL": "ADD_ROLE_SKILL",
            "EXISTING_SKILL_SYNONYM": "MERGE_SKILL",
            "SKILL_GRANULARITY_CHANGE": "REVIEW_SKILL_HIERARCHY",
            "REQUIREMENT_LEVEL_CHANGE": "CHANGE_REQUIREMENT_TYPE",
            "ROLE_MISCLASSIFICATION": "RECLASSIFY_JD",
        }
        for row in role_review:
            if (
                int(row.get("continuous_windows") or 0) < 2
                or int(row.get("growth_windows") or 0) < 2
                or int(row.get("independent_source_count") or 0) < 2
            ):
                continue
            semantic = row.get("semantic_review", {})
            if semantic.get("status") != "COMPLETED":
                continue
            analysis = semantic.get("analysis") or {}
            if float(analysis.get("confidence") or 0.0) < 0.70:
                continue
            action_type = role_action.get(analysis.get("semantic_class"))
            if action_type:
                actions.append(
                    {
                        "change_id": stable_id("graph_change", row["candidate_id"], action_type),
                        "action": action_type,
                        "candidate_id": row["candidate_id"],
                        "payload": analysis,
                        "evidence_jd_ids": [
                            item["jd_id"] for item in row["responsibility_evidence"]
                        ],
                        "approval_status": "PENDING",
                    }
                )
        for row in skill_review:
            semantic = row.get("semantic_review", {})
            if semantic.get("status") != "COMPLETED":
                continue
            analysis = semantic.get("analysis") or {}
            if float(analysis.get("confidence") or 0.0) < 0.70:
                continue
            action_type = skill_action.get(analysis.get("semantic_class"))
            if action_type:
                actions.append(
                    {
                        "change_id": stable_id("graph_change", row["candidate_id"], action_type),
                        "action": action_type,
                        "candidate_id": row["candidate_id"],
                        "role": row["role"],
                        "skill_id": row["skill_id"],
                        "payload": analysis,
                        "evidence_jd_ids": [
                            item["jd_id"] for item in row["evidence"]
                        ],
                        "approval_status": "PENDING",
                    }
                )
        return {
            "run_id": run_id,
            "status": "DRAFT",
            "dry_run": True,
            "requires_human_approval": True,
            "actions": actions,
        }

    def _write_review_csv(self, review_queue: list[dict[str, Any]]) -> None:
        import csv

        assert self.output_dir is not None
        path = self.output_dir / "review_queue.csv"
        fields = [
            "task_id",
            "task_type",
            "candidate_id",
            "title",
            "rule_reasons",
            "semantic_class",
            "semantic_confidence",
            "semantic_recommendation",
            "status",
            "human_decision",
            "human_comment",
        ]
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields)
            writer.writeheader()
            for task in review_queue:
                semantic = task.get("semantic_review") or {}
                analysis = semantic.get("analysis") or {}
                writer.writerow(
                    {
                        "task_id": task["task_id"],
                        "task_type": task["task_type"],
                        "candidate_id": task["candidate_id"],
                        "title": task["title"],
                        "rule_reasons": "；".join(task["rule_reasons"]),
                        "semantic_class": analysis.get("semantic_class", ""),
                        "semantic_confidence": analysis.get("confidence", ""),
                        "semantic_recommendation": analysis.get(
                            "recommended_action", ""
                        ),
                        "status": task["status"],
                        "human_decision": task["human_decision"],
                        "human_comment": task["human_comment"],
                    }
                )

    def _write_json(self, name: str, payload: Any) -> None:
        assert self.output_dir is not None
        (self.output_dir / name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

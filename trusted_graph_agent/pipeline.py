from __future__ import annotations

import csv
import fnmatch
import json
import math
import re
import traceback
from collections import Counter, defaultdict
from datetime import datetime
from difflib import SequenceMatcher
from itertools import combinations
from pathlib import Path
from typing import Iterable

from .config import AgentConfig
from .extractors import AbilityAnalysisExtractor, EvidenceVerifier, RuleBasedExtractor, WebhookLLMExtractor
from .models import GraphBundle, JobDocument, RoleProfile, RoleSkillEdge, model_dict
from .registry import SkillRegistry
from .taxonomy import RoleTaxonomy
from .text_utils import (
    clean_header,
    clean_value,
    hybrid_similarity,
    infer_level,
    normalize_role_title,
    normalize_text,
    parse_datetime,
    quarter_window,
    simhash64,
    simhash_bands,
    stable_id,
    text_hash,
    time_decay,
)


csv.field_size_limit(64 * 1024 * 1024)


ROLE_GENERIC_WORDS = re.compile(r"工程师|产品经理|经理|开发|设计|人员|技术员|研究员|测试员|分析师|主管")


def role_title_relevance(title: str, target_role: str) -> float:
    """估算一条 JD 标题与 CSV 所代表岗位的相关程度，仅用于轻量 Demo 抽样。"""
    normalized_title = normalize_text(title)
    normalized_target = normalize_text(target_role)
    if not normalized_title:
        return 0.0
    if normalized_title == normalized_target:
        return 1.0
    score = SequenceMatcher(None, normalized_target, normalized_title, autojunk=False).ratio() * 0.45
    if normalized_target in normalized_title:
        score += 0.40
    target_core = ROLE_GENERIC_WORDS.sub("", normalized_target)
    if len(target_core) >= 2 and target_core in normalized_title:
        score += 0.35
    target_chars = set(normalized_target)
    title_chars = set(normalized_title)
    if target_chars and title_chars:
        score += 0.20 * len(target_chars & title_chars) / len(target_chars | title_chars)
    return min(score, 1.0)


def parent_role_name(role_name: str, available_roles: set[str]) -> str:
    if role_name != "产品经理" and role_name.endswith("产品经理") and "产品经理" in available_roles:
        return "产品经理"
    return ""


class TrustedGraphAgent:
    STATES = (
        "RECEIVED",
        "PARSED",
        "CLEANED",
        "DEDUPLICATED",
        "EXTRACTED",
        "VERIFIED",
        "NORMALIZED",
        "AGGREGATED",
        "GRAPH_STAGED",
        "COMPLETED",
    )

    def __init__(self, config: AgentConfig):
        self.config = config
        self.config.output_dir.mkdir(parents=True, exist_ok=True)
        registry_path = Path(__file__).with_name("skills_registry.json")
        self.registry = SkillRegistry(registry_path)
        self.taxonomy = RoleTaxonomy(Path(__file__).with_name("it_role_taxonomy.json"))
        self.analysis_extractor = AbilityAnalysisExtractor(self.registry)
        self.rule_extractor = RuleBasedExtractor(self.registry)
        self.llm_extractor = (
            WebhookLLMExtractor(config.llm_endpoint, config.llm_timeout_seconds)
            if config.llm_endpoint
            else None
        )
        now = datetime.now().astimezone()
        self.run_record: dict = {
            "run_id": stable_id("run", now.isoformat(), str(config.input_dir)),
            "state": "RECEIVED",
            "started_at": now.isoformat(timespec="seconds"),
            "completed_at": "",
            "registry_version": self.registry.version,
            "config": config.to_dict(),
            "state_history": [],
            "warnings": [],
            "summary": {},
        }
        self._transition("RECEIVED", {})

    def run(self) -> GraphBundle:
        try:
            files = self.discover_files()
            documents = self.load_documents(files)
            self._transition("PARSED", {"files": len(files), "rows": len(documents)})
            if not documents:
                raise ValueError("没有读到可用岗位数据，请检查输入目录或筛选条件")

            reference_time = max((item.posted_at for item in documents if item.posted_at), default=datetime.now())
            for document in documents:
                document.time_weight = round(
                    time_decay(document.posted_at, reference_time, self.config.half_life_months), 6
                )
            self.run_record["reference_time"] = reference_time.isoformat(timespec="seconds")
            self._transition("CLEANED", {"reference_time": self.run_record["reference_time"]})

            dedup_metrics = self.deduplicate(documents)
            self._transition("DEDUPLICATED", dedup_metrics)
            active_documents = [document for document in documents if not document.is_duplicate]

            candidates_by_jd: dict[str, list] = {}
            source_counts: Counter[str] = Counter()
            for document in active_documents:
                candidates = self.analysis_extractor.extract(document)
                if not candidates:
                    candidates = self.rule_extractor.extract(document)
                if self.llm_extractor and not document.ability_analysis.strip():
                    try:
                        candidates.extend(self.llm_extractor.extract(document))
                    except Exception as error:  # noqa: BLE001 - 外部 LLM 失败不能中断主流水线
                        self.run_record["warnings"].append(
                            f"LLM 接口处理 {document.jd_id} 失败，已回退规则抽取：{error}"
                        )
                candidates_by_jd[document.jd_id] = candidates
                source_counts.update(candidate.source for candidate in candidates)
            self._transition(
                "EXTRACTED",
                {"candidates": sum(len(items) for items in candidates_by_jd.values()), "sources": dict(source_counts)},
            )

            verifier = EvidenceVerifier(self.registry)
            evidences_by_jd: dict[str, list] = {}
            reviews = []
            evidence_statuses: Counter[str] = Counter()
            for document in active_documents:
                result = verifier.verify(document, candidates_by_jd.get(document.jd_id, []))
                evidences_by_jd[document.jd_id] = result.evidences
                reviews.extend(result.reviews)
                evidence_statuses.update(item.evidence_status for item in result.evidences)
            self._transition("VERIFIED", {"evidence_statuses": dict(evidence_statuses), "reviews": len(reviews)})
            self._transition("NORMALIZED", {"registry_version": self.registry.version})

            bundle = self.aggregate(documents, evidences_by_jd, reviews, reference_time)
            self._transition(
                "AGGREGATED",
                {
                    "roles": len(bundle.roles),
                    "profiles": len(bundle.role_profiles),
                    "role_skill_edges": len(bundle.role_skill_edges),
                },
            )

            from .neo4j_export import export_neo4j_stage
            from .storage import update_run_record, write_database
            from .validation import validate_and_write

            export_neo4j_stage(bundle, self.config.output_dir)
            validation = validate_and_write(bundle, self.config.output_dir)
            if validation["status"] != "PASS":
                raise RuntimeError("图谱自动验收失败，请查看 validation_report.json")
            self.run_record["validation"] = {
                "status": validation["status"],
                "checks_passed": validation["checks_passed"],
                "checks_total": validation["checks_total"],
            }
            self._transition(
                "GRAPH_STAGED",
                {
                    "neo4j_stage": str(self.config.output_dir / "neo4j"),
                    "validation": self.run_record["validation"],
                },
            )
            write_database(bundle, self.config.output_dir / "knowledge_graph.db")

            self.run_record["summary"] = self._summary(bundle, documents, evidence_statuses)
            self.run_record["completed_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            self._transition("COMPLETED", self.run_record["summary"])
            bundle.run = self.run_record
            self._write_json(bundle)
            update_run_record(self.config.output_dir / "knowledge_graph.db", self.run_record)
            return bundle
        except Exception as error:
            self.run_record["state"] = "FAILED"
            self.run_record["completed_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            self.run_record["error"] = str(error)
            self.run_record["traceback"] = traceback.format_exc()
            self._save_manifest()
            raise

    def discover_files(self) -> list[Path]:
        files = sorted(self.config.input_dir.rglob("*.csv"))
        selected = []
        for path in files:
            relative = path.relative_to(self.config.input_dir).as_posix()
            if self.config.it_only and not self.taxonomy.resolve_source(relative):
                continue
            if self.config.include_patterns and not any(
                fnmatch.fnmatch(relative, pattern)
                or fnmatch.fnmatch(path.name, pattern)
                or pattern.lower() in relative.lower()
                for pattern in self.config.include_patterns
            ):
                continue
            selected.append(path)
        return selected

    def load_documents(self, files: Iterable[Path]) -> list[JobDocument]:
        documents: list[JobDocument] = []
        for path in files:
            rows, encoding, scanned_rows = self._read_csv(path)
            relative = path.relative_to(self.config.input_dir).as_posix()
            category = path.parent.name.removesuffix("_output_folder")
            file_role = normalize_role_title(path.stem, path.name)
            taxonomy_role = self.taxonomy.resolve_source(relative)
            standard_role = taxonomy_role["role_name"] if taxonomy_role else file_role
            for index, raw_row in enumerate(rows):
                row = {clean_header(key): clean_value(value) for key, value in raw_row.items() if key is not None}
                title = row.get("职位名称", "")
                description = row.get("职位描述", "")
                if not title and not description:
                    continue
                raw_job_id = row.get("jobID", "") or stable_id("source_job", relative, str(index))
                company_name = row.get("公司全称", "") or "未知公司"
                company_id = row.get("companyID", "") or stable_id("company_source", company_name)
                role = standard_role if (self.config.group_by_file_role or self.config.it_only) else normalize_role_title(title, path.name)
                experience = row.get("经验要求", "")
                document = JobDocument(
                    jd_id=stable_id("jd", relative, raw_job_id, str(index)),
                    source_file=relative,
                    source_category=category,
                    raw_job_id=raw_job_id,
                    company_id=company_id,
                    company_name=company_name,
                    title=title or path.stem,
                    canonical_role=role,
                    description=description,
                    tags=row.get("职位标签", ""),
                    ability_analysis=row.get("能力分析结果", ""),
                    industry=row.get("行业类型", "") or category,
                    education=row.get("学历要求", ""),
                    experience=experience,
                    salary=row.get("薪水", ""),
                    location=row.get("工作地区", "") or row.get("省份", ""),
                    posted_at=parse_datetime(row.get("时间", "")),
                    level=infer_level(title, experience, description),
                    exact_hash=text_hash(company_id or company_name, role, description),
                    simhash=simhash64(description or row.get("职位标签", "")),
                )
                documents.append(document)
            self.run_record.setdefault("source_files", []).append(
                {
                    "path": relative,
                    "standard_role": standard_role,
                    "taxonomy_matched": bool(taxonomy_role),
                    "encoding": encoding,
                    "rows_scanned": scanned_rows,
                    "rows_selected": len(rows),
                }
            )
        return documents

    def _read_csv(self, path: Path) -> tuple[list[dict[str, str]], str, int]:
        last_error: Exception | None = None
        for encoding in ("utf-8-sig", "gb18030", "utf-8"):
            try:
                ranked_rows: list[tuple[float, int, dict[str, str]]] = []
                scanned_rows = 0
                scan_limit = self.config.scan_rows_per_file or self.config.max_rows_per_file
                target_role = path.stem
                with path.open("r", encoding=encoding, newline="") as file:
                    reader = csv.DictReader(file)
                    for index, row in enumerate(reader):
                        if scan_limit > 0 and index >= scan_limit:
                            break
                        scanned_rows += 1
                        if self.config.group_by_file_role and self.config.max_rows_per_file > 0:
                            cleaned = {clean_header(key): clean_value(value) for key, value in row.items() if key is not None}
                            score = role_title_relevance(cleaned.get("职位名称", ""), target_role)
                        else:
                            score = 1.0
                        ranked_rows.append((score, index, row))
                if self.config.max_rows_per_file > 0:
                    ranked_rows = sorted(
                        sorted(ranked_rows, key=lambda item: (-item[0], item[1]))[: self.config.max_rows_per_file],
                        key=lambda item: item[1],
                    )
                return [item[2] for item in ranked_rows], encoding, scanned_rows
            except UnicodeDecodeError as error:
                last_error = error
        raise ValueError(f"无法识别 CSV 编码：{path}") from last_error

    def deduplicate(self, documents: list[JobDocument]) -> dict[str, int]:
        exact_seen: dict[str, JobDocument] = {}
        extracted_seen: dict[tuple[str, str], JobDocument] = {}
        exact_jd_count = 0
        exact_extracted_count = 0
        for document in documents:
            previous = exact_seen.get(document.exact_hash)
            if previous:
                document.duplicate_of = previous.jd_id
                document.duplicate_reason = "EXACT_DUPLICATE"
                exact_jd_count += 1
                continue
            exact_seen[document.exact_hash] = document
            extracted_text = normalize_text(document.ability_analysis)
            if len(extracted_text) < self.config.extracted_entry_min_length:
                continue
            extracted_key = (document.canonical_role, extracted_text)
            previous = extracted_seen.get(extracted_key)
            if previous:
                document.duplicate_of = previous.jd_id
                document.duplicate_reason = "EXACT_EXTRACTED_ENTRY_DUPLICATE"
                exact_extracted_count += 1
                continue
            extracted_seen[extracted_key] = document

        active = [document for document in documents if not document.is_duplicate]
        role_groups: dict[str, list[JobDocument]] = defaultdict(list)
        for document in active:
            role_groups[document.canonical_role].append(document)

        template_documents = 0
        template_clusters = 0
        for role, role_documents in role_groups.items():
            clusters: list[dict] = []
            band_index: dict[tuple[int, int], set[int]] = defaultdict(set)
            for document in role_documents:
                extracted_text = normalize_text(document.ability_analysis)
                if len(extracted_text) >= self.config.extracted_entry_min_length:
                    comparison_source = "EXTRACTED_ENTRY"
                    comparison_text = extracted_text
                else:
                    comparison_source = "JD_DESCRIPTION"
                    comparison_text = document.description
                comparison_hash = simhash64(comparison_text)
                bands = simhash_bands(comparison_hash, 8)
                candidates: set[int] = set()
                for band_position, band in enumerate(bands):
                    candidates.update(band_index[(band_position, band)])
                if not candidates:
                    candidates.update(range(max(0, len(clusters) - 100), len(clusters)))
                best_cluster = None
                best_score = 0.0
                for cluster_index in list(candidates)[:80]:
                    cluster = clusters[cluster_index]
                    if cluster["comparison_source"] != comparison_source:
                        continue
                    if comparison_source == "EXTRACTED_ENTRY":
                        score = SequenceMatcher(
                            None,
                            comparison_text,
                            cluster["comparison_text"],
                            autojunk=False,
                        ).ratio()
                    else:
                        score = hybrid_similarity(
                            comparison_text,
                            cluster["comparison_text"],
                            comparison_hash,
                            cluster["comparison_hash"],
                        )
                    if score >= self.config.template_similarity and score > best_score:
                        best_cluster = cluster_index
                        best_score = score
                if best_cluster is None:
                    best_cluster = len(clusters)
                    clusters.append(
                        {
                            "representative": document,
                            "comparison_source": comparison_source,
                            "comparison_text": comparison_text,
                            "comparison_hash": comparison_hash,
                            "members": [],
                        }
                    )
                clusters[best_cluster]["members"].append(document)
                for band_position, band in enumerate(bands):
                    band_index[(band_position, band)].add(best_cluster)

            for cluster in clusters:
                members: list[JobDocument] = cluster["members"]
                if len(members) < 2:
                    continue
                template_clusters += 1
                cluster_id = stable_id("template", role, cluster["comparison_source"], cluster["comparison_text"])
                weight = max(self.config.template_weight_floor, 1.0 / math.sqrt(len(members)))
                for member in members:
                    member.template_cluster_id = cluster_id
                    member.template_weight = round(weight, 6)
                    template_documents += 1
        return {
            "exact_duplicates": exact_jd_count + exact_extracted_count,
            "exact_jd_duplicates": exact_jd_count,
            "exact_extracted_entry_duplicates": exact_extracted_count,
            "semantic_duplicates": 0,
            "active_documents": len(active),
            "template_clusters": template_clusters,
            "template_documents": template_documents,
        }

    def aggregate(
        self,
        documents: list[JobDocument],
        evidences_by_jd: dict[str, list],
        reviews: list,
        reference_time: datetime,
    ) -> GraphBundle:
        active = [document for document in documents if not document.is_duplicate]
        role_documents: dict[str, list[JobDocument]] = defaultdict(list)
        for document in active:
            role_documents[document.canonical_role].append(document)

        roles = []
        available_roles = set(role_documents)
        for role_name, items in sorted(role_documents.items()):
            taxonomy_role = self.taxonomy.role(role_name)
            parent_name = taxonomy_role.get("parent_role", "") if taxonomy_role else parent_role_name(role_name, available_roles)
            family = self.taxonomy.family_by_id.get(taxonomy_role.get("family_id", ""), {}) if taxonomy_role else {}
            roles.append(
                {
                    "role_id": stable_id("role", role_name),
                    "role_name": role_name,
                    "parent_role_id": stable_id("role", parent_name) if parent_name else "",
                    "parent_role_name": parent_name,
                    "family_id": family.get("family_id", ""),
                    "family_name": family.get("family_name", ""),
                    "domain_id": self.taxonomy.domain["domain_id"] if taxonomy_role else "",
                    "domain_name": self.taxonomy.domain["domain_name"] if taxonomy_role else "",
                    "document_count": len(items),
                    "company_count": len({item.company_id or item.company_name for item in items}),
                    "industries": "|".join(sorted({item.source_category for item in items})),
                }
            )

        industry_names = sorted({document.source_category for document in active})
        industries = [
            {"industry_id": stable_id("industry", name), "industry_name": name}
            for name in industry_names
        ]
        level_names = ["实习/应届", "初级", "中级", "高级", "专家", "管理岗", "未注明"]
        levels = [{"level_id": stable_id("level", name), "level_name": name} for name in level_names]

        companies_by_id: dict[str, dict] = {}
        for document in documents:
            graph_id = self._company_graph_id(document)
            companies_by_id.setdefault(
                graph_id,
                {
                    "company_id": graph_id,
                    "source_company_id": document.company_id,
                    "company_name": document.company_name,
                },
            )

        profile_groups: dict[tuple[str, str, str, str], list[JobDocument]] = defaultdict(list)
        for document in active:
            window, _ = quarter_window(document.posted_at, reference_time)
            profile_groups[(document.canonical_role, document.source_category, document.level, window)].append(document)

        profile_rows: list[dict] = []
        profile_documents: dict[str, list[JobDocument]] = {}
        jd_profile: dict[str, str] = {}
        for (role_name, industry_name, level_name, window), items in sorted(profile_groups.items()):
            _, window_start = quarter_window(items[0].posted_at, reference_time)
            profile = RoleProfile(
                profile_id=stable_id("profile", role_name, industry_name, level_name, window),
                role_id=stable_id("role", role_name),
                role_name=role_name,
                industry_id=stable_id("industry", industry_name),
                industry_name=industry_name,
                level_id=stable_id("level", level_name),
                level_name=level_name,
                time_window=window,
                window_start=window_start,
                jd_count=len(items),
                company_count=len({item.company_id or item.company_name for item in items}),
            )
            row = model_dict(profile)
            row["previous_profile_id"] = ""
            row["window_id"] = stable_id("window", window)
            profile_rows.append(row)
            profile_documents[profile.profile_id] = items
            for document in items:
                jd_profile[document.jd_id] = profile.profile_id

        by_profile_series: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
        for profile in profile_rows:
            by_profile_series[(profile["role_id"], profile["industry_id"], profile["level_id"])].append(profile)
        for series in by_profile_series.values():
            series.sort(key=lambda item: item["window_start"])
            for previous, current in zip(series, series[1:]):
                current["previous_profile_id"] = previous["profile_id"]

        accepted_by_jd: dict[str, dict[str, object]] = defaultdict(dict)
        jd_skill_rows: list[dict] = []
        for jd_id, evidences in evidences_by_jd.items():
            for evidence in evidences:
                row = model_dict(evidence)
                row["edge_id"] = stable_id(
                    "jd_skill", jd_id, evidence.skill_id, evidence.evidence_status, evidence.source
                )
                row["profile_id"] = jd_profile.get(jd_id, "")
                jd_skill_rows.append(row)
                if evidence.evidence_status in {"VERIFIED", "LOW_CONFIDENCE", "ANALYSIS_ONLY"}:
                    accepted_by_jd[jd_id][evidence.skill_id] = evidence

        role_skill_rows: list[dict] = []
        for profile in profile_rows:
            items = profile_documents[profile["profile_id"]]
            base_weights = {item.jd_id: item.time_weight * item.template_weight for item in items}
            jd_denominator = sum(base_weights.values()) or 1.0
            company_base: dict[str, float] = defaultdict(float)
            for item in items:
                company = item.company_id or item.company_name
                company_base[company] = max(company_base[company], base_weights[item.jd_id])
            company_denominator = sum(company_base.values()) or 1.0
            all_skill_ids = sorted({skill_id for item in items for skill_id in accepted_by_jd.get(item.jd_id, {})})
            for skill_id in all_skill_ids:
                hits = []
                for item in items:
                    evidence = accepted_by_jd.get(item.jd_id, {}).get(skill_id)
                    if evidence:
                        hits.append((item, evidence))
                if not hits:
                    continue
                jd_vote = sum(base_weights[item.jd_id] * evidence.confidence for item, evidence in hits)
                skill_company_vote: dict[str, float] = defaultdict(float)
                company_template_weight: dict[str, float] = defaultdict(float)
                for item, evidence in hits:
                    company = item.company_id or item.company_name
                    skill_company_vote[company] = max(
                        skill_company_vote[company], base_weights[item.jd_id] * evidence.confidence
                    )
                    company_template_weight[company] = max(company_template_weight[company], item.template_weight)
                jd_support = jd_vote / jd_denominator
                company_support = sum(skill_company_vote.values()) / company_denominator
                adjusted = 0.7 * company_support + 0.3 * jd_support
                preferred_mentions = sum(evidence.requirement_type == "preferred" for _, evidence in hits)
                effective_companies = sum(company_template_weight.values())
                all_preferred = preferred_mentions == len(hits)
                if adjusted < self.config.preferred_support_threshold and not preferred_mentions:
                    continue
                if all_preferred:
                    relation, tier = "PREFERS_SKILL", "bonus"
                elif (
                    adjusted >= self.config.required_support_threshold
                    and effective_companies >= self.config.min_required_companies
                ):
                    relation, tier = "REQUIRES_SKILL", "required"
                elif adjusted >= self.config.common_support_threshold:
                    relation, tier = "REQUIRES_SKILL", "common"
                else:
                    relation, tier = "REQUIRES_SKILL", "emerging"
                edge = RoleSkillEdge(
                    edge_id=stable_id("role_skill", profile["profile_id"], skill_id),
                    profile_id=profile["profile_id"],
                    role_id=profile["role_id"],
                    skill_id=skill_id,
                    relation=relation,
                    tier=tier,
                    jd_support=round(jd_support, 6),
                    company_support=round(company_support, 6),
                    adjusted_support=round(adjusted, 6),
                    company_count=len(skill_company_vote),
                    effective_company_count=round(effective_companies, 4),
                    evidence_count=len(hits),
                    preferred_mentions=preferred_mentions,
                )
                role_skill_rows.append(model_dict(edge))

        related_rows = self._related_edges(active, accepted_by_jd)
        evolution_rows = self._evolution_edges(profile_rows, role_skill_rows)
        snapshot_rows = self._role_skill_snapshots(profile_rows, role_skill_rows)

        jd_rows = []
        for document in documents:
            jd_rows.append(
                {
                    "jd_id": document.jd_id,
                    "raw_job_id": document.raw_job_id,
                    "title": document.title,
                    "canonical_role": document.canonical_role,
                    "role_id": stable_id("role", document.canonical_role),
                    "profile_id": jd_profile.get(document.jd_id, ""),
                    "company_id": self._company_graph_id(document),
                    "company_name": document.company_name,
                    "industry_id": stable_id("industry", document.source_category),
                    "industry_name": document.source_category,
                    "industry_detail": document.industry,
                    "level_id": stable_id("level", document.level),
                    "level_name": document.level,
                    "education": document.education,
                    "experience": document.experience,
                    "salary": document.salary,
                    "location": document.location,
                    "posted_at": document.posted_at.isoformat(timespec="seconds") if document.posted_at else "",
                    "source_file": document.source_file,
                    "description": document.description,
                    "tags": document.tags,
                    "ability_analysis": document.ability_analysis,
                    "duplicate_of": document.duplicate_of,
                    "duplicate_reason": document.duplicate_reason,
                    "template_cluster_id": document.template_cluster_id,
                    "template_weight": document.template_weight,
                    "time_weight": document.time_weight,
                    "base_weight": round(document.template_weight * document.time_weight, 6),
                }
            )

        review_rows = [model_dict(review) for review in reviews]
        time_windows = [
            {
                "window_id": stable_id("window", window),
                "time_window": window,
                "window_start": min(
                    row["window_start"] for row in profile_rows if row["time_window"] == window
                ),
            }
            for window in sorted({row["time_window"] for row in profile_rows})
        ]
        return GraphBundle(
            run=self.run_record,
            role_families=self.taxonomy.family_rows() if self.config.it_only else [],
            role_aliases=[row for row in self.taxonomy.alias_rows() if row["role_id"] in {role["role_id"] for role in roles}],
            role_relations=[row for row in self.taxonomy.relation_rows() if row["parent_role_id"] in {role["role_id"] for role in roles} and row["child_role_id"] in {role["role_id"] for role in roles}],
            roles=roles,
            role_profiles=profile_rows,
            skills=self._skill_rows(evidences_by_jd),
            industries=industries,
            levels=levels,
            time_windows=time_windows,
            companies=list(companies_by_id.values()),
            jds=jd_rows,
            role_skill_edges=role_skill_rows,
            role_skill_snapshots=snapshot_rows,
            jd_skill_edges=jd_skill_rows,
            related_skill_edges=related_rows,
            evolution_edges=evolution_rows,
            review_tasks=review_rows,
        )

    def _skill_rows(self, evidences_by_jd: dict[str, list]) -> list[dict[str, str]]:
        rows = {row["skill_id"]: row for row in self.registry.as_rows()}
        category_votes: dict[str, Counter[str]] = defaultdict(Counter)
        stack_votes: dict[str, Counter[str]] = defaultdict(Counter)
        names: dict[str, Counter[str]] = defaultdict(Counter)
        aliases: dict[str, set[str]] = defaultdict(set)
        for evidences in evidences_by_jd.values():
            for evidence in evidences:
                if evidence.evidence_status.startswith("REJECTED"):
                    continue
                names[evidence.skill_id][evidence.skill_name] += 1
                aliases[evidence.skill_id].add(evidence.raw_term)
                if evidence.competency_category:
                    category_votes[evidence.skill_id][evidence.competency_category] += 1
                if evidence.tech_stack:
                    stack_votes[evidence.skill_id][evidence.tech_stack] += 1

        for skill_id, name_counts in names.items():
            canonical_name = name_counts.most_common(1)[0][0]
            row = rows.get(skill_id)
            if row is None:
                row = {
                    "skill_id": skill_id,
                    "canonical_name": canonical_name,
                    "aliases": "",
                    "competency_category": "其他能力",
                    "tech_stack": "",
                    "registry_version": "ability-analysis",
                }
                rows[skill_id] = row
            if category_votes[skill_id]:
                row["competency_category"] = category_votes[skill_id].most_common(1)[0][0]
            if stack_votes[skill_id]:
                row["tech_stack"] = stack_votes[skill_id].most_common(1)[0][0]
            row["aliases"] = "|".join(sorted({row["canonical_name"], *aliases[skill_id]}))
        return sorted(rows.values(), key=lambda row: (row["competency_category"], row["canonical_name"]))

    @staticmethod
    def _role_skill_snapshots(profiles: list[dict], role_skill_edges: list[dict]) -> list[dict]:
        profiles_by_id = {row["profile_id"]: row for row in profiles}
        grouped: dict[tuple[str, str, str], list[tuple[dict, dict]]] = defaultdict(list)
        for edge in role_skill_edges:
            profile = profiles_by_id[edge["profile_id"]]
            grouped[(edge["role_id"], profile["time_window"], edge["skill_id"])].append((profile, edge))

        rows = []
        for (role_id, time_window, skill_id), items in grouped.items():
            total_jds = sum(profile["jd_count"] for profile, _ in items) or 1
            support = sum(edge["adjusted_support"] * profile["jd_count"] for profile, edge in items) / total_jds
            jd_support = sum(edge["jd_support"] * profile["jd_count"] for profile, edge in items) / total_jds
            company_support = sum(edge["company_support"] * profile["jd_count"] for profile, edge in items) / total_jds
            preferred = sum(edge["preferred_mentions"] for _, edge in items)
            evidence = sum(edge["evidence_count"] for _, edge in items)
            relation = "PREFERS_SKILL" if evidence and preferred >= evidence else "REQUIRES_SKILL"
            if relation == "PREFERS_SKILL":
                tier = "bonus"
            elif support >= 0.60:
                tier = "required"
            elif support >= 0.30:
                tier = "common"
            else:
                tier = "emerging"
            rows.append(
                {
                    "snapshot_id": stable_id("role_skill_snapshot", role_id, time_window, skill_id),
                    "role_id": role_id,
                    "skill_id": skill_id,
                    "time_window": time_window,
                    "window_start": min(profile["window_start"] for profile, _ in items),
                    "relation": relation,
                    "tier": tier,
                    "adjusted_support": round(support, 6),
                    "jd_support": round(jd_support, 6),
                    "company_support": round(company_support, 6),
                    "evidence_count": evidence,
                    "jd_count": total_jds,
                    "company_count": sum(edge["company_count"] for _, edge in items),
                    "previous_support": 0.0,
                    "delta": 0.0,
                    "trend": "NEW",
                }
            )

        series: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for row in rows:
            series[(row["role_id"], row["skill_id"])].append(row)
        for snapshots in series.values():
            snapshots.sort(key=lambda item: item["window_start"])
            for previous, current in zip(snapshots, snapshots[1:]):
                current["previous_support"] = previous["adjusted_support"]
                current["delta"] = round(current["adjusted_support"] - previous["adjusted_support"], 6)
                if current["delta"] >= 0.08:
                    current["trend"] = "UP"
                elif current["delta"] <= -0.08:
                    current["trend"] = "DOWN"
                else:
                    current["trend"] = "STABLE"
        return sorted(rows, key=lambda item: (item["window_start"], item["role_id"], -item["adjusted_support"]))

    def _related_edges(self, documents: list[JobDocument], verified_by_jd: dict[str, dict]) -> list[dict]:
        skill_documents: Counter[str] = Counter()
        cooccurrence: Counter[tuple[str, str]] = Counter()
        for document in documents:
            skills = sorted(verified_by_jd.get(document.jd_id, {}))
            skill_documents.update(skills)
            cooccurrence.update(combinations(skills, 2))
        rows = []
        for (source, target), count in cooccurrence.items():
            if count < self.config.related_min_cooccurrence:
                continue
            union = skill_documents[source] + skill_documents[target] - count
            score = count / union if union else 0.0
            if score < self.config.related_min_score:
                continue
            rows.append(
                {
                    "edge_id": stable_id("skill_related", source, target),
                    "source_skill_id": source,
                    "target_skill_id": target,
                    "relation": "RELATED_TO",
                    "cooccurrence": count,
                    "jaccard_score": round(score, 6),
                }
            )
        rows.sort(key=lambda item: (item["jaccard_score"], item["cooccurrence"]), reverse=True)
        return rows[:2000]

    @staticmethod
    def _evolution_edges(profiles: list[dict], role_skill_edges: list[dict]) -> list[dict]:
        supports: dict[str, dict[str, float]] = defaultdict(dict)
        for edge in role_skill_edges:
            supports[edge["profile_id"]][edge["skill_id"]] = edge["adjusted_support"]
        rows = []
        for current in profiles:
            previous_id = current.get("previous_profile_id", "")
            if not previous_id:
                continue
            previous_supports = supports.get(previous_id, {})
            current_supports = supports.get(current["profile_id"], {})
            for skill_id in sorted(set(previous_supports) | set(current_supports)):
                before = previous_supports.get(skill_id, 0.0)
                after = current_supports.get(skill_id, 0.0)
                delta = after - before
                if after >= 0.20 and before < 0.10:
                    change = "ADDED"
                elif before >= 0.20 and after < 0.10:
                    change = "REMOVED"
                elif delta >= 0.15:
                    change = "STRENGTHENED"
                elif delta <= -0.15:
                    change = "WEAKENED"
                else:
                    continue
                rows.append(
                    {
                        "evolution_id": stable_id("evolution", previous_id, current["profile_id"], skill_id),
                        "previous_profile_id": previous_id,
                        "current_profile_id": current["profile_id"],
                        "skill_id": skill_id,
                        "change_type": change,
                        "previous_support": round(before, 6),
                        "current_support": round(after, 6),
                        "delta": round(delta, 6),
                    }
                )
        return rows

    def _summary(self, bundle: GraphBundle, documents: list[JobDocument], statuses: Counter[str]) -> dict:
        active = [document for document in documents if not document.is_duplicate]
        return {
            "input_documents": len(documents),
            "active_documents": len(active),
            "duplicates": len(documents) - len(active),
            "template_documents": sum(bool(document.template_cluster_id) for document in active),
            "roles": len(bundle.roles),
            "profiles": len(bundle.role_profiles),
            "skills_in_graph": len(bundle.skills),
            "base_registry_skills": len(self.registry.skills),
            "role_skill_edges": len(bundle.role_skill_edges),
            "related_skill_edges": len(bundle.related_skill_edges),
            "evolution_edges": len(bundle.evolution_edges),
            "review_tasks": len(bundle.review_tasks),
            "evidence_statuses": dict(statuses),
        }

    def _transition(self, state: str, metrics: dict) -> None:
        self.run_record["state"] = state
        self.run_record["state_history"].append(
            {
                "state": state,
                "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "metrics": metrics,
            }
        )
        self._save_manifest()

    def _save_manifest(self) -> None:
        path = self.config.output_dir / "run_manifest.json"
        path.write_text(json.dumps(self.run_record, ensure_ascii=False, indent=2), encoding="utf-8")

    def _write_json(self, bundle: GraphBundle) -> None:
        path = self.config.output_dir / "knowledge_graph.json"
        path.write_text(json.dumps(bundle.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        self._save_manifest()

    @staticmethod
    def _company_graph_id(document: JobDocument) -> str:
        return stable_id("company", document.company_id or document.company_name)

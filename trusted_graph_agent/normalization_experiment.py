from __future__ import annotations

import csv
import json
import math
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import Protocol

from .registry import SkillRegistry
from .text_utils import normalize_text, stable_id


ACCEPTED_EVIDENCE = {"VERIFIED", "LOW_CONFIDENCE", "ANALYSIS_ONLY"}
PREFIXES = re.compile(
    r"^(?:(?:具备|具有|能够|可以|熟悉|熟练掌握|掌握|精通|了解|拥有|有)|"
    r"(?:良好的|较好的|较强的|优秀的|扎实的|丰富的|一定的|基础的))+"
)
SUFFIXES = re.compile(r"(?:相关能力|相关技术|相关经验|使用经验|开发经验)$")
NOISE_PATTERN = re.compile(
    r"未明确提及|未明确描述|无明确描述|无明确表述|无直接描述|原文中无|原文未|未体现|"
    r"没有明确|没有对应描述|无对应描述|没有相关描述|无相关描述|没有对应内容|无对应内容|"
    r"没有相关内容|无相关内容|暂无相关|未给出|未说明|未提及|不适用|无法判断|无法确定|"
    r"根据(?:胜任力模型)?(?:分析)?要求|根据要求|已归入|显性要求|"
    r"注[:：]|维度无直接|价值观等"
)
NOISE_EXACT = {"无", "没有", "暂无", "未提及", "无描述", "无相关要求", "不明确", "不详", "未知", "无要求"}
META_COMMENTARY_PATTERN = re.compile(
    r"所有输出要素|输出要素均|严格对应原文|均对应原文|"
    r"所有(?:能力)?要素均直接提取|所有短语均直接来自招聘文本|"
    r"胜任力模型(?:分析)?|未纳入胜任力|"
    r"直接描述要素|能力要素描述|对应的能力要素|"
    r"无明确对应项|无明确对应要素|无直接对应要素|无明确(?:动机|技术|知识|技能|特质|自我概念)?(?:要素|要求)|"
    r"未出现直接对应|未发现直接对应|未找到直接对应|未明确体现|未涉及(?:动机|特质|自我概念)|"
    r"原文表述中未|原文中未出现|原文中未发现|"
    r"(?:采用|并以).*?(?:短词组|形式呈现)|"
    r"(?:故|因此)(?:未|保持|留空|标注|输出|在两个维度)|"
    r"按(?:原文|维度).*?(?:呈现|列出|分组)|归类为.*?维度|"
    r"如果您有其他|我可以进一步|严格按胜任力维度|严格提取招聘文本|"
    r"(?:知识|技术|技能|动机|特质|自我概念)(?:和|及|、)?"
    r"(?:知识|技术|技能|动机|特质|自我概念)?维度的具体要求"
)
COMPETENCY_DIMENSION_PATTERN = re.compile(r"知识|技术|技能|动机|特质|自我概念")
META_ABSENCE_PATTERN = re.compile(
    r"未出现|未提及|未发现|未找到|未体现|无直接|无明确|没有|缺少|"
    r"直接描述|对应(?:的)?(?:能力)?要素|描述要素"
)


def is_noise_phrase(value: str) -> bool:
    text = re.sub(r"\s+", "", value or "").strip("。；;，,：: ")
    if not text:
        return True
    if META_COMMENTARY_PATTERN.search(text):
        return True
    dimension_hits = set(COMPETENCY_DIMENSION_PATTERN.findall(text))
    return "维度" in text and bool(dimension_hits) and bool(META_ABSENCE_PATTERN.search(text))


QUALIFICATION_PATTERN = re.compile(
    r"学历|本科|硕士|博士|大专|专业|工作经验|项目经验|行业经验|产品经验|"
    r"从业经验|开发经验|实习经验|英语[四六46]级|CET[- ]?[46]|"
    r"(?:\d+|[一二三四五六七八九十]+)年|出差"
)
PREFERENCE_PATTERN = re.compile(r"形象|外貌|年龄|性别|婚育|户籍")
VAGUE_PHRASES = {"工程能力", "综合能力", "工作能力", "业务能力", "专业能力", "技术能力", "能吃苦", "吃苦耐劳"}
YEAR_EXPRESSION = re.compile(r"(?:\d+|[一二三四五六七八九十]+)\s*年")


class TextEmbedder(Protocol):
    def encode(self, texts: list[str]) -> object: ...


class SentenceTransformerEmbedder:
    def __init__(self, model_name: str, batch_size: int = 32, device: str = "cpu"):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as error:
            raise RuntimeError(
                "缺少 sentence-transformers，请先运行：python -m pip install sentence-transformers"
            ) from error
        self.model = SentenceTransformer(model_name, device=device)
        self.batch_size = batch_size

    def encode(self, texts: list[str]):
        return self.model.encode(
            texts,
            batch_size=self.batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=True,
        )


@dataclass(slots=True)
class NormalizationConfig:
    model_name: str = "BAAI/bge-small-zh-v1.5"
    embedding_batch_size: int = 32
    nearest_neighbor_top_k: int = 10
    auto_merge_score: float = 0.94
    cluster_similarity_threshold: float = 0.88
    minimum_lexical_guard: float = 0.45
    phrase_similarity_weight: float = 0.65
    context_similarity_weight: float = 0.25
    lexical_similarity_weight: float = 0.10
    minimum_company_count: int = 3
    minimum_verified_jd_count: int = 4
    standard_candidate_min_companies: int = 3
    standard_candidate_min_jds: int = 5
    standard_candidate_verified_rate: float = 0.50
    company_coverage_weight: float = 0.40
    jd_coverage_weight: float = 0.20
    role_distinctiveness_weight: float = 0.10
    requirement_weight: float = 0.10
    evidence_weight: float = 0.10
    time_weight: float = 0.10
    mmr_relevance_weight: float = 0.75
    mmr_candidate_count_per_category: int = 20
    candidate_min_jds: int = 8
    candidate_jd_ratio: float = 0.001
    candidate_min_companies: int = 5
    candidate_company_ratio: float = 0.001
    core_min_jds: int = 15
    core_jd_ratio: float = 0.005
    core_min_companies: int = 8
    core_company_ratio: float = 0.005
    small_role_cutoff: int = 100
    small_candidate_ratio: float = 0.10
    small_core_ratio: float = 0.20
    ann_top_k: int = 20
    ann_hnsw_m: int = 32
    ann_ef_construction: int = 160
    ann_ef_search: int = 96
    ann_mutual_top_k: int = 5
    ann_phrase_similarity: float = 0.96
    ann_context_similarity: float = 0.93
    ann_combined_similarity: float = 0.95
    ann_no_lexical_phrase_similarity: float = 0.98
    ann_no_lexical_context_similarity: float = 0.95
    ann_max_cluster_size: int = 60
    top_k: dict[str, int] = field(
        default_factory=lambda: {"技术": 6, "知识": 4, "特质": 3, "动机": 2, "自我概念": 2}
    )

    @classmethod
    def load(cls, path: Path) -> "NormalizationConfig":
        return cls(**json.loads(path.read_text(encoding="utf-8")))


@dataclass(slots=True)
class SkillCandidateGroup:
    key: str
    skill_id: str
    name: str
    category: str
    aliases: set[str] = field(default_factory=set)
    jd_ids: set[str] = field(default_factory=set)
    company_ids: set[str] = field(default_factory=set)
    roles: set[str] = field(default_factory=set)
    contexts: list[str] = field(default_factory=list)
    verified_jds: set[str] = field(default_factory=set)
    role_jds: dict[str, set[str]] = field(default_factory=dict)
    role_companies: dict[str, set[str]] = field(default_factory=dict)


class MentionSource:
    """可重复迭代的 SQLite 能力证据流，避免把数百万行一次性读入内存。"""

    QUERY = """
        SELECT e.jd_id, e.skill_id, e.skill_name, e.raw_term, e.requirement_type,
               e.evidence_quote, e.evidence_status, e.confidence, e.competency_category,
               j.canonical_role, j.role_id, j.company_id, j.posted_at,
               j.time_weight, j.template_weight
        FROM jd_skill_edges e
        JOIN jds j ON j.jd_id = e.jd_id
        WHERE j.duplicate_of = ''
          AND e.evidence_status IN ('VERIFIED', 'LOW_CONFIDENCE', 'ANALYSIS_ONLY')
        ORDER BY e.jd_id
    """

    def __init__(self, database_path: Path, count: int):
        self.database_path = database_path
        self.count = count

    def __len__(self) -> int:
        return self.count

    def __iter__(self):
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            for row in connection.execute(self.QUERY):
                yield dict(row)
        finally:
            connection.close()


class UnionFind:
    def __init__(self, size: int):
        self.parent = list(range(size))
        self.component_size = [1] * size

    def find(self, value: int) -> int:
        while self.parent[value] != value:
            self.parent[value] = self.parent[self.parent[value]]
            value = self.parent[value]
        return value

    def union(self, left: int, right: int, max_size: int = 0) -> bool:
        left_root = self.find(left)
        right_root = self.find(right)
        if left_root != right_root:
            combined_size = self.component_size[left_root] + self.component_size[right_root]
            if max_size and combined_size > max_size:
                return False
            self.parent[right_root] = left_root
            self.component_size[left_root] = combined_size
            return True
        return False


def normalize_surface(value: str) -> str:
    text = re.sub(r"\s+", " ", value).strip(" \t\r\n。；;，,：:")
    previous = ""
    while text != previous:
        previous = text
        text = PREFIXES.sub("", text).strip()
    text = SUFFIXES.sub("", text).strip()
    return text.strip("()（）[]【】")


def corrected_category(name: str, current: str) -> str:
    if is_noise_phrase(name):
        return "噪声"
    if NOISE_PATTERN.search(name) or name.strip() in NOISE_EXACT or name in VAGUE_PHRASES:
        return "噪声"
    if PREFERENCE_PATTERN.search(name):
        return "招聘偏好"
    if QUALIFICATION_PATTERN.search(name):
        return "任职条件"
    return current or "其他能力"


def lexical_similarity(left: str, right: str) -> float:
    left_normalized = normalize_text(left)
    right_normalized = normalize_text(right)
    if not left_normalized or not right_normalized:
        return 0.0
    if left_normalized == right_normalized:
        return 1.0
    containment = min(len(left_normalized), len(right_normalized)) / max(len(left_normalized), len(right_normalized))
    if left_normalized in right_normalized or right_normalized in left_normalized:
        return max(containment, SequenceMatcher(None, left_normalized, right_normalized).ratio())
    return SequenceMatcher(None, left_normalized, right_normalized).ratio()


def shared_ascii_token(left: str, right: str) -> bool:
    pattern = re.compile(r"[a-z][a-z0-9+#.]{1,}", re.IGNORECASE)
    return bool(set(pattern.findall(left.lower())) & set(pattern.findall(right.lower())))


def merge_allowed(left: str, right: str, lexical_score: float, minimum_lexical_guard: float) -> bool:
    normalized_left = normalize_text(left)
    normalized_right = normalize_text(right)
    left_years = set(YEAR_EXPRESSION.findall(left))
    right_years = set(YEAR_EXPRESSION.findall(right))
    if left_years and right_years and left_years != right_years:
        return False
    return (
        lexical_score >= minimum_lexical_guard
        or normalized_left in normalized_right
        or normalized_right in normalized_left
        or shared_ascii_token(left, right)
    )


class NormalizationExperiment:
    def __init__(
        self,
        database_path: Path,
        output_dir: Path,
        config: NormalizationConfig,
        embedder: TextEmbedder,
    ):
        self.database_path = database_path
        self.output_dir = output_dir
        self.config = config
        self.embedder = embedder
        self.registry = SkillRegistry(Path(__file__).with_name("skills_registry.json"))
        self.cross_category_exact_merges = 0
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def run(self) -> dict:
        import numpy as np

        mentions, role_totals = self._load_mentions()
        groups = self._group_candidates(mentions)
        groups, candidate_metrics = self._filter_candidate_groups(groups, role_totals)
        registry_groups = self._registry_groups()
        all_groups = [*registry_groups, *groups]
        phrase_texts = [f"{item.category}能力：{item.name}" for item in all_groups]
        context_texts = [self._context_text(item) for item in all_groups]
        phrase_vectors = np.asarray(self.embedder.encode(phrase_texts), dtype="float32")
        context_vectors = np.asarray(self.embedder.encode(context_texts), dtype="float32")

        registry_count = len(registry_groups)
        mapping, reviews, metrics = self._normalize_groups(
            all_groups,
            phrase_vectors,
            context_vectors,
            registry_count,
        )
        concepts = self._build_concepts(all_groups, mapping, registry_count, phrase_vectors)
        role_scores, top_skills = self._rank_roles(
            mentions,
            mapping,
            all_groups,
            concepts,
            phrase_vectors,
            role_totals,
        )
        report = self._write_outputs(
            mentions,
            groups,
            concepts,
            mapping,
            reviews,
            role_scores,
            top_skills,
            {**candidate_metrics, **metrics},
        )
        return report

    def analyze_candidate_pool(self) -> dict:
        mentions, role_totals = self._load_mentions()
        groups = self._group_candidates(mentions)
        filtered, metrics = self._filter_candidate_groups(groups, role_totals)
        return {
            "input_mentions": len(mentions),
            "roles": len(role_totals),
            **metrics,
            "estimated_phrase_vector_mb": round(len(filtered) * 384 * 4 / (1024**2), 2),
            "estimated_two_vector_sets_mb": round(len(filtered) * 384 * 4 * 2 / (1024**2), 2),
        }

    def _load_mentions(self) -> tuple[MentionSource, dict[str, dict[str, set[str]]]]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            mention_count = int(
                connection.execute(
                    """
                    SELECT count(*)
                    FROM jd_skill_edges e
                    JOIN jds j ON j.jd_id = e.jd_id
                    WHERE j.duplicate_of = ''
                      AND e.evidence_status IN ('VERIFIED', 'LOW_CONFIDENCE', 'ANALYSIS_ONLY')
                    """
                ).fetchone()[0]
            )
            role_totals: dict[str, dict[str, set[str]]] = defaultdict(lambda: {"jds": set(), "companies": set()})
            for row in connection.execute(
                "SELECT jd_id, canonical_role, company_id FROM jds WHERE duplicate_of = ''"
            ):
                role = sys.intern(str(row["canonical_role"]))
                role_totals[role]["jds"].add(sys.intern(str(row["jd_id"])))
                role_totals[role]["companies"].add(sys.intern(str(row["company_id"])))
        finally:
            connection.close()
        return MentionSource(self.database_path, mention_count), dict(role_totals)

    def _group_candidates(self, mentions) -> list[SkillCandidateGroup]:
        self.cross_category_exact_merges = 0
        grouped: dict[str, SkillCandidateGroup] = {}
        for mention in mentions:
            name = normalize_surface(mention["skill_name"] or mention["raw_term"])
            category = corrected_category(name, mention["competency_category"] or "其他能力")
            if category == "噪声" or not name:
                continue
            definition = self.registry.resolve(name) or self.registry.resolve(mention["raw_term"] or "")
            if definition is not None:
                category = definition.competency_category
            key = f"{category}|{normalize_text(name)}"
            item = grouped.get(key)
            if item is None:
                item = SkillCandidateGroup(
                    key=key,
                    skill_id=mention["skill_id"],
                    name=name,
                    category=category,
                )
                grouped[key] = item
            jd_id = sys.intern(str(mention["jd_id"]))
            company_id = sys.intern(str(mention["company_id"]))
            role = sys.intern(str(mention["canonical_role"]))
            item.aliases.update(filter(None, (mention["raw_term"], mention["skill_name"])))
            item.jd_ids.add(jd_id)
            item.company_ids.add(company_id)
            item.roles.add(role)
            item.role_jds.setdefault(role, set()).add(jd_id)
            item.role_companies.setdefault(role, set()).add(company_id)
            if mention["evidence_status"] == "VERIFIED":
                item.verified_jds.add(jd_id)
            quote = str(mention["evidence_quote"] or "").strip()
            if quote and quote not in item.contexts and len(item.contexts) < 5:
                item.contexts.append(quote[:256])
        harmonized = self._harmonize_exact_cross_category_groups(list(grouped.values()))
        return sorted(harmonized, key=lambda item: (item.category, item.name))

    def _harmonize_exact_cross_category_groups(
        self,
        groups: list[SkillCandidateGroup],
    ) -> list[SkillCandidateGroup]:
        """同一规范化名称只保留一个类别，避免同一角色的 Top-K 出现完全重复技能。"""
        by_name: dict[str, list[SkillCandidateGroup]] = defaultdict(list)
        for group in groups:
            by_name[normalize_text(group.name)].append(group)

        result: list[SkillCandidateGroup] = []
        for normalized_name, variants in by_name.items():
            if len(variants) == 1:
                result.append(variants[0])
                continue

            definition = next(
                (
                    resolved
                    for group in variants
                    for value in (group.name, *group.aliases)
                    if (resolved := self.registry.resolve(value)) is not None
                ),
                None,
            )
            if definition is not None:
                preferred_category = definition.competency_category
                target = next(
                    (group for group in variants if group.category == preferred_category),
                    max(variants, key=lambda group: (len(group.verified_jds), len(group.jd_ids))),
                )
            else:
                target = max(
                    variants,
                    key=lambda group: (
                        len(group.verified_jds),
                        len(group.jd_ids),
                        len(group.company_ids),
                    ),
                )
                preferred_category = target.category

            target.category = preferred_category
            target.key = f"{preferred_category}|{normalized_name}"
            for source in variants:
                if source is target:
                    continue
                target.aliases.update(source.aliases)
                target.jd_ids.update(source.jd_ids)
                target.company_ids.update(source.company_ids)
                target.roles.update(source.roles)
                target.verified_jds.update(source.verified_jds)
                for role, jd_ids in source.role_jds.items():
                    target.role_jds.setdefault(role, set()).update(jd_ids)
                for role, company_ids in source.role_companies.items():
                    target.role_companies.setdefault(role, set()).update(company_ids)
                for context in source.contexts:
                    if context not in target.contexts and len(target.contexts) < 5:
                        target.contexts.append(context)
                self.cross_category_exact_merges += 1
            result.append(target)
        return result

    def _filter_candidate_groups(
        self,
        groups: list[SkillCandidateGroup],
        role_totals: dict[str, dict[str, set[str]]],
    ) -> tuple[list[SkillCandidateGroup], dict[str, int]]:
        kept: list[SkillCandidateGroup] = []
        registry_retained = 0
        adaptive_retained = 0
        no_verified_evidence_removed = 0
        for group in groups:
            if self.registry.resolve(group.name) is not None or any(
                self.registry.resolve(alias) is not None for alias in group.aliases
            ):
                kept.append(group)
                registry_retained += 1
                continue
            if not group.verified_jds:
                no_verified_evidence_removed += 1
                continue
            eligible = False
            for role in group.roles:
                totals = role_totals.get(role)
                if not totals:
                    continue
                jd_total = len(totals["jds"])
                company_total = len(totals["companies"])
                jd_threshold, company_threshold = self._adaptive_thresholds(
                    jd_total,
                    company_total,
                    final=False,
                )
                if (
                    len(group.role_jds.get(role, ())) >= jd_threshold
                    and len(group.role_companies.get(role, ())) >= company_threshold
                ):
                    eligible = True
                    break
            if eligible:
                kept.append(group)
                adaptive_retained += 1
        return kept, {
            "candidate_groups_before_filter": len(groups),
            "candidate_groups_after_filter": len(kept),
            "candidate_groups_removed": len(groups) - len(kept),
            "candidate_groups_registry_retained": registry_retained,
            "candidate_groups_adaptive_retained": adaptive_retained,
            "candidate_groups_no_verified_evidence_removed": no_verified_evidence_removed,
            "cross_category_exact_merges": self.cross_category_exact_merges,
        }

    def _adaptive_thresholds(
        self,
        jd_total: int,
        company_total: int,
        *,
        final: bool,
    ) -> tuple[int, int]:
        if jd_total < self.config.small_role_cutoff:
            ratio = (
                self.config.small_core_ratio
                if final
                else self.config.small_candidate_ratio
            )
            return (
                max(3 if final else 2, math.ceil(jd_total * ratio)),
                max(2, math.ceil(company_total * ratio)),
            )
        if final:
            return (
                max(self.config.core_min_jds, math.ceil(jd_total * self.config.core_jd_ratio)),
                max(
                    self.config.core_min_companies,
                    math.ceil(company_total * self.config.core_company_ratio),
                ),
            )
        return (
            max(
                self.config.candidate_min_jds,
                math.ceil(jd_total * self.config.candidate_jd_ratio),
            ),
            max(
                self.config.candidate_min_companies,
                math.ceil(company_total * self.config.candidate_company_ratio),
            ),
        )

    def _registry_groups(self) -> list[SkillCandidateGroup]:
        return [
            SkillCandidateGroup(
                key=f"registry|{definition.skill_id}",
                skill_id=definition.skill_id,
                name=definition.canonical_name,
                category=definition.competency_category,
                aliases=set(definition.aliases),
                contexts=["；".join(definition.aliases)],
            )
            for definition in self.registry.skills
        ]

    @staticmethod
    def _context_text(item: SkillCandidateGroup) -> str:
        evidence = "；".join(item.contexts[:3])
        return f"类别：{item.category}；能力：{item.name}；证据：{evidence}"[:512]

    def _combined_score(
        self,
        left_index: int,
        right_index: int,
        groups: list[SkillCandidateGroup],
        phrase_vectors,
        context_vectors,
    ) -> tuple[float, float, float, float]:
        phrase_score = float(phrase_vectors[left_index] @ phrase_vectors[right_index])
        context_score = float(context_vectors[left_index] @ context_vectors[right_index])
        lexical_score = lexical_similarity(groups[left_index].name, groups[right_index].name)
        combined = (
            self.config.phrase_similarity_weight * phrase_score
            + self.config.context_similarity_weight * context_score
            + self.config.lexical_similarity_weight * lexical_score
        )
        return combined, phrase_score, context_score, lexical_score

    def _resolve_registry_phrase(self, group: SkillCandidateGroup):
        exact = self.registry.resolve(group.name)
        if exact is not None:
            return exact
        normalized_name = normalize_text(group.name)
        matches = []
        for definition in self.registry.skills:
            match = self.registry.find_alias(group.name, definition.aliases)
            if match is None:
                continue
            alias = normalize_text(match[0])
            coverage = len(alias) / max(1, len(normalized_name))
            if coverage < 0.45:
                continue
            if group.category not in {definition.competency_category, "知识", "技术"}:
                continue
            matches.append((coverage, len(alias), definition))
        return max(matches, default=(0.0, 0, None), key=lambda item: (item[0], item[1]))[2]

    def _normalize_groups(
        self,
        groups: list[SkillCandidateGroup],
        phrase_vectors,
        context_vectors,
        registry_count: int,
    ) -> tuple[dict[int, int], list[dict], dict]:
        import numpy as np

        mapping = {index: index for index in range(len(groups))}
        reviews: list[dict] = []
        exact_alias_mappings = 0
        vector_registry_merges = 0
        vector_cluster_merges = 0
        lexical_cluster_merges = 0
        ann_cluster_merges = 0

        registry_by_id = {item.skill_id: index for index, item in enumerate(groups[:registry_count])}
        unresolved: list[int] = []
        for index in range(registry_count, len(groups)):
            definition = self._resolve_registry_phrase(groups[index])
            if definition and definition.skill_id in registry_by_id:
                mapping[index] = registry_by_id[definition.skill_id]
                exact_alias_mappings += 1
            else:
                unresolved.append(index)

        if unresolved and registry_count:
            similarity = phrase_vectors[unresolved] @ phrase_vectors[:registry_count].T
            for row_index, group_index in enumerate(unresolved):
                candidate_indices = np.argsort(-similarity[row_index])[: self.config.nearest_neighbor_top_k]
                for registry_index in candidate_indices:
                    if groups[group_index].category != groups[int(registry_index)].category:
                        continue
                    combined, phrase, context, lexical = self._combined_score(
                        group_index, int(registry_index), groups, phrase_vectors, context_vectors
                    )
                    if combined >= self.config.auto_merge_score and phrase >= self.config.cluster_similarity_threshold and merge_allowed(
                        groups[group_index].name,
                        groups[int(registry_index)].name,
                        lexical,
                        self.config.minimum_lexical_guard,
                    ):
                        mapping[group_index] = int(registry_index)
                        vector_registry_merges += 1
                        break

        unassigned = [index for index in unresolved if mapping[index] == index]
        union_find = UnionFind(len(groups))
        for index in unassigned:
            union_find.parent[index] = index
        for category in sorted({groups[index].category for index in unassigned}):
            if category not in self.config.top_k:
                continue
            category_indices = [index for index in unassigned if groups[index].category == category]
            if len(category_indices) < 2:
                continue
            for group_index, other_index in self._lexical_candidate_pairs(category_indices, groups):
                combined, phrase, context, lexical = self._combined_score(
                    group_index, other_index, groups, phrase_vectors, context_vectors
                )
                if combined >= self.config.auto_merge_score and phrase >= self.config.cluster_similarity_threshold:
                    if union_find.union(
                        group_index,
                        other_index,
                        self.config.ann_max_cluster_size,
                    ):
                        vector_cluster_merges += 1
                        lexical_cluster_merges += 1
            for group_index, other_index in self._ann_candidate_pairs(
                category_indices,
                phrase_vectors,
            ):
                combined, phrase, context, lexical = self._combined_score(
                    group_index, other_index, groups, phrase_vectors, context_vectors
                )
                semantic_combined = 0.72 * phrase + 0.28 * context
                has_overlap = self._has_meaningful_lexical_overlap(
                    groups[group_index].name,
                    groups[other_index].name,
                )
                phrase_threshold = (
                    self.config.ann_phrase_similarity
                    if has_overlap
                    else self.config.ann_no_lexical_phrase_similarity
                )
                context_threshold = (
                    self.config.ann_context_similarity
                    if has_overlap
                    else self.config.ann_no_lexical_context_similarity
                )
                if (
                    phrase >= phrase_threshold
                    and context >= context_threshold
                    and semantic_combined >= self.config.ann_combined_similarity
                    and self._semantic_merge_allowed(
                        groups[group_index].name,
                        groups[other_index].name,
                    )
                ):
                    if union_find.union(
                        group_index,
                        other_index,
                        self.config.ann_max_cluster_size,
                    ):
                        vector_cluster_merges += 1
                        ann_cluster_merges += 1

        components: dict[int, list[int]] = defaultdict(list)
        for index in unassigned:
            components[union_find.find(index)].append(index)
        for members in components.values():
            representative = self._choose_representative(members, groups, phrase_vectors)
            for member in members:
                mapping[member] = representative

        metrics = {
            "exact_alias_mappings": exact_alias_mappings,
            "vector_registry_merges": vector_registry_merges,
            "vector_cluster_merges": vector_cluster_merges,
            "lexical_cluster_merges": lexical_cluster_merges,
            "ann_cluster_merges": ann_cluster_merges,
            "review_pairs": len(reviews),
        }
        return mapping, reviews, metrics

    def _ann_candidate_pairs(self, indices: list[int], phrase_vectors):
        if len(indices) < 2:
            return
        try:
            import faiss
            import numpy as np
        except ImportError as error:
            raise RuntimeError(
                "缺少 faiss-cpu；请在项目 .venv 中安装 faiss-cpu。"
            ) from error

        vectors = np.ascontiguousarray(phrase_vectors[indices], dtype="float32")
        dimension = int(vectors.shape[1])
        index = faiss.IndexHNSWFlat(
            dimension,
            self.config.ann_hnsw_m,
            faiss.METRIC_INNER_PRODUCT,
        )
        index.hnsw.efConstruction = self.config.ann_ef_construction
        index.hnsw.efSearch = self.config.ann_ef_search
        index.add(vectors)
        neighbor_count = min(len(indices), self.config.ann_top_k + 1)
        _, neighbors = index.search(vectors, neighbor_count)
        mutual_depth = max(1, min(self.config.ann_mutual_top_k, neighbor_count - 1))
        mutual_neighbors: list[set[int]] = []
        for source_local, row in enumerate(neighbors):
            selected = []
            for target_local in row:
                target_local = int(target_local)
                if target_local >= 0 and target_local != source_local:
                    selected.append(target_local)
                if len(selected) >= mutual_depth:
                    break
            mutual_neighbors.append(set(selected))
        for source_local, targets in enumerate(mutual_neighbors):
            for target_local in targets:
                if source_local < target_local and source_local in mutual_neighbors[target_local]:
                    yield indices[source_local], indices[target_local]

    @staticmethod
    def _semantic_merge_allowed(left: str, right: str) -> bool:
        ascii_pattern = re.compile(r"[a-z][a-z0-9+#.]{1,}", re.IGNORECASE)
        left_tokens = {token.lower() for token in ascii_pattern.findall(left)}
        right_tokens = {token.lower() for token in ascii_pattern.findall(right)}
        if left_tokens and right_tokens and left_tokens.isdisjoint(right_tokens):
            return False
        left_years = set(YEAR_EXPRESSION.findall(left))
        right_years = set(YEAR_EXPRESSION.findall(right))
        return not (left_years and right_years and left_years != right_years)

    @staticmethod
    def _has_meaningful_lexical_overlap(left: str, right: str) -> bool:
        generic = re.compile(
            r"能力|技能|经验|知识|意识|相关|熟练|掌握|使用|工作|"
            r"计划|分析|设计|开发|管理|维护|优化|制定|执行|操作"
        )
        left_normalized = generic.sub("", normalize_text(left))
        right_normalized = generic.sub("", normalize_text(right))
        if not left_normalized or not right_normalized:
            return False
        if left_normalized in right_normalized or right_normalized in left_normalized:
            return True
        left_bigrams = {
            left_normalized[index : index + 2]
            for index in range(max(0, len(left_normalized) - 1))
        }
        right_bigrams = {
            right_normalized[index : index + 2]
            for index in range(max(0, len(right_normalized) - 1))
        }
        return bool(left_bigrams & right_bigrams)

    def _lexical_candidate_pairs(
        self,
        indices: list[int],
        groups: list[SkillCandidateGroup],
    ):
        inverted: dict[str, list[int]] = defaultdict(list)
        keys_by_index: dict[int, set[str]] = {}
        ascii_pattern = re.compile(r"[a-z][a-z0-9+#.]{1,}", re.IGNORECASE)
        for index in indices:
            normalized = normalize_text(groups[index].name)
            keys = {f"g:{normalized[position:position + 2]}" for position in range(max(0, len(normalized) - 1))}
            keys.update(f"a:{token.lower()}" for token in ascii_pattern.findall(groups[index].name))
            keys_by_index[index] = keys
            for key in keys:
                inverted[key].append(index)

        pair_limit = max(20, self.config.nearest_neighbor_top_k * 5)
        for index in indices:
            candidates: set[int] = set()
            for key in keys_by_index[index]:
                bucket = inverted[key]
                if len(bucket) <= 1000:
                    candidates.update(other for other in bucket if other > index)
            ranked = []
            for other in candidates:
                lexical = lexical_similarity(groups[index].name, groups[other].name)
                if merge_allowed(
                    groups[index].name,
                    groups[other].name,
                    lexical,
                    self.config.minimum_lexical_guard,
                ):
                    ranked.append((lexical, other))
            for _, other in sorted(ranked, reverse=True)[:pair_limit]:
                yield index, other

    @staticmethod
    def _choose_representative(indices: list[int], groups: list[SkillCandidateGroup], phrase_vectors) -> int:
        if len(indices) == 1:
            return indices[0]
        centroid = phrase_vectors[indices].mean(axis=0)
        centroid /= max(float((centroid @ centroid) ** 0.5), 1e-8)
        return max(
            indices,
            key=lambda index: (
                len(groups[index].company_ids),
                len(groups[index].jd_ids),
                float(phrase_vectors[index] @ centroid),
                -len(groups[index].name),
            ),
        )

    def _build_concepts(self, groups, mapping, registry_count, phrase_vectors) -> dict[int, dict]:
        members_by_root: dict[int, list[int]] = defaultdict(list)
        for index in range(registry_count, len(groups)):
            members_by_root[mapping[index]].append(index)
        concepts: dict[int, dict] = {}
        for root, members in members_by_root.items():
            representative = groups[root]
            companies = set().union(*(groups[index].company_ids for index in members))
            jds = set().union(*(groups[index].jd_ids for index in members))
            verified = set().union(*(groups[index].verified_jds for index in members))
            is_registry = root < registry_count
            verified_rate = len(verified) / max(1, len(jds))
            if is_registry:
                status = "STANDARD"
                concept_id = representative.skill_id
            elif (
                len(companies) >= self.config.standard_candidate_min_companies
                and len(jds) >= self.config.standard_candidate_min_jds
                and verified_rate >= self.config.standard_candidate_verified_rate
            ):
                status = "STANDARD_CANDIDATE"
                concept_id = stable_id("normalized_skill", representative.category, representative.name)
            elif len(members) > 1:
                status = "AUTO_CLUSTER"
                concept_id = stable_id("normalized_skill", representative.category, representative.name)
            else:
                status = "CANDIDATE"
                concept_id = stable_id("normalized_skill", representative.category, representative.name)
            concepts[root] = {
                "root_index": root,
                "concept_id": concept_id,
                "canonical_name": representative.name,
                "category": representative.category,
                "status": status,
                "aliases": sorted({alias for index in members for alias in groups[index].aliases}),
                "source_phrase_count": len(members),
                "jd_count": len(jds),
                "company_count": len(companies),
                "verified_rate": round(verified_rate, 6),
                "vector": phrase_vectors[root],
            }
        return concepts

    def _rank_roles(self, mentions, mapping, groups, concepts, phrase_vectors, role_totals):
        # _group_candidates 已将完全同名的跨类别候选统一；此处按名称回查，
        # 才不会漏掉原始类别与最终类别不同的那部分提及。
        candidate_index = {normalize_text(item.name): index for index, item in enumerate(groups)}
        aggregates: dict[tuple[str, int], dict] = {}
        roles_by_root: dict[int, set[str]] = defaultdict(set)
        current_jd = ""
        current_role = ""
        jd_votes: dict[int, dict] = {}

        def flush_jd() -> None:
            if not current_jd:
                return
            for root, vote in jd_votes.items():
                role = current_role
                roles_by_root[root].add(role)
                key = (role, root)
                aggregate = aggregates.get(key)
                if aggregate is None:
                    aggregate = {
                        "companies": set(),
                        "jd_count": 0,
                        "verified_jd_count": 0,
                        "requirement_sum": 0.0,
                        "evidence_sum": 0.0,
                        "time_sum": 0.0,
                    }
                    aggregates[key] = aggregate
                aggregate["companies"].add(vote["company_id"])
                aggregate["jd_count"] += 1
                aggregate["verified_jd_count"] += vote["evidence_status"] == "VERIFIED"
                aggregate["requirement_sum"] += (
                    1.0 if vote["requirement_type"] == "required" else 0.4
                )
                aggregate["evidence_sum"] += float(vote["confidence"])
                aggregate["time_sum"] += float(vote["time_weight"]) * float(
                    vote["template_weight"]
                )

        for mention in mentions:
            name = normalize_surface(mention["skill_name"] or mention["raw_term"])
            category = corrected_category(name, mention["competency_category"] or "其他能力")
            if category == "噪声" or not name:
                continue
            local_index = candidate_index.get(normalize_text(name))
            if local_index is None:
                continue
            root = mapping[local_index]
            role = mention["canonical_role"]
            jd_id = mention["jd_id"]
            if current_jd and jd_id != current_jd:
                flush_jd()
                jd_votes = {}
            if jd_id != current_jd:
                current_jd = jd_id
                current_role = role
            previous = jd_votes.get(root)
            if previous is None or float(mention["confidence"]) > float(previous["confidence"]):
                jd_votes[root] = mention
        flush_jd()

        role_count = max(1, len(role_totals))
        rows: list[dict] = []
        for (role, root), aggregate in aggregates.items():
            concept = concepts.get(root)
            if concept is None:
                continue
            total_jds = max(1, len(role_totals[role]["jds"]))
            total_companies = max(1, len(role_totals[role]["companies"]))
            companies = aggregate["companies"]
            jd_count = aggregate["jd_count"]
            verified_jds = aggregate["verified_jd_count"]
            minimum_jds, minimum_companies = self._adaptive_thresholds(
                len(role_totals[role]["jds"]),
                len(role_totals[role]["companies"]),
                final=True,
            )
            minimum_verified_jds = min(self.config.minimum_verified_jd_count, minimum_jds)
            if (
                jd_count < minimum_jds
                or len(companies) < minimum_companies
                or verified_jds < minimum_verified_jds
            ):
                continue
            company_coverage = len(companies) / total_companies
            jd_coverage = jd_count / total_jds
            roles_with_skill = len(roles_by_root[root])
            distinctiveness = 0.0 if role_count <= 1 else math.log(role_count / max(1, roles_with_skill)) / math.log(role_count)
            requirement = aggregate["requirement_sum"] / jd_count
            evidence = aggregate["evidence_sum"] / jd_count
            recency = aggregate["time_sum"] / jd_count
            score = (
                self.config.company_coverage_weight * company_coverage
                + self.config.jd_coverage_weight * jd_coverage
                + self.config.role_distinctiveness_weight * distinctiveness
                + self.config.requirement_weight * requirement
                + self.config.evidence_weight * evidence
                + self.config.time_weight * recency
            )
            rows.append(
                {
                    "role": role,
                    "concept_root": root,
                    "concept_id": concept["concept_id"],
                    "canonical_name": concept["canonical_name"],
                    "category": concept["category"],
                    "concept_status": concept["status"],
                    "company_count": len(companies),
                    "jd_count": jd_count,
                    "verified_jd_count": verified_jds,
                    "minimum_jd_count": minimum_jds,
                    "minimum_company_count": minimum_companies,
                    "minimum_verified_jd_count": minimum_verified_jds,
                    "company_coverage": round(company_coverage, 6),
                    "jd_coverage": round(jd_coverage, 6),
                    "distinctiveness": round(distinctiveness, 6),
                    "requirement_score": round(requirement, 6),
                    "evidence_score": round(evidence, 6),
                    "time_score": round(recency, 6),
                    "final_score": round(score, 6),
                }
            )
        rows.sort(key=lambda row: (row["role"], row["category"], -row["final_score"]))
        top_skills = self._select_top_skills(rows, concepts)
        return rows, top_skills

    def _select_top_skills(self, rows: list[dict], concepts: dict[int, dict]) -> list[dict]:
        grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
        for row in rows:
            grouped[(row["role"], row["category"])].append(row)
        selected: list[dict] = []
        relevance = self.config.mmr_relevance_weight
        for (role, category), candidates in grouped.items():
            quota = self.config.top_k.get(category, 0)
            if quota <= 0:
                continue
            remaining = candidates[: self.config.mmr_candidate_count_per_category]
            chosen: list[dict] = []
            while remaining and len(chosen) < quota:
                eligible = [
                    candidate
                    for candidate in remaining
                    if not any(
                        self._selection_redundant(candidate, existing, concepts)
                        for existing in chosen
                    )
                ]
                if not eligible:
                    break

                def mmr_score(candidate: dict) -> float:
                    if not chosen:
                        redundancy = 0.0
                    else:
                        vector = concepts[candidate["concept_root"]]["vector"]
                        redundancy = max(
                            float(vector @ concepts[item["concept_root"]]["vector"])
                            for item in chosen
                        )
                    return relevance * candidate["final_score"] - (1.0 - relevance) * redundancy

                best = max(eligible, key=mmr_score)
                row = dict(best)
                row["mmr_rank"] = len(chosen) + 1
                row["selection"] = "TOP_K"
                chosen.append(row)
                remaining.remove(best)
            selected.extend(chosen)

        # 类别配额分别选择后，再做一次跨类别硬去重；同义技能只保留得分更高者。
        by_role: dict[str, list[dict]] = defaultdict(list)
        for row in selected:
            by_role[row["role"]].append(row)
        deduplicated: list[dict] = []
        for role, role_rows in by_role.items():
            accepted: list[dict] = []
            for candidate in sorted(role_rows, key=lambda row: -row["final_score"]):
                if any(
                    self._selection_redundant(candidate, existing, concepts)
                    for existing in accepted
                ):
                    continue
                accepted.append(candidate)
            category_ranks: dict[str, int] = defaultdict(int)
            for row in sorted(accepted, key=lambda item: (item["category"], -item["final_score"])):
                category_ranks[row["category"]] += 1
                row["mmr_rank"] = category_ranks[row["category"]]
                deduplicated.append(row)
        return sorted(
            deduplicated,
            key=lambda row: (row["role"], row["category"], row["mmr_rank"]),
        )

    def _selection_redundant(
        self,
        left: dict,
        right: dict,
        concepts: dict[int, dict],
    ) -> bool:
        left_name = left["canonical_name"]
        right_name = right["canonical_name"]
        if normalize_text(left_name) == normalize_text(right_name):
            return True
        left_core = self._selection_lexical_core(left_name)
        right_core = self._selection_lexical_core(right_name)
        if (
            len(left_core) >= 2
            and len(right_core) >= 2
            and (
                left_core == right_core
                or (
                    len(left_core) >= 4
                    and len(left_core) == len(right_core)
                    and sorted(left_core) == sorted(right_core)
                )
            )
        ):
            return True
        similarity = float(
            concepts[left["concept_root"]]["vector"]
            @ concepts[right["concept_root"]]["vector"]
        )
        if similarity < max(0.95, self.config.ann_phrase_similarity - 0.01):
            return False
        return (
            self._has_meaningful_lexical_overlap(left_name, right_name)
            or shared_ascii_token(left_name, right_name)
        )

    @staticmethod
    def _selection_lexical_core(value: str) -> str:
        text = normalize_text(value)
        text = re.sub(
            r"^(?:具备|具有|能够|可以|熟练掌握|熟练使用|熟练应用|熟练操作|"
            r"熟练运用|掌握|使用|应用|操作|运用|制定|进行|开展|实施|执行)",
            "",
            text,
        )
        text = re.sub(r"(?:相关)?(?:能力|技能|经验)$", "", text)
        if "办公" in text:
            text = text.replace("office", "")
        return text

    def _write_outputs(self, mentions, groups, concepts, mapping, reviews, scores, top_skills, metrics) -> dict:
        mapping_rows = []
        registry_count = len(self.registry.skills)
        all_groups = [*self._registry_groups(), *groups]
        for index in range(registry_count, len(all_groups)):
            root = mapping[index]
            concept = concepts[root]
            source = all_groups[index]
            mapping_rows.append(
                {
                    "source_name": source.name,
                    "source_category": source.category,
                    "canonical_name": concept["canonical_name"],
                    "concept_id": concept["concept_id"],
                    "concept_status": concept["status"],
                    "merged": source.name != concept["canonical_name"] or index != root,
                    "jd_count": len(source.jd_ids),
                    "company_count": len(source.company_ids),
                }
            )
        concept_rows = [
            {key: value for key, value in concept.items() if key not in {"root_index", "vector"}}
            | {"aliases": "|".join(concept["aliases"])}
            for concept in concepts.values()
        ]
        self._write_csv("skill_normalization_mapping.csv", mapping_rows)
        self._write_csv("normalization_review_pairs.csv", reviews)
        self._write_csv("normalized_concepts.csv", concept_rows)
        self._write_csv("role_skill_scores.csv", scores)
        self._write_csv("role_top_skills.csv", top_skills)

        report = {
            "database": str(self.database_path),
            "config": asdict(self.config),
            "input_mentions": len(mentions),
            "unique_source_phrases": len(groups),
            "normalized_concepts": len(concepts),
            "reduction_count": len(groups) - len(concepts),
            "reduction_rate": round((len(groups) - len(concepts)) / max(1, len(groups)), 6),
            "standard_concepts": sum(concept["status"] == "STANDARD" for concept in concepts.values()),
            "standard_candidates": sum(concept["status"] == "STANDARD_CANDIDATE" for concept in concepts.values()),
            "auto_clusters": sum(concept["status"] == "AUTO_CLUSTER" for concept in concepts.values()),
            "single_candidates": sum(concept["status"] == "CANDIDATE" for concept in concepts.values()),
            "role_score_rows": len(scores),
            "top_skill_rows": len(top_skills),
            **metrics,
            "top_skills": top_skills,
        }
        (self.output_dir / "normalization_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        self._write_markdown(report)
        return report

    def _write_markdown(self, report: dict) -> None:
        lines = [
            "# 技能标准化试验报告",
            "",
            f"- 输入能力证据：**{report['input_mentions']}**",
            f"- 自适应筛选前短语：**{report['candidate_groups_before_filter']}**",
            f"- 自适应筛选后短语：**{report['candidate_groups_after_filter']}**",
            f"- 标准化前唯一短语：**{report['unique_source_phrases']}**",
            f"- 标准化后能力概念：**{report['normalized_concepts']}**",
            f"- 节点减少：**{report['reduction_count']}（{report['reduction_rate']:.1%}）**",
            f"- 精确别名归一：**{report['exact_alias_mappings']}**",
            f"- 向量合并到基础词库：**{report['vector_registry_merges']}**",
            f"- 向量自动聚类合并：**{report['vector_cluster_merges']}**",
            f"- 待人工审核相似对：**{report['review_pairs']}**",
            "",
            "## 岗位Top-K",
            "",
        ]
        by_role: dict[str, list[dict]] = defaultdict(list)
        for row in report["top_skills"]:
            by_role[row["role"]].append(row)
        for role, rows in sorted(by_role.items()):
            lines.extend([f"### {role}", ""])
            for row in rows:
                lines.append(
                    f"- {row['category']}：{row['canonical_name']}（得分 {row['final_score']:.3f}，"
                    f"公司 {row['company_count']}，JD {row['jd_count']}）"
                )
            lines.append("")
        (self.output_dir / "normalization_report.md").write_text("\n".join(lines), encoding="utf-8")

    def _write_csv(self, name: str, rows: list[dict]) -> None:
        path = self.output_dir / name
        if not rows:
            path.write_text("", encoding="utf-8-sig")
            return
        with path.open("w", encoding="utf-8-sig", newline="") as file:
            writer = csv.DictWriter(file, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)

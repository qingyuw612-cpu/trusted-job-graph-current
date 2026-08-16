from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol
from urllib.request import Request, urlopen

from .models import JobDocument, ReviewTask, SkillCandidate, SkillEvidence
from .registry import SkillDefinition, SkillRegistry
from .text_utils import normalize_text, safe_float, stable_id


PREFERRED_WORDS = re.compile(r"优先|加分|更佳|preferred|plus", re.IGNORECASE)
SENTENCE_BOUNDARY = re.compile(r"[\n。；;！？!?]")
ABILITY_SECTION = re.compile(
    r"(?m)^\s*(?:\d+\s*[.、．]\s*)?(知识|技术|动机|特质|自我概念)\s*[：:]\s*"
)
ABILITY_ITEM_BOUNDARY = re.compile(r"[；;\n]+")
ABILITY_LIST_BOUNDARY = re.compile(r"[、，/]+")
ABILITY_EMPTY_VALUES = {"", "无", "无相关内容", "未提及", "未明确", "网络错误", "none", "null"}
ABILITY_CATEGORIES = ("知识", "技术", "动机", "特质", "自我概念")
ABILITY_CATEGORY_ALIASES = {
    "知识": "知识",
    "knowledge": "知识",
    "技术": "技术",
    "技能": "技术",
    "skill": "技术",
    "skills": "技术",
    "technology": "技术",
    "technical": "技术",
    "动机": "动机",
    "motivation": "动机",
    "特质": "特质",
    "trait": "特质",
    "traits": "特质",
    "自我概念": "自我概念",
    "selfconcept": "自我概念",
}
ABILITY_PREFIX = re.compile(
    r"^(?:(?:具备|具有|能够|可以|熟悉|熟练掌握|掌握|精通|了解|拥有|有)|"
    r"(?:良好的|较好的|较强的|优秀的|扎实的|丰富的|一定的|基础的))+"
)


class SkillExtractor(Protocol):
    def extract(self, document: JobDocument) -> list[SkillCandidate]: ...


def _quote_around(text: str, position: int, limit: int = 240) -> str:
    start = max(text.rfind("\n", 0, position), text.rfind("。", 0, position), text.rfind("；", 0, position))
    start = 0 if start < 0 else start + 1
    ends = [index for index in (text.find("\n", position), text.find("。", position), text.find("；", position)) if index >= 0]
    end = min(ends) + 1 if ends else min(len(text), position + limit)
    quote = re.sub(r"\s+", " ", text[start:end]).strip()
    if len(quote) > limit:
        relative = max(0, position - start)
        left = max(0, relative - limit // 2)
        quote = quote[left : left + limit]
    return quote


def _requirement_type(text: str, position: int) -> str:
    window = text[max(0, position - 45) : position + 80]
    return "preferred" if PREFERRED_WORDS.search(window) else "required"


class RuleBasedExtractor:
    """离线可运行的候选生成器；最终是否入图由回标验证器决定。"""

    def __init__(self, registry: SkillRegistry):
        self.registry = registry

    def extract(self, document: JobDocument) -> list[SkillCandidate]:
        candidates: list[SkillCandidate] = []
        primary = document.evidence_text
        legacy = document.ability_analysis
        for skill in self.registry.skills:
            primary_hit = self.registry.find_alias(primary, skill.aliases)
            if primary_hit:
                raw_term, position = primary_hit
                candidates.append(
                    SkillCandidate(
                        skill_name=skill.canonical_name,
                        raw_term=raw_term,
                        requirement_type=_requirement_type(primary, position),
                        evidence_quote=_quote_around(primary, position),
                        confidence=0.96,
                        source="RULE_JD",
                    )
                )
                continue
            legacy_hit = self.registry.find_alias(legacy, skill.aliases)
            if legacy_hit:
                raw_term, position = legacy_hit
                candidates.append(
                    SkillCandidate(
                        skill_name=skill.canonical_name,
                        raw_term=raw_term,
                        requirement_type=_requirement_type(legacy, position),
                        evidence_quote=_quote_around(legacy, position),
                        confidence=0.58,
                        source="LEGACY_ANALYSIS",
                    )
                )
        return candidates


class AbilityAnalysisExtractor:
    """直接解析 CSV 最后一列已有的大模型能力分析结果。"""

    def __init__(self, registry: SkillRegistry):
        self.registry = registry

    def extract(self, document: JobDocument) -> list[SkillCandidate]:
        text = document.ability_analysis.strip()
        if not text or text.lower() in ABILITY_EMPTY_VALUES:
            return []
        # 新批次能力列是 JSON 对象；转换后继续复用原有清洗、归一和证据验证。
        if text.startswith("{"):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict):
                grouped: dict[str, list[str]] = {category: [] for category in ABILITY_CATEGORIES}
                for raw_category, raw_value in payload.items():
                    category_key = re.sub(r"[\s_\-]+", "", str(raw_category)).casefold()
                    category = ABILITY_CATEGORY_ALIASES.get(category_key)
                    if not category:
                        continue
                    if isinstance(raw_value, list):
                        values = [str(item).strip() for item in raw_value if str(item).strip()]
                    elif isinstance(raw_value, dict):
                        values = [str(item).strip() for item in raw_value.values() if str(item).strip()]
                    else:
                        value = str(raw_value or "").strip()
                        values = [value] if value else []
                    for value in values:
                        if value not in grouped[category]:
                            grouped[category].append(value)
                section_text: list[str] = []
                for category in ABILITY_CATEGORIES:
                    value = "、".join(grouped[category]).strip()
                    if value:
                        section_text.append(f"{category}：{value}")
                text = "\n".join(section_text)
        sections = list(ABILITY_SECTION.finditer(text))
        if not sections:
            return []

        candidates: list[SkillCandidate] = []
        seen: set[tuple[str, str]] = set()
        for index, section in enumerate(sections):
            category = section.group(1)
            end = sections[index + 1].start() if index + 1 < len(sections) else len(text)
            content = text[section.end() : end]
            for raw_item in ABILITY_ITEM_BOUNDARY.split(content):
                raw_item = raw_item.strip(" \t\r\n。；;，,")
                if not raw_item:
                    continue
                for raw_term in ABILITY_LIST_BOUNDARY.split(raw_item):
                    term = self._clean_term(raw_term)
                    if term.lower() in ABILITY_EMPTY_VALUES or len(normalize_text(term)) < 2:
                        continue
                    definition = self._resolve_phrase(term)
                    skill_name = definition.canonical_name if definition else term
                    key = (normalize_text(skill_name), category)
                    if key in seen:
                        continue
                    seen.add(key)
                    candidates.append(
                        SkillCandidate(
                            skill_name=skill_name,
                            raw_term=term,
                            requirement_type=_requirement_type(raw_item, 0),
                            evidence_quote=raw_item,
                            confidence=0.88,
                            source="ABILITY_ANALYSIS",
                            competency_category=category,
                            tech_stack=definition.tech_stack if definition else "",
                        )
                    )
        return candidates

    @staticmethod
    def _clean_term(value: str) -> str:
        term = re.sub(r"^\s*(?:[-—•·]|\d+\s*[.、．])\s*", "", value).strip()
        previous = ""
        while term != previous:
            previous = term
            term = ABILITY_PREFIX.sub("", term).strip()
        return term.strip(" \t\r\n。；;，,：:")

    def _resolve_phrase(self, term: str) -> SkillDefinition | None:
        exact = self.registry.resolve(term)
        if exact:
            return exact
        normalized_term = normalize_text(term)
        best: tuple[float, int, SkillDefinition] | None = None
        for definition in self.registry.skills:
            hit = self.registry.find_alias(term, definition.aliases)
            if not hit:
                continue
            normalized_alias = normalize_text(hit[0])
            coverage = len(normalized_alias) / max(1, len(normalized_term))
            score = (coverage, len(normalized_alias), definition)
            if coverage >= 0.5 and (best is None or score[:2] > best[:2]):
                best = score
        return best[2] if best else None


class WebhookLLMExtractor:
    """可选 LLM 接口适配器。

    接口接收岗位原文与 JSON Schema，返回 ``{"skills": [...]}``。无接口时流水线
    自动使用规则抽取器，不伪造任何 LLM 调用。
    """

    def __init__(self, endpoint: str, timeout_seconds: int = 45):
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds

    def extract(self, document: JobDocument) -> list[SkillCandidate]:
        schema = {
            "skills": [
                {
                    "skill": "标准技能名或原文技能名",
                    "raw_term": "原文词",
                    "requirement_type": "required|preferred",
                    "evidence_quote": "JD 原文句子",
                    "confidence": "0~1",
                }
            ]
        }
        payload = json.dumps(
            {
                "task": "extract_job_skills_with_evidence",
                "title": document.title,
                "description": document.description,
                "tags": document.tags,
                "output_schema": schema,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        request = Request(self.endpoint, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        with urlopen(request, timeout=self.timeout_seconds) as response:
            result = json.loads(response.read().decode("utf-8"))
        raw_items = result.get("skills", []) if isinstance(result, dict) else []
        candidates: list[SkillCandidate] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            requirement = str(item.get("requirement_type", "required")).lower()
            candidates.append(
                SkillCandidate(
                    skill_name=str(item.get("skill", "")).strip(),
                    raw_term=str(item.get("raw_term", "")).strip(),
                    requirement_type="preferred" if requirement == "preferred" else "required",
                    evidence_quote=str(item.get("evidence_quote", "")).strip(),
                    confidence=max(0.0, min(1.0, safe_float(item.get("confidence"), 0.5))),
                    source="LLM_WEBHOOK",
                )
            )
        return candidates


@dataclass(slots=True)
class VerificationResult:
    evidences: list[SkillEvidence]
    reviews: list[ReviewTask]


class EvidenceVerifier:
    def __init__(self, registry: SkillRegistry):
        self.registry = registry

    def verify(self, document: JobDocument, candidates: list[SkillCandidate]) -> VerificationResult:
        checked: list[SkillEvidence] = []
        reviews: list[ReviewTask] = []
        for candidate in candidates:
            definition = self.registry.resolve(candidate.skill_name) or self.registry.resolve(candidate.raw_term)
            if definition is None:
                if candidate.source == "ABILITY_ANALYSIS":
                    evidence = self._verify_analysis_term(document, candidate)
                    checked.append(evidence)
                    if evidence.evidence_status != "VERIFIED":
                        reviews.append(self._review(document, evidence, "已有能力分析包含该技能，但JD原文未精确回标"))
                    continue
                skill_id = stable_id("skill_unknown", candidate.skill_name or candidate.raw_term)
                evidence = SkillEvidence(
                    jd_id=document.jd_id,
                    skill_id=skill_id,
                    skill_name=candidate.skill_name or candidate.raw_term or "未知技能",
                    raw_term=candidate.raw_term,
                    requirement_type=candidate.requirement_type,
                    evidence_quote=candidate.evidence_quote,
                    evidence_status="REJECTED_UNKNOWN_SKILL",
                    confidence=min(candidate.confidence, 0.30),
                    source=candidate.source,
                    competency_category="待审核",
                    tech_stack="待审核",
                )
                checked.append(evidence)
                reviews.append(self._review(document, evidence, "技能不在归一词典中"))
                continue
            evidence = self._verify_known(document, candidate, definition)
            checked.append(evidence)
            if evidence.evidence_status != "VERIFIED":
                if evidence.evidence_status == "LOW_CONFIDENCE":
                    reason = "原文仅能归一化模糊回标"
                elif evidence.evidence_status == "ANALYSIS_ONLY":
                    reason = "已有能力分析包含该技能，但JD原文未精确回标"
                else:
                    reason = "JD 原文未找到对应证据，疑似幻觉"
                reviews.append(self._review(document, evidence, reason))

        best: dict[str, SkillEvidence] = {}
        rejected: dict[tuple[str, str, str], SkillEvidence] = {}
        rank = {
            "VERIFIED": 4,
            "LOW_CONFIDENCE": 3,
            "ANALYSIS_ONLY": 2,
            "REJECTED_HALLUCINATION": 1,
            "REJECTED_UNKNOWN_SKILL": 0,
        }
        for evidence in checked:
            if evidence.evidence_status.startswith("REJECTED"):
                key = (evidence.skill_id, evidence.evidence_status, evidence.source)
                previous_rejected = rejected.get(key)
                if previous_rejected is None or evidence.confidence > previous_rejected.confidence:
                    rejected[key] = evidence
                continue
            previous = best.get(evidence.skill_id)
            current_score = (rank[evidence.evidence_status], evidence.confidence, evidence.requirement_type == "required")
            previous_score = (
                rank[previous.evidence_status],
                previous.confidence,
                previous.requirement_type == "required",
            ) if previous else (-1, -1.0, False)
            if current_score > previous_score:
                best[evidence.skill_id] = evidence
        review_by_id = {review.task_id: review for review in reviews}
        return VerificationResult(evidences=[*best.values(), *rejected.values()], reviews=list(review_by_id.values()))

    def _verify_known(
        self,
        document: JobDocument,
        candidate: SkillCandidate,
        definition: SkillDefinition,
    ) -> SkillEvidence:
        primary = document.evidence_text
        hit = self.registry.find_alias(primary, definition.aliases)
        if hit:
            raw_term, position = hit
            quote = _quote_around(primary, position)
            status = "VERIFIED"
            confidence = max(candidate.confidence, 0.90) if candidate.source != "LEGACY_ANALYSIS" else 0.82
            requirement = _requirement_type(primary, position)
        else:
            normalized_primary = normalize_text(primary)
            normalized_candidates = [normalize_text(candidate.raw_term), *(normalize_text(alias) for alias in definition.aliases)]
            fuzzy = next((term for term in normalized_candidates if len(term) >= 2 and term in normalized_primary), "")
            if fuzzy:
                status = "LOW_CONFIDENCE"
                confidence = min(candidate.confidence, 0.68)
                quote = candidate.evidence_quote
                requirement = candidate.requirement_type
                raw_term = candidate.raw_term or definition.canonical_name
            elif candidate.source == "ABILITY_ANALYSIS":
                status = "ANALYSIS_ONLY"
                confidence = min(candidate.confidence, 0.60)
                quote = candidate.evidence_quote
                requirement = candidate.requirement_type
                raw_term = candidate.raw_term or definition.canonical_name
            else:
                status = "REJECTED_HALLUCINATION"
                confidence = min(candidate.confidence, 0.25)
                quote = candidate.evidence_quote
                requirement = candidate.requirement_type
                raw_term = candidate.raw_term or definition.canonical_name
        return SkillEvidence(
            jd_id=document.jd_id,
            skill_id=definition.skill_id,
            skill_name=definition.canonical_name,
            raw_term=raw_term,
            requirement_type=requirement,
            evidence_quote=quote,
            evidence_status=status,
            confidence=round(confidence, 4),
            source=candidate.source,
            competency_category=candidate.competency_category or definition.competency_category,
            tech_stack=candidate.tech_stack or definition.tech_stack,
        )

    def _verify_analysis_term(self, document: JobDocument, candidate: SkillCandidate) -> SkillEvidence:
        hit = self._normalized_hit(document.evidence_text, candidate.raw_term or candidate.skill_name)
        if hit is not None:
            status = "VERIFIED"
            confidence = max(candidate.confidence, 0.90)
            quote = _quote_around(document.evidence_text, hit)
            requirement = _requirement_type(document.evidence_text, hit)
        else:
            status = "ANALYSIS_ONLY"
            confidence = min(candidate.confidence, 0.60)
            quote = candidate.evidence_quote
            requirement = candidate.requirement_type
        name = candidate.skill_name or candidate.raw_term or "未命名能力"
        return SkillEvidence(
            jd_id=document.jd_id,
            skill_id=stable_id("skill", name),
            skill_name=name,
            raw_term=candidate.raw_term or name,
            requirement_type=requirement,
            evidence_quote=quote,
            evidence_status=status,
            confidence=round(confidence, 4),
            source=candidate.source,
            competency_category=candidate.competency_category or "其他能力",
            tech_stack=candidate.tech_stack,
        )

    @staticmethod
    def _normalized_hit(text: str, term: str) -> int | None:
        needle = normalize_text(term)
        if len(needle) < 2:
            return None
        normalized: list[str] = []
        positions: list[int] = []
        for position, character in enumerate(text.lower()):
            if re.fullmatch(r"[0-9a-z\u4e00-\u9fff+#.]", character):
                normalized.append(character)
                positions.append(position)
        match_at = "".join(normalized).find(needle)
        return positions[match_at] if match_at >= 0 else None

    @staticmethod
    def _review(document: JobDocument, evidence: SkillEvidence, reason: str) -> ReviewTask:
        return ReviewTask(
            task_id=stable_id("review", document.jd_id, evidence.skill_id, evidence.evidence_status),
            jd_id=document.jd_id,
            skill_id=evidence.skill_id,
            skill_name=evidence.skill_name,
            reason=reason,
            evidence_status=evidence.evidence_status,
            confidence=evidence.confidence,
            evidence_quote=evidence.evidence_quote,
        )

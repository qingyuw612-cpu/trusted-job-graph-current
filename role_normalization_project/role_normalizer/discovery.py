"""未匹配岗位聚类与新岗位候选发现。

本模块只处理已经被岗位解析器标记为 ``REVIEW`` 或 ``UNMAPPED`` 的记录。
它不会直接修改岗位注册表，而是生成带完整门禁证据的候选，供人工审核和后续发布。
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from itertools import combinations
from typing import Any, Callable, Iterable, Mapping, Sequence

from .models import JobTitleRecord, RoleResolution, ResolutionType


PairSimilarity = Callable[[JobTitleRecord, JobTitleRecord], float]
BlockingKey = Callable[[JobTitleRecord], Iterable[str]]
CandidatePairProvider = Callable[[Sequence[JobTitleRecord]], Iterable[tuple[int, int]]]


def _enum_value(value: Any) -> Any:
    """把枚举转换为适合 JSON 序列化的值。"""

    return value.value if isinstance(value, Enum) else value


def _first_value(obj: Any, names: Sequence[str], default: Any = None) -> Any:
    """兼容不同数据源字段名，返回第一个非空属性或字典值。"""

    for name in names:
        if isinstance(obj, Mapping):
            value = obj.get(name)
        else:
            value = getattr(obj, name, None)
        if value not in (None, "", [], (), {}):
            return value
    return default


def _as_string_set(value: Any) -> set[str]:
    """把字符串或字符串序列安全转换成去空值集合。"""

    if value in (None, ""):
        return set()
    if isinstance(value, str):
        # 技能字段有时由常见分隔符拼接，统一在此拆开。
        for separator in ("、", "，", ",", ";", "；", "|"):
            value = value.replace(separator, "\n")
        return {part.strip() for part in value.splitlines() if part.strip()}
    if isinstance(value, Mapping):
        value = value.keys()
    try:
        return {str(item).strip() for item in value if str(item).strip()}
    except TypeError:
        text = str(value).strip()
        return {text} if text else set()


@dataclass(frozen=True)
class NewRoleCandidate:
    """一个待审核的新岗位候选及其可复核统计证据。"""

    candidate_id: str
    representative_name: str
    aliases: tuple[str, ...]
    record_ids: tuple[str, ...]
    jd_count: int
    company_count: int
    template_count: int
    skill_count: int
    skills: tuple[str, ...]
    resolution_type: ResolutionType
    passed_gate: bool
    gate_reasons: tuple[str, ...]
    cluster_similarity: float
    nearest_existing_roles: tuple[dict[str, Any], ...] = field(default_factory=tuple)
    evidence: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """转换为稳定、可直接写入 JSON 的字典。"""

        return {
            "candidate_id": self.candidate_id,
            "representative_name": self.representative_name,
            "aliases": list(self.aliases),
            "record_ids": list(self.record_ids),
            "jd_count": self.jd_count,
            "company_count": self.company_count,
            "template_count": self.template_count,
            "skill_count": self.skill_count,
            "skills": list(self.skills),
            "resolution_type": _enum_value(self.resolution_type),
            "passed_gate": self.passed_gate,
            "gate_reasons": list(self.gate_reasons),
            "cluster_similarity": self.cluster_similarity,
            "nearest_existing_roles": [dict(item) for item in self.nearest_existing_roles],
            "evidence": [dict(item) for item in self.evidence],
        }


class _UnionFind:
    """带簇大小上限的确定性并查集。"""

    def __init__(self, size: int, max_cluster_size: int) -> None:
        """初始化父节点、簇大小和允许的最大簇规模。"""

        self.parent = list(range(size))
        self.size = [1] * size
        self.max_cluster_size = max_cluster_size

    def find(self, item: int) -> int:
        """查找根节点，并进行路径压缩。"""

        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: int, right: int) -> bool:
        """在不超过簇大小上限时合并两个簇。"""

        root_left, root_right = self.find(left), self.find(right)
        if root_left == root_right:
            return True
        if self.size[root_left] + self.size[root_right] > self.max_cluster_size:
            return False

        # 大簇优先作为根；大小相等时用较小索引保证结果确定。
        if (self.size[root_left], -root_left) < (self.size[root_right], -root_right):
            root_left, root_right = root_right, root_left
        self.parent[root_right] = root_left
        self.size[root_left] += self.size[root_right]
        return True


class NewRoleDiscovery:
    """对未匹配岗位做聚类，并依据证据门槛生成新岗位候选。"""

    _ALLOWED_TYPES = {ResolutionType.REVIEW, ResolutionType.UNMAPPED}

    def __init__(
        self,
        pair_similarity: PairSimilarity,
        *,
        min_jds: int = 3,
        min_companies: int = 3,
        min_templates: int = 3,
        min_skills: int = 3,
        cluster_similarity: float = 0.82,
        max_cluster_size: int = 200,
        max_evidence: int = 10,
        max_nearest_roles: int = 5,
        blocking_key: BlockingKey | None = None,
        candidate_pair_provider: CandidatePairProvider | None = None,
        max_pair_comparisons: int = 5_000_000,
    ) -> None:
        """初始化相似度函数、候选门槛与审计输出上限。"""

        if not callable(pair_similarity):
            raise TypeError("pair_similarity 必须是可调用对象")
        for name, value in {
            "min_jds": min_jds,
            "min_companies": min_companies,
            "min_templates": min_templates,
            "min_skills": min_skills,
            "max_cluster_size": max_cluster_size,
            "max_evidence": max_evidence,
            "max_nearest_roles": max_nearest_roles,
            "max_pair_comparisons": max_pair_comparisons,
        }.items():
            if value < 1:
                raise ValueError(f"{name} 必须大于等于 1")
        if not 0.0 <= cluster_similarity <= 1.0:
            raise ValueError("cluster_similarity 必须位于 0 到 1 之间")

        self.pair_similarity = pair_similarity
        self.min_jds = min_jds
        self.min_companies = min_companies
        self.min_templates = min_templates
        self.min_skills = min_skills
        self.cluster_similarity = cluster_similarity
        self.max_cluster_size = max_cluster_size
        self.max_evidence = max_evidence
        self.max_nearest_roles = max_nearest_roles
        self.blocking_key = blocking_key
        self.candidate_pair_provider = candidate_pair_provider
        self.max_pair_comparisons = max_pair_comparisons

    def discover(self, resolutions: Iterable[RoleResolution]) -> list[NewRoleCandidate]:
        """聚类 REVIEW/UNMAPPED 解析结果，并返回稳定排序的候选列表。"""

        items = list(resolutions)
        self._validate_inputs(items)
        if not items:
            return []

        ordered = sorted(items, key=self._resolution_sort_key)
        clusters = self._cluster(ordered)
        candidates = [self._build_candidate(cluster) for cluster in clusters]
        return sorted(candidates, key=lambda item: (item.representative_name.casefold(), item.candidate_id))

    def run(self, resolutions: Iterable[RoleResolution]) -> list[NewRoleCandidate]:
        """提供与批处理管线一致的 discover 别名入口。"""

        return self.discover(resolutions)

    def _validate_inputs(self, resolutions: Sequence[RoleResolution]) -> None:
        """拒绝已自动映射或已成为候选的记录，避免重复发现。"""

        for resolution in resolutions:
            resolution_type = _first_value(
                resolution, ("resolution_type", "type", "status")
            )
            if resolution_type not in self._ALLOWED_TYPES:
                raise ValueError(
                    "NewRoleDiscovery 只接受 REVIEW/UNMAPPED 记录，"
                    f"实际收到：{_enum_value(resolution_type)!r}"
                )
            self._record_of(resolution)

    def _record_of(self, resolution: RoleResolution) -> JobTitleRecord:
        """从解析结果中提取原始岗位记录。"""

        record = _first_value(resolution, ("record", "job", "input_record", "source_record"))
        if record is None:
            raise ValueError("RoleResolution 缺少 record/input_record 字段")
        return record

    def _resolution_sort_key(self, resolution: RoleResolution) -> tuple[str, str, str]:
        """构造与输入顺序无关的稳定排序键。"""

        record = self._record_of(resolution)
        return (
            self._record_id(record),
            self._title_of(record).casefold(),
            str(_enum_value(_first_value(resolution, ("resolution_type", "type", "status")))),
        )

    def _cluster(self, resolutions: Sequence[RoleResolution]) -> list[list[RoleResolution]]:
        """按两两相似度降序做并查集合并，并阻止超过上限的超大簇。"""

        union_find = _UnionFind(len(resolutions), self.max_cluster_size)
        edges: list[tuple[float, int, int]] = []
        pairs = self._candidate_pairs(resolutions)
        for left, right in pairs:
            score = float(
                self.pair_similarity(
                    self._record_of(resolutions[left]), self._record_of(resolutions[right])
                )
            )
            score = max(0.0, min(1.0, score))
            if score >= self.cluster_similarity:
                edges.append((score, left, right))

        # 先连接最相似的记录；索引用于相同分数时的确定性决策。
        for _score, left, right in sorted(edges, key=lambda item: (-item[0], item[1], item[2])):
            union_find.union(left, right)

        grouped: dict[int, list[RoleResolution]] = {}
        for index, resolution in enumerate(resolutions):
            grouped.setdefault(union_find.find(index), []).append(resolution)
        return [grouped[root] for root in sorted(grouped)]

    def _candidate_pairs(self, resolutions: Sequence[RoleResolution]) -> list[tuple[int, int]]:
        """按阻塞键生成候选对，避免对大批数据进行无边界全量两两比较。"""

        if self.candidate_pair_provider is not None:
            records = [self._record_of(item) for item in resolutions]
            pairs = {
                (min(int(left), int(right)), max(int(left), int(right)))
                for left, right in self.candidate_pair_provider(records)
                if int(left) != int(right)
            }
            if any(left < 0 or right >= len(resolutions) for left, right in pairs):
                raise ValueError("candidate_pair_provider 返回了越界索引")
            if len(pairs) > self.max_pair_comparisons:
                raise ValueError(
                    f"Top-K 候选对数量 {len(pairs)} 超过上限 {self.max_pair_comparisons}"
                )
            return sorted(pairs)

        if self.blocking_key is None:
            pair_count = len(resolutions) * (len(resolutions) - 1) // 2
            if pair_count > self.max_pair_comparisons:
                raise ValueError(
                    f"候选对数量 {pair_count} 超过上限 {self.max_pair_comparisons}；"
                    "请提供 blocking_key 或 ANN Top-K 候选"
                )
            return list(combinations(range(len(resolutions)), 2))

        blocks: dict[str, list[int]] = {}
        for index, resolution in enumerate(resolutions):
            keys = {
                str(value).strip().casefold()
                for value in self.blocking_key(self._record_of(resolution))
                if str(value).strip()
            }
            if not keys:
                keys = {f"singleton:{index}"}
            for key in keys:
                blocks.setdefault(key, []).append(index)

        pairs: set[tuple[int, int]] = set()
        for key in sorted(blocks):
            indices = sorted(set(blocks[key]))
            for left, right in combinations(indices, 2):
                pairs.add((left, right))
                if len(pairs) > self.max_pair_comparisons:
                    raise ValueError(
                        f"阻塞后候选对仍超过上限 {self.max_pair_comparisons}；"
                        "请细化 blocking_key 或改用 ANN Top-K"
                    )
        return sorted(pairs)

    def _build_candidate(self, cluster: Sequence[RoleResolution]) -> NewRoleCandidate:
        """汇总一个簇的统计量、门禁结果和人工审核证据。"""

        records = [self._record_of(item) for item in cluster]
        titles = [self._title_of(record) for record in records]
        representative = self._representative_title(titles)
        aliases = tuple(sorted(set(titles) - {representative}, key=lambda value: value.casefold()))
        record_ids = tuple(sorted({self._record_id(record) for record in records}))
        companies = {
            str(value).strip()
            for record in records
            if (
                value := self._record_value(
                    record, ("company_id", "company_name", "company")
                )
            )
        }
        templates = {
            str(value).strip()
            for record in records
            if (
                value := self._record_value(
                    record, ("template_id", "template_hash", "jd_template_id")
                )
            )
        }
        skills: set[str] = set()
        for record in records:
            skills.update(
                _as_string_set(
                    _first_value(
                        record,
                        ("skills", "normalized_skills", "skill_names", "candidate_skills"),
                    )
                )
            )

        reasons = self._gate_reasons(
            jd_count=len(record_ids),
            company_count=len(companies),
            template_count=len(templates),
            skill_count=len(skills),
        )
        passed = not reasons
        resolution_type = (
            ResolutionType.NEW_ROLE_CANDIDATE
            if passed
            else self._fallback_resolution_type(cluster)
        )
        similarity = self._cluster_cohesion(records)
        candidate_id = self._candidate_id(representative, record_ids)

        return NewRoleCandidate(
            candidate_id=candidate_id,
            representative_name=representative,
            aliases=aliases,
            record_ids=record_ids,
            jd_count=len(record_ids),
            company_count=len(companies),
            template_count=len(templates),
            skill_count=len(skills),
            skills=tuple(sorted(skills, key=lambda value: value.casefold())),
            resolution_type=resolution_type,
            passed_gate=passed,
            gate_reasons=tuple(reasons),
            cluster_similarity=round(similarity, 6),
            nearest_existing_roles=self._nearest_existing_roles(cluster),
            evidence=self._evidence(records),
        )

    def _gate_reasons(
        self, *, jd_count: int, company_count: int, template_count: int, skill_count: int
    ) -> list[str]:
        """返回所有未通过门禁的原因；空列表表示全部通过。"""

        checks = (
            ("独立JD", jd_count, self.min_jds),
            ("独立企业", company_count, self.min_companies),
            ("独立模板", template_count, self.min_templates),
            ("稳定技能", skill_count, self.min_skills),
        )
        return [f"{name}不足：{actual} < {minimum}" for name, actual, minimum in checks if actual < minimum]

    def _fallback_resolution_type(self, cluster: Sequence[RoleResolution]) -> ResolutionType:
        """未过门禁时保留更谨慎的 REVIEW 状态，否则保持 UNMAPPED。"""

        types = {
            _first_value(item, ("resolution_type", "type", "status")) for item in cluster
        }
        return ResolutionType.REVIEW if ResolutionType.REVIEW in types else ResolutionType.UNMAPPED

    def _cluster_cohesion(self, records: Sequence[JobTitleRecord]) -> float:
        """计算簇内全部记录对的平均相似度，单记录簇记为 1。"""

        if len(records) < 2:
            return 1.0
        scores = [
            max(0.0, min(1.0, float(self.pair_similarity(left, right))))
            for left, right in combinations(records, 2)
        ]
        return sum(scores) / len(scores)

    def _representative_title(self, titles: Sequence[str]) -> str:
        """按出现频率优先、信息量次之选择岗位代表名称。"""

        counts = Counter(titles)

        def information_score(title: str) -> tuple[int, int, int, str]:
            # 非纯符号字符、不同字符数和长度共同表示名称的信息量。
            meaningful = sum(character.isalnum() or "\u4e00" <= character <= "\u9fff" for character in title)
            return counts[title], meaningful, len(set(title.casefold())), title.casefold()

        return max(counts, key=information_score)

    def _nearest_existing_roles(
        self, cluster: Sequence[RoleResolution]
    ) -> tuple[dict[str, Any], ...]:
        """合并解析阶段保留的最近现有岗位，供审核人员对照。"""

        best: dict[str, dict[str, Any]] = {}
        for resolution in cluster:
            raw_candidates = _first_value(
                resolution,
                (
                    "nearest_existing_roles",
                    "candidates",
                    "top_candidates",
                    "nearest_roles",
                    "candidate_role_ids",
                ),
                (),
            )
            if isinstance(raw_candidates, Mapping):
                raw_candidates = (raw_candidates,)
            for candidate in raw_candidates or ():
                if isinstance(candidate, str):
                    role_name = candidate
                    role_id = candidate
                    scores = _first_value(resolution, ("scores",))
                    score = float(
                        _first_value(scores, ("combined_similarity",), 0.0)
                    )
                else:
                    role_name = _first_value(
                        candidate, ("role_name", "canonical_name", "name", "role_id")
                    )
                    role_id = _first_value(candidate, ("role_id", "id"))
                    score = float(
                        _first_value(candidate, ("score", "similarity", "combined_score"), 0.0)
                    )
                if not role_name:
                    continue
                value = {
                    "role_id": role_id,
                    "role_name": str(role_name),
                    "score": round(score, 6),
                }
                previous = best.get(str(role_name))
                if previous is None or score > float(previous["score"]):
                    best[str(role_name)] = value
        ordered = sorted(best.values(), key=lambda item: (-float(item["score"]), item["role_name"]))
        return tuple(ordered[: self.max_nearest_roles])

    def _evidence(self, records: Sequence[JobTitleRecord]) -> tuple[dict[str, Any], ...]:
        """生成有限数量、可定位回原始 JD 的审核证据。"""

        items = []
        for record in sorted(records, key=self._record_id)[: self.max_evidence]:
            items.append(
                {
                    "record_id": self._record_id(record),
                    "title": self._title_of(record),
                    "original_title": _first_value(record, ("original_name", "original_title", "title")),
                    "company": self._record_value(
                        record, ("company_name", "company_id", "company")
                    ),
                    "template_id": self._record_value(
                        record, ("template_id", "template_hash", "jd_template_id")
                    ),
                    "skills": sorted(
                        _as_string_set(
                            _first_value(
                                record,
                                ("skills", "normalized_skills", "skill_names", "candidate_skills"),
                            )
                        ),
                        key=lambda value: value.casefold(),
                    ),
                }
            )
        return tuple(items)

    def _title_of(self, record: JobTitleRecord) -> str:
        """读取用于聚类的清洗后岗位名称，并确保非空。"""

        title = str(
            _first_value(
                record,
                ("normalized_name", "normalized_title", "cleaned_title", "original_name", "title"),
                "",
            )
        ).strip()
        if not title:
            raise ValueError("岗位记录缺少可用于聚类的名称")
        return title

    def _record_id(self, record: JobTitleRecord) -> str:
        """读取 JD 唯一标识；缺失时基于稳定字段生成可复现标识。"""

        value = self._record_value(
            record, ("source_id", "jd_id", "record_id", "job_id", "id")
        )
        if value not in (None, ""):
            return str(value)
        source = "|".join(
            (
                self._title_of(record),
                str(
                    self._record_value(
                        record, ("company_id", "company_name", "company"), ""
                    )
                ),
                str(
                    self._record_value(
                        record,
                        ("responsibilities", "description", "jd_text", "job_description"),
                        "",
                    )
                ),
            )
        )
        return "generated:" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:20]

    @staticmethod
    def _record_value(
        record: JobTitleRecord, names: Sequence[str], default: Any = None
    ) -> Any:
        """优先读取岗位字段，再读取 metadata 中的来源扩展字段。"""

        value = _first_value(record, names)
        if value not in (None, "", [], (), {}):
            return value
        metadata = _first_value(record, ("metadata",), {})
        return _first_value(metadata, names, default)

    @staticmethod
    def _candidate_id(representative: str, record_ids: Sequence[str]) -> str:
        """根据代表名称和簇成员生成稳定候选标识。"""

        source = representative + "|" + "|".join(sorted(record_ids))
        return "new-role:" + hashlib.sha256(source.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "BlockingKey", "CandidatePairProvider", "NewRoleCandidate", "NewRoleDiscovery",
    "PairSimilarity",
]

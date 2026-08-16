"""现有受控岗位的精确匹配、向量召回和决策器。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping

from .embedding import TextEmbedder
from .models import JobTitleRecord, MatchScores, ResolutionType, RoleDefinition, RoleResolution
from .scoring import build_match_scores, cosine_similarity, detect_function_conflict, lexical_similarity


@dataclass(frozen=True)
class _Candidate:
    """解析器内部使用的候选岗位及其评分。"""

    role: RoleDefinition
    scores: MatchScores
    function_conflict: bool = False
    conflict_reason: str = ""


class ExistingRoleResolver:
    """把清洗后的岗位记录映射到现有受控岗位。"""

    def __init__(
        self,
        registry: Any,
        embedder: TextEmbedder,
        config: Mapping[str, Any],
    ):
        """注入岗位注册表、向量器和评分配置。"""

        self.registry = registry
        self.embedder = embedder
        if hasattr(config, "to_dict"):
            config = config.to_dict()
        self.config = dict(config)
        self.weights = self.config.get("weights")
        if not isinstance(self.weights, Mapping):
            raise ValueError("config.weights 必须提供四项评分权重")
        thresholds = self.config.get("thresholds", {})
        self.auto_match_threshold = float(
            self.config.get("auto_match_threshold", thresholds.get("auto_match"))
        )
        self.review_threshold = float(
            self.config.get("review_threshold", thresholds.get("review"))
        )
        self.top_k = int(self.config.get("top_k", 5))
        self.resolver_version = str(
            self.config.get("resolver_version", self.config.get("version", "role-normalizer"))
        )
        if not 0.0 <= self.review_threshold <= self.auto_match_threshold <= 1.0:
            raise ValueError("阈值必须满足 0 <= review_threshold <= auto_match_threshold <= 1")
        if self.top_k <= 0:
            raise ValueError("top_k 必须大于 0")
        self._cached_roles: list[RoleDefinition] | None = None
        self._cached_title_vectors: list[list[float]] = []
        self._cached_context_vectors: list[list[float]] = []
        self._cached_skill_vectors: list[list[float]] = []
        # 保存本轮批处理已经生成的记录向量，供后续新岗位发现直接复用。
        self._record_vector_cache: dict[int, tuple[Any, Any, Any]] = {}

    @staticmethod
    def _resolution_type(name: str):
        """兼容枚举成员或字符串常量形式的 ResolutionType。"""

        return getattr(ResolutionType, name, name)

    def _all_roles(self) -> list[RoleDefinition]:
        """从注册表读取岗位列表，兼容映射、属性和可迭代实现。"""

        roles = getattr(self.registry, "roles", None)
        if callable(roles):
            roles = roles()
        if roles is None:
            roles = self.registry
        if isinstance(roles, Mapping):
            roles = roles.values()
        return list(roles)

    @staticmethod
    def _role_context(role: RoleDefinition) -> str:
        """拼接岗位名称、描述和技能，作为职责语义上下文。"""

        return " ".join(
            part
            for part in (
                ExistingRoleResolver._role_name(role),
                str(getattr(role, "description", "")),
                ExistingRoleResolver._skill_text(ExistingRoleResolver._role_skills(role)),
            )
            if part
        )

    @staticmethod
    def _record_context(record: JobTitleRecord) -> str:
        """优先使用结构化职责，缺失时退回完整 JD。"""

        responsibilities = str(getattr(record, "responsibilities", "") or "")
        return responsibilities or str(getattr(record, "jd_text", "") or "")

    @staticmethod
    def _role_name(role: RoleDefinition) -> str:
        """兼容 canonical_name 和 name 两种岗位名称字段。"""

        return str(getattr(role, "canonical_name", getattr(role, "name", "")) or "")

    @staticmethod
    def _record_name(record: JobTitleRecord) -> str:
        """兼容 normalized_name 和 normalized_title 两种记录字段。"""

        return str(
            getattr(record, "normalized_name", getattr(record, "normalized_title", "")) or ""
        )

    @staticmethod
    def _record_id(record: JobTitleRecord) -> str:
        """兼容 source_id 和 record_id 两种记录标识字段。"""

        return str(getattr(record, "source_id", getattr(record, "record_id", "")) or "")

    @staticmethod
    def _role_skills(role: RoleDefinition):
        """优先读取显式技能字段，否则从 metadata.skills 读取。"""

        explicit = getattr(role, "skills", None)
        if explicit is not None:
            return explicit
        metadata = getattr(role, "metadata", {}) or {}
        metadata_skills = metadata.get("skills", ()) if isinstance(metadata, Mapping) else ()
        return metadata_skills or getattr(role, "tags", ()) or ()

    @staticmethod
    def _skill_text(value: Any) -> str:
        """把技能序列稳定转换为向量输入文本。"""

        return " ".join(sorted({str(item).strip() for item in (value or ()) if str(item).strip()}))

    def _exact_role(self, name: str) -> tuple[RoleDefinition | None, str]:
        """调用注册表精确匹配，并推断是标准名还是别名命中。"""

        matcher = getattr(self.registry, "exact_match", None)
        if matcher is None:
            matcher = getattr(self.registry, "match_exact")
        match = matcher(name)
        if not match:
            return None, ""
        match_kind = ""
        if isinstance(match, tuple):
            role = match[0]
            if len(match) > 1:
                match_kind = str(getattr(match[1], "value", match[1])).upper()
        else:
            role = match
        if not match_kind:
            role_name = self._role_name(role)
            match_kind = "EXACT" if role_name.casefold() == name.casefold() else "ALIAS"
        return role, match_kind

    def _score_roles(
        self,
        record: JobTitleRecord,
        roles: Iterable[RoleDefinition],
        record_vectors: tuple[Any, Any, Any] | None = None,
    ) -> list[_Candidate]:
        """先按标题向量召回 Top-K，再计算职责、技能和字面的混合分。"""

        role_list = list(roles)
        if not role_list:
            return []
        self._ensure_role_vector_cache(role_list)
        normalized_title = self._record_name(record)
        record_context = self._record_context(record)
        record_skills = self._skill_text(getattr(record, "skills", ()))
        if record_vectors is None:
            encoded = self.embedder.encode([normalized_title, record_context, record_skills])
            record_vectors = (encoded[0], encoded[1], encoded[2])
        recalled = sorted(
            (
                (role, cosine_similarity(record_vectors[0], self._cached_title_vectors[index]))
                for index, role in enumerate(role_list)
            ),
            key=lambda item: (-item[1], str(item[0].role_id)),
        )[: self.top_k]
        recalled_roles = [role for role, _ in recalled]
        title_scores = {role.role_id: score for role, score in recalled}
        role_indices = {role.role_id: index for index, role in enumerate(role_list)}

        candidates: list[_Candidate] = []
        for index, role in enumerate(recalled_roles, start=1):
            role_context = str(getattr(role, "description", "") or "").strip()
            role_skills = self._skill_text(self._role_skills(role))
            # 能力抽取前可能没有 skills；缺失信号不应按 0 分惩罚，而应从
            # 本次评分权重中移除，再由 build_match_scores 自动归一化。
            effective_weights = dict(self.weights)
            if not record_context or not role_context:
                effective_weights["responsibility"] = 0.0
            if not record_skills or not role_skills:
                effective_weights["skill"] = 0.0
            scores = build_match_scores(
                title_similarity=title_scores[role.role_id],
                responsibility_similarity=cosine_similarity(
                    record_vectors[1], self._cached_context_vectors[role_indices[role.role_id]]
                ),
                skill_similarity=cosine_similarity(
                    record_vectors[2], self._cached_skill_vectors[role_indices[role.role_id]]
                ),
                lexical_similarity_value=lexical_similarity(normalized_title, self._role_name(role)),
                weights=effective_weights,
            )
            conflict, reason = detect_function_conflict(
                normalized_title,
                self._role_name(role),
                self.config.get("conflict_rules") or {
                    "function_terms": self.config.get("hard_constraint_terms", {})
                },
            )
            candidates.append(_Candidate(role, scores, conflict, reason))
        return sorted(
            candidates,
            key=lambda item: (-item.scores.combined_similarity, str(item.role.role_id)),
        )

    def _ensure_role_vector_cache(self, roles: list[RoleDefinition]) -> None:
        """预计算稳定注册表的三类向量，避免每条 JD 重复编码全部岗位。"""

        if self._cached_roles == roles:
            return
        self._cached_roles = list(roles)
        self._cached_title_vectors = self.embedder.encode(
            [self._role_name(role) for role in roles]
        )
        self._cached_context_vectors = self.embedder.encode(
            [self._role_context(role) for role in roles]
        )
        self._cached_skill_vectors = self.embedder.encode(
            [self._skill_text(self._role_skills(role)) for role in roles]
        )

    @staticmethod
    def _nearest_roles(candidates: Iterable[_Candidate]) -> tuple[dict[str, Any], ...]:
        """构造可直接序列化的 Top-K 审计摘要。"""

        return tuple(
            {
                "role_id": candidate.role.role_id,
                "role_name": ExistingRoleResolver._role_name(candidate.role),
                "combined_similarity": candidate.scores.combined_similarity,
                "function_conflict": candidate.function_conflict,
            }
            for candidate in candidates
        )

    def _result(
        self,
        record: JobTitleRecord,
        resolution: str,
        *,
        candidate: _Candidate | None = None,
        nearest_roles: tuple[dict[str, Any], ...] = (),
        reason: str = "",
    ) -> RoleResolution:
        """统一构造岗位解析结果。"""

        metadata = {"nearest_roles": list(nearest_roles)} if nearest_roles else {}
        is_final_match = resolution in {"EXACT", "ALIAS", "VECTOR_MATCH"}
        return RoleResolution(
            record=record,
            resolution_type=self._resolution_type(resolution),
            role_id=candidate.role.role_id if candidate and is_final_match else "",
            canonical_name=self._role_name(candidate.role) if candidate and is_final_match else None,
            scores=candidate.scores if candidate else MatchScores(),
            candidate_role_ids=[item["role_id"] for item in nearest_roles],
            reason=reason,
            resolver_version=self.resolver_version,
            metadata=metadata,
        )

    def resolve(self, record: JobTitleRecord) -> RoleResolution:
        """依次执行精确匹配、Top-K 召回、冲突保护和阈值决策。"""

        normalized_title = self._record_name(record).strip()
        if not normalized_title:
            return self._result(record, "UNMAPPED", reason="归一化岗位名称为空")

        exact_role, match_kind = self._exact_role(normalized_title)
        if exact_role is not None:
            exact_candidate = _Candidate(
                exact_role,
                MatchScores(
                    title_similarity=1.0,
                    responsibility_similarity=0.0,
                    skill_similarity=0.0,
                    lexical_similarity=1.0,
                    combined_similarity=1.0,
                ),
            )
            decision = "ALIAS" if "ALIAS" in match_kind else "EXACT"
            return self._result(record, decision, candidate=exact_candidate, reason=f"{decision} 命中")

        candidates = self._score_roles(record, self._all_roles())
        nearest = self._nearest_roles(candidates)
        if not candidates:
            return self._result(record, "UNMAPPED", reason="岗位注册表为空")

        best = candidates[0]
        score = best.scores.combined_similarity
        if score >= self.auto_match_threshold and not best.function_conflict:
            return self._result(
                record,
                "VECTOR_MATCH",
                candidate=best,
                nearest_roles=nearest,
                reason="综合评分达到自动匹配阈值",
            )
        if score >= self.review_threshold:
            reason = best.conflict_reason or "综合评分处于人工审核区间"
            return self._result(
                record,
                "REVIEW",
                candidate=best,
                nearest_roles=nearest,
                reason=reason,
            )
        return self._result(
            record,
            "UNMAPPED",
            nearest_roles=nearest,
            reason="最高综合评分低于人工审核阈值",
        )

    def resolve_many(
        self,
        records: Iterable[JobTitleRecord],
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> list[RoleResolution]:
        """按批次编码输入记录，避免为每条 JD 单独调用语义模型。"""

        items = list(records)
        if not items:
            return []
        output: list[RoleResolution | None] = [None] * len(items)
        unresolved: list[tuple[int, JobTitleRecord]] = []
        for index, record in enumerate(items):
            normalized_title = self._record_name(record).strip()
            if not normalized_title:
                output[index] = self._result(record, "UNMAPPED", reason="归一化岗位名称为空")
                continue
            exact_role, match_kind = self._exact_role(normalized_title)
            if exact_role is None:
                unresolved.append((index, record))
                continue
            exact_candidate = _Candidate(
                exact_role,
                MatchScores(
                    title_similarity=1.0,
                    responsibility_similarity=0.0,
                    skill_similarity=0.0,
                    lexical_similarity=1.0,
                    combined_similarity=1.0,
                ),
            )
            decision = "ALIAS" if "ALIAS" in match_kind else "EXACT"
            output[index] = self._result(
                record, decision, candidate=exact_candidate, reason=f"{decision} 命中"
            )

        roles = self._all_roles()
        self._ensure_role_vector_cache(roles)
        batch_size = int(self.config.get("record_batch_size", 256))
        if progress_callback:
            progress_callback(0, len(unresolved))
        for start in range(0, len(unresolved), batch_size):
            batch = unresolved[start : start + batch_size]
            batch_records = [record for _, record in batch]
            title_vectors = self.embedder.encode([self._record_name(record) for record in batch_records])
            context_vectors = self.embedder.encode([self._record_context(record) for record in batch_records])
            skill_vectors = self.embedder.encode(
                [self._skill_text(getattr(record, "skills", ())) for record in batch_records]
            )
            for offset, (output_index, record) in enumerate(batch):
                record_vectors = (
                    title_vectors[offset], context_vectors[offset], skill_vectors[offset]
                )
                self._record_vector_cache[id(record)] = record_vectors
                candidates = self._score_roles(
                    record,
                    roles,
                    record_vectors,
                )
                output[output_index] = self._resolve_candidates(record, candidates)
            if progress_callback:
                progress_callback(min(start + len(batch), len(unresolved)), len(unresolved))

        return [item for item in output if item is not None]

    def cached_record_vectors(
        self, record: JobTitleRecord
    ) -> tuple[Any, Any, Any] | None:
        """返回 resolve_many 已计算的名称、职责和技能向量。"""

        return self._record_vector_cache.get(id(record))

    def _resolve_candidates(
        self, record: JobTitleRecord, candidates: list[_Candidate]
    ) -> RoleResolution:
        """对已经评分的候选执行冲突保护和阈值决策。"""

        nearest = self._nearest_roles(candidates)
        if not candidates:
            return self._result(record, "UNMAPPED", reason="岗位注册表为空")
        best = candidates[0]
        score = best.scores.combined_similarity
        if score >= self.auto_match_threshold and not best.function_conflict:
            return self._result(
                record,
                "VECTOR_MATCH",
                candidate=best,
                nearest_roles=nearest,
                reason="综合评分达到自动匹配阈值",
            )
        if score >= self.review_threshold:
            return self._result(
                record,
                "REVIEW",
                candidate=best,
                nearest_roles=nearest,
                reason=best.conflict_reason or "综合评分处于人工审核区间",
            )
        return self._result(
            record,
            "UNMAPPED",
            nearest_roles=nearest,
            reason="最高综合评分低于人工审核阈值",
        )

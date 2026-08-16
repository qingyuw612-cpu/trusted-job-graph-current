"""岗位名称归一化项目使用的领域数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class ResolutionType(str, Enum):
    """岗位名称归一化的最终处理类型。"""

    EXACT = "EXACT"
    ALIAS = "ALIAS"
    VECTOR_MATCH = "VECTOR_MATCH"
    REVIEW = "REVIEW"
    NEW_ROLE_CANDIDATE = "NEW_ROLE_CANDIDATE"
    UNMAPPED = "UNMAPPED"
    NON_IT = "NON_IT"


@dataclass(frozen=True)
class RoleDefinition:
    """受控岗位注册表中的一个规范岗位定义。"""

    role_id: str
    canonical_name: str
    aliases: List[str] = field(default_factory=list)
    family: str = ""
    description: str = ""
    is_it_role: bool = True
    tags: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """返回可直接写入 JSON 的字典。"""

        return {
            "role_id": self.role_id,
            "canonical_name": self.canonical_name,
            "aliases": list(self.aliases),
            "family": self.family,
            "description": self.description,
            "is_it_role": self.is_it_role,
            "tags": list(self.tags),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "RoleDefinition":
        """从 JSON 字典构造岗位定义，并复制可变字段。"""

        return cls(
            role_id=str(data["role_id"]).strip(),
            canonical_name=str(data["canonical_name"]).strip(),
            aliases=[str(item).strip() for item in data.get("aliases", [])],
            family=str(data.get("family", "")).strip(),
            description=str(data.get("description", "")).strip(),
            is_it_role=bool(data.get("is_it_role", True)),
            tags=[str(item).strip() for item in data.get("tags", [])],
            metadata=dict(data.get("metadata", {})),
        )


@dataclass(frozen=True)
class JobTitleRecord:
    """进入岗位归一化流水线的一条招聘岗位记录。"""

    original_name: str
    normalized_name: str = ""
    tags: List[str] = field(default_factory=list)
    responsibilities: str = ""
    skills: List[str] = field(default_factory=list)
    source_id: str = ""
    search_name: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """返回可直接序列化的岗位记录字典。"""

        return {
            "original_name": self.original_name,
            "normalized_name": self.normalized_name,
            "tags": list(self.tags),
            "responsibilities": self.responsibilities,
            "skills": list(self.skills),
            "source_id": self.source_id,
            "search_name": self.search_name,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True)
class MatchScores:
    """候选岗位在各信号上的匹配得分。"""

    title_similarity: float = 0.0
    responsibility_similarity: float = 0.0
    skill_similarity: float = 0.0
    lexical_similarity: float = 0.0
    combined_similarity: float = 0.0

    def to_dict(self) -> Dict[str, float]:
        """返回各项匹配分数。"""

        return {
            "title_similarity": float(self.title_similarity),
            "responsibility_similarity": float(self.responsibility_similarity),
            "skill_similarity": float(self.skill_similarity),
            "lexical_similarity": float(self.lexical_similarity),
            "combined_similarity": float(self.combined_similarity),
        }


@dataclass(frozen=True)
class RoleResolution:
    """一条岗位记录的归一化决策及可审计信息。"""

    record: JobTitleRecord
    resolution_type: ResolutionType
    role_id: Optional[str] = None
    canonical_name: Optional[str] = None
    scores: MatchScores = field(default_factory=MatchScores)
    candidate_role_ids: List[str] = field(default_factory=list)
    reason: str = ""
    resolver_version: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """返回包含原始证据和决策结果的 JSON 友好字典。"""

        return {
            "record": self.record.to_dict(),
            "resolution_type": self.resolution_type.value,
            "role_id": self.role_id,
            "canonical_name": self.canonical_name,
            "scores": self.scores.to_dict(),
            "candidate_role_ids": list(self.candidate_role_ids),
            "reason": self.reason,
            "resolver_version": self.resolver_version,
            "metadata": dict(self.metadata),
        }
